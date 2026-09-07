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

import _openmp_first  # noqa: F401  MUST precede sklearn/xgboost/catboost, see module docstring

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

# ---------------------------------------------------------------------------------------
# Rule 2 mode. See causal_encoding.py for why this exists.
#
#   "exact"     theta_g(i) over P(i,g) = { j : g_j = g, t_res_j < t_open_i }, which is what
#               eq:rule2 in the manuscript actually states. Admissible by construction, so it
#               needs no refitting inside folds: there is no boundary for it to leak across.
#   "trainrows" the pre-2026-09-07 behaviour, grouping over the training rows. Under a random
#               split that encodes a 2012 test case with cases that resolved in 2014. Kept so
#               the paper can report what the approximation was worth: measured at +0.0040 AUC
#               of unearned signal on XGBoost at 80/20.
RULE2_MODE = os.environ.get("BPI_RULE2", "exact")

_CAUSAL_TE = None   # DataFrame indexed like d, columns Group_Breach_Rate_TE, Group_Volume_TE


def set_causal_te(df):
    global _CAUSAL_TE
    _CAUSAL_TE = df


def causal_te_available():
    return _CAUSAL_TE is not None



# How a case with no recorded resolution time is treated when counting the standing queue.
# "open"     : it is still open, because at t_open nothing says otherwise. Admissible.
# "excluded" : it never occupied the queue. Uses future information; kept for sensitivity only.
# 1,780 of 46,606 cases (3.8 per cent) never resolve, spread evenly across years rather than
# clustered at the export date, so this is a data-quality gap and not right-censoring. Under
# "open" the backlog accumulates monotonically and Queue_Length_At_Open correlates 0.93 with
# open time; under "excluded" it correlates -0.44. Both numbers belong in the paper.
QUEUE_UNRESOLVED = os.environ.get("BPI_QUEUE_UNRESOLVED", "open")


# BPI 2014 timestamp formats. BOTH files are day-first; the incident file uses "/" and the
# activity file uses "-". This was previously parsed with format="mixed", dayfirst=False on the
# incident file, which silently swapped day and month on the 41 per cent of rows where both
# fields are <= 12. Verified from the raw bytes on 2026-09-07: across 46,606 non-blank Open Time
# values, 27,994 have a first field > 12 and ZERO have a second field > 12, so month-first is
# arithmetically impossible. Never use format="mixed" on an ambiguous numeric date.
INCIDENT_TS_FORMAT = "%d/%m/%Y %H:%M:%S"
ACTIVITY_TS_FORMAT = "%d-%m-%Y %H:%M:%S"

