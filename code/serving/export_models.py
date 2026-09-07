"""Refit the two winning models and persist them for serving.

WHY THIS FILE EXISTS.
`model_a/model_a_conversion.py` and `tune_lean.py` train, evaluate and then throw the
fitted estimator away. That is fine for a paper and useless for an application. This
script refits the exact winning configuration of each, verifies it reproduces the
reported test AUC on the same split and seed, measures what the CRM feature adapter
actually costs, and writes the estimator plus every fitted preprocessing object to
`artifacts/`.

WHICH CONFIGURATIONS, AND WHY THESE.

Model A, conversion. `A3_tuned_no_duration`, tuned XGBoost, test AUC 0.8185, CV AUC
0.7981 (`model_a/model_a_results.csv`). `duration` is excluded because it is call
length, which is only known after the call has happened. Keeping it lifts AUC to 0.9537
and makes the model undeployable. Best params come from `model_a/model_a_run.log:27`.

Model B, SLA breach. `XGBoost_tuned` on the leak-free feature set, test AUC 0.7927,
CV AUC 0.7932, and the only single model whose gain over the untuned 0.7877 reference
has a paired-bootstrap 95% CI excluding zero (`results_bpi_leakfree/model_b_significance.csv`).
Stacking scores 0.7935, eight ten-thousandths higher, at the cost of three base learners
plus a meta learner in the serving path. Not worth it. Best params come from
`model_b_tune_run.log:14`.

WHAT IS PERSISTED IS THE TRAIN-SPLIT MODEL, NOT A 100% REFIT. An earlier version refit
on all rows before saving, which is normal deployment practice but creates a reporting
problem: the number in the results table then describes a model that is not the one
running. Serving the evaluated model costs 20 per cent of the training data and removes
the objection completely, which is the right trade for a paper.

THE ADAPTER ABLATION IS THE POINT OF THIS FILE.
A 7-model council review on 2026-09-02 returned one unanimous attack: the CRM cannot
supply every feature, so the adapter holds the rest at the training median, and the
published AUC therefore does NOT describe what the deployed system does. Every seat
prescribed the same fix, so this script runs it. For each model it re-scores the SAME
held-out set with exactly the columns the adapter cannot fill clamped to the training
median, and reports that "adapter AUC" beside the full-feature AUC. That gives an honest
bracket rather than an argument: full-feature AUC X, deployment-condition AUC Y.
"""

import os as _o, sys as _s  # noqa: E402
_s.path.insert(0, _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__))))
import _openmp_first  # noqa: F401,E402  pins threads, must precede every ML import

from pathlib import Path
import json
import time

import joblib
import numpy as np
import pandas as pd
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import LabelEncoder, OrdinalEncoder
from sklearn.metrics import roc_auc_score
from xgboost import XGBClassifier

# BPI 2014 timestamp formats. BOTH files are day-first; the incident file uses "/" and the
# activity file uses "-". This was previously parsed with format="mixed", dayfirst=False on the
# incident file, which silently swapped day and month on the 41 per cent of rows where both
# fields are <= 12. Verified from the raw bytes on 2026-09-07: across 46,606 non-blank Open Time
# values, 27,994 have a first field > 12 and ZERO have a second field > 12, so month-first is
# arithmetically impossible. Never use format="mixed" on an ambiguous numeric date.
INCIDENT_TS_FORMAT = "%d/%m/%Y %H:%M:%S"
ACTIVITY_TS_FORMAT = "%d-%m-%Y %H:%M:%S"


SEED = 42
SMOOTH = 20  # target-encoding smoothing, identical to tune_lean.py
np.random.seed(SEED)

HERE = Path(__file__).parent
CODE = HERE.parent
ART = HERE / "artifacts"
ART.mkdir(exist_ok=True)

# Best params, copied verbatim from the tuning logs cited in the docstring.
A_PARAMS = dict(subsample=0.7, reg_lambda=10, n_estimators=500, min_child_weight=10,
                max_depth=6, learning_rate=0.01, gamma=0.3, colsample_bytree=0.8)
B_PARAMS = dict(subsample=0.8, reg_lambda=3, n_estimators=500, min_child_weight=1,
                max_depth=8, learning_rate=0.03, gamma=0, colsample_bytree=0.6)


def xgb(params, spw):
    return XGBClassifier(**params, scale_pos_weight=spw, eval_metric="logloss",
                         random_state=SEED, verbosity=0, tree_method="hist", n_jobs=-1)


def clamped_auc(model, X_test, y_test, live_features, medians):
    """AUC when only `live_features` vary and every other column is pinned to its
    training median. That is exactly what the CRM adapter feeds the model, so it is the
    honest upper bound on deployed discrimination."""
    Xc = X_test.copy()
    for c in Xc.columns:
        if c not in live_features:
            Xc[c] = float(medians[c])
    return roc_auc_score(y_test, model.predict_proba(Xc)[:, 1])


