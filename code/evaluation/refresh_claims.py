"""Rewrite the hard-coded claim literals in the verifiers from the current artifacts.

The two verifiers hold the manuscript's numbers as Python literals and compare them to the
CSVs. That is the right design: it forces a human decision every time a published number
changes. It is also unusable by hand when every number changes at once, which is what happened
when the dataset was replaced, the date parse fixed, Rule 2 implemented as written and the
OpenMP thread count pinned. 523 of 543 checks went stale in one run.

HOW IT FINDS A LITERAL, and the two ways that went wrong first
--------------------------------------------------------------
Attempt one matched each claim by its check name and reached under half of them, because most
claims are not written as `chk("name", 0.7859, ...)`. They are rows of a table written as
tuples that a loop walks, and there is no name beside the literal:

    for frac, n, tr, te in [(0.05, 1631, 0.9999, 0.7765), (0.10, 3262, ...), ...]:
        r = lc.loc[frac]
        chk("lc %.2f test AUC" % frac, te, float(r["Test_AUC"]))

Attempt two matched on the literal value, sequentially. That is worse than it sounds. A value
like 0.19, 0.85 or 300 appears a dozen times across the file, the reported failures do not run
in source order once loops interleave, and the rewrite silently landed on the wrong occurrence
forty-odd times. It also overwrote `0.85` where it was the lookup KEY of its own row, and the
verifier then died on a training fraction that does not exist.

This version does not guess. Each verifier gains a `--dump` flag that prints every check,
passing and failing, in the order `chk` was called. This file then walks the verifier's AST,
simulating the loops, to produce the claim literals in that same order. The two lists are
compared element by element BEFORE anything is written: if they differ in length, or if any
literal disagrees with the value the verifier reported for that position, the alignment is
wrong and nothing is touched. When they agree, every claim is located exactly and the rewrite
is a direct substitution at a known source offset.

  (no flag)  run the verifiers, print every mismatch as claimed -> actual. Changes nothing.
  --apply    rewrite the literals in place, verify, and write a diff report beside them.

`--apply` updates the VERIFIER only. It deliberately does not touch the manuscript: the
verifier exists to catch a number in the paper that no longer matches its artifact, and a tool
that silently updated both would delete the only signal that anything moved. The manuscript is
checked separately, and structurally, by verify_latex_against_artifacts.py.

    python evaluation/refresh_claims.py
    python evaluation/refresh_claims.py --apply
"""
from __future__ import annotations

import argparse
import ast
import io
import json
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CODE = os.path.dirname(HERE)

VERIFIERS = ["verify_paper_claims.py", "verify_deep_claims.py"]

SUMMARY_RE = re.compile(r"(\d+)\s+checks,\s+(\d+)\s+passed")


# --------------------------------------------------------------------------- running


def run(name, dump=False):
    """Run one verifier. Returns (ordered checks, total, completed)."""
    cmd = [sys.executable, os.path.join(HERE, name)] + (["--dump"] if dump else [])
    p = subprocess.run(cmd, cwd=CODE, capture_output=True, text=True,
                       env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    out = p.stdout + p.stderr
    checks = []
    for line in out.splitlines():
        if line.startswith("CHECK\t"):
            parts = line.split("\t")
            if len(parts) == 5:
                checks.append({"status": parts[1], "label": parts[2],
                               "claimed": parts[3], "actual": parts[4]})
    total, completed = None, False
    for line in out.splitlines():
        m = SUMMARY_RE.search(line)
        if m:
            total, completed = int(m.group(1)), True
    return checks, total, completed


# --------------------------------------------------------------------------- locating


def _offsets(src):
    starts, off = [], 0
    for ln in src.split("\n"):
        starts.append(off)
        off += len(ln) + 1
    return starts


def _num(node):
    """The numeric value of a literal node, following a unary minus."""
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float, bool)):
        return node
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        if isinstance(node.operand, ast.Constant):
            return node
    return None


