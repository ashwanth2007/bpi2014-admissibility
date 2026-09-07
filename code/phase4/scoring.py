"""
Scoring layer for Phase 4: turns an assignment into objective values.
=====================================================================

DESIGN DECISION, recorded because it changes what the paper can claim.
----------------------------------------------------------------------
The specification defines objective f1 as expected CONVERSIONS, scored by Model A.
Model A is trained on UCI Bank Marketing (Portuguese bank telemarketing). BPI 2014
is a Dutch IT service desk log and contains NO conversion label and none of Model A's
features. Scoring f1 with Model A on BPI 2014 is therefore impossible, and pretending
otherwise is exactly the "two unrelated datasets presented as one pipeline" objection
raised in the 7-model council review.

RESOLUTION. Within the optimiser, f1 is the ITSM analogue of conversion:
FIRST-TIME-RIGHT RESOLUTION, meaning the case is resolved without ever being
reassigned. This is:
  - observable in BPI 2014 (# Reassignments == 0),
  - operationally the same decision (did we route it to the right place first time),
  - already shown to matter on this data: reassigned cases take 8.95x longer to
    resolve at the median (n=46,369, Mann-Whitney p<0.001, rank-biserial 0.593).

Consequence: Phase 4 runs entirely on BPI 2014 and is internally consistent. Model A
remains a separately trained and separately evaluated transferable component for the
lead-intake stage; it is NOT used inside the optimiser. The paper must say this
plainly rather than implying one fused dataset.

THE MODEL C PROBLEM AND THE FACTORISATION.
Both f1 and f2 want a value conditional on (lead i, group j). A historical log records
only the assignment that happened, so P(outcome | i, j) is unlearnable for j != observed.
Both are therefore factorised as an explicit rank-1 approximation:

    P(outcome | i, j)  ~=  P(outcome | i)  x  R(j, spec(i))

where P(outcome | i) comes from a leak-free lead-level model and R is the group's
historical relative performance on that specialisation, smoothed. This ASSUMES group
suitability is separable from lead identity. That assumption is a limitation and is
labelled as one. It is not Model C and is not claimed to be.

All features used are decision-time admissible: nothing counted over the closed case.
"""
from __future__ import annotations

import os as _o, sys as _s  # noqa: E402
_s.path.insert(0, _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__))))
import _openmp_first  # noqa: F401,E402  pins threads, must precede every ML import

import json
import numpy as np
import pandas as pd
from pathlib import Path
from dataclasses import dataclass

from sklearn.model_selection import train_test_split
from sklearn.preprocessing import LabelEncoder
from sklearn.metrics import roc_auc_score
from xgboost import XGBClassifier

SEED = 42
SMOOTH = 20


def _causal_entity_rate(keys: np.ndarray, t_open: np.ndarray, t_res: np.ndarray,
                        y: np.ndarray, prior: float, smooth: int = SMOOTH):
    """Expanding-window target encoding using only cases of the same key that had
    already RESOLVED before this case OPENED. Same rule as Model B v3."""
    enc = np.full(len(keys), prior, dtype=float)
    cnt = np.zeros(len(keys), dtype=float)
    for k in pd.unique(keys):
        m = np.where(keys == k)[0]
        rt = t_res[m]
        o = np.argsort(rt)
        rt_s = rt[o]
        y_s = y[m][o]
        cum = np.concatenate([[0.0], np.cumsum(y_s)])
        pos = np.searchsorted(rt_s, t_open[m], side="left")
        enc[m] = (cum[pos] + prior * smooth) / (pos + smooth)
        cnt[m] = pos
    return enc, cnt


@dataclass
class ScoringTables:
    """Everything the objective functions need, precomputed."""
    p_breach: np.ndarray            # (n_leads,) lead-level P(SLA breach)
    p_reassign: np.ndarray          # (n_leads,) lead-level P(reassigned)
    group_breach_rel: np.ndarray    # (n_groups, n_specs) relative breach multiplier
    group_ftr_rel: np.ndarray       # (n_groups, n_specs) relative first-time-right mult.
    expertise: np.ndarray           # (n_groups, n_specs) expertise match score in [0,1]
    delay_hours: np.ndarray         # (n_groups,) expected delay before first touch
    auc_breach: float
    auc_reassign: float


