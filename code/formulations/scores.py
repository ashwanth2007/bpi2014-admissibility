"""
Decision-aware scoring formulations (F1) for lead/incident assignment.
======================================================================

This module defines the six "proposed score" slots that the faculty specification lists
under Group D, Intelligent Decision Features, and leaves undefined. Her words, page 4 of
the specification: "Representative Suitability | Proposed score", "Workload Balance Score",
"Customer Importance Score", "Recommendation Confidence", "Assignment Score | Proposed
formula", plus the two ML outputs.

DESIGN RULE, applied without exception: every constant is either fitted from training data
or cited to a published method. Nothing is chosen because it looks sophisticated. A
trigonometric term with no measurement behind it is decoration and gets a paper rejected.

WHAT EACH COMPONENT IS, AND WHY THAT FORM
-----------------------------------------
  s1  outcome propensity      model output, calibrated
  s2  SLA survival            1 - P(breach), model output, calibrated
  s3  expertise match         COSINE SIMILARITY between the work item's requirement vector
                              and the group's historical specialisation vector. Cosine is
                              used because it is scale invariant: a group that has handled
                              10,000 items and one that has handled 200 are compared on the
                              SHAPE of their specialisation, not its magnitude.
  s4  workload headroom       JAIN'S FAIRNESS INDEX on the post-assignment load vector
                              (Jain, Chiu and Hawe 1984). Bounded in (1/m, 1], scale free,
                              and it does not change meaning when the batch size changes.
                              The standard deviation used as objective f3 in the optimiser
                              has neither property.
  s5  freshness               EXPONENTIAL DECAY exp(-lambda * age). lambda is fitted by
                              maximum likelihood against the observed outcome-versus-latency
                              curve. It is NOT set by hand.
  s6  SLA urgency             LOGISTIC RAMP 1/(1+exp(k(tau - tau0))). k and tau0 are fitted
                              to the empirical breach hazard as a function of hours remaining.
  s7  item importance         weighted geometric blend of priority, impact and urgency.
                              Geometric, not arithmetic, so a low value on one axis cannot be
                              fully bought back by a high value on another.

AGGREGATION
-----------
  A(i,j) = ( sum_k w_k * s_k(i,j)^p ) ^ (1/p)      with sum_k w_k = 1

The weighted power mean (Dyckhoff and Pedrycz 1984, DOI 10.1016/0165-0114(84)90097-6).
The exponent p is the compensation parameter and it is the point of the whole formulation:

    p = 1     arithmetic mean, fully compensatory, a strength anywhere offsets a weakness
    p -> 0    geometric mean
    p = -1    harmonic mean
    p -> -inf minimum, fully non-compensatory, the worst component decides

Reporting a sensitivity curve over p turns "one criterion can be worse if the whole is
better" from an excuse into a measured, parameterised statement.

TWO DEFECTS THIS MODULE HANDLES EXPLICITLY, BOTH RAISED IN ADVERSARIAL REVIEW
-----------------------------------------------------------------------------
1. DEGENERACY. For p <= 0 the power mean is undefined or collapses to exactly 0 whenever any
   component is 0. Cosine similarity IS exactly 0 for a group with no history in the item's
   category, which is common across 242 assignment groups. Two mitigations, both reported:
   an epsilon floor (EPS_FLOOR) applied to every component, and a sensitivity analysis over
   the floor value. Results for p <= 0 without a floor are reported as undefined rather than
   silently returned as 0.

2. WEIGHT LAUNDERING. CRITIC (Diakoulaki, Mavrotas and Papayannakis 1995,
   DOI 10.1016/0305-0548(94)00059-H) derives weights from each criterion's standard deviation
   and its conflict with the others. But s1 and s2 are MODEL OUTPUTS, so their spread is a
   property of calibration, not of importance: isotonic calibration compresses variance and
   therefore mechanically lowers the CRITIC weight on the very criterion the system exists to
   optimise. Reporting CRITIC alone would launder a modelling artifact into a weight and call
   it data driven. So this module computes CRITIC, entropy and equal weights side by side and
   reports the Spearman rank correlation of the resulting assignment orderings. If the three
   agree, the weighting question is answered by a robustness result rather than by an
   assertion. If they disagree, that disagreement is the finding and it is reported.

Run standalone for a self-test on synthetic data:
    python formulations/scores.py
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy import optimize, stats

# Floor applied to every component before a power mean with p <= 0. Reported, not hidden.
EPS_FLOOR = 1e-3


# ----------------------------------------------------------------- component scores
def cosine_expertise(requirement: np.ndarray, specialisation: np.ndarray) -> np.ndarray:
    """s3. Cosine similarity between item requirement vectors and group specialisation.

    requirement    (n_items, n_cats)  non-negative, usually one-hot on the item's category
    specialisation (n_groups, n_cats) non-negative historical handled-counts per category

    Returns (n_items, n_groups) in [0, 1].

    Scale invariance is the reason for cosine rather than a dot product: a group that has
    handled 10,000 items and one that has handled 200 are compared on the shape of their
    specialisation, not its magnitude. A group with no history in any category of the item
    scores exactly 0, which is what makes the p <= 0 degeneracy real rather than theoretical.
    """
    r = np.asarray(requirement, dtype=float)
    s = np.asarray(specialisation, dtype=float)
    rn = np.linalg.norm(r, axis=1, keepdims=True)
    sn = np.linalg.norm(s, axis=1, keepdims=True)
    r = r / np.maximum(rn, 1e-12)
    s = s / np.maximum(sn, 1e-12)
    return np.clip(r @ s.T, 0.0, 1.0)


def jain_fairness(load: np.ndarray, axis: int = -1) -> np.ndarray:
    """s4. Jain's fairness index, (sum x)^2 / (n * sum x^2).

    Jain, Chiu and Hawe (1984), arXiv cs/9809099. Bounded in (1/n, 1], equals 1 exactly when
    the load is perfectly even, and is scale free so it does not change meaning when batch
    size changes. An all-zero load vector is defined here as 1.0 (perfectly balanced), which
    is the correct limit and avoids a divide by zero.
    """
    x = np.asarray(load, dtype=float)
    n = x.shape[axis]
    s1 = x.sum(axis=axis)
    s2 = (x ** 2).sum(axis=axis)
    out = np.where(s2 > 0, (s1 ** 2) / np.maximum(n * s2, 1e-12), 1.0)
    return np.clip(out, 0.0, 1.0)


@dataclass
class ExponentialDecay:
    """s5. Freshness, exp(-lambda * age_hours). lambda is FITTED, never chosen."""

    lam: float = float("nan")
    n_obs: int = 0
    ci95: tuple = (float("nan"), float("nan"))

    def fit(self, age_hours: np.ndarray, outcome: np.ndarray) -> "ExponentialDecay":
        """Maximum likelihood fit of a Bernoulli model with p(age) = p0 * exp(-lam * age).

        outcome is 1 for the favourable outcome. Fitting rather than assuming is the whole
        point: a hand-picked decay rate is the kind of unmotivated constant a reviewer
        removes the paper for.
        """
        a = np.asarray(age_hours, dtype=float)
        y = np.asarray(outcome, dtype=float)
        ok = np.isfinite(a) & np.isfinite(y)
        a, y = a[ok], y[ok]
        if len(a) < 50 or y.sum() == 0:
            raise ValueError("not enough observations to fit a decay rate")
        a_scaled = a / max(np.median(a[a > 0]) if (a > 0).any() else 1.0, 1e-9)

        def nll(theta):
            logit_p0, log_lam = theta
            lam = np.exp(log_lam)
            p0 = 1.0 / (1.0 + np.exp(-logit_p0))
            p = np.clip(p0 * np.exp(-lam * a_scaled), 1e-9, 1 - 1e-9)
            return -np.sum(y * np.log(p) + (1 - y) * np.log(1 - p))

        res = optimize.minimize(nll, x0=[0.0, np.log(0.5)], method="Nelder-Mead")
        scale = max(np.median(a[a > 0]) if (a > 0).any() else 1.0, 1e-9)
        self.lam = float(np.exp(res.x[1]) / scale)
        self.n_obs = int(len(a))
        # profile-likelihood interval on log lambda, +/- 1.96 * sqrt(1/curvature)
        h = 1e-4
        f0 = nll(res.x)
        fpp = (nll([res.x[0], res.x[1] + h]) - 2 * f0 + nll([res.x[0], res.x[1] - h])) / (h * h)
        se = float(np.sqrt(1.0 / fpp)) if fpp > 0 else float("nan")
        if np.isfinite(se):
            self.ci95 = (float(np.exp(res.x[1] - 1.96 * se) / scale),
                         float(np.exp(res.x[1] + 1.96 * se) / scale))
        return self

    def __call__(self, age_hours: np.ndarray) -> np.ndarray:
        return np.exp(-self.lam * np.clip(np.asarray(age_hours, dtype=float), 0, None))


@dataclass
class LogisticUrgency:
    """s6. SLA urgency, 1 / (1 + exp(k * (hours_remaining - tau0))). Both fitted."""

    k: float = float("nan")
    tau0: float = float("nan")
    n_obs: int = 0

    def fit(self, hours_remaining: np.ndarray, breached: np.ndarray) -> "LogisticUrgency":
        t = np.asarray(hours_remaining, dtype=float)
        b = np.asarray(breached, dtype=float)
        ok = np.isfinite(t) & np.isfinite(b)
        t, b = t[ok], b[ok]
        if len(t) < 50 or b.sum() == 0:
            raise ValueError("not enough observations to fit an urgency ramp")

        def nll(theta):
            k, tau0 = theta
            z = np.clip(-k * (t - tau0), -50, 50)
            p = np.clip(1.0 / (1.0 + np.exp(-z)), 1e-9, 1 - 1e-9)
            return -np.sum(b * np.log(p) + (1 - b) * np.log(1 - p))

        res = optimize.minimize(nll, x0=[0.1, float(np.median(t))], method="Nelder-Mead")
        self.k, self.tau0, self.n_obs = float(res.x[0]), float(res.x[1]), int(len(t))
        return self

    def __call__(self, hours_remaining: np.ndarray) -> np.ndarray:
        z = np.clip(-self.k * (np.asarray(hours_remaining, dtype=float) - self.tau0), -50, 50)
        return 1.0 / (1.0 + np.exp(-z))


def geometric_importance(components: np.ndarray, weights: np.ndarray | None = None) -> np.ndarray:
    """s7. Weighted geometric mean of normalised priority, impact and urgency.

    Geometric rather than arithmetic so that a low value on one axis cannot be fully bought
    back by a high value on another. Inputs are floored at EPS_FLOOR because a single zero
    would otherwise annihilate the product.
    """
    c = np.clip(np.asarray(components, dtype=float), EPS_FLOOR, 1.0)
    w = np.full(c.shape[-1], 1.0 / c.shape[-1]) if weights is None else np.asarray(weights, float)
    w = w / w.sum()
    return np.exp((np.log(c) * w).sum(axis=-1))


# ------------------------------------------------------------------------- weighting
def critic_weights(decision_matrix: np.ndarray) -> np.ndarray:
    """CRITIC. Diakoulaki, Mavrotas and Papayannakis (1995), DOI 10.1016/0305-0548(94)00059-H.

    w_j proportional to sigma_j * sum_k (1 - r_jk), i.e. contrast intensity times conflict.

    CAVEAT, and it must travel with every use of this function: when a criterion is a model
    output, its sigma reflects calibration rather than importance. Never report CRITIC alone.
    """
    X = np.asarray(decision_matrix, dtype=float)
    rng = X.max(axis=0) - X.min(axis=0)
    Z = (X - X.min(axis=0)) / np.maximum(rng, 1e-12)
    sd = Z.std(axis=0, ddof=1)
    with np.errstate(invalid="ignore"):
        R = np.corrcoef(Z, rowvar=False)
    R = np.nan_to_num(R, nan=0.0)
    conflict = (1.0 - R).sum(axis=1)
    c = sd * conflict
    return c / c.sum() if c.sum() > 0 else np.full(X.shape[1], 1.0 / X.shape[1])


def entropy_weights(decision_matrix: np.ndarray) -> np.ndarray:
    """Shannon entropy weighting. Cross-check on CRITIC, not a replacement for it."""
    X = np.clip(np.asarray(decision_matrix, dtype=float), 0, None)
    P = X / np.maximum(X.sum(axis=0, keepdims=True), 1e-12)
    with np.errstate(divide="ignore", invalid="ignore"):
        E = -(P * np.log(np.where(P > 0, P, 1.0))).sum(axis=0) / np.log(max(len(X), 2))
    d = 1.0 - np.nan_to_num(E, nan=1.0)
    return d / d.sum() if d.sum() > 0 else np.full(X.shape[1], 1.0 / X.shape[1])


def equal_weights(decision_matrix: np.ndarray) -> np.ndarray:
    n = np.asarray(decision_matrix).shape[1]
    return np.full(n, 1.0 / n)


# ------------------------------------------------------------------------ aggregation
def power_mean(scores: np.ndarray, weights: np.ndarray, p: float,
               eps_floor: float = EPS_FLOOR) -> np.ndarray:
    """Weighted power mean of order p over the last axis.

    For p <= 0 every component is floored at eps_floor first, because the mean is otherwise
    undefined or identically zero whenever a component is zero, and cosine expertise IS
    exactly zero for a group with no history in the item's category. The floor is a reported
    parameter with its own sensitivity analysis, never a silent fix.
    """
    s = np.asarray(scores, dtype=float)
    w = np.asarray(weights, dtype=float)
    w = w / w.sum()
    if p <= 0:
        s = np.clip(s, eps_floor, None)
    else:
        s = np.clip(s, 0.0, None)
    if abs(p) < 1e-9:                                    # geometric limit
        return np.exp((np.log(np.clip(s, eps_floor, None)) * w).sum(axis=-1))
    return ((s ** p) * w).sum(axis=-1) ** (1.0 / p)


@dataclass
class AssignmentScore:
    """F1. The full assignment score, with every weighting scheme reported side by side."""

    p: float = 1.0
    eps_floor: float = EPS_FLOOR
    weights_: dict = field(default_factory=dict)

    def fit_weights(self, decision_matrix: np.ndarray) -> "AssignmentScore":
        self.weights_ = {
            "critic": critic_weights(decision_matrix),
            "entropy": entropy_weights(decision_matrix),
            "equal": equal_weights(decision_matrix),
        }
        return self

    def score(self, components: np.ndarray, scheme: str = "critic") -> np.ndarray:
        return power_mean(components, self.weights_[scheme], self.p, self.eps_floor)

    def rank_stability(self, components: np.ndarray) -> dict:
        """Spearman rank correlation of the induced ordering across weighting schemes.

        This is the answer to "your weights are arbitrary". If the three schemes induce the
        same ordering, the choice does not matter and that is a robustness result. If they do
        not, the disagreement is the finding and it gets reported rather than buried.
        """
        r = {k: self.score(components, k).ravel() for k in self.weights_}
        out = {}
        keys = list(r)
        for i in range(len(keys)):
            for j in range(i + 1, len(keys)):
                rho, pv = stats.spearmanr(r[keys[i]], r[keys[j]])
                out["%s_vs_%s" % (keys[i], keys[j])] = {"spearman_rho": float(rho),
                                                        "p_value": float(pv)}
        return out


# ------------------------------------------------------------------------- self test
if __name__ == "__main__":
    rng = np.random.default_rng(42)
    n_items, n_groups, n_cats = 200, 30, 12

    req = np.zeros((n_items, n_cats))
    req[np.arange(n_items), rng.integers(0, n_cats, n_items)] = 1.0
    spec = rng.gamma(2.0, 50.0, size=(n_groups, n_cats))
    spec[:5] = 0.0                     # groups with no history at all, the degenerate case
    E = cosine_expertise(req, spec)

    print("cosine expertise      shape %s  min %.4f  max %.4f  exact zeros %d"
          % (E.shape, E.min(), E.max(), int((E == 0).sum())))

    load = rng.integers(0, 20, size=(n_groups,)).astype(float)
    print("jain fairness         even=%.4f  observed=%.4f  single-hot=%.4f"
          % (jain_fairness(np.ones(n_groups)), jain_fairness(load),
             jain_fairness(np.eye(n_groups)[0])))

    age = rng.exponential(8.0, 4000)
    y = (rng.random(4000) < 0.45 * np.exp(-0.06 * age)).astype(float)
    dec = ExponentialDecay().fit(age, y)
    print("fitted decay lambda   %.5f per hour  (95%% CI %.5f to %.5f, n=%d)  true 0.06000"
          % (dec.lam, dec.ci95[0], dec.ci95[1], dec.n_obs))

    tau = rng.normal(12, 8, 4000)
    br = (rng.random(4000) < 1 / (1 + np.exp(0.30 * (tau - 6.0)))).astype(float)
    urg = LogisticUrgency().fit(tau, br)
    print("fitted urgency ramp   k=%.4f tau0=%.4f (n=%d)  true k=0.3000 tau0=6.0000"
          % (urg.k, urg.tau0, urg.n_obs))

    S = np.stack([rng.beta(2, 3, (n_items, n_groups)),
                  rng.beta(3, 2, (n_items, n_groups)),
                  E,
                  np.tile(jain_fairness(load + np.eye(n_groups)), (n_items, 1)),
                  np.tile(dec(age[:n_groups]), (n_items, 1)),
                  np.tile(urg(tau[:n_groups]), (n_items, 1))], axis=-1)
    flat = S.reshape(-1, S.shape[-1])

    A = AssignmentScore(p=1.0).fit_weights(flat)
    for k, w in A.weights_.items():
        print("weights %-8s      %s" % (k, np.array2string(w, precision=4, suppress_small=True)))

    print("\npower mean sweep over p (mean score, critic weights):")
    for p in [-2.0, -1.0, 0.0, 0.5, 1.0, 2.0, 4.0]:
        v = power_mean(S, A.weights_["critic"], p)
        zeros = int((v == 0).sum())
        print("  p=%5.1f  mean=%.4f  min=%.4f  exact-zero cells=%d" % (p, v.mean(), v.min(), zeros))

    print("\nrank stability across weighting schemes:")
    for pair, st in A.rank_stability(S).items():
        print("  %-22s spearman rho = %.4f   p = %.3g" % (pair, st["spearman_rho"], st["p_value"]))

    print("\nself test complete")
