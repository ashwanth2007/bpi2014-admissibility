"""
PAA-NSGA-II - Phase 3: SLA Breach Prediction on BPI Challenge 2014
LEAK-FREE PIPELINE (v2)
===================================================================
Dataset: Rabobank Nederland Group ICT, real incident management data.
Target:  SLA Breach (binary), Handle_Time > 2x the priority-specific median.

WHY THIS FILE EXISTS
--------------------
v1 (bpi2014_ml_pipeline.py) reported 0.8958 test AUC. That number is not
usable. Two defects:

  1. POST-HOC FEATURES. The label is a function of total case handle time.
     Features counted across the whole closed case (Total_Activity_Events,
     # Reassignments, Num_Groups_Touched, Has_Reopen, Interaction_Count,
     FCR_Rate, and the final # Related_* tallies) grow with case duration,
     so they are proxies for the label. None of them are knowable at the
     moment the incident is assigned, which is the decision point the
     framework optimises.

  2. TARGET ENCODING BEFORE THE SPLIT. v1 built group_stats
     (Group_Breach_Rate = mean of the target, Group_Avg_Handle_Time = mean
     of the quantity the target thresholds) over the ENTIRE dataset and
     merged it in before train_test_split. That contaminates the test set
     and includes each row's own label.

This file predicts using ONLY what is known at first assignment, and fits
every group statistic inside the training fold with smoothing. It also
re-runs the contaminated configuration on purpose, so the size of the
correction is measured and reportable rather than asserted.

Validation battery: 5-fold stratified cross validation, precision, recall,
F1, confusion matrix, ROC-AUC, calibration + Brier score, SHAP, plus a
chronological holdout because event-log data is temporal and a random split
flatters it.

Run:  python bpi2014_pipeline_leakfree.py
"""

import os
import warnings
warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=FutureWarning)

import pandas as pd
import numpy as np
from pathlib import Path

from sklearn.model_selection import StratifiedKFold, train_test_split
from sklearn.preprocessing import LabelEncoder
from sklearn.metrics import (
    accuracy_score, precision_score, recall_score, f1_score,
    roc_auc_score, brier_score_loss, classification_report,
    roc_curve, confusion_matrix,
)
from sklearn.calibration import calibration_curve
from sklearn.ensemble import RandomForestClassifier

from xgboost import XGBClassifier
from lightgbm import LGBMClassifier
from catboost import CatBoostClassifier

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import shap

BASE_DIR = Path(__file__).parent
# Split fraction and run tag are configurable so the published 80/20 numbers stay
# reproducible while Ragunandhan's 70/30 protocol runs as a separate, comparable arm.
TEST_SIZE = float(os.environ.get("BPI_TEST_SIZE", "0.2"))
RUN_TAG = os.environ.get("BPI_RUN_TAG", "")
DATA_DIR = BASE_DIR / "bpi2014"
RESULTS_DIR = BASE_DIR / ("results_bpi_leakfree" + (("_" + RUN_TAG) if RUN_TAG else ""))
RESULTS_DIR.mkdir(exist_ok=True)

SEED = 42

SMOOTHING = 20          # prior weight for group target encoding
np.random.seed(SEED)

# =====================================================================
# 1.  LOAD
# =====================================================================
print("Loading BPI Challenge 2014 data...")
inc = pd.read_csv(DATA_DIR / "Detail_Incident.csv", sep=";", encoding="latin1")
act = pd.read_csv(DATA_DIR / "Detail_Incident_Activity.csv", sep=";", encoding="latin1")

print(f"  Incidents:  {len(inc):,} rows")
print(f"  Activities: {len(act):,} rows")

inc["Handle_Time_Hours"] = pd.to_numeric(
    inc["Handle Time (Hours)"].astype(str).str.replace(",", "."), errors="coerce")
for col in ["Open Time", "Resolved Time", "Close Time"]:
    inc[col] = pd.to_datetime(inc[col], format="mixed", dayfirst=False, errors="coerce")
act["DateStamp"] = pd.to_datetime(act["DateStamp"], format="mixed", dayfirst=True, errors="coerce")

