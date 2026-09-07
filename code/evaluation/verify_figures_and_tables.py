"""Check that every rendered figure is in the paper and every include has a file.

This exists because of a specific miss. Two architecture figures were built, rendered to PDF
and PNG, recorded as done in the task ledger, and never referenced by any .tex file. The
artifact existed, so every check that asked "was it produced" passed, and the paper still did
not contain the diagram the supervisor asked for. "The file exists" is not the same claim as
"the paper shows it", and nothing here was testing the second one.

Four things are checked, and each can fail on its own:

  1. every \\includegraphics target resolves to a file under figures_out/
  2. every figure rendered into figures_out/ is included by some .tex   (the miss above)
  3. every \\input{generated/...} target exists
  4. every generated table is \\input by some .tex, and every table float either \\inputs a
     generated file or holds its own tabular

Orphans are reported as failures, not warnings. A figure worth generating is worth showing,
and one that is not should stop being generated.

    python evaluation/verify_figures_and_tables.py
"""
from __future__ import annotations

import glob
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CODE = os.path.dirname(HERE)
ROOT = os.path.dirname(CODE)
PAPER = os.path.join(ROOT, "paper")
FIGS = os.path.join(CODE, "figures_out")
GEN = os.path.join(PAPER, "generated")

INC_RE = re.compile(r"\\includegraphics(?:\[[^\]]*\])?\{([^}]+)\}")
INPUT_RE = re.compile(r"\\input\{([^}]+)\}")
TABLE_RE = re.compile(r"\\begin\{table\}(.*?)\\end\{table\}", re.S)
LABEL_RE = re.compile(r"\\label\{([^}]+)\}")

# Figures that are deliberately rendered and deliberately not printed. Each needs a reason.
FIGURE_EXEMPT = {}


def tex_sources():
    out = {}
    for p in sorted(glob.glob(os.path.join(PAPER, "*.tex"))):
        out[os.path.basename(p)] = open(p, encoding="utf-8").read()
    return out


def main():
    tex = tex_sources()
    body = "\n".join(tex.values())
    fails = []

    # 1. every include resolves
    included = set()
    for name, src in tex.items():
        for m in INC_RE.finditer(src):
            target = m.group(1).strip()
            stem = os.path.splitext(os.path.basename(target))[0]
            included.add(stem)
            hits = glob.glob(os.path.join(FIGS, stem + ".*"))
            if not hits:
                fails.append("%s includes %s, which is not in figures_out/" % (name, target))

    # 2. every rendered figure is included
    rendered = {os.path.splitext(os.path.basename(p))[0]
                for p in glob.glob(os.path.join(FIGS, "*.pdf"))}
    orphan_figs = sorted(rendered - included - set(FIGURE_EXEMPT))
    for f in orphan_figs:
        fails.append("figures_out/%s.pdf is rendered but no .tex includes it" % f)

    # 3. every \input target exists
    inputs = set()
    for name, src in tex.items():
        for m in INPUT_RE.finditer(src):
            target = m.group(1).strip()
            inputs.add(os.path.basename(target))
            path = os.path.join(PAPER, target)
            if not (os.path.exists(path) or os.path.exists(path + ".tex")):
                fails.append("%s inputs %s, which does not exist" % (name, target))

    # 4a. every generated table is used
    generated = {os.path.splitext(os.path.basename(p))[0]
                 for p in glob.glob(os.path.join(GEN, "*.tex"))}
    orphan_tabs = sorted(generated - inputs)
    for t in orphan_tabs:
        fails.append("paper/generated/%s.tex is generated but no .tex inputs it" % t)

    # 4b. every table float has a body
    empty = []
    for name, src in tex.items():
        for m in TABLE_RE.finditer(src):
            blk = m.group(1)
            if "\\input{generated/" in blk or "\\begin{tabular}" in blk:
                continue
            lab = LABEL_RE.search(blk)
            empty.append("%s: table %s has neither a tabular nor a generated input"
                         % (name, lab.group(1) if lab else "(unlabelled)"))
    fails += empty

    print("figures rendered   : %d" % len(rendered))
    print("figures included   : %d" % len(included))
    print("tables generated   : %d" % len(generated))
    print("inputs referenced  : %d" % len(inputs))
    print()
    if fails:
        for f in fails:
            print("  FAIL  %s" % f)
        print()
        print("%d problem(s). A figure worth generating is worth showing." % len(fails))
        return 1
    print("Every rendered figure is shown, every include and input resolves, and every")
    print("table float has a body.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
