"""BPI Challenge 2014: the single definition of the modelling matrix.

Sections 1 to 6 of ``bpi2014_pipeline_leakfree.py`` used to live inside that
script. They now live here, and both the pipeline and the deep-metrics
analysis import them, so there is exactly one definition of the data, the
label, the feature sets and the splits.

The rewiring is verified by re-running the pipeline and diffing its output
CSVs against the ones produced before the refactor. They are identical.

Label: an incident breaches when its handle time exceeds TWICE the median
handle time of its own priority class. On the random split that median is
taken over the whole log; the chronological arm re-derives it from the
training period alone, because a system running forward in time cannot see
the future medians.
"""

import os
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import LabelEncoder
from sklearn.ensemble import RandomForestClassifier
from xgboost import XGBClassifier
from lightgbm import LGBMClassifier
from catboost import CatBoostClassifier

SEED = 42
SMOOTHING = 20          # prior weight for group target encoding

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


class Bundle:
    """Everything downstream needs, built once."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


def build(data_dir=None, test_size=None, verbose=True):
    """Load the log, engineer the admissible features, and split.

    Returns a Bundle carrying: inc, act, df, d, y_all, g_all, idx_tr, idx_te,
    idx_tr_time, idx_te_time, priority_medians, sla_thresholds.
    """
    data_dir = Path(data_dir) if data_dir else Path(__file__).parent / "bpi2014"
    test_size = float(os.environ.get("BPI_TEST_SIZE", "0.2")) if test_size is None else float(test_size)
    _p = print if verbose else (lambda *a, **k: None)
    np.random.seed(SEED)

    _p("Loading BPI Challenge 2014 data...")
    inc = pd.read_csv(data_dir / "Detail_Incident.csv", sep=";", encoding="latin1")
    act = pd.read_csv(data_dir / "Detail_Incident_Activity.csv", sep=";", encoding="latin1")

    _p(f"  Incidents:  {len(inc):,} rows")
    _p(f"  Activities: {len(act):,} rows")

    inc["Handle_Time_Hours"] = pd.to_numeric(
        inc["Handle Time (Hours)"].astype(str).str.replace(",", "."), errors="coerce")
    for col in ["Open Time", "Resolved Time", "Close Time"]:
        inc[col] = pd.to_datetime(inc[col], format="mixed", dayfirst=False, errors="coerce")
    act["DateStamp"] = pd.to_datetime(act["DateStamp"], format="mixed", dayfirst=True, errors="coerce")

    # =====================================================================
    # 2.  TARGET  (unchanged from v1, so results stay comparable)
    # =====================================================================
    priority_medians = inc.groupby("Priority")["Handle_Time_Hours"].median()
    _p("\nMedian handle time by priority:")
    for p, m in priority_medians.items():
        _p(f"  Priority {p}: {m:.2f} hours")

    sla_thresholds = {p: m * 2.0 for p, m in priority_medians.items()}
    inc["SLA_Threshold_Hours"] = inc["Priority"].map(sla_thresholds)
    inc["SLA_Breached"] = (inc["Handle_Time_Hours"] > inc["SLA_Threshold_Hours"]).astype(int)

    df = inc[inc["Handle_Time_Hours"].notna() & inc["Priority"].notna()].copy()
    _p(f"\nAfter cleaning: {len(df):,} incidents")
    _p(f"SLA breach rate: {df['SLA_Breached'].mean():.1%}")

    # =====================================================================
    # 3.  FEATURES KNOWN AT FIRST ASSIGNMENT
    # =====================================================================
    _p("\nEngineering leak-free features...")

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

    needed = sorted(set(BASE_CLEAN + CONTAMINATED))
    d = df[needed + ["SLA_Breached", "First_Assignment_Group", "Open Time", "Incident ID"]].dropna(
        subset=needed + ["SLA_Breached"]).copy()
    d["First_Assignment_Group"] = d["First_Assignment_Group"].fillna("unknown").astype(str)

    y_all = d["SLA_Breached"]
    g_all = d["First_Assignment_Group"]

    idx_tr, idx_te = train_test_split(d.index, test_size=test_size, stratify=y_all, random_state=SEED)

    # Chronological holdout: train on the earliest 80 per cent of arrivals, test on the latest 20
    d_sorted = d.sort_values("Open Time")
    cut = int(len(d_sorted) * 0.8)
    idx_tr_time, idx_te_time = d_sorted.index[:cut], d_sorted.index[cut:]


    return Bundle(inc=inc, act=act, df=df, d=d, y_all=y_all, g_all=g_all,
                  idx_tr=idx_tr, idx_te=idx_te,
                  idx_tr_time=idx_tr_time, idx_te_time=idx_te_time,
                  priority_medians=priority_medians, sla_thresholds=sla_thresholds,
                  test_size=test_size)