def _is_check_call(node):
    """chk(...) or CHECKS.append((...)), the two ways this file records a check."""
    if not isinstance(node, ast.Call):
        return None
    fn = node.func
    if isinstance(fn, ast.Name) and fn.id == "chk":
        return "chk"
    if (isinstance(fn, ast.Attribute) and fn.attr == "append"
            and isinstance(fn.value, ast.Name) and fn.value.id == "CHECKS"):
        return "append"
    return None


def claim_sites(src):
    """Every claim literal in the order the verifier will report it.

    Loops over list-of-tuple literals are unrolled, which is what makes the order match: a
    loop body runs once per row, and the checks inside it are reported in body order within
    each row. A claim that cannot be resolved to a literal yields None, so the list still
    lines up positionally with what the verifier printed.
    """
    tree = ast.parse(src)
    sites = []

    def deref(node, env):
        """Follow a loop variable back to the literal node it was bound to."""
        seen = 0
        while isinstance(node, ast.Name) and node.id in env and seen < 8:
            node = env[node.id]
            seen += 1
        return node

    def resolve(node, env):
        return _num(deref(node, env))

    def record(call, kind, env):
        if kind == "chk":
            claim = call.args[1] if len(call.args) > 1 else None
        else:                                        # CHECKS.append((status, label, c, a))
            arg = call.args[0] if call.args else None
            claim = arg.elts[2] if isinstance(arg, ast.Tuple) and len(arg.elts) > 2 else None
        sites.append(resolve(claim, env) if claim is not None else None)

    def as_list(node, env):
        """The list literal an iterable ultimately refers to, if there is one.

        Covers a bare list, a loop variable bound to one, and `enumerate(xs, start=1)`, which
        is how the per-fold cross-validation claims are written. Missing the enumerate form
        cost sixteen claim sites and blocked the whole alignment.
        """
        node = deref(node, env)
        if isinstance(node, ast.List):
            return node.elts
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in ("enumerate", "reversed", "list", "sorted")
                and node.args):
            inner = deref(node.args[0], env)
            if isinstance(inner, ast.List):
                return inner.elts
        return None

    def walk(body, env):
        for stmt in body:
            if isinstance(stmt, ast.For):
                rows = as_list(stmt.iter, env)
                tgt = stmt.target
                enum = (isinstance(stmt.iter, ast.Call)
                        and isinstance(stmt.iter.func, ast.Name)
                        and stmt.iter.func.id == "enumerate")
                if rows is not None and isinstance(tgt, ast.Tuple):
                    names = [n.id if isinstance(n, ast.Name) else None for n in tgt.elts]
                    for row in rows:
                        child = dict(env)
                        if enum and len(names) == 2:
                            child[names[1]] = row          # (index, value)
                        elif isinstance(row, ast.Tuple) and len(row.elts) == len(names):
                            for nm, val in zip(names, row.elts):
                                if nm:
                                    child[nm] = val
                        else:
                            continue
                        walk(stmt.body, child)
                    continue
                if rows is not None and isinstance(tgt, ast.Name):
                    for row in rows:
                        child = dict(env)
                        child[tgt.id] = row
                        walk(stmt.body, child)
                    continue
                walk(stmt.body, env)                 # dynamic iterable: body seen once
                continue
            if isinstance(stmt, (ast.If, ast.While, ast.With)):
                walk(stmt.body, env)
                walk(getattr(stmt, "orelse", []) or [], env)
                continue
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # chk itself contains the CHECKS.append that records every check. Counting it
                # would add one phantom site at the top of the file and shift everything.
                if stmt.name != "chk":
                    walk(stmt.body, env)
                continue
            for node in ast.walk(stmt):
                kind = _is_check_call(node)
                if kind:
                    record(node, kind, env)

    walk(tree.body, {})
    return sites


def literal_text(src, node, starts):
    a = starts[node.lineno - 1] + node.col_offset
    b = starts[node.end_lineno - 1] + node.end_col_offset
    return a, b, src[a:b]