class Scorer:
    """Trains the two lead-level models on the TRAINING period only, then exposes
    per-(lead, group) objective components for the held-out replay period."""

    def __init__(self, replay, train_frac: float = 0.8, artifacts: Path | None = None):
        self.r = replay
        self.train_frac = train_frac
        self.artifacts = Path(artifacts) if artifacts else None
        self._build()

    # ------------------------------------------------------------------ features
    def _features(self, df: pd.DataFrame) -> pd.DataFrame:
        r = self.r
        t_open, t_res = r.t_open, r.t_res
        X = pd.DataFrame(index=df.index)
        X["Priority"] = df["Priority"].values
        X["Impact"] = pd.to_numeric(df["Impact"], errors="coerce").fillna(-1).values
        X["Urgency"] = pd.to_numeric(df["Urgency"], errors="coerce").fillna(-1).values
        ot = df["Open Time"]
        X["Open_Hour"] = ot.dt.hour.values
        X["Open_DayOfWeek"] = ot.dt.dayofweek.values
        X["Is_Weekend"] = (ot.dt.dayofweek >= 5).astype(int).values
        X["Is_Business_Hours"] = ((ot.dt.hour >= 8) & (ot.dt.hour <= 18)).astype(int).values
        X["Open_Month"] = ot.dt.month.values
        X["Assignment_Delay_Hours"] = df["Assignment_Delay_Hours"].values
        X["Spec_Id"] = df["Category_Id"].values
        X["CI_Type"] = LabelEncoder().fit_transform(
            df["CI Type (aff)"].fillna("unknown").astype(str)).astype(int)
        X["WBS"] = LabelEncoder().fit_transform(
            df["Service Component WBS (aff)"].fillna("unknown").astype(str)).astype(int)

        # queue length at open, global
        ot_s = np.sort(t_open); rt_s = np.sort(t_res)
        X["Queue_At_Open"] = (np.searchsorted(ot_s, t_open, side="left")
                              - np.searchsorted(rt_s, t_open, side="left"))
        return X

    # ------------------------------------------------------------------- build
    def _build(self) -> None:
        r = self.r
        df = r.df
        n = len(df)
        cut = int(n * self.train_frac)

        y_breach = r.breached
        reassign = pd.to_numeric(df["# Reassignments"], errors="coerce").fillna(0).values
        y_reassign = (reassign > 0).astype(int)

        X = self._features(df)

        # Causal encodings, fitted on the whole timeline but strictly past-only per row.
        pb, pc = _causal_entity_rate(df["Observed_Group"].values, r.t_open, r.t_res,
                                     y_breach.astype(float), y_breach.mean())
        rb, rc = _causal_entity_rate(df["Observed_Group"].values, r.t_open, r.t_res,
                                     y_reassign.astype(float), y_reassign.mean())
        sb, _ = _causal_entity_rate(df["Spec"].values, r.t_open, r.t_res,
                                    y_breach.astype(float), y_breach.mean())
        X_b = X.copy(); X_b["Grp_BreachRate"] = pb; X_b["Grp_N"] = pc; X_b["Spec_BreachRate"] = sb
        X_r = X.copy(); X_r["Grp_ReassignRate"] = rb; X_r["Grp_N"] = rc

        tr = np.arange(cut)
        te = np.arange(cut, n)

        def fit(Xf, y, name):
            spw = (y[tr] == 0).sum() / max((y[tr] == 1).sum(), 1)
            m = XGBClassifier(n_estimators=400, max_depth=6, learning_rate=0.05,
                              subsample=0.8, colsample_bytree=0.8, scale_pos_weight=spw,
                              eval_metric="logloss", random_state=SEED, verbosity=0,
                              tree_method="hist", n_jobs=-1)
            m.fit(Xf.iloc[tr], y[tr])
            p = m.predict_proba(Xf)[:, 1]
            auc = roc_auc_score(y[te], p[te])
            print(f"  {name:28s} holdout AUC = {auc:.4f}")
            return m, p, auc

        print("Training lead-level models on the first "
              f"{int(self.train_frac*100)}% of the timeline:")
        self.m_breach, p_breach, auc_b = fit(X_b, y_breach, "P(SLA breach)")
        self.m_reassign, p_reassign, auc_r = fit(X_r, y_reassign, "P(reassigned)")

        # ---------------- group x specialisation relative performance ----------
        n_g, n_c = len(r.groups), len(r.categories)
        gid = df["Observed_Group_Id"].values
        cid = df["Category_Id"].values
        prior_b, prior_r = y_breach.mean(), y_reassign.mean()

        gb = np.ones((n_g, n_c)); gf = np.ones((n_g, n_c))
        share = r.group_category_share
        for g in range(n_g):
            for c in np.where(share[g] > 0)[0]:
                m = np.where((gid == g) & (cid == c) & (np.arange(n) < cut))[0]
                if len(m) == 0:
                    continue
                # smoothed relative multiplier vs the global base rate
                br = (y_breach[m].sum() + prior_b * SMOOTH) / (len(m) + SMOOTH)
                rr = (y_reassign[m].sum() + prior_r * SMOOTH) / (len(m) + SMOOTH)
                gb[g, c] = br / max(prior_b, 1e-6)
                gf[g, c] = (1.0 - rr) / max(1.0 - prior_r, 1e-6)

        self.tables = ScoringTables(
            p_breach=p_breach,
            p_reassign=p_reassign,
            group_breach_rel=np.clip(gb, 0.25, 4.0),
            group_ftr_rel=np.clip(gf, 0.25, 4.0),
            expertise=share / np.maximum(share.max(axis=0, keepdims=True), 1e-9),
            delay_hours=self._group_delay(df, cut),
            auc_breach=auc_b, auc_reassign=auc_r,
        )

    def _group_delay(self, df: pd.DataFrame, cut: int) -> np.ndarray:
        """Median observed hours from open to first assignment, per group, train period."""
        n_g = len(self.r.groups)
        out = np.full(n_g, float(df["Assignment_Delay_Hours"].median()))
        gid = df["Observed_Group_Id"].values
        d = df["Assignment_Delay_Hours"].values
        for g in range(n_g):
            m = np.where((gid == g) & (np.arange(len(df)) < cut))[0]
            if len(m) >= 10:
                out[g] = float(np.median(d[m]))
        return out

    # -------------------------------------------------------------- objectives
    def objective_matrices(self, lead_idx: np.ndarray):
        """Return per-(lead, group) matrices for f1, f2, f4, f5.

        f1  first-time-right probability   (maximise)
        f2  SLA breach probability         (minimise)
        f4  expertise match                (maximise)
        f5  expected delay hours           (minimise)
        f3 is workload imbalance, computed from the assignment vector, not here.
        """
        t = self.tables
        specs = self.r.df["Category_Id"].values[lead_idx]
        pb = t.p_breach[lead_idx][:, None]        # (L,1)
        pr = t.p_reassign[lead_idx][:, None]      # (L,1)

        F2 = np.clip(pb * t.group_breach_rel[:, specs].T, 0.0, 1.0)
        F1 = np.clip((1.0 - pr) * t.group_ftr_rel[:, specs].T, 0.0, 1.0)
        F4 = t.expertise[:, specs].T
        F5 = np.repeat(t.delay_hours[None, :], len(lead_idx), axis=0)
        return F1, F2, F4, F5


if __name__ == "__main__":
    from sim.replay import BPI2014Replay
    base = Path(__file__).parent
    r = BPI2014Replay(base / "bpi2014", batch_size=40)
    s = Scorer(r)
    b = r.batches(max_batches=1)[0]
    F1, F2, F4, F5 = s.objective_matrices(b.lead_idx)
    print(f"\nObjective matrices for batch 0: {F1.shape} (leads x groups)")
    print(f"  f1 first-time-right   min {F1.min():.3f}  mean {F1.mean():.3f}  max {F1.max():.3f}")
    print(f"  f2 SLA breach         min {F2.min():.3f}  mean {F2.mean():.3f}  max {F2.max():.3f}")
    print(f"  f4 expertise match    min {F4.min():.3f}  mean {F4.mean():.3f}  max {F4.max():.3f}")
    print(f"  f5 delay hours        min {F5.min():.2f}  mean {F5.mean():.2f}  max {F5.max():.2f}")