# The columns each adapter genuinely fills from a CRM request. Kept here so the ablation
# and the running service cannot drift apart.
A_DRIVEN = ["job", "contact", "month", "day_of_week", "campaign", "pdays", "previous", "poutcome"]
B_DRIVEN = ["Priority", "Impact", "Urgency", "Open_Hour", "Open_DayOfWeek", "Is_Weekend",
            "Is_Business_Hours", "Open_Month", "Queue_Length_At_Open",
            "Assignment_Delay_Hours", "Group_Breach_Rate_TE", "Group_Volume_TE"]
B_DRIVEN_NO_PRIORITY = [f for f in B_DRIVEN if f not in ("Priority", "Impact", "Urgency")]


# ---------------------------------------------------------------- Model A
def export_model_a():
    print("=" * 78)
    print("  MODEL A, conversion, UCI Bank Marketing")
    print("=" * 78)
    df = pd.read_csv(CODE / "bank" / "bank-additional-full.csv", sep=";")
    y = (df["y"] == "yes").astype(int)

    X = df.drop(columns=["y"]).copy()
    cat_cols = X.select_dtypes(include="object").columns.tolist()
    # Ordinal encoding uses no label information, so fitting it on all rows before the
    # split is not leakage. It is fitted here once so the serving path can encode a
    # single incoming row against exactly the categories the model was trained on.
    enc = OrdinalEncoder(handle_unknown="use_encoded_value", unknown_value=-1)
    X[cat_cols] = enc.fit_transform(X[cat_cols].astype(str))
    X = X.astype(float)

    feats = [c for c in X.columns if c != "duration"]  # duration is post-hoc
    X = X[feats]

    tr, te = train_test_split(X.index, test_size=0.2, stratify=y, random_state=SEED)
    spw = (y.loc[tr] == 0).sum() / max((y.loc[tr] == 1).sum(), 1)

    t0 = time.time()
    m = xgb(A_PARAMS, spw).fit(X.loc[tr], y.loc[tr])
    holdout_auc = roc_auc_score(y.loc[te], m.predict_proba(X.loc[te])[:, 1])
    print(f"  held-out AUC {holdout_auc:.4f}  (published A3_tuned_no_duration: 0.8185)")

    medians = X.loc[tr].median()
    adapter_auc = clamped_auc(m, X.loc[te], y.loc[te], A_DRIVEN, medians)
    print(f"  adapter AUC  {adapter_auc:.4f}  ({len(A_DRIVEN)} of {len(feats)} features live, "
          f"the rest clamped to the training median)")
    print(f"  fit in {time.time()-t0:.0f}s")

    final = m  # serve the model that was evaluated, not a 100 per cent refit
    joblib.dump(
        {
            "model": final,
            "encoder": enc,
            "cat_cols": cat_cols,
            "features": feats,
            # Column medians. The CRM has no macroeconomic indicators or campaign
            # history, so the adapter fills those columns with the training median.
            # Storing them here keeps that decision in the artifact rather than
            # hard-coded in the service.
            "medians": medians.to_dict(),
            "base_rate": float(y.mean()),
        },
        ART / "model_a.joblib",
    )
    return {
        "name": "model_a_conversion",
        "version": "xgb-tuned-a3-nodur",
        "dataset": "UCI Bank Marketing bank-additional-full.csv (41,188 rows)",
        "config": "A3_tuned_no_duration",
        "holdout_auc": round(float(holdout_auc), 4),
        "adapter_auc": round(float(adapter_auc), 4),
        "live_features": len(A_DRIVEN),
        "published_test_auc": 0.8185,
        "published_cv_auc": 0.7981,
        "n_features": len(feats),
        "base_rate": round(float(y.mean()), 4),
    }


