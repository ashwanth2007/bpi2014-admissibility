"""Export the two models the Streamlit prototype serves, from the CURRENT pipeline.

WHY THIS EXISTS RATHER THAN REUSING code/serving/artifacts/. Those artifacts were
exported on 2026-09-02, before the dataset was completed, the date parse corrected, Rule 2
implemented as its equation and the thread count pinned. Their registry still reports a
holdout AUC of 0.7927 and a base rate of 0.3664 on 31,236 incidents. Every one of those
numbers is superseded. A UI built on them would show a visitor figures the paper no longer
claims, which is the same defect the paper is about, committed in the demo.

WHAT IS EXPORTED, AND WHY BOTH. Two models, trained on the same rows with the same learner
and the same split, differing ONLY in which attributes they may see:

  admissible    the 17 attributes knowable at the assignment moment, Rules 1 and 2 enforced
  unconstrained the 20 attributes of default practice, including five that are only knowable
                once the case is closed

The prototype shows both on every incident. That side by side is the whole point: the
unconstrained model looks better on paper and cannot be deployed, because at the moment the
decision is taken its inputs do not exist yet.

Run:  python app/export_ui_models.py
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _openmp_first  # noqa: F401,E402  pins threads, must precede every ML import

import joblib  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from sklearn.metrics import roc_auc_score  # noqa: E402

import bpi2014_data as bd  # noqa: E402

HERE = Path(__file__).resolve().parent
OUT = HERE / "artifacts"


def main() -> int:
    t0 = time.time()
    OUT.mkdir(exist_ok=True)
    print("building the modelling matrix from the canonical log ...")
    b = bd.build()
    d, y, g = b.d, b.y_all, b.g_all
    tr, te = b.idx_tr, b.idx_te

    base = [f for f in bd.BASE_CLEAN]
    admissible = base + list(bd.GROUP_TE)
    unconstrained = list(bd.CONTAMINATED)

    enc = bd.fit_group_encoding(g.loc[tr], y.loc[tr])
    Xtr = bd.apply_group_encoding(d.loc[tr, base], g.loc[tr], enc)
    Xte = bd.apply_group_encoding(d.loc[te, base], g.loc[te], enc)

    models = bd.get_models(y.loc[tr]) if hasattr(bd, "get_models") else None
    if models is None:
        from xgboost import XGBClassifier
        neg, pos = int((y.loc[tr] == 0).sum()), int((y.loc[tr] == 1).sum())
        def _xgb():
            return XGBClassifier(n_estimators=300, max_depth=6, learning_rate=0.05,
                                 subsample=0.8, colsample_bytree=0.8,
                                 scale_pos_weight=neg / max(pos, 1),
                                 eval_metric="logloss", random_state=bd.SEED,
                                 tree_method="hist")
    else:
        def _xgb():
            return bd.get_models(y.loc[tr])["XGBoost"]

    out = {"exported_at": pd.Timestamp.now().isoformat(timespec="seconds"),
           "dataset": "BPI Challenge 2014, Rabobank ITSM incidents",
           "incidents": int(len(d)), "train_rows": int(len(tr)), "test_rows": int(len(te)),
           "base_rate": round(float(y.mean()), 4),
           "rule2_mode": bd.RULE2_MODE,
           "threads_pinned": os.environ.get("OMP_NUM_THREADS"),
           "models": {}}

    # ---------------------------------------------------------------- admissible
    m_adm = _xgb()
    m_adm.fit(Xtr[admissible], y.loc[tr])
    p_adm = m_adm.predict_proba(Xte[admissible])[:, 1]
    auc_adm = float(roc_auc_score(y.loc[te], p_adm))
    joblib.dump({"model": m_adm, "features": admissible, "encoding": enc},
                OUT / "ui_admissible.joblib")
    out["models"]["admissible"] = {"features": len(admissible), "holdout_auc": round(auc_adm, 4),
                                   "feature_list": admissible}
    print("  admissible    %2d attributes  holdout AUC %.4f" % (len(admissible), auc_adm))

    # ---------------------------------------------------------------- unconstrained
    m_unc = _xgb()
    m_unc.fit(d.loc[tr, unconstrained], y.loc[tr])
    p_unc = m_unc.predict_proba(d.loc[te, unconstrained])[:, 1]
    auc_unc = float(roc_auc_score(y.loc[te], p_unc))
    joblib.dump({"model": m_unc, "features": unconstrained}, OUT / "ui_unconstrained.joblib")
    out["models"]["unconstrained"] = {"features": len(unconstrained),
                                      "holdout_auc": round(auc_unc, 4),
                                      "feature_list": unconstrained}
    print("  unconstrained %2d attributes  holdout AUC %.4f" % (len(unconstrained), auc_unc))

    out["cost_of_admissibility"] = round(auc_unc - auc_adm, 4)
    out["share_of_signal_pct"] = round(100.0 * (auc_unc - auc_adm) / (auc_unc - 0.5), 1)
    print("  cost %.4f AUC, %.1f%% of above-chance signal"
          % (out["cost_of_admissibility"], out["share_of_signal_pct"]))

    # ------------------------------------------------- the evaluation slice the UI browses
    # Everything the queue view needs, joined once here so the app never re-derives it.
    slice_cols = ["Incident ID", "Open Time", "First_Assignment_Group"]
    q = d.loc[te, [c for c in slice_cols if c in d.columns]].copy()
    q["breach_risk_admissible"] = p_adm
    q["breach_risk_unconstrained"] = p_unc
    q["actually_breached"] = y.loc[te].values
    meta = b.df.set_index("Incident ID")
    for col, dest in (("Category", "category"), ("Handle_Time_Hours", "handle_hours"),
                      ("SLA_Threshold_Hours", "threshold_hours")):
        if col in meta.columns:
            q[dest] = q["Incident ID"].map(meta[col])
    # Category_Encoded is what the assignment view matches a group's expertise against.
    # It was missing from the first export and the expertise column read zero for every
    # candidate without saying so, which is the quiet kind of wrong.
    for col in ("Priority", "Impact", "Urgency", "Queue_Length_At_Open",
                "Assignment_Delay_Hours", "Open_Hour", "Is_Business_Hours",
                "Category_Encoded", "CI_Type_Encoded", "WBS_Encoded"):
        if col in d.columns:
            q[col] = d.loc[te, col].values
    q.to_parquet(OUT / "ui_queue.parquet") if _parquet_ok() else q.to_csv(
        OUT / "ui_queue.csv", index=False)
    print("  queue slice   %d incidents" % len(q))

    # --------------------------------------------------- candidate groups for the assignment view
    # Built from TRAINING rows only. A group profile assembled over the whole log would tell the
    # assignment view things about a group that had not happened yet when the incident arrived,
    # which is Rule 1 applied to the thing doing the assigning rather than to the model.
    tr_g = g.loc[tr]
    tr_y = y.loc[tr]
    tr_cat = d.loc[tr, "Category_Encoded"] if "Category_Encoded" in d.columns else None
    prof = pd.DataFrame({
        "group": tr_g.values, "breached": tr_y.values,
        "category": (tr_cat.values if tr_cat is not None else 0),
    })
    grp = (prof.groupby("group")
                .agg(cases=("breached", "size"), breach_rate=("breached", "mean"))
                .reset_index())
    grp["breach_rate"] = grp["breach_rate"].round(4)
    grp.to_csv(OUT / "ui_groups.csv", index=False)

    # per group and category, how many training cases that group has handled. This is the
    # evidence behind the expertise criterion and it is a count, not a model output.
    gc = (prof.groupby(["group", "category"]).size().rename("cases").reset_index())
    gc = gc[gc["cases"] >= 3]
    gc.to_csv(OUT / "ui_group_category.csv", index=False)
    out["groups"] = int(len(grp))
    out["group_category_pairs"] = int(len(gc))
    print("  group profile %d groups, %d group-category pairs with 3+ cases"
          % (len(grp), len(gc)))

    out["runtime_seconds"] = round(time.time() - t0, 1)
    json.dump(out, open(OUT / "ui_registry.json", "w"), indent=2)
    print("\nwrote %s" % OUT)
    return 0


def _parquet_ok():
    try:
        import pyarrow  # noqa: F401
        return True
    except ImportError:
        return False


if __name__ == "__main__":
    sys.exit(main())
