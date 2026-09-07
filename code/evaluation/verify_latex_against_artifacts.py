"""Find numbers in the manuscript that still reflect the pre-correction run.

The two claim verifiers compare a hand-typed Python literal against a CSV, so a value
transcribed wrongly into a LaTeX table is checked against a second hand transcription of
the same wrong number. Two real defects survived exactly that way.

The obvious fix, asking "does this printed number appear anywhere in the artifacts", does
not work. It was tried first and measured: across the live result set it reported 8 of 9
known-stale values as supported, because a few thousand four-decimal tokens produce
coincidental matches for almost any probability-like number. A screen that lenient is
worse than none, because it prints a clean report over a stale paper.

This asks a sharper question, one with a real control group:

    Does this number match the PRE-CORRECTION baseline and NOT the current artifacts?

A value that does is stale by construction rather than by coincidence. That is only
possible because `_baseline_<date>_pre_correction/` was kept before anything was
regenerated.

What it does NOT catch, measured against nine known defects:

  * A DERIVED value. The ladder gain 0.2597 is the difference of two rungs and appears in
    no CSV, so it is in neither the old set nor the new one and cannot be compared here.
  * A TRANSCRIPTION TYPO. 0.7728 was never produced by any run; the artifact says 0.7727.
    A value that belongs to no run at all is invisible to a stale-versus-fresh test.
    Those are the claim verifiers' job, at a tolerance tight enough to see them.

Control test on nine known-stale values: 5 flagged, 0 false positives against 5 known-good
values. Re-run that control after changing anything here. A checker nobody has tried to
fool is a checker nobody should trust.

    python evaluation/verify_latex_against_artifacts.py
    python evaluation/verify_latex_against_artifacts.py --all
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CODE = os.path.dirname(HERE)
ROOT = os.path.dirname(CODE)
PAPER = os.path.join(ROOT, "paper")

LIVE_DIRS = [d for d in glob.glob(os.path.join(CODE, "results_*")) if "_baseline" not in d]
BASE_ROOT = glob.glob(os.path.join(CODE, "_baseline_*_pre_correction"))

# Dense files sample a continuum: roc_curves.csv and pr_curves.csv hold 1,440 rows each of
# 4dp values in [0,1], enough to match almost any probability-like number by coincidence.
# They are evidence for the figures, never the source of a printed claim.
#
# threshold_sweep.csv and calibration_bins.csv were in this list and should not have been.
# They hold 19 and 10 rows, they are the source of a printed table each, and excluding them
# made every number quoted from the sweep look stale: the operating-point sentence quoting
# \SI{5.1}{\percent} was flagged while being exactly right. An exclusion that hides a real
# source is a false positive generator, and false positives are how a checker stops being
# read. Density is the criterion, not inconvenience.
DENSE = ("roc_curves.csv", "pr_curves.csv", "fronts_")

# Reports about staleness are not artifacts, and reading them as artifacts breaks this check
# in the one direction that matters. claim_diff.json records, for every mismatch, both the
# OLD claimed value and the new one. Scanned as live output, its "claimed" fields put every
# stale number back into the live set, and a number in the manuscript that matches only the
# old run then looks current. That is exactly what happened here: this check reported zero
# stale values while fifteen were still in the text, and the fifteen only appeared once the
# report shrank. A tool that launders the evidence it was written to produce is worse than no
# tool, so these files are excluded by name.
REPORTS = ("claim_diff.json", "prose_migration.json", "pdf_parity.json", "latex_check.json")

NUM_RE = re.compile(r"\\num\{([-+]?[0-9][0-9.eE+-]*)\}")
SI_RE = re.compile(r"\\SI\{([-+]?[0-9][0-9.eE+-]*)\}\{")
CELL_RE = re.compile(r"(?<![\\A-Za-z0-9.])([-+]?\d+\.\d{3,})(?![0-9])")


def values_under(roots):
    vals = set()

    def add(x):
        try:
            f = float(x)
        except (TypeError, ValueError):
            return
        if not math.isfinite(f):
            return
        for fmt in ("%.4f", "%.3f", "%.2f", "%.1f"):
            vals.add(fmt % f)
        if abs(f - round(f)) < 1e-9:
            vals.add("%d" % round(f))
        vals.add("%.1f" % (f * 100.0))

    for root in roots:
        for p in glob.glob(os.path.join(root, "**", "*.csv"), recursive=True):
            if any(k in os.path.basename(p) for k in DENSE):
                continue
            with open(p, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    for tok in re.split(r"[,;\t]", line):
                        add(tok.strip())
        for p in glob.glob(os.path.join(root, "**", "*.json"), recursive=True):
            if os.path.basename(p) in REPORTS:
                continue
            try:
                obj = json.load(open(p, encoding="utf-8"))
            except Exception:
                continue
            stack = [obj]
            while stack:
                o = stack.pop()
                if isinstance(o, dict):
                    stack.extend(o.values())
                elif isinstance(o, list):
                    stack.extend(o)
                else:
                    add(o)
    return vals


def forms(raw):
    """Only the precision the paper actually prints.

    Including coarser roundings destroys the test: "0.7859" then also matches "0.8", which
    appears in every artifact, so nothing is ever flagged. Measured: with coarse forms in,
    the control test caught 0 of 8 known-stale values. Match what is printed, nothing else.
    """
    try:
        f = float(raw)
    except ValueError:
        return set()
    dec = len(raw.split(".")[1]) if "." in raw else 0
    if dec:
        return {"%.*f" % (dec, f)}
    return {"%d" % round(f)} if abs(f - round(f)) < 1e-9 else set()


def paper_numbers():
    out = []
    for p in sorted(glob.glob(os.path.join(PAPER, "*.tex"))):
        name = os.path.basename(p)
        for i, line in enumerate(open(p, encoding="utf-8").read().split("\n"), 1):
            if line.lstrip().startswith("%"):
                continue
            seen = set()
            for rx in (NUM_RE, SI_RE, CELL_RE):
                for m in rx.finditer(line):
                    if m.group(1) in seen:
                        continue
                    seen.add(m.group(1))
                    out.append((name, i, m.group(1), line.strip()))
    return out


# --------------------------------------------------------------------------- allowlist
# A check that always reports three findings a human has to re-adjudicate is a check nobody
# reads. These three were each traced to their source and are not stale. Every entry carries
# the reason, so a future reader can disagree with it rather than guess at it. Nothing is
# added here to silence a finding: an entry is only correct if the value in the manuscript is
# right for a reason the automatic test cannot see.
ALLOWED = {
    ("sections_data.tex", "50"):
        "the group-size threshold that DEFINES the assignment working set, a design constant "
        "stated in the method, not a measured quantity. It coincides with a pre-correction "
        "artifact token by arithmetic accident.",
    ("sections_discussion.tex", "1055"):
        "a deliberate historical citation. The sentence reads 'The observation window was "
        "GIVEN as 1055 days against a true 785' and exists to disclose the wrong value.",
    ("sections_discussion.tex", "31238"):
        "the row count of the TRUNCATED working copy, quoted in the paragraph disclosing that "
        "it was truncated. The sentence is 'held 31,238 rows against the canonical 46,606'.",
    ("sections_discussion.tex", "0.7859"):
        "one of the two figures an earlier version of this work PUBLISHED, quoted in the "
        "paragraph that reports how much of the change the attribution study accounts for. "
        "Removing it would delete the disclosure.",
    ("sections_discussion.tex", "0.6533"):
        "the chronological half of the same pair of previously published figures, same reason.",
    ("sections_results_predictive.tex", "0.0879"):
        "the like-for-like cost against an admissible model denied the group encodings, "
        "0.9050 minus 0.8171, computed from the current ladder and current model comparison. "
        "It is a derived value that appears in no CSV, which is the class this screen "
        "documents as invisible to it.",
    ("sections_results_predictive.tex", "0.0005"):
        "the current AUC change under isotonic calibration, |0.8258 - 0.8253| = 0.00047, "
        "which rounds to a token the old run also produced. Verified against "
        "results_deep_7030/calibration_summary.json in the same run that printed it.",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--strict", action="store_true",
                    help="report allowlisted values as stale too, to re-audit the allowlist")
    args = ap.parse_args()

    if not BASE_ROOT:
        print("No _baseline_*_pre_correction/ directory found.")
        print("This check needs the pre-correction artifacts as a control group.")
        return 1

    live = values_under(LIVE_DIRS)
    base = values_under(BASE_ROOT)
    only_base = base - live

    nums = paper_numbers()
    stale, allowed = [], []
    for name, line, raw, ctx in nums:
        f = forms(raw)
        if f & only_base and not (f & live):
            if not args.strict and (name, raw) in ALLOWED:
                allowed.append((name, line, raw, ctx))
            else:
                stale.append((name, line, raw, ctx))

    print("live artifact tokens      : %d" % len(live))
    print("baseline-only tokens      : %d" % len(only_base))
    print("numbers printed in paper  : %d" % len(nums))
    print("STALE (match old, not new): %d" % len(stale))
    print("allowlisted, with reasons : %d" % len(allowed))
    print()

    for name, line, raw, ctx in allowed:
        print("  ALLOWED %s:%d  %s" % (name, line, raw))
        print("          %s" % ALLOWED[(name, raw)])
    if allowed:
        print()

    by_file = {}
    for name, line, raw, ctx in stale:
        by_file.setdefault(name, []).append((line, raw, ctx))
    for name in sorted(by_file, key=lambda k: -len(by_file[k])):
        rows = by_file[name]
        print("%-34s %3d stale" % (name, len(rows)))
        for line, raw, ctx in (rows if args.all else rows[:8]):
            print("    :%-4d %-11s %s" % (line, raw, ctx[:86]))
        if not args.all and len(rows) > 8:
            print("    ... %d more (use --all)" % (len(rows) - 8))
        print()

    if stale:
        print("Every value above was produced by the pre-correction run and is not produced by")
        print("the current one. Each is stale unless it is a deliberate historical citation.")
        return 1
    print("No printed number matches the old run in preference to the new one.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