# =====================================================================
# 2.  TARGET  (unchanged from v1, so results stay comparable)
# =====================================================================
priority_medians = inc.groupby("Priority")["Handle_Time_Hours"].median()
print("\nMedian handle time by priority:")
for p, m in priority_medians.items():
    print(f"  Priority {p}: {m:.2f} hours")

sla_thresholds = {p: m * 2.0 for p, m in priority_medians.items()}
inc["SLA_Threshold_Hours"] = inc["Priority"].map(sla_thresholds)
inc["SLA_Breached"] = (inc["Handle_Time_Hours"] > inc["SLA_Threshold_Hours"]).astype(int)

df = inc[inc["Handle_Time_Hours"].notna() & inc["Priority"].notna()].copy()
print(f"\nAfter cleaning: {len(df):,} incidents")
print(f"SLA breach rate: {df['SLA_Breached'].mean():.1%}")

# =====================================================================
# 3.  FEATURES KNOWN AT FIRST ASSIGNMENT
# =====================================================================
print("\nEngineering leak-free features...")

assign_events = act[act["IncidentActivity_Type"].isin(["Assignment", "Reassignment"])]

# Which group the case is FIRST routed to. Known at the decision point:
# it IS the decision. Its identity is legitimate; its historical stats are
# only legitimate if fitted on past/training cases (handled in section 5).
first_assign = (assign_events.sort_values("DateStamp")
                .groupby("Incident ID")["Assignment Group"].first()
                .reset_index().rename(columns={"Assignment Group": "First_Assignment_Group"}))
df = df.merge(first_assign, on="Incident ID", how="left")

# Time from open to first assignment. Elapsed and observable at the moment
# of assignment, so it is admissible. It is NOT total handle time.
first_assign_time = (assign_events.sort_values("DateStamp")
                     .groupby("Incident ID")["DateStamp"].first()
                     .reset_index().rename(columns={"DateStamp": "First_Assignment_Time"}))
df = df.merge(first_assign_time, on="Incident ID", how="left")
df["Assignment_Delay_Hours"] = (
    (df["First_Assignment_Time"] - df["Open Time"]).dt.total_seconds() / 3600
).clip(lower=0).fillna(0)

# Temporal context of arrival
df["Open_Hour"] = df["Open Time"].dt.hour
df["Open_DayOfWeek"] = df["Open Time"].dt.dayofweek
df["Is_Weekend"] = (df["Open_DayOfWeek"] >= 5).astype(int)
df["Is_Business_Hours"] = ((df["Open_Hour"] >= 8) & (df["Open_Hour"] <= 18)).astype(int)
df["Open_Month"] = df["Open Time"].dt.month

# System load at intake: how many cases were already open. Uses other cases'
# timestamps only, and only those already open, so no future information
# about THIS case enters.
ds = df.sort_values("Open Time")
queue_lengths, open_set = [], []
for o, r in zip(ds["Open Time"].values, ds["Resolved Time"].values):
    open_set = [x for x in open_set if x > o or pd.isna(x)]
    queue_lengths.append(len(open_set))
    if pd.notna(r):
        open_set.append(r)
ds = ds.assign(Queue_Length_At_Open=queue_lengths)
df = df.merge(ds[["Incident ID", "Queue_Length_At_Open"]], on="Incident ID", how="left")

# Static configuration-item and category attributes, all present on the record at open
for src, dst in [("CI Type (aff)", "CI_Type_Encoded"),
                 ("CI Subtype (aff)", "CI_Subtype_Encoded"),
                 ("Service Component WBS (aff)", "WBS_Encoded"),
                 ("Category", "Category_Encoded"),
                 ("Alert Status", "Alert_Status_Encoded")]:
    df[dst] = LabelEncoder().fit_transform(df[src].fillna("unknown").astype(str))

# --- post-hoc columns, rebuilt ONLY to quantify the contamination ---
groups_touched = (assign_events.groupby("Incident ID")["Assignment Group"].nunique()
                  .reset_index().rename(columns={"Assignment Group": "Num_Groups_Touched"}))
df = df.merge(groups_touched, on="Incident ID", how="left")
df["Num_Groups_Touched"] = df["Num_Groups_Touched"].fillna(0).astype(int)
event_counts = act.groupby("Incident ID").size().reset_index(name="Total_Activity_Events")
df = df.merge(event_counts, on="Incident ID", how="left")
df["Total_Activity_Events"] = df["Total_Activity_Events"].fillna(0).astype(int)
df["Num_Related_Incidents"] = pd.to_numeric(df["# Related Incidents"], errors="coerce").fillna(0)
df["Has_Reopen"] = df["Reopen Time"].notna().astype(int)

