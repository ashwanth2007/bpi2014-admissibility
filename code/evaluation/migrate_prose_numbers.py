"""Propose replacements for the numbers still quoted in the manuscript's prose.

Every table in the paper is now generated from artifacts. What remains is prose: figures
quoted inside sentences, which cannot be generated because the sentence around them carries
the meaning. There are roughly two hundred of them and they all moved when the dataset was
replaced, the date parse corrected and Rule 2 implemented as written.

Editing two hundred numbers by hand is exactly how the original transcription defects got
in. So this builds a MIGRATION MAP instead, by pairing each artifact against its
pre-correction baseline: for every quantity that both runs produced, the old value maps to
the new one. A prose number matching an old value, and matching nothing in the new run, is
then a candidate replacement.

It is a proposal, not an edit. Nothing is written to the manuscript unless --apply is
passed, and even then every change is logged with its surrounding line so a human can read
what the sentence now claims. A number can move for a reason the sentence does not survive:
if the paper says "a quarter of the signal" and the figure is now a fifth, replacing the
digits leaves the words wrong. Those cases are listed separately as PROSE REVIEW.

    python evaluation/migrate_prose_numbers.py            # propose
    python evaluation/migrate_prose_numbers.py --apply    # apply and log
"""
from __future__ import annotations

import argparse
import glob
import io
import json
import os
import re
import sys

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
CODE = os.path.dirname(HERE)
ROOT = os.path.dirname(CODE)
PAPER = os.path.join(ROOT, "paper")
BASE = (glob.glob(os.path.join(CODE, "_baseline_*_pre_correction")) or [None])[0]

# Words that describe a magnitude. If one sits within this many characters of a number that
# is changing, the sentence needs reading, not just the digits swapping.
QUALIFIERS = ["quarter", "fifth", "third", "half", "twice", "double", "more than",
              "less than", "about", "roughly", "nearly", "almost", "close to"]
NEAR = 180


def _pairs_from_csv(rel, keys, value_cols):
    """Yield (old, new) for every row present in both the baseline and the live artifact."""
    live_p, base_p = os.path.join(CODE, rel), os.path.join(BASE, rel) if BASE else None
    if not (base_p and os.path.exists(live_p) and os.path.exists(base_p)):
        return []
    live, base = pd.read_csv(live_p), pd.read_csv(base_p)
    if not all(k in live.columns and k in base.columns for k in keys):
        return []
    out = []
    for vc in value_cols:
        if vc not in live.columns or vc not in base.columns:
            continue
        l = live.set_index(keys)[vc]
        b = base.set_index(keys)[vc]
        for k in b.index:
            if k in l.index:
                try:
                    ov, nv = float(b.loc[k]), float(l.loc[k])
                except (TypeError, ValueError):
                    continue
                if ov == ov and nv == nv and abs(ov - nv) > 1e-9:
                    out.append((ov, nv, "%s %s %s" % (rel, k, vc)))
    return out


def build_map():
    pairs = []
    pairs += _pairs_from_csv("results_bpi_leakfree_7030/model_comparison_leakfree.csv",
                             ["Config", "Model"],
                             ["Test_AUC", "Test_F1", "Test_Accuracy", "Test_Precision",
                              "Test_Recall", "CV_AUC_mean", "Test_Brier"])
    pairs += _pairs_from_csv("results_bpi_leakfree_7030/feature_ladder_leakfree.csv",
                             ["Feature_Set"],
                             ["Test_AUC", "Test_F1", "Test_Accuracy", "CV_AUC"])
    pairs += _pairs_from_csv("results_deep_7030/full_metrics.csv", ["Config", "Model"],
                             ["AUC", "Accuracy", "Precision", "Recall", "F1", "MCC",
                              "Brier", "AveragePrecision", "Specificity", "NPV"])
    pairs += _pairs_from_csv("results_deep_7030/confusion_matrices.csv", ["Config", "Model"],
                             ["TN", "FP", "FN", "TP"])
    m = {}
    for ov, nv, src in pairs:
        for dp in (4, 3, 2):
            key = ("%." + str(dp) + "f") % ov
            m.setdefault(key, []).append((("%." + str(dp) + "f") % nv, src))
        if abs(ov - round(ov)) < 1e-9:
            m.setdefault("%d" % round(ov), []).append(("%d" % round(nv), src))
    # keep only unambiguous mappings: one old value, one new value
    return {k: v[0] for k, v in m.items() if len({x[0] for x in v}) == 1}


NUM_RE = re.compile(r"\\num\{([-+]?[0-9][0-9.]*)\}")


def scan(mapping):
    props, review = [], []
    for path in sorted(glob.glob(os.path.join(PAPER, "*.tex"))):
        name = os.path.basename(path)
        text = io.open(path, encoding="utf-8").read()
        lines = text.split("\n")
        for i, line in enumerate(lines, 1):
            if line.lstrip().startswith("%"):
                continue
            for mt in NUM_RE.finditer(line):
                raw = mt.group(1)
                if raw not in mapping:
                    continue
                new, src = mapping[raw]
                if new == raw:
                    continue
                window = " ".join(lines[max(0, i - 3):i + 2]).lower()
                near = [q for q in QUALIFIERS if q in window]
                rec = dict(file=name, line=i, old=raw, new=new, src=src,
                           context=line.strip()[:120], qualifiers=near)
                (review if near else props).append(rec)
    return props, review


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    if not BASE:
        print("No _baseline_*_pre_correction/ directory. This tool needs the old run to pair against.")
        return 1

    mapping = build_map()
    props, review = scan(mapping)
    print("unambiguous old -> new mappings : %d" % len(mapping))
    print("prose numbers safe to migrate   : %d" % len(props))
    print("prose numbers NEEDING A READ    : %d" % len(review))
    print()

    for r in review[:14]:
        print("  REVIEW %s:%-4d %s -> %s   near: %s"
              % (r["file"][:28], r["line"], r["old"], r["new"], ", ".join(r["qualifiers"])))
        print("         %s" % r["context"])
    if len(review) > 14:
        print("  ... %d more needing a read" % (len(review) - 14))
    print()

    if not args.apply:
        for r in props[:12]:
            print("  MIGRATE %s:%-4d %s -> %s" % (r["file"][:28], r["line"], r["old"], r["new"]))
        if len(props) > 12:
            print("  ... %d more" % (len(props) - 12))
        print()
        print("Nothing written. Re-run with --apply to migrate the safe ones.")
        return 0

    by_file = {}
    for r in props:
        by_file.setdefault(r["file"], []).append(r)
    changed = 0
    for name, recs in by_file.items():
        path = os.path.join(PAPER, name)
        lines = io.open(path, encoding="utf-8").read().split("\n")
        for r in recs:
            idx = r["line"] - 1
            old_tok = "\\num{" + r["old"] + "}"
            new_tok = "\\num{" + r["new"] + "}"
            if old_tok in lines[idx]:
                lines[idx] = lines[idx].replace(old_tok, new_tok)
                changed += 1
        io.open(path, "w", encoding="utf-8", newline="\n").write("\n".join(lines))
    log = os.path.join(CODE, "results_crosslog", "prose_migration.json")
    os.makedirs(os.path.dirname(log), exist_ok=True)
    json.dump({"applied": props, "needs_review": review}, open(log, "w"), indent=2)
    print("applied %d substitutions; log -> results_crosslog/prose_migration.json" % changed)
    print("%d passage(s) still need a human read, listed above and in the log." % len(review))
    return 0


if __name__ == "__main__":
    sys.exit(main())
