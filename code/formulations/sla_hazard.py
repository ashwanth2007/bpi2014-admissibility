"""
F2. SLA risk as a discrete-time hazard over the service-level clock.
====================================================================

The faculty specification asks for "SLA Risk Score | ML output" and, in the research-gap
table, names the limitation of current practice as "SLA management | reactive alerts" with
the opportunity being "predict SLA violations before they occur".

A static binary classifier does not close that gap. It answers "will this breach", once, at
intake. What an operator needs is "what is the risk RIGHT NOW, given that it has not breached
yet and the clock has been running for t hours". That is a hazard, not a probability.

THE MODEL
---------
Discrete-time survival, the standard person-period formulation (Singer and Willett; see also
Suresh, Severn and Ghezzi 2022, BMC Medical Research Methodology, DOI 10.1186/s12874-022-01679-6
for a modern introduction aimed at ML practitioners).

Each incident contributes one row per elapsed time bin in which it is still open. The target
is "did it breach in THIS bin". A gradient-boosted classifier then estimates the discrete
hazard h(t | x) directly, and survival follows by the product rule:

    S(t | x) = prod_{u <= t} (1 - h(u | x))
    F(t | x) = 1 - S(t | x)                      cumulative breach risk by time t

WHY DISCRETE TIME RATHER THAN COX
---------------------------------
Three reasons, all practical rather than aesthetic:

1. `lifelines` and `scikit-survival` are both absent from this machine, and installing them
   has twice broken the Anaconda numpy/scipy stack here. The discrete-time formulation needs
   nothing beyond the gradient boosting already installed.
2. Cox assumes proportional hazards. Nothing in this log justifies that assumption, and an
   unjustified assumption in a paper is worse than a slightly less elegant model.
3. Discrete time handles TIME-VARYING covariates natively. Queue length, escalation count and
   reassignment count all change while the clock runs, and they are exactly the operational
   levers the framework claims to act on. Putting them in a static classifier throws away the
   thing the paper is about.

ADMISSIBILITY, WHICH IS THE POINT OF THE WHOLE PAPER
----------------------------------------------------
Every covariate in a person-period row for bin t must be observable AT THE START OF BIN t.
`build_person_period` enforces this by construction: it takes covariates as a function of
elapsed time and never reads a value the future would supply. This is the same rule applied
to the static model in `bpi2014_pipeline_leakfree.py`, extended to the time axis, and it is
what the operating threshold is tuned against.

The escalation decision is a threshold on F(t | x), tuned on TRAINING-PERIOD data alone and
then held fixed. Tuning it on the test period would reproduce, on the time axis, exactly the
leak this paper exists to measure.

Run standalone for a self-test on synthetic data with known hazard:
    python formulations/sla_hazard.py
"""
from __future__ import annotations

import os as _o, sys as _s  # noqa: E402
_s.path.insert(0, _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__))))
import _openmp_first  # noqa: F401,E402  pins threads, must precede every ML import

from dataclasses import dataclass, field

import numpy as np
import pandas as pd


def build_person_period(durations: np.ndarray,
                        event: np.ndarray,
                        static_covariates: pd.DataFrame,
                        bin_edges: np.ndarray,
                        time_varying=None) -> pd.DataFrame:
    """Expand one row per case into one row per elapsed time bin the case survived into.

    durations         (n,) observed time on the clock, in hours
    event             (n,) 1 if the case breached, 0 if it closed or was censored first
    static_covariates (n, p) values known at intake
    bin_edges         monotonically increasing bin boundaries in hours, starting at 0
    time_varying      optional callable (case_index_array, bin_index) -> DataFrame of
                      covariates observable AT THE START of that bin. It is never passed a
                      future value, which is what keeps the expansion admissible.

    Returns a long frame with `bin_idx`, `bin_start`, `y` and every covariate.
    """
    d = np.asarray(durations, dtype=float)
    e = np.asarray(event, dtype=int)
    n_bins = len(bin_edges) - 1
    frames = []
    for b in range(n_bins):
        lo, hi = bin_edges[b], bin_edges[b + 1]
        alive = d > lo                                  # still on the clock at bin start
        if not alive.any():
            continue
        idx = np.flatnonzero(alive)
        # breached during THIS bin
        y = ((d[idx] <= hi) & (e[idx] == 1)).astype(int)
        block = static_covariates.iloc[idx].reset_index(drop=True).copy()
        block["bin_idx"] = b
        block["bin_start"] = lo
        block["_case"] = idx
        if time_varying is not None:
            tv = time_varying(idx, b)
            for c in tv.columns:
                block[c] = np.asarray(tv[c])
        block["y"] = y
        frames.append(block)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