# ---------------------------------------------------------------- Model B
def build_bpi_frame():
    """Rebuild the leak-free BPI 2014 feature frame exactly as tune_lean.py does."""
    D = CODE / "phase4" / "bpi2014"
    inc = pd.read_csv(D / "Detail_Incident.csv", sep=";", encoding="latin1")

    # 203 completely empty trailing rows in the published file carry a null Incident ID and
    # collide as duplicate NaN keys under set_index. Real incidents: 46,606 of 46,809 raw rows.
    inc = inc[inc["Incident ID"].notna()].copy()
    # One row carries Urgency = "5 - Very Low" rather than "5", which makes the column object
    # dtype and causes XGBoost to reject the matrix. Take the leading integer.
    for _c in ["Priority", "Impact", "Urgency"]:
        if _c in inc.columns:
            inc[_c] = pd.to_numeric(
                inc[_c].astype(str).str.extract(r"^\s*(\d+)", expand=False), errors="coerce"
            ) if inc[_c].dtype == object else pd.to_numeric(inc[_c], errors="coerce")
    act = pd.read_csv(D / "Detail_Incident_Activity.csv", sep=";", encoding="latin1")

    inc["Handle_Time_Hours"] = pd.to_numeric(
        inc["Handle Time (Hours)"].astype(str).str.replace(",", "."), errors="coerce")
    for c in ["Open Time", "Resolved Time", "Close Time"]:
        inc[c] = pd.to_datetime(inc[c], format=INCIDENT_TS_FORMAT, errors="coerce")
    act["DateStamp"] = pd.to_datetime(act["DateStamp"], format=ACTIVITY_TS_FORMAT, errors="coerce")

    # The SLA target: handle time above twice the per-priority median. There is no
    # contractual SLA in BPI 2014, so this is the standard derived proxy.
    pm = inc.groupby("Priority")["Handle_Time_Hours"].median()
    inc["SLA_Breached"] = (inc["Handle_Time_Hours"] >
                           inc["Priority"].map({p: m * 2.0 for p, m in pm.items()})).astype(int)
    df = inc[inc["Handle_Time_Hours"].notna() & inc["Priority"].notna()].copy()

    ae = act[act["IncidentActivity_Type"].isin(["Assignment", "Reassignment"])]
    df = df.merge(ae.sort_values("DateStamp").groupby("Incident ID")["Assignment Group"].first()
                  .reset_index().rename(columns={"Assignment Group": "First_Assignment_Group"}),
                  on="Incident ID", how="left")
    df = df.merge(ae.sort_values("DateStamp").groupby("Incident ID")["DateStamp"].first()
                  .reset_index().rename(columns={"DateStamp": "First_Assignment_Time"}),
                  on="Incident ID", how="left")
    df["Assignment_Delay_Hours"] = (
        (df["First_Assignment_Time"] - df["Open Time"]).dt.total_seconds() / 3600
    ).clip(lower=0).fillna(0)
    df["Open_Hour"] = df["Open Time"].dt.hour
    df["Open_DayOfWeek"] = df["Open Time"].dt.dayofweek
    df["Is_Weekend"] = (df["Open_DayOfWeek"] >= 5).astype(int)
    df["Is_Business_Hours"] = ((df["Open_Hour"] >= 8) & (df["Open_Hour"] <= 18)).astype(int)
    df["Open_Month"] = df["Open Time"].dt.month

    # Queue length at open: how many incidents were still unresolved at that moment.
    ds = df.sort_values("Open Time")
    ql, open_set = [], []
    for o, r in zip(ds["Open Time"].values, ds["Resolved Time"].values):
        open_set = [x for x in open_set if x > o or pd.isna(x)]
        ql.append(len(open_set))
        if pd.notna(r):
            open_set.append(r)
    df = df.merge(ds.assign(Queue_Length_At_Open=ql)[["Incident ID", "Queue_Length_At_Open"]],
                  on="Incident ID", how="left")

    encoders = {}
    for src, tgt in [("CI Type (aff)", "CI_Type_Encoded"), ("CI Subtype (aff)", "CI_Subtype_Encoded"),
                     ("Service Component WBS (aff)", "WBS_Encoded"), ("Category", "Category_Encoded"),
                     ("Alert Status", "Alert_Status_Encoded")]:
        le = LabelEncoder()
        df[tgt] = le.fit_transform(df[src].fillna("unknown").astype(str))
        encoders[tgt] = le
    return df, encoders


B_BASE = ["Priority", "Impact", "Urgency", "Open_Hour", "Open_DayOfWeek", "Is_Weekend",
          "Is_Business_Hours", "Open_Month", "Queue_Length_At_Open", "Assignment_Delay_Hours",
          "CI_Type_Encoded", "CI_Subtype_Encoded", "WBS_Encoded", "Category_Encoded",
          "Alert_Status_Encoded"]


