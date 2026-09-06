"""
The three PAA-NSGA-II modifications, plus the repair operator.
==============================================================
These four classes ARE the novelty claim of the project. Everything else in Phase 4 is
standard NSGA-II from pymoo, deliberately, so that the difference between
`StandardNSGA2` and `PAA-NSGA-II` isolates exactly these operators and nothing else.
That is what makes the ablation in experiment.py clean.

From Section 6.3 of the interim report:

  1. PriorityAwareSampling   a fraction of the initial population is seeded greedily by
                             a composite priority score instead of uniformly at random,
                             with the remainder random to preserve diversity.
  2. AdaptiveCrossover       crossover probability is modulated by measured population
                             diversity: raised when the front stagnates, lowered as it
                             converges.
  3. SLAAwareMutation        mutation is biased toward genes whose predicted breach
                             probability is high, so search effort concentrates on the
                             cases actually at risk.
  4. CapacityRepair          restores feasibility after crossover and mutation by moving
                             the lowest-priority excess lead to the best feasible group.

Each is switchable, so the ablation can turn any one of them off.
"""
from __future__ import annotations

import numpy as np
from pymoo.core.sampling import Sampling
from pymoo.core.crossover import Crossover
from pymoo.core.mutation import Mutation
from pymoo.core.repair import Repair


def _rng(kwargs):
    """pymoo 0.6.2 passes `random_state` (a numpy Generator) into operators."""
    rs = kwargs.get("random_state", None)
    if isinstance(rs, np.random.Generator):
        return rs
    if isinstance(rs, (int, np.integer)):
        return np.random.default_rng(int(rs))
    return np.random.default_rng()


# --------------------------------------------------------------------- 1. sampling
class PriorityAwareSampling(Sampling):
    """Seed `greedy_frac` of the population by composite priority score, rest random."""

    def __init__(self, greedy_frac: float = 0.35):
        super().__init__()
        self.greedy_frac = greedy_frac

    def _do(self, problem, n_samples, **kwargs):
        rng = _rng(kwargs)
        X = np.zeros((n_samples, problem.n_var), dtype=int)
        n_greedy = int(n_samples * self.greedy_frac)

        # Composite priority: high breach risk, low first-time-right, high stakes first.
        # These are exactly the leads where the assignment decision matters most.
        priority = (problem.F2.max(axis=1) - problem.F1.max(axis=1))
        order = np.argsort(-priority)

        for k in range(n_samples):
            remaining = problem.capacity.copy()
            x = np.zeros(problem.n_var, dtype=int)
            if k < n_greedy:
                # greedy pass in priority order, with a little jitter per individual so
                # the seeded portion is not n_greedy identical clones
                jitter = rng.normal(0, 0.02, size=problem.F1.shape)
                for i in order:
                    ok = problem.eligible[i] & (remaining > 0)
                    if not ok.any():
                        ok = problem.eligible[i]
                    if not ok.any():
                        ok = np.ones(problem.n_groups, dtype=bool)
                    cand = np.where(ok)[0]
                    score = (problem.F1[i, cand] - problem.F2[i, cand]
                             + problem.F4[i, cand] + jitter[i, cand])
                    pick = cand[int(np.argmax(score))]
                    x[i] = pick
                    remaining[pick] -= 1
            else:
                for i in range(problem.n_var):
                    ok = problem.eligible[i] & (remaining > 0)
                    if not ok.any():
                        ok = problem.eligible[i]
                    cand = np.where(ok)[0]
                    pick = int(rng.choice(cand))
                    x[i] = pick
                    remaining[pick] -= 1
            X[k] = x
        return X


# -------------------------------------------------------------------- 2. crossover
class AdaptiveCrossover(Crossover):
    """Uniform crossover whose probability tracks population diversity.

    diversity = mean fraction of genes on which two random parents disagree.
    Low diversity means the front has stagnated, so exploration is raised.
    """

    def __init__(self, p_min: float = 0.5, p_max: float = 0.95, adaptive: bool = True):
        super().__init__(2, 2)
        self.p_min, self.p_max = p_min, p_max
        self.adaptive = adaptive
        self.history: list[float] = []

    def _do(self, problem, X, **kwargs):
        _, n_matings, n_var = X.shape
        rng = _rng(kwargs)
        a, b = X[0], X[1]

        if self.adaptive:
            disagree = (a != b).mean()
            # stagnation (low disagreement) -> push probability up toward p_max
            p = self.p_max - (self.p_max - self.p_min) * float(np.clip(disagree / 0.5, 0, 1))
        else:
            p = 0.9
        self.history.append(float(p))

        mask = rng.random((n_matings, n_var)) < 0.5
        do_cross = rng.random(n_matings) < p
        mask &= do_cross[:, None]

        c1, c2 = a.copy(), b.copy()
        c1[mask], c2[mask] = b[mask], a[mask]
        return np.stack([c1, c2])


