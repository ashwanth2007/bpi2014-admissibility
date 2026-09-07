"""Re-export Model B from the v3 causal-encoding configuration, which is the one the
architecture brief actually reports.

WHY THIS FILE EXISTS, AND THE MISTAKE IT CORRECTS.
The first serving export used the v2 leak-free feature set, 17 features, tuned XGBoost
at 0.7927 held out. That is not the project's best Model B and it is not the number in
`deliverables/Model-Architecture-and-Status-Brief.pdf`, which reports 0.8071. The brief
is right: `model_b_v3.py` adds causal expanding-window target encodings across five
entities plus group open load, CI recency and a trailing breach rate, reaching 28
features, and it clears 0.80 where v2 explicitly did not. Serving v2 while presenting
v3 would have been an inconsistency an examiner would find in one question.

THE RESULT THAT CAME OUT OF THIS, AND WHY v3 IS NOT SERVED.
Running the adapter ablation on v3 inverted the ranking. On BPI columns v3 beats v2,
0.8070 against 0.7927. Under the CRM adapter v3 FALLS BELOW it, 0.6782 against 0.7097,
because most of v3's advantage lives in causal encodings over configuration item,
service component and category, and a CRM has none of those. The better research model
is the worse product model, and the gap is measured rather than argued.

So the paper reports v3 and the prototype serves v2. This script therefore writes
`model_b_v3.joblib`, NOT `model_b.joblib`, and registers itself as a comparison entry.

WHICH v3 MODEL IS COMPARED, AND WHY NOT THE HEADLINE ENSEMBLE.
The v3 headline 0.8071 is a stacked ensemble: XGBoost plus LightGBM plus CatBoost with
a logistic meta-learner. This uses the tuned XGBoost at 0.8060 instead. Two reasons,
both stated in the brief rather than hidden. First, the gap is 0.0011 and the tuned
model's own gain over the plain reference already has a confidence interval spanning
zero, so the ensemble's advantage is inside the noise. Second, a single gradient-boosted
tree gives exact TreeSHAP attributions for the probability it actually produced; a
stacked ensemble does not, and explaining a number with a different model's attributions
is worse than losing 0.0011.

THE ADAPTER ABLATION RUNS HERE TOO, for the reason given in `export_models.py`: the
published AUC describes the model on BPI columns, not on what this CRM can send it.
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
from sklearn.preprocessing import LabelEncoder
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
SMOOTH = 20
W = 500  # trailing-window length, identical to model_b_v3.py
np.random.seed(SEED)

HERE = Path(__file__).parent
CODE = HERE.parent
ART = HERE / "artifacts"
ART.mkdir(exist_ok=True)

# Tuned params from code/results_v3/model_b_v3_summary.json, produced by the v3 search.
B3_PARAMS = dict(subsample=0.7, reg_lambda=3, n_estimators=1200, min_child_weight=5,
                 max_depth=10, learning_rate=0.01, gamma=0.5, colsample_bytree=0.6)

FEATS = [
    "Priority", "Impact", "Urgency",
    "Open_Hour", "Open_DayOfWeek", "Is_Weekend", "Is_Business_Hours", "Open_Month", "Open_Year",
    "Queue_Length_At_Open", "Assignment_Delay_Hours",
    "CI_Type_Encoded", "CI_Subtype_Encoded", "WBS_Encoded", "Category_Encoded", "Alert_Status_Encoded",
    "Group_BreachRate_Causal", "Group_PriorCount", "Group_Open_Load",
    "CIName_BreachRate_Causal", "CIName_PriorCount",
    "CIType_BreachRate_Causal", "Category_BreachRate_Causal", "WBS_BreachRate_Causal",
    "Trailing_Breach_Rate_500", "Trailing_Window_N",
    "CI_Days_Since_Prev", "CI_Incidents_30d",
]

# What a CRM request can genuinely fill. Priority, impact and urgency stay clamped for
# the construct-mismatch reason documented in app.py. The CI, WBS and category columns
# have no CRM counterpart at all.
B3_DRIVEN = [
    "Open_Hour", "Open_DayOfWeek", "Is_Weekend", "Is_Business_Hours", "Open_Month", "Open_Year",
    "Queue_Length_At_Open", "Assignment_Delay_Hours",
    "Group_BreachRate_Causal", "Group_PriorCount", "Group_Open_Load",
    "Trailing_Breach_Rate_500", "Trailing_Window_N",
]


def clamped_auc(model, X_test, y_test, live, medians):
    Xc = X_test.copy()
    for c in Xc.columns:
        if c not in live:
            Xc[c] = float(medians[c])
    return roc_auc_score(y_test, model.predict_proba(Xc)[:, 1])


def build_v3_frame():
    """Rebuild the 28-feature causal frame exactly as code/model_b_v3.py does."""
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

    pm = inc.groupby("Priority")["Handle_Time_Hours"].median()
    inc["SLA_Breached"] = (inc["Handle_Time_Hours"] >
                           inc["Priority"].map({p: m * 2.0 for p, m in pm.items()})).astype(int)
    df = inc[inc["Handle_Time_Hours"].notna() & inc["Priority"].notna()].copy().reset_index(drop=True)

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
    df["Open_Year"] = df["Open Time"].dt.year

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

    # ---- causal machinery, verbatim in behaviour from model_b_v3.py
    t_open = df["Open Time"].values.astype("datetime64[ns]").astype(np.int64)
    t_res = df["Resolved Time"].values.astype("datetime64[ns]").astype(np.int64)
    t_res_filled = np.where(pd.isna(df["Resolved Time"]).values, np.iinfo(np.int64).max, t_res)
    brch = df["SLA_Breached"].values.astype(float)
    prior = float(brch.mean())

    order = np.argsort(t_res_filled)
    res_times_sorted = t_res_filled[order]
    cum_b = np.concatenate([[0.0], np.cumsum(brch[order])])
    k_all = np.searchsorted(res_times_sorted, t_open, side="left")
    lo = np.maximum(k_all - W, 0)
    cnt = k_all - lo
    with np.errstate(invalid="ignore", divide="ignore"):
        trail = (cum_b[k_all] - cum_b[lo]) / np.maximum(cnt, 1)
    df["Trailing_Breach_Rate_500"] = np.where(cnt > 0, trail, prior)
    df["Trailing_Window_N"] = cnt

    def causal_target_encode(key_series, name):
        keys = key_series.fillna("unknown").astype(str).values
        enc = np.full(len(df), prior, dtype=float)
        cntv = np.zeros(len(df), dtype=float)
        for k in pd.unique(keys):
            m = np.where(keys == k)[0]
            o = np.argsort(t_res_filled[m])
            rt_s = t_res_filled[m][o]
            cb = np.concatenate([[0.0], np.cumsum(brch[m][o])])
            pos = np.searchsorted(rt_s, t_open[m], side="left")
            enc[m] = (cb[pos] + prior * SMOOTH) / (pos + SMOOTH)
            cntv[m] = pos
        df[f"{name}_BreachRate_Causal"] = enc
        df[f"{name}_PriorCount"] = cntv

    for col, nm in [("First_Assignment_Group", "Group"), ("CI Name (aff)", "CIName"),
                    ("CI Type (aff)", "CIType"), ("Category", "Category"),
                    ("Service Component WBS (aff)", "WBS")]:
        causal_target_encode(df[col], nm)

    grp = df["First_Assignment_Group"].values
    load = np.zeros(len(df))
    for k in pd.unique(grp):
        m = np.where(grp == k)[0]
        ot_s = np.sort(t_open[m])
        rt_s = np.sort(t_res_filled[m])
        load[m] = (np.searchsorted(ot_s, t_open[m], side="left")
                   - np.searchsorted(rt_s, t_open[m], side="left"))
    df["Group_Open_Load"] = load

    ci = df["CI Name (aff)"].fillna("unknown").astype(str).values
    days_since = np.full(len(df), -1.0)
    ci_30d = np.zeros(len(df))
    DAY = 86_400_000_000_000
    for k in pd.unique(ci):
        m = np.where(ci == k)[0]
        mi = m[np.argsort(t_open[m])]
        ts = t_open[mi]
        prev = np.concatenate([[np.nan], ts[:-1].astype(float)])
        days_since[mi] = (ts - prev) / DAY
        ci_30d[mi] = np.searchsorted(ts, ts, side="left") - np.searchsorted(ts, ts - 30 * DAY, side="left")
    df["CI_Days_Since_Prev"] = np.nan_to_num(days_since, nan=-1.0)
    df["CI_Incidents_30d"] = ci_30d

    return df, encoders, prior


def main():
    print("=" * 78)
    print("  MODEL B v3, SLA breach with causal entity encodings")
    print("=" * 78)
    t0 = time.time()
    df, encoders, prior = build_v3_frame()
    d = df[FEATS + ["SLA_Breached"]].replace([np.inf, -np.inf], np.nan).fillna(-1)
    X, y = d[FEATS], d["SLA_Breached"]
    print(f"  feature matrix {X.shape}, built in {time.time()-t0:.0f}s")

    tr, te = train_test_split(X.index, test_size=0.2, stratify=y, random_state=SEED)
    spw = (y.loc[tr] == 0).sum() / max((y.loc[tr] == 1).sum(), 1)
    m = XGBClassifier(**B3_PARAMS, scale_pos_weight=spw, eval_metric="logloss",
                      random_state=SEED, verbosity=0, tree_method="hist", n_jobs=-1)
    m.fit(X.loc[tr], y.loc[tr])
    holdout = roc_auc_score(y.loc[te], m.predict_proba(X.loc[te])[:, 1])
    print(f"  held-out AUC {holdout:.4f}  (v3 run reported XGBoost_tuned 0.8060, "
          f"best ensemble 0.8071)")

    medians = X.loc[tr].median()
    adapter = clamped_auc(m, X.loc[te], y.loc[te], B3_DRIVEN, medians)
    print(f"  adapter AUC  {adapter:.4f}  ({len(B3_DRIVEN)} of {len(FEATS)} features live)")

    # The CRM analogue of Group_BreachRate_Causal is the rep's own causal breach rate,
    # so the serving path needs the same prior and smoothing constant to rebuild it.
    joblib.dump(
        {
            "model": m,
            "features": FEATS,
            "medians": medians.to_dict(),
            "group_prior": prior,
            "smooth": SMOOTH,
            "base_rate": float(y.mean()),
            "driven": B3_DRIVEN,
        },
        ART / "model_b_v3.joblib",
    )

    meta = {
        "name": "model_b_sla_v3_comparison",
        "served": False,
        "version": "xgb-tuned-v3-causal",
        "dataset": "BPI Challenge 2014, Rabobank ITSM incidents",
        "config": "v3 causal entity encodings, 28 features, tuned XGBoost",
        "holdout_auc": round(float(holdout), 4),
        "adapter_auc": round(float(adapter), 4),
        "live_features": len(B3_DRIVEN),
        "n_features": len(FEATS),
        "best_ensemble_auc": 0.8071,
        "chronological_auc": 0.7544,
        "published_test_auc": 0.8060,
        "base_rate": round(float(y.mean()), 4),
    }

    reg = json.loads((ART / "registry.json").read_text(encoding="utf-8"))
    reg["models"] = [mm for mm in reg["models"]
                     if mm["name"] != "model_b_sla_v3_comparison"] + [meta]
    reg["exported_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    (ART / "registry.json").write_text(json.dumps(reg, indent=2), encoding="utf-8")

    print(f"\n  full-feature {meta['holdout_auc']}  ->  adapter {meta['adapter_auc']}")
    print(f"  registry updated, version {meta['version']} (comparison only, not served)")


if __name__ == "__main__":
    main()