# =====================================================================
# 4.  FEATURE SETS
# =====================================================================
BASE_CLEAN = [
    "Priority",                 # business impact x urgency, on the record at open
    "Impact",                   # business impact level, on the record at open
    "Urgency",                  # urgency level, on the record at open
    "Open_Hour",                # shift and staffing pattern at arrival
    "Open_DayOfWeek",           # weekday load pattern
    "Is_Weekend",               # reduced-staffing flag
    "Is_Business_Hours",        # in-hours vs out-of-hours handling
    "Open_Month",               # seasonal load
    "Queue_Length_At_Open",     # system congestion at intake
    "Assignment_Delay_Hours",   # elapsed wait before routing, observable at assignment
    "CI_Type_Encoded",          # configuration item type
    "CI_Subtype_Encoded",       # configuration item subtype
    "WBS_Encoded",              # service component
    "Category_Encoded",         # incident category
    "Alert_Status_Encoded",     # alert state at open
]
GROUP_TE = ["Group_Breach_Rate_TE", "Group_Volume_TE"]   # fitted inside the fold

# Honest ladder: what each tier of admissible information is worth
LADDER = [
    ("3_static_only",       ["Priority", "Impact", "Urgency"]),
    ("8_plus_temporal",     ["Priority", "Impact", "Urgency", "Open_Hour", "Open_DayOfWeek",
                             "Is_Weekend", "Is_Business_Hours", "Open_Month"]),
    ("10_plus_operational", ["Priority", "Impact", "Urgency", "Open_Hour", "Open_DayOfWeek",
                             "Is_Weekend", "Is_Business_Hours", "Open_Month",
                             "Queue_Length_At_Open", "Assignment_Delay_Hours"]),
    ("15_plus_config",      BASE_CLEAN),
    ("17_plus_group_TE",    BASE_CLEAN + GROUP_TE),
]

# The v1 configuration, reproduced so the correction is measured not asserted
CONTAMINATED = BASE_CLEAN + ["# Reassignments", "Num_Groups_Touched",
                             "Total_Activity_Events", "Has_Reopen",
                             "Num_Related_Incidents"]

# =====================================================================
# 5.  FOLD-SAFE GROUP TARGET ENCODING
# =====================================================================
def fit_group_encoding(groups_tr, y_tr):
    """Smoothed target encoding fitted on TRAINING ROWS ONLY."""
    stats = pd.DataFrame({"g": groups_tr.values, "y": y_tr.values}).groupby("g")["y"].agg(["mean", "count"])
    prior = float(y_tr.mean())
    breach = (stats["mean"] * stats["count"] + prior * SMOOTHING) / (stats["count"] + SMOOTHING)
    return breach, stats["count"], prior


def apply_group_encoding(X, groups, enc):
    breach, volume, prior = enc
    X = X.copy()
    X["Group_Breach_Rate_TE"] = groups.map(breach).fillna(prior).values
    X["Group_Volume_TE"] = groups.map(volume).fillna(0).values
    return X


def get_models(y_tr):
    spw = (y_tr == 0).sum() / max((y_tr == 1).sum(), 1)
    return {
        "XGBoost": XGBClassifier(n_estimators=300, max_depth=6, learning_rate=0.05,
                                 subsample=0.8, colsample_bytree=0.8, scale_pos_weight=spw,
                                 eval_metric="logloss", random_state=SEED, verbosity=0),
        "LightGBM": LGBMClassifier(n_estimators=300, max_depth=6, learning_rate=0.05,
                                   subsample=0.8, colsample_bytree=0.8, is_unbalance=True,
                                   random_state=SEED, verbose=-1),
        "CatBoost": CatBoostClassifier(iterations=300, depth=6, learning_rate=0.05,
                                       auto_class_weights="Balanced", random_seed=SEED, verbose=0),
        "RandomForest": RandomForestClassifier(n_estimators=300, max_depth=12,
                                               class_weight="balanced", random_state=SEED, n_jobs=-1),
    }


