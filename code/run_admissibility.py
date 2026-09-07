"""Run the admissibility measurement on any log in log_schemas.

Same three configurations as BPI 2014, same four learners, same splits:

    contaminated   admissible features plus the case-completion aggregates
    admissible     admissible features only, Rule 2 encoding
    chronological  admissible features, trained on the earliest 80 per cent by decision time

The cost of admissibility is contaminated minus admissible, and the share of signal is that
difference over (contaminated AUC minus 0.5).

    python run_admissibility.py bpic2017
"""
from __future__ import annotations

import _openmp_first  # noqa: F401  MUST precede sklearn/xgboost, see module docstring

import json
import os
import sys
import warnings

import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score, f1_score, accuracy_score, precision_score, recall_score
from sklearn.model_selection import StratifiedKFold

import build_log
from bpi2014_data import get_models

warnings.filterwarnings("ignore")
HERE = os.path.dirname(os.path.abspath(__file__))


def score(y_true, proba, thr=0.5):
    pred = (proba >= thr).astype(int)
    return dict(
        Test_AUC=roc_auc_score(y_true, proba),
        Test_F1=f1_score(y_true, pred, zero_division=0),
        Test_Accuracy=accuracy_score(y_true, pred),
        Test_Precision=precision_score(y_true, pred, zero_division=0),
        Test_Recall=recall_score(y_true, pred, zero_division=0),
    )


def evaluate(b, feats, tr, te, cv=True):
    rows = []
    d, y = b.d, b.y_all
    for name, model in get_models(y.loc[tr]).items():
        cv_auc = np.nan
        if cv:
            aucs = []
            skf = StratifiedKFold(5, shuffle=True, random_state=build_log.SEED)
            for f_tr, f_va in skf.split(d.loc[tr, feats], y.loc[tr]):
                i_tr, i_va = np.asarray(tr)[f_tr], np.asarray(tr)[f_va]
                m = get_models(y.loc[i_tr])[name]
                m.fit(d.loc[i_tr, feats], y.loc[i_tr])
                aucs.append(roc_auc_score(y.loc[i_va], m.predict_proba(d.loc[i_va, feats])[:, 1]))
            cv_auc = float(np.mean(aucs))
        model.fit(d.loc[tr, feats], y.loc[tr])
        proba = model.predict_proba(d.loc[te, feats])[:, 1]
        rows.append(dict(Model=name, Num_Features=len(feats), CV_AUC_mean=cv_auc,
                         **score(y.loc[te], proba)))
    return rows


def main(slug):
    b = build_log.build(slug)
    out = os.path.join(HERE, "results_%s" % slug)
    os.makedirs(out, exist_ok=True)

    rows = []
    for cfg, feats, tr, te, cv in [
        ("contaminated", b.CONTAMINATED + b.GROUP_TE, b.idx_tr, b.idx_te, True),
        ("admissible", b.BASE_CLEAN + b.GROUP_TE, b.idx_tr, b.idx_te, True),
        ("chronological", b.BASE_CLEAN + b.GROUP_TE, b.idx_tr_time, b.idx_te_time, False),
    ]:
        print("\n%s (%d features)" % (cfg, len(feats)))
        for r in evaluate(b, feats, tr, te, cv=cv):
            r["Config"] = cfg
            rows.append(r)
            print("  %-13s AUC %.4f  F1 %.4f  Acc %.4f" %
                  (r["Model"], r["Test_AUC"], r["Test_F1"], r["Test_Accuracy"]))

    df = pd.DataFrame(rows)[["Config", "Model", "Num_Features", "CV_AUC_mean",
                             "Test_AUC", "Test_F1", "Test_Accuracy",
                             "Test_Precision", "Test_Recall"]]
    df.to_csv(os.path.join(out, "model_comparison.csv"), index=False)

    piv = df.pivot(index="Model", columns="Config", values="Test_AUC")
    piv["cost"] = piv["contaminated"] - piv["admissible"]
    piv["share_of_signal_pct"] = 100.0 * piv["cost"] / (piv["contaminated"] - 0.5)
    piv.to_csv(os.path.join(out, "admissibility_cost.csv"))

    summary = dict(
        slug=slug, name=b.spec.name, domain=b.spec.domain, group_level=b.spec.group_level,
        n_cases=int(len(b.d)), n_groups=int(b.g_all.nunique()),
        breach_rate=float(b.y_all.mean()),
        n_admissible=len(b.BASE_CLEAN) + len(b.GROUP_TE),
        n_inadmissible=len(b.INADMISSIBLE),
        best_admissible_auc=float(piv["admissible"].max()),
        cost_best=float(piv.loc[piv["admissible"].idxmax(), "cost"]),
        share_of_signal_pct=float(piv.loc[piv["admissible"].idxmax(), "share_of_signal_pct"]),
    )
    json.dump(summary, open(os.path.join(out, "summary.json"), "w"), indent=2)

    print("\n" + "=" * 66)
    print(piv.round(4).to_string())
    print("\nwritten to results_%s/" % slug)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "bpic2017"))
