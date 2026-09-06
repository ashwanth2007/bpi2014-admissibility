"""
The four operational baseline assignment policies (Section 7 of the interim report).
====================================================================================
Built and scored BEFORE the optimiser, because if the baselines do not behave sanely
the objective functions are wrong and no optimiser result would mean anything.

Expected sane behaviour, used as the correctness check in experiment.py:
  round robin    near-best workload balance, poor SLA and poor expertise
  FIFO           same as round robin in spirit, worse balance
  least loaded   best workload balance
  greedy         best first-time-right, worst workload balance

Every policy respects the eligibility mask (C3, C4) and the capacity limit (C2).
"""
from __future__ import annotations

import numpy as np


def _feasible_groups(problem, i, remaining):
    """Groups that are eligible for lead i and still have capacity left."""
    ok = problem.eligible[i] & (remaining > 0)
    if not ok.any():                       # capacity exhausted, fall back to eligibility
        ok = problem.eligible[i]
    if not ok.any():                       # nothing eligible at all, allow anything
        ok = np.ones(problem.n_groups, dtype=bool)
    return np.where(ok)[0]


def round_robin(problem, seed: int = 42) -> np.ndarray:
    """Cycle through groups in order, skipping ineligible or full ones."""
    remaining = problem.capacity.copy()
    x = np.zeros(problem.n_leads, dtype=int)
    ptr = 0
    for i in range(problem.n_leads):
        cand = _feasible_groups(problem, i, remaining)
        # first candidate at or after the rotating pointer
        pick = cand[np.searchsorted(cand, ptr % problem.n_groups) % len(cand)]
        x[i] = pick
        remaining[pick] -= 1
        ptr += 1
    return x


def fifo(problem, seed: int = 42) -> np.ndarray:
    """First in, first out: each lead goes to the lowest-indexed feasible group.
    The pure queue discipline, ignoring both priority and suitability."""
    remaining = problem.capacity.copy()
    x = np.zeros(problem.n_leads, dtype=int)
    for i in range(problem.n_leads):
        cand = _feasible_groups(problem, i, remaining)
        x[i] = cand[0]
        remaining[cand[0]] -= 1
    return x


def least_loaded(problem, seed: int = 42) -> np.ndarray:
    """Always pick the feasible group with the smallest projected load. Optimises f3
    alone and nothing else."""
    remaining = problem.capacity.copy()
    load = problem.base_load.copy()
    x = np.zeros(problem.n_leads, dtype=int)
    for i in range(problem.n_leads):
        cand = _feasible_groups(problem, i, remaining)
        pick = cand[int(np.argmin(load[cand]))]
        x[i] = pick
        load[pick] += 1
        remaining[pick] -= 1
    return x


def greedy_best_outcome(problem, seed: int = 42) -> np.ndarray:
    """Assign each lead to the feasible group with the highest first-time-right
    probability. This is what a conventional lead-scoring system produces, and beating
    it is the answer to research question one."""
    remaining = problem.capacity.copy()
    x = np.zeros(problem.n_leads, dtype=int)
    # process the highest-stakes leads first so they get the best groups
    order = np.argsort(-problem.F1.max(axis=1))
    for i in order:
        cand = _feasible_groups(problem, i, remaining)
        pick = cand[int(np.argmax(problem.F1[i, cand]))]
        x[i] = pick
        remaining[pick] -= 1
    return x


def random_policy(problem, seed: int = 42) -> np.ndarray:
    """Uniform random over feasible groups. The floor everything must beat."""
    rng = np.random.default_rng(seed)
    remaining = problem.capacity.copy()
    x = np.zeros(problem.n_leads, dtype=int)
    for i in range(problem.n_leads):
        cand = _feasible_groups(problem, i, remaining)
        pick = int(rng.choice(cand))
        x[i] = pick
        remaining[pick] -= 1
    return x


BASELINES = {
    "Random": random_policy,
    "RoundRobin": round_robin,
    "FIFO": fifo,
    "LeastLoaded": least_loaded,
    "GreedyBestOutcome": greedy_best_outcome,
}