# =====================================================================
# 6.  DATA MATRIX + SPLITS
# =====================================================================
needed = sorted(set(BASE_CLEAN + CONTAMINATED))
d = df[needed + ["SLA_Breached", "First_Assignment_Group", "Open Time", "Incident ID"]].dropna(
    subset=needed + ["SLA_Breached"]).copy()
d["First_Assignment_Group"] = d["First_Assignment_Group"].fillna("unknown").astype(str)

y_all = d["SLA_Breached"]
g_all = d["First_Assignment_Group"]

idx_tr, idx_te = train_test_split(d.index, test_size=TEST_SIZE, stratify=y_all, random_state=SEED)

# Chronological holdout: train on the earliest 80 per cent of arrivals, test on the latest 20
d_sorted = d.sort_values("Open Time")
cut = int(len(d_sorted) * 0.8)
idx_tr_time, idx_te_time = d_sorted.index[:cut], d_sorted.index[cut:]

print(f"\nModelling rows: {len(d):,}   breach rate: {y_all.mean():.1%}")
print(f"Random split   -> train {len(idx_tr):,} | test {len(idx_te):,}")
print(f"Temporal split -> train {len(idx_tr_time):,} | test {len(idx_te_time):,}"
      f"  (test breach rate {y_all.loc[idx_te_time].mean():.1%})")


def evaluate(feats, tr, te, label, use_group_te=False, models=None, collect=None):
    """Train + evaluate, fitting any group encoding inside the training rows only."""
    base = [f for f in feats if f not in GROUP_TE]
    Xtr_raw, Xte_raw = d.loc[tr, base], d.loc[te, base]
    ytr, yte = y_all.loc[tr], y_all.loc[te]
    gtr, gte = g_all.loc[tr], g_all.loc[te]

    if use_group_te:
        enc = fit_group_encoding(gtr, ytr)
        Xtr, Xte = apply_group_encoding(Xtr_raw, gtr, enc), apply_group_encoding(Xte_raw, gte, enc)
    else:
        Xtr, Xte = Xtr_raw, Xte_raw

    rows, fitted = [], {}
    for name, model in (models or get_models(ytr)).items():
        # 5-fold stratified CV, re-fitting the group encoding inside every fold
        skf = StratifiedKFold(5, shuffle=True, random_state=SEED)
        fold_auc = []
        for f_tr, f_va in skf.split(Xtr_raw, ytr):
            i_tr, i_va = Xtr_raw.index[f_tr], Xtr_raw.index[f_va]
            if use_group_te:
                e = fit_group_encoding(g_all.loc[i_tr], y_all.loc[i_tr])
                A = apply_group_encoding(d.loc[i_tr, base], g_all.loc[i_tr], e)
                B = apply_group_encoding(d.loc[i_va, base], g_all.loc[i_va], e)
            else:
                A, B = d.loc[i_tr, base], d.loc[i_va, base]
            m = get_models(y_all.loc[i_tr])[name]
            m.fit(A, y_all.loc[i_tr])
            fold_auc.append(roc_auc_score(y_all.loc[i_va], m.predict_proba(B)[:, 1]))

        model.fit(Xtr, ytr)
        proba, pred = model.predict_proba(Xte)[:, 1], model.predict(Xte)
        rows.append({
            "Config": label, "Model": name, "Num_Features": Xtr.shape[1],
            "CV_AUC_mean": round(float(np.mean(fold_auc)), 4),
            "CV_AUC_std": round(float(np.std(fold_auc)), 4),
            "Test_Accuracy": round(accuracy_score(yte, pred), 4),
            "Test_Precision": round(precision_score(yte, pred, zero_division=0), 4),
            "Test_Recall": round(recall_score(yte, pred, zero_division=0), 4),
            "Test_F1": round(f1_score(yte, pred, zero_division=0), 4),
            "Test_AUC": round(roc_auc_score(yte, proba), 4),
            "Test_Brier": round(brier_score_loss(yte, proba), 4),
        })
        fitted[name] = (model, Xte, yte, proba, pred)
        print(f"  {label:22s} {name:13s} n={Xtr.shape[1]:2d}  CV-AUC={np.mean(fold_auc):.4f}"
              f"  Test-AUC={rows[-1]['Test_AUC']:.4f}  F1={rows[-1]['Test_F1']:.4f}"
              f"  P={rows[-1]['Test_Precision']:.4f}  R={rows[-1]['Test_Recall']:.4f}")
    if collect is not None:
        collect.extend(rows)
    return rows, fitted


