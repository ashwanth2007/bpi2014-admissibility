"""
The five-objective lead assignment problem (Phase 4, Section 6 of the interim report).
======================================================================================

ENCODING. The specification writes the decision variable as binary x_ij. We use the
equivalent integer encoding x in {0..n_groups-1}^n_leads, where x[i] is the group
assigned to lead i. This is mathematically identical under constraint C1 (every lead
assigned exactly once) and satisfies C1 by construction, so the GA never wastes
evaluations on solutions that assign a lead twice or zero times.

CONSTRAINTS
  C1 assign-once      satisfied by the encoding
  C3 availability     folded into the eligibility mask
  C4 expertise        eligibility mask, enforced by repair
  C2 capacity         a real inequality constraint, handled by the repair operator and
                      also reported to pymoo so infeasible solutions are ranked last

OBJECTIVES, all expressed as minimisation (f1 and f4 negated, per Section 6.2)
  f1  -mean first-time-right probability
  f2   mean SLA breach probability
  f3   standard deviation of post-assignment load across groups (workload imbalance)
  f4  -mean expertise match
  f5   mean expected delay hours, log1p-compressed

NOTE ON f5 SCALING. Observed median assignment delay per group spans 0.03 to 4248
hours, so the raw value would dominate crowding distance and hypervolume. It is
log1p-compressed. Dominance ranking is scale-invariant so this does not change the
Pareto front's membership, only the geometry used for diversity and for the indicator.
"""
from __future__ import annotations

import numpy as np
from pymoo.core.problem import Problem


class LeadAssignmentProblem(Problem):
    def __init__(self, F1, F2, F4, F5, eligible, capacity, base_load=None):
        """
        F1, F2, F4 : (n_leads, n_groups) objective component matrices
        F5         : (n_leads, n_groups) expected delay hours
        eligible   : (n_leads, n_groups) boolean, C3 and C4 combined
        capacity   : (n_groups,) remaining capacity at batch start (C2)
        base_load  : (n_groups,) load already on each group before this batch
        """
        self.F1 = np.asarray(F1, dtype=float)
        self.F2 = np.asarray(F2, dtype=float)
        self.F4 = np.asarray(F4, dtype=float)
        self.F5 = np.log1p(np.asarray(F5, dtype=float))
        self.eligible = np.asarray(eligible, dtype=bool)
        self.capacity = np.asarray(capacity, dtype=float)
        self.base_load = (np.zeros(len(capacity)) if base_load is None
                          else np.asarray(base_load, dtype=float))

        self.n_leads, self.n_groups = self.F1.shape
        super().__init__(n_var=self.n_leads, n_obj=5, n_ieq_constr=1,
                         xl=0, xu=self.n_groups - 1, vtype=int)

    # ------------------------------------------------------------------ helpers
    def objectives_for(self, x: np.ndarray) -> np.ndarray:
        """Objective vector for a single assignment vector. Used by the baselines too,
        so baselines and the GA are scored by identical code."""
        rows = np.arange(self.n_leads)
        g = x.astype(int)
        counts = np.bincount(g, minlength=self.n_groups).astype(float)
        load = self.base_load + counts
        return np.array([
            -self.F1[rows, g].mean(),
            self.F2[rows, g].mean(),
            load.std(),
            -self.F4[rows, g].mean(),
            self.F5[rows, g].mean(),
        ])

    def capacity_violation(self, x: np.ndarray) -> float:
        g = x.astype(int)
        counts = np.bincount(g, minlength=self.n_groups).astype(float)
        return float(np.maximum(counts - self.capacity, 0).sum())

    # ---------------------------------------------------------------- evaluation
    def _evaluate(self, X, out, *args, **kwargs):
        X = np.asarray(X, dtype=int)
        n_pop = X.shape[0]
        rows = np.arange(self.n_leads)

        f1 = np.empty(n_pop); f2 = np.empty(n_pop); f3 = np.empty(n_pop)
        f4 = np.empty(n_pop); f5 = np.empty(n_pop); cv = np.empty(n_pop)

        for k in range(n_pop):
            g = X[k]
            counts = np.bincount(g, minlength=self.n_groups).astype(float)
            load = self.base_load + counts
            f1[k] = -self.F1[rows, g].mean()
            f2[k] = self.F2[rows, g].mean()
            f3[k] = load.std()
            f4[k] = -self.F4[rows, g].mean()
            f5[k] = self.F5[rows, g].mean()
            # infeasible assignments (ineligible group) are penalised hard
            bad = (~self.eligible[rows, g]).sum()
            cv[k] = np.maximum(counts - self.capacity, 0).sum() + 1000.0 * bad

        out["F"] = np.column_stack([f1, f2, f3, f4, f5])
        out["G"] = cv.reshape(-1, 1)


def operational_metrics(problem: LeadAssignmentProblem, x: np.ndarray,
                        observed_group: np.ndarray | None = None,
                        observed_breach: np.ndarray | None = None) -> dict:
    """Report the objectives in human units, plus the honest matched-assignment control.

    matched_fraction     how often the policy picked the group the log actually used
    matched_real_breach  the REAL observed breach rate on exactly those cases. This is
                         the only non-simulated outcome signal available, and it is
                         reported alongside every simulated result.
    """
    rows = np.arange(problem.n_leads)
    g = x.astype(int)
    counts = np.bincount(g, minlength=problem.n_groups).astype(float)
    load = problem.base_load + counts
    m = {
        "first_time_right": float(problem.F1[rows, g].mean()),
        "sla_breach_rate": float(problem.F2[rows, g].mean()),
        "workload_sd": float(load.std()),
        # Jain fairness index reported ALONGSIDE the standard deviation, never in place
        # of it. f3 remains the standard deviation. Swapping the objective after
        # observing that the proposed method loses on it would be fitting the metric to
        # the result, which is the objection this study exists to avoid.
        "workload_jain": float((load.sum() ** 2) / max(len(load) * (load ** 2).sum(), 1e-12)),
        "expertise_match": float(problem.F4[rows, g].mean()),
        "delay_hours": float(np.expm1(problem.F5[rows, g]).mean()),
        "capacity_violation": problem.capacity_violation(x),
        "groups_used": int((counts > 0).sum()),
        "max_group_load": float(counts.max()),
    }
    if observed_group is not None:
        match = (g == observed_group.astype(int))
        m["matched_fraction"] = float(match.mean())
        if observed_breach is not None and match.any():
            m["matched_real_breach_rate"] = float(observed_breach[match].mean())
        else:
            m["matched_real_breach_rate"] = float("nan")
    return m