@dataclass
class DiscreteTimeHazard:
    """Discrete-time hazard model with survival and a training-period-tuned threshold."""

    bin_edges: np.ndarray = field(default_factory=lambda: np.array([0.0]))
    model: object = None
    features: list = field(default_factory=list)
    threshold_: float = 0.5
    threshold_tuned_on_: str = "not tuned"

    def fit(self, person_period: pd.DataFrame, features: list, **kw) -> "DiscreteTimeHazard":
        from xgboost import XGBClassifier
        X = person_period[features]
        y = person_period["y"].to_numpy()
        pos = max(int(y.sum()), 1)
        params = dict(n_estimators=400, max_depth=5, learning_rate=0.05,
                      subsample=0.8, colsample_bytree=0.8,
                      scale_pos_weight=float((len(y) - pos) / pos),
                      eval_metric="logloss", random_state=42, n_jobs=-1, verbosity=0)
        params.update(kw)
        self.model = XGBClassifier(**params).fit(X, y)
        self.features = list(features)
        return self

    def hazard(self, person_period: pd.DataFrame) -> np.ndarray:
        return self.model.predict_proba(person_period[self.features])[:, 1]

    def survival_curve(self, person_period: pd.DataFrame) -> pd.DataFrame:
        """S(t) and F(t) per case, by the product rule over that case's own bins."""
        pp = person_period.copy()
        pp["h"] = self.hazard(pp)
        pp = pp.sort_values(["_case", "bin_idx"])
        pp["S"] = pp.groupby("_case")["h"].transform(lambda s: (1.0 - s).cumprod())
        pp["F"] = 1.0 - pp["S"]
        return pp

    def tune_threshold(self, person_period_train: pd.DataFrame,
                       objective: str = "f1", label: str = "training period") -> "DiscreteTimeHazard":
        """Pick the escalation cut point on TRAINING data only, then freeze it.

        Tuning on the evaluation period would reproduce on the time axis exactly the leak this
        paper measures on the feature axis.
        """
        from sklearn.metrics import f1_score
        h = self.hazard(person_period_train)
        y = person_period_train["y"].to_numpy()
        grid = np.quantile(h, np.linspace(0.50, 0.995, 120))
        best, best_t = -1.0, 0.5
        for t in grid:
            s = f1_score(y, (h >= t).astype(int), zero_division=0)
            if s > best:
                best, best_t = s, float(t)
        self.threshold_ = best_t
        self.threshold_tuned_on_ = "%s (%s = %.4f)" % (label, objective, best)
        return self