def evaluate_temporal(feats, tr, te):
    """Chronological holdout done causally.

    Two things in the random-split setup are not available to a system running
    forward in time, and both are corrected here:

      1. THE LABEL. The SLA threshold is 2x the priority median handle time. In
         section 2 that median is computed over the whole 2012-2014 span. A model
         deployed in Jan 2014 can only know the 2012-2013 medians, so the label
         is recomputed from training-period cases only.
      2. THE OPERATING THRESHOLD. Predicting at p>0.5 assumes the future base
         rate matches the past. It does not: the Rabobank process got faster
         through 2014. The cut point is therefore tuned for F1 on a validation
         slice held out from the END of the training period, never on test.

    AUC is threshold-free and so measures pure ranking degradation under drift.
    """
    base = [f for f in feats if f not in GROUP_TE]

    # 1. Re-derive the label from training-period information only
    tr_med = d.loc[tr].join(inc.set_index("Incident ID")["Handle_Time_Hours"],
                            on="Incident ID").groupby("Priority")["Handle_Time_Hours"].median()
    ht = inc.set_index("Incident ID")["Handle_Time_Hours"]
    prio = d["Priority"]
    thr_causal = prio.map({p: m * 2.0 for p, m in tr_med.items()})
    y_causal = (d["Incident ID"].map(ht).values > thr_causal.values).astype(int)
    y_causal = pd.Series(y_causal, index=d.index)

    ytr, yte = y_causal.loc[tr], y_causal.loc[te]
    gtr, gte = g_all.loc[tr], g_all.loc[te]
    print(f"  Train-period medians: {dict(tr_med.round(2))}")
    print(f"  Breach rate  train {ytr.mean():.1%}  test {yte.mean():.1%}")

    # 2. Validation slice from the END of train, for threshold tuning only
    n_val = int(len(tr) * 0.2)
    tr_fit, tr_val = tr[:-n_val], tr[-n_val:]

    enc_fit = fit_group_encoding(g_all.loc[tr_fit], y_causal.loc[tr_fit])
    enc_full = fit_group_encoding(gtr, ytr)

    rows = []
    for name in ["XGBoost", "LightGBM", "CatBoost", "RandomForest"]:
        # tune the cut point on the validation slice
        m_fit = get_models(y_causal.loc[tr_fit])[name]
        m_fit.fit(apply_group_encoding(d.loc[tr_fit, base], g_all.loc[tr_fit], enc_fit),
                  y_causal.loc[tr_fit])
        p_val = m_fit.predict_proba(
            apply_group_encoding(d.loc[tr_val, base], g_all.loc[tr_val], enc_fit))[:, 1]
        grid = np.linspace(0.05, 0.95, 91)
        cut_pt = float(grid[np.argmax([f1_score(y_causal.loc[tr_val], (p_val >= t).astype(int),
                                                zero_division=0) for t in grid])])

        # refit on the full training period, evaluate on the future
        m = get_models(ytr)[name]
        m.fit(apply_group_encoding(d.loc[tr, base], gtr, enc_full), ytr)
        proba = m.predict_proba(apply_group_encoding(d.loc[te, base], gte, enc_full))[:, 1]
        pred = (proba >= cut_pt).astype(int)
        rows.append({
            "Config": "leakfree_temporal", "Model": name, "Num_Features": len(base) + 2,
            "CV_AUC_mean": np.nan, "CV_AUC_std": np.nan,
            "Test_Accuracy": round(accuracy_score(yte, pred), 4),
            "Test_Precision": round(precision_score(yte, pred, zero_division=0), 4),
            "Test_Recall": round(recall_score(yte, pred, zero_division=0), 4),
            "Test_F1": round(f1_score(yte, pred, zero_division=0), 4),
            "Test_AUC": round(roc_auc_score(yte, proba), 4),
            "Test_Brier": round(brier_score_loss(yte, proba), 4),
        })
        print(f"  {'leakfree_temporal':22s} {name:13s} cut={cut_pt:.2f}"
              f"  Test-AUC={rows[-1]['Test_AUC']:.4f}  F1={rows[-1]['Test_F1']:.4f}"
              f"  P={rows[-1]['Test_Precision']:.4f}  R={rows[-1]['Test_Recall']:.4f}")
    return rows


