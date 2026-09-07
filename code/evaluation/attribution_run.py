"""What actually moved the headline, measured one defect at a time.

The manuscript's headline admissible AUC went from 0.7859 to 0.8258 and the chronological arm
from 0.6533 to 0.8076 when four things were corrected at once: a truncated copy of the
incident file was replaced, a month-first date parse was fixed, a look-ahead in the standing
queue count was removed, and Rule 2 was implemented as the equation states rather than as a
refit inside every fold.

Correcting four things at once and then telling a story about which one mattered is exactly
the habit this paper argues against. So each defect is reintroduced on its own, everything
else held at its corrected value, and the pipeline is run unchanged. The switches are the
ones the loader already exposes plus two added for this purpose, and none of them is used by
any published run.

    BPI_TRUNCATE=31238        the partial copy of Detail_Incident.csv
    BPI_LEGACY_DATES=1        month-first incident timestamps
    BPI_QUEUE_UNRESOLVED=excluded   the queue count that reads the future
    BPI_RULE2=trainrows       fold-refitted encoding instead of the past set

Run:  python evaluation/attribution_run.py
      python evaluation/attribution_run.py --only all_defects
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

TRUNCATED_ROWS = 31238

VARIANTS = [
    ("corrected", {}, "every defect fixed; this is what the paper reports"),
    ("truncated_data", {"BPI_TRUNCATE": str(TRUNCATED_ROWS)},
     "the partial copy of the incident file, %d rows instead of 46,606" % TRUNCATED_ROWS),
    ("legacy_dates", {"BPI_LEGACY_DATES": "1"},
     "incident timestamps parsed month-first, transposing day and month on 41 per cent of rows"),
    ("queue_lookahead", {"BPI_QUEUE_UNRESOLVED": "excluded"},
     "standing queue count excludes cases that never resolve, which is future information"),
    ("rule2_foldrefit", {"BPI_RULE2": "trainrows"},
     "group encoding refitted inside every fold instead of over the admissible past set"),
    ("all_defects", {"BPI_TRUNCATE": str(TRUNCATED_ROWS), "BPI_LEGACY_DATES": "1",
                     "BPI_QUEUE_UNRESOLVED": "excluded", "BPI_RULE2": "trainrows"},
     "all four together, which should approximate the figures the first draft reported"),
]


def run(name, extra):
    env = dict(os.environ)
    env.update(extra)
    env["BPI_TEST_SIZE"] = "0.3"
    env["BPI_RUN_TAG"] = "attrib_" + name          # never the canonical 7030 directory
    env["PYTHONIOENCODING"] = "utf-8"
    p = subprocess.run([sys.executable, str(CODE / "bpi2014_pipeline_leakfree.py")],
                       cwd=str(CODE), capture_output=True, text=True, env=env)
    if p.returncode != 0:
        print("  %-18s FAILED\n%s" % (name, p.stdout[-1500:]))
        return None
    row = {"variant": name}
    for key, pat in PATTERNS.items():
        m = pat.search(p.stdout)
        row[key] = float(m.group(1)) if m else float("nan")
    m = re.search(r"After cleaning:\s*([\d,]+)\s+incidents", p.stdout)
    row["incidents"] = int(m.group(1).replace(",", "")) if m else None
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    args = ap.parse_args()

    todo = [v for v in VARIANTS if not args.only or v[0] == args.only]
    rows, base = [], None
    for name, extra, why in todo:
        r = run(name, extra)
        if r is None:
            continue
        r["description"] = why
        if name == "corrected":
            base = r
        if base:
            r["admissible_delta_vs_corrected"] = round(r["admissible_auc"] - base["admissible_auc"], 4)
            r["chronological_delta_vs_corrected"] = round(
                r["chronological_auc"] - base["chronological_auc"], 4)
        rows.append(r)
        print("  %-18s incidents %-7s admissible %.4f  chronological %.4f"
              % (name, r["incidents"], r["admissible_auc"], r["chronological_auc"]))

    OUT.mkdir(exist_ok=True)
    path = OUT / "attribution.csv"
    cols = ["variant", "incidents", "contaminated_auc", "admissible_auc", "chronological_auc",
            "correction_auc", "admissible_delta_vs_corrected",
            "chronological_delta_vs_corrected", "description"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in cols})
    print()
    print("wrote %s" % path)
    print()
    print("Each row reintroduces ONE defect with everything else corrected, so the deltas do")
    print("not have to add up: defects can interact, and the all_defects row is what says")
    print("whether they do.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