def aligned(src, sites, checks):
    """True when every located literal equals the value the verifier reported there."""
    if len(sites) != len(checks):
        return False, "%d claim sites against %d reported checks" % (len(sites), len(checks))
    starts = _offsets(src)
    for i, (node, c) in enumerate(zip(sites, checks)):
        if node is None:
            continue
        _, _, text = literal_text(src, node, starts)
        try:
            if abs(float(ast.literal_eval(text)) - float(c["claimed"])) > 1e-9:
                return False, ("position %d: source says %s, verifier reported %s (%s)"
                               % (i, text, c["claimed"], c["label"]))
        except (ValueError, SyntaxError):
            if text.strip() != str(c["claimed"]).strip():
                return False, "position %d: %s against %s" % (i, text, c["claimed"])
    return True, "all %d claim sites line up with the reported checks" % len(sites)


def literal_for(paper, actual):
    """Format the artifact value the way the paper literal was written."""
    if paper in ("True", "False") or actual in ("True", "False"):
        return "True" if str(actual) == "True" else "False"
    try:
        a = float(actual)
    except ValueError:
        return None
    if re.fullmatch(r"-?\d+", paper):
        return str(int(round(a)))
    dp = len(paper.split(".")[1]) if "." in paper else 4
    return ("%." + str(dp) + "f") % a


def rewrite(src, sites, checks):
    """Substitute every failing claim at its own offset, back to front."""
    starts = _offsets(src)
    edits = []
    for node, c in zip(sites, checks):
        if node is None or c["status"] != "FAIL":
            continue
        a, b, text = literal_text(src, node, starts)
        new = literal_for(text, c["actual"])
        if new is None or new == text:
            continue
        edits.append((a, b, new))
    for a, b, new in sorted(edits, reverse=True):
        src = src[:a] + new + src[b:]
    return src, len(edits)


# --------------------------------------------------------------------------- driver


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    report, rc = {}, 0
    for name in VERIFIERS:
        path = os.path.join(HERE, name)
        checks, total, completed = run(name, dump=True)
        fails = [c for c in checks if c["status"] == "FAIL"]
        print("%-30s %4s checks, %4d mismatched" % (name, total, len(fails)))
        for c in fails[:10]:
            print("    %-42s %-12s -> %s" % (c["label"][:42], c["claimed"], c["actual"]))
        if len(fails) > 10:
            print("    ... %d more" % (len(fails) - 10))
        report[name] = {"total": total, "mismatched": fails}

        if not completed:
            print("    the verifier did not run to completion; nothing attempted")
            rc = 1
            print()
            continue

        src = io.open(path, encoding="utf-8").read()
        sites = claim_sites(src)
        ok, why = aligned(src, sites, checks)
        print("    alignment: %s" % why)
        report[name]["alignment"] = why

        if args.apply and fails:
            if not ok:
                print("    NOT APPLIED: the claim literals could not be lined up with the")
                print("    reported checks, so a rewrite would land on the wrong number.")
                rc = 1
                print()
                continue
            backup = path + ".before_refresh"
            shutil.copyfile(path, backup)
            new_src, n = rewrite(src, sites, checks)
            io.open(path, "w", encoding="utf-8", newline="\n").write(new_src)
            checks2, total2, completed2 = run(name, dump=True)
            fails2 = [c for c in checks2 if c["status"] == "FAIL"]
            if not completed2 or len(fails2) >= len(fails):
                shutil.copyfile(backup, path)
                why2 = ("it no longer runs to completion" if not completed2
                        else "%d failures before, %d after" % (len(fails), len(fails2)))
                print("    REVERTED: %s. Nothing changed." % why2)
                report[name]["applied"] = 0
                rc = 1
            else:
                print("    applied %d literal updates, %d failure(s) remain"
                      % (n, len(fails2)))
                report[name]["applied"] = n
                report[name]["remaining"] = fails2
                os.remove(backup)
                if fails2:
                    rc = 1
        print()

    dst = os.path.join(CODE, "results_crosslog", "claim_diff.json")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    json.dump(report, io.open(dst, "w", encoding="utf-8"), indent=2)
    print("diff report -> results_crosslog/claim_diff.json")
    print()
    print("The manuscript is NOT touched by this tool. It is checked separately, and")
    print("structurally, by evaluation/verify_latex_against_artifacts.py.")
    return rc


if __name__ == "__main__":
    sys.exit(main())