# =====================================================================
# 7.  MAIN COMPARISON
# =====================================================================
all_rows = []
print("\n" + "=" * 118)
print("  CONTAMINATED CONFIGURATION (v1 behaviour, reported only to size the correction)")
print("=" * 118)
evaluate(CONTAMINATED, idx_tr, idx_te, "contaminated_v1", use_group_te=False, collect=all_rows)

print("\n" + "=" * 118)
print("  LEAK-FREE CONFIGURATION (all four models, random split)")
print("=" * 118)
_, fitted_clean = evaluate(BASE_CLEAN + GROUP_TE, idx_tr, idx_te, "leakfree",
                           use_group_te=True, collect=all_rows)

print("\n" + "=" * 118)
print("  LEAK-FREE, CHRONOLOGICAL HOLDOUT (train on earliest 80%, test on latest 20%)")
print("  Label uses TRAIN-PERIOD medians only, and the operating threshold is tuned on")
print("  a validation slice of train, because neither is knowable from the future.")
print("=" * 118)
temporal_rows = evaluate_temporal(BASE_CLEAN + GROUP_TE, idx_tr_time, idx_te_time)
all_rows.extend(temporal_rows)

res = pd.DataFrame(all_rows)
res.to_csv(RESULTS_DIR / "model_comparison_leakfree.csv", index=False)

# =====================================================================
# 8.  HONEST FEATURE LADDER  (XGBoost)
# =====================================================================
print("\n" + "=" * 118)
print("  FEATURE LADDER, admissible information only (XGBoost)")
print("=" * 118)
ladder_rows = []
for name, feats in LADDER:
    use_te = "Group_Breach_Rate_TE" in feats
    r, _ = evaluate(feats, idx_tr, idx_te, name, use_group_te=use_te,
                    models={"XGBoost": get_models(y_all.loc[idx_tr])["XGBoost"]})
    ladder_rows.append({"Feature_Set": name, "Num_Features": r[0]["Num_Features"],
                        "CV_AUC": r[0]["CV_AUC_mean"], "Test_AUC": r[0]["Test_AUC"],
                        "Test_F1": r[0]["Test_F1"], "Test_Accuracy": r[0]["Test_Accuracy"],
                        "Test_Precision": r[0]["Test_Precision"], "Test_Recall": r[0]["Test_Recall"]})
ladder = pd.DataFrame(ladder_rows)
ladder.to_csv(RESULTS_DIR / "feature_ladder_leakfree.csv", index=False)

# =====================================================================
# 9.  BEST LEAK-FREE MODEL: report, confusion matrix, calibration, SHAP
# =====================================================================
clean = res[res["Config"] == "leakfree"]
best_name = clean.loc[clean["Test_AUC"].idxmax(), "Model"]
best_model, Xte_b, yte_b, proba_b, pred_b = fitted_clean[best_name]
print(f"\nBest leak-free model: {best_name}\n")
print(classification_report(yte_b, pred_b, target_names=["No Breach", "SLA Breach"]))

cm = confusion_matrix(yte_b, pred_b)
fig, ax = plt.subplots(figsize=(5.5, 4.6))
ax.imshow(cm, cmap="Blues")
for i in range(2):
    for j in range(2):
        ax.text(j, i, f"{cm[i, j]:,}", ha="center", va="center",
                color="white" if cm[i, j] > cm.max() / 2 else "black", fontsize=13)
ax.set_xticks([0, 1]); ax.set_yticks([0, 1])
ax.set_xticklabels(["No Breach", "SLA Breach"]); ax.set_yticklabels(["No Breach", "SLA Breach"])
ax.set_xlabel("Predicted"); ax.set_ylabel("Actual")
ax.set_title(f"SLA Breach, leak-free ({best_name})")
plt.tight_layout(); plt.savefig(RESULTS_DIR / "confusion_matrix_leakfree.png", dpi=150); plt.close()