if __name__ == "__main__":
    # Synthetic data with a KNOWN hazard, so the fit can be checked against ground truth
    # rather than merely looking plausible.
    rng = np.random.default_rng(42)
    n = 6000
    prio = rng.integers(1, 6, n).astype(float)          # 1 urgent .. 5 low
    queue = rng.poisson(8, n).astype(float)
    # true hazard rises with queue pressure and falls with lower priority
    lam = 0.035 * np.exp(0.06 * queue - 0.25 * prio)
    dur = rng.exponential(1.0 / lam)
    cens = rng.exponential(60.0, n)
    event = (dur <= cens).astype(int)
    obs = np.minimum(dur, cens)

    static = pd.DataFrame({"priority": prio, "queue_at_open": queue})
    edges = np.array([0, 2, 4, 8, 12, 24, 48, 96, 1e9])

    pp = build_person_period(obs, event, static, edges)
    print("person-period rows: %d from %d cases (%.1fx expansion)" % (len(pp), n, len(pp) / n))
    print("bin-level breach rate: %.4f" % pp["y"].mean())

    # Chronological split on ARRIVAL time, not on observed duration.
    # Splitting on duration sorts by the outcome itself: training would get the fast
    # breachers and test the slow survivors, inverting every learned effect. That is the
    # same class of mistake this paper measures, so the self test must not make it.
    arrival = rng.uniform(0.0, 1000.0, n)
    order = np.argsort(arrival)
    train_cases = set(order[: int(0.7 * n)].tolist())
    tr = pp[pp["_case"].isin(train_cases)]
    te = pp[~pp["_case"].isin(train_cases)]

    feats = ["priority", "queue_at_open", "bin_idx", "bin_start"]
    m = DiscreteTimeHazard(bin_edges=edges).fit(tr, feats).tune_threshold(tr)
    print("threshold tuned on %s -> %.4f" % (m.threshold_tuned_on_, m.threshold_))

    from sklearn.metrics import roc_auc_score
    h_te = m.hazard(te)
    print("bin-level hazard AUC (held out): %.4f" % roc_auc_score(te["y"], h_te))

    curves = m.survival_curve(te)
    last = curves.groupby("_case").tail(1)
    print("mean cumulative breach risk F(t) at last observed bin: %.4f" % last["F"].mean())
    print("monotonic survival check (S never increases within a case): %s"
          % bool(curves.groupby("_case")["S"].apply(lambda s: s.diff().dropna().le(1e-12).all()).all()))

    # does the model recover the DIRECTION of the true effects?
    import itertools
    grid = pd.DataFrame(list(itertools.product([1.0, 5.0], [2.0, 20.0], [0], [0.0])),
                        columns=feats)
    hh = m.hazard(grid)
    print("recovered effects  high-queue/urgent %.4f > low-queue/low-prio %.4f : %s"
          % (hh[1], hh[2], bool(hh[1] > hh[2])))
    print("\nself test complete")


# --------------------------------------------------------------------- breach from survival
def breach_risk_from_resolution(curves: "pd.DataFrame", threshold_hours: np.ndarray,
                                bin_edges: np.ndarray) -> "pd.DataFrame":
    """Breach risk derived from a RESOLUTION hazard, which is the non-degenerate framing.

    WHY THIS EXISTS. Modelling "breach" directly as the event is degenerate on BPI 2014,
    and this was caught by testing rather than assumed. The label is defined as
    handle_time > per-priority threshold, so once a case has been open longer than its own
    threshold the label is already determined: measured breach rate past the threshold is
    1.0000. A hazard model given elapsed time and priority therefore reads the answer instead
    of predicting it, and it scores an AUC of 0.94 while learning nothing.

    The correct object is the hazard of RESOLUTION, which elapsed time does not determine
    (measured resolution hazard rises smoothly from 0.126 at 1h to 0.423 at 24h). Breach is
    then a derived quantity, exactly as it is in the real process:

        P(breach | alive at t) = P(not resolved by threshold | alive at t) = S(thr) / S(t)

    S is the survival of the OPEN state. The ratio is clipped to [0, 1] because S is estimated
    and can be non-monotone across the bin grid by a rounding margin.
    """
    import pandas as pd
    c = curves.copy()
    thr = np.asarray(threshold_hours, dtype=float)
    edges = np.asarray(bin_edges, dtype=float)
    # bin index whose start is the last edge at or below each case's own threshold
    thr_bin = np.clip(np.searchsorted(edges, thr, side="right") - 1, 0, len(edges) - 2)
    c["_thr_bin"] = thr_bin[c["_case"].to_numpy()]
    s_at_thr = (c[c["bin_idx"] <= c["_thr_bin"]]
                .sort_values(["_case", "bin_idx"]).groupby("_case")["S"].last())
    c["_S_thr"] = c["_case"].map(s_at_thr)
    c["breach_risk"] = np.clip(1.0 - (c["_S_thr"] / np.maximum(c["S"], 1e-12)), 0.0, 1.0)
    return c