# ---------------------------------------------------------------------------- diagnostics
# Two switches that deliberately reintroduce a defect, so its effect can be measured rather
# than asserted. The manuscript says the headline moved because the dataset was completed and
# the date parse corrected; that is a claim about attribution and it deserves a measurement,
# not a story. Both default OFF and neither is used by any published run.
#
#   BPI_LEGACY_DATES=1   parse the incident file month-first, as the first version did.
#   BPI_TRUNCATE=<n>     keep only the first n incident rows, emulating the partial copy of
#                        Detail_Incident.csv this work started from (31,238 rows).
LEGACY_DATES = os.environ.get("BPI_LEGACY_DATES", "") not in ("", "0", "false")
TRUNCATE_ROWS = int(os.environ.get("BPI_TRUNCATE", "0") or 0)
if LEGACY_DATES:
    INCIDENT_TS_FORMAT = "%m/%d/%Y %H:%M:%S"


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
    """Attach the two group-level encodings to X.

    In "exact" mode the fitted `enc` is deliberately ignored: the Rule 2 encoding is a
    function of each case's own history, not of whichever rows happen to be in the training
    split, so every call site gets the same admissible values without changing its own code.
    """
    if RULE2_MODE == "exact" and _CAUSAL_TE is not None:
        X = X.copy()
        X["Group_Breach_Rate_TE"] = _CAUSAL_TE.loc[X.index, "Group_Breach_Rate_TE"].values
        X["Group_Volume_TE"] = _CAUSAL_TE.loc[X.index, "Group_Volume_TE"].values
        return X
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

    # The published Detail_Incident.csv ends with 203 completely empty rows: every real column
    # is null, including Incident ID. They survive read_csv as NaN-keyed rows, and any
    # set_index("Incident ID") then carries 203 duplicate NaN keys, which makes .map() raise
    # InvalidIndexError. The truncated working copy used before 2026-09-07 did not include
    # them. Real incidents: 46,606 of 46,809 raw rows, all with unique IDs once these are gone.
    inc = inc[inc["Incident ID"].notna()].copy()
    if TRUNCATE_ROWS:
        # Diagnostic only. The partial copy this work started from was a prefix of the
        # canonical file, so a prefix is what emulates it.
        inc = inc.head(TRUNCATE_ROWS).copy()
        _p("  [diagnostic] BPI_TRUNCATE=%d: incident file cut to %d rows"
           % (TRUNCATE_ROWS, len(inc)))
    if LEGACY_DATES:
        _p("  [diagnostic] BPI_LEGACY_DATES: incident timestamps parsed MONTH-first")
    act = pd.read_csv(data_dir / "Detail_Incident_Activity.csv", sep=";", encoding="latin1")

    _p(f"  Incidents:  {len(inc):,} rows")
    _p(f"  Activities: {len(act):,} rows")


    # Priority, Impact and Urgency are ordinal 1..5 but are NOT clean integers in the full
    # published file: one row carries Urgency = "5 - Very Low" rather than "5", which makes
    # the whole column object dtype and causes XGBoost to reject the matrix outright. The
    # truncated working copy used before 2026-09-07 did not contain that row, so this never
    # surfaced. Take the leading integer and coerce; anything unparseable becomes NaN and is
    # dropped by the existing Priority notna() filter.
    for _c in ["Priority", "Impact", "Urgency"]:
        if _c in inc.columns and inc[_c].dtype == object:
            inc[_c] = pd.to_numeric(
                inc[_c].astype(str).str.extract(r"^\s*(\d+)", expand=False), errors="coerce")
        else:
            inc[_c] = pd.to_numeric(inc[_c], errors="coerce")

    inc["Handle_Time_Hours"] = pd.to_numeric(
        inc["Handle Time (Hours)"].astype(str).str.replace(",", "."), errors="coerce")
    for col in ["Open Time", "Resolved Time", "Close Time"]:
        inc[col] = pd.to_datetime(inc[col], format=INCIDENT_TS_FORMAT, errors="coerce")
    act["DateStamp"] = pd.to_datetime(act["DateStamp"], format=ACTIVITY_TS_FORMAT, errors="coerce")

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

    # System load at intake: how many cases were already open at this case's open time.
    # Uses other cases' timestamps only, and only those already known at that instant.
    #
    # CORRECTED 2026-09-07. The previous O(n*k) loop appended a resolution time to the
    # open set only when one existed, so a case that never resolves in the log was never
    # counted as occupying the queue at all. That is a look-ahead: at the moment case i
    # opens, an earlier case with no resolution yet recorded is, on the information then
    # available, still open. Excluding it uses the fact that it never resolves ANYWHERE in
    # the log, which is future information. Measured gap on the first 6,000 cases: mean 108
    # queue positions, max 198, equal to the running count of never-resolved earlier cases.
    # An unresolved case is now treated as open indefinitely, which is what a decision maker
    # at time t_open would observe. This is also O(n log n) rather than O(n*k).
    ds = df.sort_values("Open Time")
    t_open = ds["Open Time"].values.astype("datetime64[ns]").astype(np.int64)
    t_res_raw = ds["Resolved Time"].values
    t_res = np.where(np.isnat(t_res_raw), np.iinfo(np.int64).max,
                     t_res_raw.astype("datetime64[ns]").astype(np.int64))
    opened_before = np.searchsorted(np.sort(t_open), t_open, side="left")
    resolved_before = np.searchsorted(np.sort(t_res), t_open, side="left")
    if QUEUE_UNRESOLVED == "open":
        ds = ds.assign(Queue_Length_At_Open=opened_before - resolved_before)
    else:
        # Sensitivity arm: never-resolved cases are excluded from the queue population
        # entirely. This is the pre-2026-09-07 behaviour. It is NOT admissible (it uses the
        # fact that a case never resolves anywhere in the log), and it is retained only so
        # the paper can report the measurement under both treatments.
        keep = ~np.isnat(t_res_raw)
        ob2 = np.searchsorted(np.sort(t_open[keep]), t_open, side="left")
        rb2 = np.searchsorted(np.sort(t_res[keep]), t_open, side="left")
        ds = ds.assign(Queue_Length_At_Open=ob2 - rb2)
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

    # Rule 2, computed once over the whole log because it is a function of each case's own
    # history and of nothing else. Attaching it here means every downstream call site gets
    # the admissible values without knowing this module changed.
    from causal_encoding import encode_causal
    _res_raw = d["Incident ID"].map(inc.set_index("Incident ID")["Resolved Time"]).values
    _t_res = np.where(np.isnat(_res_raw), np.iinfo(np.int64).max,
                      _res_raw.astype("datetime64[ns]").astype(np.int64))
    _t_open = pd.to_datetime(d["Open Time"]).values.astype("datetime64[ns]").astype(np.int64)
    _theta, _cnt = encode_causal(_t_open, _t_res, g_all.values, y_all.values, alpha=SMOOTHING)
    set_causal_te(pd.DataFrame({"Group_Breach_Rate_TE": _theta, "Group_Volume_TE": _cnt},
                               index=d.index))
    _p("Rule 2 mode: %s" % RULE2_MODE)

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