fig, ax = plt.subplots(figsize=(5.5, 4.6))
pt, pp = calibration_curve(yte_b, proba_b, n_bins=10)
ax.plot(pp, pt, "o-", label=best_name)
ax.plot([0, 1], [0, 1], "k--", alpha=0.5, label="Perfect")
ax.set_xlabel("Predicted probability"); ax.set_ylabel("Observed frequency")
ax.set_title(f"Calibration, leak-free (Brier={brier_score_loss(yte_b, proba_b):.4f})")
ax.legend(); plt.tight_layout()
plt.savefig(RESULTS_DIR / "calibration_leakfree.png", dpi=150); plt.close()

fig, ax = plt.subplots(figsize=(6.4, 5.2))
for _, r in clean.iterrows():
    m, Xt, yt, pr, _p = fitted_clean[r["Model"]]
    fpr, tpr, _ = roc_curve(yt, pr)
    ax.plot(fpr, tpr, label=f"{r['Model']} (AUC={r['Test_AUC']:.3f})")
ax.plot([0, 1], [0, 1], "k--", alpha=0.5)
ax.set_xlabel("False Positive Rate"); ax.set_ylabel("True Positive Rate")
ax.set_title("SLA Breach, leak-free, ROC curves"); ax.legend()
plt.tight_layout(); plt.savefig(RESULTS_DIR / "roc_leakfree.png", dpi=150); plt.close()

fig, ax = plt.subplots(figsize=(7.2, 4.4))
ax.bar(ladder["Feature_Set"], ladder["Test_AUC"], color="#2196F3")
for i, v in enumerate(ladder["Test_AUC"]):
    ax.text(i, v + 0.004, f"{v:.3f}", ha="center", fontsize=9)
ax.set_ylim(0.5, 0.85); ax.set_ylabel("Test AUC")
ax.set_title("Admissible-information ladder (XGBoost)")
plt.xticks(rotation=20, ha="right"); plt.tight_layout()
plt.savefig(RESULTS_DIR / "feature_ladder_leakfree.png", dpi=150); plt.close()

# NOTE: shap 0.45.1 cannot parse XGBoost 3.2.0's model JSON (it reads base_score
# as the string "[5E-1]" and fails on float conversion). This is a library version
# incompatibility, not a data problem. shap >= 0.46 fixes it but requires numpy 2,
# which breaks the rest of this Anaconda environment. LightGBM is used for the
# explainability layer instead: it is the same gradient-boosted tree family and
# scores within 0.006 AUC of XGBoost here, so the attributions are representative.
shap_name = "LightGBM"
shap_model, Xte_s, yte_s, _pr, _pd = fitted_clean[shap_name]
try:
    sample = Xte_s.sample(min(2000, len(Xte_s)), random_state=SEED)
    sv = shap.TreeExplainer(shap_model).shap_values(sample)
    if isinstance(sv, list):
        sv = sv[1]
    sv = np.asarray(sv)
    if sv.ndim == 3:
        sv = sv[:, :, 1]
    plt.figure()
    shap.summary_plot(sv, sample, show=False, max_display=15)
    plt.title(f"SHAP, leak-free ({shap_name})")
    plt.tight_layout(); plt.savefig(RESULTS_DIR / "shap_beeswarm_leakfree.png", dpi=150); plt.close()

    imp = pd.DataFrame({"Feature": sample.columns,
                        "Mean_Abs_SHAP": np.abs(sv).mean(axis=0)}).sort_values(
                            "Mean_Abs_SHAP", ascending=False)
    imp.to_csv(RESULTS_DIR / "shap_importance_leakfree.csv", index=False)
    print(f"\nTop 10 features by mean |SHAP| ({shap_name}):")
    print(imp.head(10).to_string(index=False))
except Exception as exc:
    print(f"SHAP step failed: {type(exc).__name__}: {exc}")