def export_model_b():
    print("=" * 78)
    print("  MODEL B, SLA breach, BPI Challenge 2014")
    print("=" * 78)
    df, encoders = build_bpi_frame()
    d = df[B_BASE + ["SLA_Breached", "First_Assignment_Group"]].dropna(
        subset=B_BASE + ["SLA_Breached"]).copy()
    d["First_Assignment_Group"] = d["First_Assignment_Group"].fillna("unknown").astype(str)
    y, g = d["SLA_Breached"], d["First_Assignment_Group"]

    def enc_fit(gg, yy):
        st = pd.DataFrame({"g": gg.values, "y": yy.values}).groupby("g")["y"].agg(["mean", "count"])
        prior = float(yy.mean())
        smoothed = (st["mean"] * st["count"] + prior * SMOOTH) / (st["count"] + SMOOTH)
        return smoothed, st["count"], prior

    def enc_apply(X, gg, e):
        rate, vol, prior = e
        X = X.copy()
        X["Group_Breach_Rate_TE"] = gg.map(rate).fillna(prior).values
        X["Group_Volume_TE"] = gg.map(vol).fillna(0).values
        return X

    tr, te = train_test_split(d.index, test_size=0.2, stratify=y, random_state=SEED)

    # Held-out check. The target encoding is fitted on the training rows ONLY, which is
    # the whole point of the leak-free rebuild: fitting it on all rows was defect (2)
    # in the original pipeline and inflated AUC to 0.8958.
    e_tr = enc_fit(g.loc[tr], y.loc[tr])
    Xtr = enc_apply(d.loc[tr, B_BASE], g.loc[tr], e_tr)
    Xte = enc_apply(d.loc[te, B_BASE], g.loc[te], e_tr)
    spw = (y.loc[tr] == 0).sum() / max((y.loc[tr] == 1).sum(), 1)

    t0 = time.time()
    m = xgb(B_PARAMS, spw).fit(Xtr, y.loc[tr])
    holdout_auc = roc_auc_score(y.loc[te], m.predict_proba(Xte)[:, 1])
    print(f"  held-out AUC {holdout_auc:.4f}  (published XGBoost_tuned: 0.7927)")

    medians = Xtr.median()
    adapter_auc = clamped_auc(m, Xte, y.loc[te], B_DRIVEN, medians)
    print(f"  adapter AUC  {adapter_auc:.4f}  ({len(B_DRIVEN)} of {Xtr.shape[1]} features live)")
    # The council also flagged that CRM priority and BPI priority are different
    # constructs, so the model gives a CRM high-priority lead a LOWER breach probability
    # than intuition expects, and that inverted signal would feed the optimiser. This
    # variant clamps priority, impact and urgency too, leaving the model on the features
    # that genuinely transfer: queue depth, assignment delay and rep history.
    adapter_auc_nopri = clamped_auc(m, Xte, y.loc[te], B_DRIVEN_NO_PRIORITY, medians)
    print(f"  adapter AUC  {adapter_auc_nopri:.4f}  (priority masked too, "
          f"{len(B_DRIVEN_NO_PRIORITY)} features live)")
    print(f"  fit in {time.time()-t0:.0f}s")

    # Stability of the group target encoding, which is the strongest single feature.
    counts = g.loc[tr].value_counts()
    print(f"  assignment groups in train: {len(counts)}, median {int(counts.median())} cases, "
          f"{(counts < SMOOTH).mean():.0%} below the k={SMOOTH} smoothing threshold")

    final = m  # serve the model that was evaluated
    rate, vol, prior = e_tr
    joblib.dump(
        {
            "model": final,
            "features": Xtr.columns.tolist(),
            "label_encoders": {k: list(v.classes_) for k, v in encoders.items()},
            "group_breach_rate": rate.to_dict(),
            "group_volume": vol.to_dict(),
            "group_prior": prior,
            "medians": medians.to_dict(),
            "base_rate": float(y.mean()),
        },
        ART / "model_b.joblib",
    )
    return {
        "name": "model_b_sla",
        "version": "xgb-tuned-leakfree",
        "dataset": "BPI Challenge 2014, Rabobank ITSM incidents",
        "config": "XGBoost_tuned (leak-free feature set)",
        "holdout_auc": round(float(holdout_auc), 4),
        # The SERVED adapter masks priority, so the served figure is the masked one.
        # The unmasked variant is kept beside it as the comparison that justified the
        # decision: two thousandths of AUC to remove an inverted signal.
        "adapter_auc": round(float(adapter_auc_nopri), 4),
        "adapter_auc_priority_passed_through": round(float(adapter_auc), 4),
        "live_features": len(B_DRIVEN_NO_PRIORITY),
        "group_median_cases": int(counts.median()),
        "groups_below_smoothing": round(float((counts < SMOOTH).mean()), 4),
        "published_test_auc": 0.7927,
        "published_cv_auc": 0.7932,
        "n_features": Xtr.shape[1],
        "base_rate": round(float(y.mean()), 4),
    }


if __name__ == "__main__":
    meta = {"exported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "models": [export_model_a(), export_model_b()]}
    (ART / "registry.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print("\nWrote", ART / "registry.json")
    print("\n  THE HONEST BRACKET, which is what goes in the paper:")
    for m in meta["models"]:
        print("  {:20s} full-feature {}  ->  adapter {}   ({} of {} features live)".format(
            m["name"], m["holdout_auc"], m["adapter_auc"], m["live_features"], m["n_features"]))
