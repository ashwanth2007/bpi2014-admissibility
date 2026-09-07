"""Is the leakage cost a property of the DOMAIN, or of how directly each log's recorded
aggregates proxy its outcome?

Measured so far, share of above-chance signal lost to admissibility:

    BPI 2014   IT service management      18.5 per cent
    BPI 2017   consumer lending           52.8 per cent
    Sepsis     acute care                 50.7 per cent

The obvious reading is that BPI 2014 is unusually clean and roughly half is typical. That
reading is not safe yet, and writing it without this check would be exactly the kind of
unearned claim this paper exists to complain about.

The confound: every label here is a duration threshold, and the inadmissible sets are not
alike. BPI 2014's are process-shape counts (reassignments, groups touched, related
incidents). The XES logs' are dominated by `n_events`, and a case with more events almost
mechanically ran longer. If the cost collapses once `n_events` and `n_activities` are
removed, the cross-log gap is an artifact of feature choice, not a finding about domains.

Three arms per log:

    full        every inadmissible feature the spec names
    counts      ONLY the raw event and activity counts
    no_counts   every inadmissible feature EXCEPT those counts

If `counts` alone recovers most of `full`, the mechanism is the duration proxy and the paper
must say so. If `no_counts` stays high, the leakage is genuinely spread across the recorded
aggregates and the cross-log comparison means what it appears to mean.

    python mechanism_check.py
"""
from __future__ import annotations

import _openmp_first  # noqa: F401

import json
import os
import warnings

import pandas as pd
from sklearn.metrics import roc_auc_score

import build_log
from bpi2014_data import get_models

warnings.filterwarnings("ignore")
HERE = os.path.dirname(os.path.abspath(__file__))
COUNTS = {"n_events", "n_activities"}


def auc(b, feats, tr, te, model="XGBoost"):
    m = get_models(b.y_all.loc[tr])[model]
    m.fit(b.d.loc[tr, feats], b.y_all.loc[tr])
    return roc_auc_score(b.y_all.loc[te], m.predict_proba(b.d.loc[te, feats])[:, 1])


def run(slug):
    b = build_log.build(slug, verbose=False)
    tr, te = b.idx_tr, b.idx_te
    adm = b.BASE_CLEAN + b.GROUP_TE
    inad = [f for f in b.INADMISSIBLE if f in b.d.columns]
    counts = [f for f in inad if f in COUNTS]
    others = [f for f in inad if f not in COUNTS]

    base = auc(b, adm, tr, te)
    arms = {"full": adm + inad}
    if counts:
        arms["counts"] = adm + counts
    if others:
        arms["no_counts"] = adm + others

    out = {"slug": slug, "domain": b.spec.domain, "admissible_auc": base,
           "n_inadmissible": len(inad), "counts_used": counts, "others_used": others}
    for name, feats in arms.items():
        a = auc(b, feats, tr, te)
        out[name + "_auc"] = a
        out[name + "_cost"] = a - base
        out[name + "_share_pct"] = 100.0 * (a - base) / (a - 0.5) if a > 0.5 else float("nan")
    return out


def main():
    rows = []
    for slug in ["bpic2017", "sepsis", "hospital", "trafficfines"]:
        try:
            rows.append(run(slug))
            print("  %s done" % slug)
        except Exception as e:
            print("  %-13s SKIPPED: %s" % (slug, str(e)[:70]))

    if not rows:
        print("no logs available yet")
        return 1

    df = pd.DataFrame(rows)
    df.to_csv(os.path.join(HERE, "results_crosslog", "mechanism_check.csv"), index=False) \
        if os.path.isdir(os.path.join(HERE, "results_crosslog")) else None
    os.makedirs(os.path.join(HERE, "results_crosslog"), exist_ok=True)
    df.to_csv(os.path.join(HERE, "results_crosslog", "mechanism_check.csv"), index=False)

    print()
    print("%-13s %9s %9s %9s %9s" % ("log", "adm AUC", "full%", "counts%", "no_counts%"))
    for r in rows:
        print("%-13s %9.4f %8.1f%% %8s %10s" % (
            r["slug"], r["admissible_auc"], r.get("full_share_pct", float("nan")),
            ("%.1f%%" % r["counts_share_pct"]) if "counts_share_pct" in r else "-",
            ("%.1f%%" % r["no_counts_share_pct"]) if "no_counts_share_pct" in r else "-"))
    json.dump(rows, open(os.path.join(HERE, "results_crosslog", "mechanism_check.json"), "w"), indent=2)
    print("\nwritten to results_crosslog/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