# =====================================================================
# 10. FEATURE JUSTIFICATIONS  (ma'am requires one per attribute)
# =====================================================================
JUST = {
    "Priority": ("Impact x urgency as recorded on the incident at open. The SLA clock is set per priority band.", "yes"),
    "Impact": ("Business impact level recorded at open. Higher impact cases receive different handling paths.", "yes"),
    "Urgency": ("Urgency level recorded at open. Independent of impact and jointly determines priority.", "yes"),
    "Open_Hour": ("Hour of arrival. Proxies shift staffing, which governs how fast a case is picked up.", "yes"),
    "Open_DayOfWeek": ("Weekday of arrival. Captures the weekly demand cycle in the service desk.", "yes"),
    "Is_Weekend": ("Weekend flag. Reduced staffing materially changes achievable response time.", "yes"),
    "Is_Business_Hours": ("In-hours vs out-of-hours arrival. Out-of-hours cases queue until staffed.", "yes"),
    "Open_Month": ("Month of arrival. Captures seasonal load and release-cycle effects.", "yes"),
    "Queue_Length_At_Open": ("Count of incidents already open at arrival. Direct measure of congestion, computed from other cases only.", "yes"),
    "Assignment_Delay_Hours": ("Elapsed hours from open to first assignment. Observable at the assignment decision; distinct from total handle time.", "yes"),
    "CI_Type_Encoded": ("Configuration item type of the affected asset. Different asset classes have different repair profiles.", "yes"),
    "CI_Subtype_Encoded": ("Configuration item subtype. Finer-grained asset class than CI type.", "yes"),
    "WBS_Encoded": ("Service component work breakdown code of the affected service.", "yes"),
    "Category_Encoded": ("Incident category assigned at intake.", "yes"),
    "Alert_Status_Encoded": ("Alert state on the record at open.", "yes"),
    "Group_Breach_Rate_TE": ("Smoothed historical breach rate of the first assignment group, fitted on training rows only with a prior weight of 20. This is the Group B representative-performance signal in the specification.", "yes, fitted on past/training cases only"),
    "Group_Volume_TE": ("Case volume handled by the first assignment group, fitted on training rows only. Proxies group capacity and experience.", "yes, fitted on past/training cases only"),
    "# Reassignments": ("EXCLUDED. Total reassignments over the whole closed case. Not knowable at assignment and rises with duration, which defines the label.", "no"),
    "Num_Groups_Touched": ("EXCLUDED. Distinct groups over the whole closed case. Post-hoc.", "no"),
    "Total_Activity_Events": ("EXCLUDED. Count of every activity event across the closed case. Strongest duration proxy in the feature set.", "no"),
    "Has_Reopen": ("EXCLUDED. Reopens occur late in a case. Post-hoc.", "no"),
    "Num_Related_Incidents": ("EXCLUDED. Final tally on the closed record, not the value visible at open.", "no"),
    "Interaction_Count": ("EXCLUDED. Aggregated across the full case from the interaction log. Post-hoc.", "no"),
    "FCR_Rate": ("EXCLUDED. First-call-resolution outcome is known only after handling. Post-hoc.", "no"),
    "Group_Avg_Handle_Time": ("EXCLUDED as used in v1. Mean of the quantity the label thresholds, computed over the full dataset before the split.", "no"),
    "Group_Breach_Rate (v1 form)": ("EXCLUDED as used in v1. Mean of the target computed over the full dataset including test rows and the row's own label.", "no"),
}
pd.DataFrame([{"Feature": k, "Justification": v[0], "Known_at_assignment_time": v[1]}
              for k, v in JUST.items()]).to_csv(
    RESULTS_DIR / "feature_justifications_leakfree.csv", index=False)

# =====================================================================
# 11. SUMMARY
# =====================================================================
cont_best = res[res["Config"] == "contaminated_v1"]["Test_AUC"].max()
clean_best = res[res["Config"] == "leakfree"]["Test_AUC"].max()
temp_best = res[res["Config"] == "leakfree_temporal"]["Test_AUC"].max()

print("\n" + "=" * 118)
print("  SUMMARY")
print("=" * 118)
print(f"  Contaminated (v1 style):        Test-AUC {cont_best:.4f}")
print(f"  Leak-free, random split:        Test-AUC {clean_best:.4f}")
print(f"  Leak-free, chronological split: Test-AUC {temp_best:.4f}")
print(f"\n  Correction: {cont_best - clean_best:.4f} AUC, "
      f"{(cont_best - clean_best) / (cont_best - 0.5) * 100:.1f}% of all signal above chance "
      f"was leakage.")
print(f"\n  Best leak-free model: {best_name}")
print(f"  Outputs written to: {RESULTS_DIR}")
print("=" * 118)
