"""Measure how far the published AUCs move when only the thread count changes.

Why this exists
---------------
Every number in the paper is quoted to four decimals and checked against a tolerance of
0.00005. That tolerance is only meaningful if a re-run produces the same number. It does
not, unless the OpenMP thread count is fixed: LightGBM and XGBoost reduce per-thread
histogram partials in thread order, and floating-point addition is not associative, so the
chosen split points differ slightly and the difference compounds through the tree.

This script runs the headline pipeline unchanged at several thread counts, changing nothing
else, and records what moved. It is the evidence behind the pin in _openmp_first.py and
behind the reproducibility paragraph in the paper. It is a measurement, not a fix.

The repeats flag is the control. If two runs at the SAME thread count differ, the cause is
not the thread count and the pin would be a false explanation.

Run:  python evaluation/thread_determinism.py
      python evaluation/thread_determinism.py --threads 2,4,8 --repeats 2
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CODE = HERE.parent
OUT = CODE / "results_crosslog"

PATTERNS = {
    "contaminated_auc": re.compile(r"Contaminated \(v1 style\):\s+Test-AUC\s+([0-9.]+)"),
    "admissible_auc": re.compile(r"Leak-free, random split:\s+Test-AUC\s+([0-9.]+)"),
    "chronological_auc": re.compile(r"Leak-free, chronological split:\s+Test-AUC\s+([0-9.]+)"),
    "correction_auc": re.compile(r"Correction:\s+([0-9.]+) AUC"),
}


def run_once(threads, env_base):
    env = dict(env_base)
    env["BPI_THREADS"] = str(threads)
    env["BPI_TEST_SIZE"] = "0.3"
    # A SEPARATE run tag on purpose. This script runs the pipeline several times with
    # deliberately wrong settings, and it must not leave the canonical 7030 artifacts
    # holding whichever thread count happened to run last.
    env["BPI_RUN_TAG"] = "7030_threadprobe"
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run([sys.executable, str(CODE / "bpi2014_pipeline_leakfree.py")],
                          cwd=str(CODE), env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit("pipeline failed at threads=%s: %s" % (threads, proc.stdout[-2000:]))
    row = {"threads": threads}
    for key, pat in PATTERNS.items():
        m = pat.search(proc.stdout)
        row[key] = float(m.group(1)) if m else float("nan")
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--threads", default="1,2,4,8")
    ap.add_argument("--repeats", type=int, default=1,
                    help="runs per thread count; >1 proves the pin makes runs bit-identical")
    args = ap.parse_args()

    counts = [int(x) for x in args.threads.split(",") if x.strip()]
    env_base = dict(os.environ)

    rows = []
    for t in counts:
        for rep in range(args.repeats):
            r = run_once(t, env_base)
            r["repeat"] = rep + 1
            rows.append(r)
            print("  threads=%-3d rep=%d  contaminated %.4f  admissible %.4f  chronological %.4f"
                  % (t, rep + 1, r["contaminated_auc"], r["admissible_auc"],
                     r["chronological_auc"]))

    OUT.mkdir(exist_ok=True)
    path = OUT / "thread_determinism.csv"
    cols = ["threads", "repeat", "contaminated_auc", "admissible_auc",
            "chronological_auc", "correction_auc"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({c: r[c] for c in cols})

    print()
    for key in ("contaminated_auc", "admissible_auc", "chronological_auc"):
        vals = [r[key] for r in rows]
        print("  %-20s min %.4f  max %.4f  spread %.4f"
              % (key, min(vals), max(vals), max(vals) - min(vals)))

    for t in counts:
        same = [r for r in rows if r["threads"] == t]
        if len(same) > 1:
            ident = all(abs(s["admissible_auc"] - same[0]["admissible_auc"]) < 1e-12
                        for s in same)
            print("  threads=%-3d repeats bit-identical: %s" % (t, ident))

    print()
    print("wrote %s" % path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