# --------------------------------------------------------------------- 3. mutation
class SLAAwareMutation(Mutation):
    """Bias mutation toward genes whose predicted breach probability is high.

    Standard uniform mutation spends effort evenly across all leads. Here a lead whose
    best achievable breach probability exceeds `threshold` is `boost` times more likely
    to be re-assigned, so the search concentrates on the cases actually at risk.
    """

    def __init__(self, base_rate: float | None = None, threshold: float = 0.5,
                 boost: float = 4.0, sla_aware: bool = True, adaptive: bool = False,
                 adapt_max: float = 3.0):
        super().__init__()
        self.base_rate = base_rate
        self.threshold = threshold
        self.boost = boost
        self.sla_aware = sla_aware
        # THE REPAIR HYPOTHESIS. The full protocol showed adaptive CROSSOVER is net negative:
        # removing it raised hypervolume from 0.0922 to 0.0944. The likely reason is
        # mechanical rather than mysterious. Raising crossover probability on stagnation
        # recombines parents that have already converged to near-identical genomes, which
        # produces offspring identical to their parents and consumes evaluations for nothing.
        # Diversity is restored by MUTATION, not by recombination. This flag moves the same
        # adaptive schedule onto the operator that can actually act on it.
        self.adaptive = adaptive
        self.adapt_max = adapt_max

    def _do(self, problem, X, **kwargs):
        rng = _rng(kwargs)
        X = X.copy()
        n_pop, n_var = X.shape
        base = self.base_rate if self.base_rate is not None else 1.0 / n_var

        if self.sla_aware:
            risk = problem.F2.min(axis=1)                  # unavoidable breach risk
            w = np.where(risk > self.threshold, self.boost, 1.0)
            rate = np.clip(base * w, 0, 1)
        else:
            rate = np.full(n_var, base)

        if self.adaptive and n_pop > 1:
            # Population diversity, measured the same way AdaptiveCrossover measures it so the
            # two are directly comparable: mean per-gene disagreement across the population.
            disagree = float((X != X[0][None, :]).mean())
            # Low disagreement means the population has converged. Scale mutation UP then,
            # which is the operator that can actually reintroduce diversity.
            scale = 1.0 + (self.adapt_max - 1.0) * float(np.clip(1.0 - disagree / 0.5, 0, 1))
            rate = np.clip(rate * scale, 0, 1)

        flip = rng.random((n_pop, n_var)) < rate[None, :]
        for k, i in zip(*np.where(flip)):
            cand = np.where(problem.eligible[i])[0]
            if len(cand):
                X[k, i] = int(rng.choice(cand))
        return X


# ----------------------------------------------------------------------- 4. repair
class CapacityRepair(Repair):
    """Restore C2 (capacity) and C4 (eligibility) after variation.

    Where a group is over capacity, the lowest-priority excess lead is moved to the
    feasible group with the highest suitability score, per Section 6.3.
    """

    def __init__(self, enabled: bool = True):
        super().__init__()
        self.enabled = enabled

    def _do(self, problem, X, **kwargs):
        if not self.enabled:
            return X
        X = np.asarray(X, dtype=int).copy()
        cap = problem.capacity
        suit = problem.F1 - problem.F2 + problem.F4
        priority = problem.F2.max(axis=1) - problem.F1.max(axis=1)

        for k in range(X.shape[0]):
            x = X[k]
            # first fix ineligible placements
            bad = np.where(~problem.eligible[np.arange(len(x)), x])[0]
            for i in bad:
                cand = np.where(problem.eligible[i])[0]
                if len(cand):
                    x[i] = cand[int(np.argmax(suit[i, cand]))]

            # then fix capacity overflow
            for _ in range(6):
                counts = np.bincount(x, minlength=problem.n_groups)
                over = np.where(counts > cap)[0]
                if len(over) == 0:
                    break
                for g in over:
                    members = np.where(x == g)[0]
                    excess = int(counts[g] - cap[g])
                    if excess <= 0 or len(members) == 0:
                        continue
                    # move the LOWEST priority excess leads out
                    victims = members[np.argsort(priority[members])[:excess]]
                    free = cap - np.bincount(x, minlength=problem.n_groups)
                    for i in victims:
                        ok = problem.eligible[i] & (free > 0)
                        ok[g] = False
                        if not ok.any():
                            continue
                        cand = np.where(ok)[0]
                        pick = cand[int(np.argmax(suit[i, cand]))]
                        x[i] = pick
                        free[pick] -= 1
            X[k] = x
        return X


class RandomFeasibleSampling(PriorityAwareSampling):
    """Purely random but CAPACITY- and ELIGIBILITY-feasible initial population.

    Used by the StandardNSGA2 control so that both algorithms start from feasible
    populations. Without this the control would be handicapped by starting infeasible,
    which would inflate the apparent benefit of priority-aware seeding.
    """

    def __init__(self):
        super().__init__(greedy_frac=0.0)
