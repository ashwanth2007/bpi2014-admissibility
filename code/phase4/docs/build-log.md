# Phase 4 build log

Append-only. One entry per work session: what was built, what was verified, what is left.

---

## 2026-09-01, session 1: Phase 4 built from zero

**Why this session happened.** A 7-model council review returned a unanimous verdict that
the single biggest hole in the project was that PAA-NSGA-II, the declared novelty, did not
exist. The models were judged adequate and not the bottleneck. Phase 4 was chosen over the
prototype CRM for that reason.

### What was built

| Component | File | Status |
|---|---|---|
| Arrival replay simulator | `sim/replay.py` | done, verified |
| Objective scoring layer | `scoring.py` | done, verified |
| Five-objective problem | `problem.py` | done, verified |
| Five baseline policies | `baselines.py` | done, verified |
| Three PAA operators + repair | `operators.py` | done |
| Experiment and statistics harness | `experiment.py` | done |

### What the simulator built, verified

- 15,828 incidents retained (groups with at least 50 cases)
- 50 assignment groups, 47 technical specialisations
- 42.9 per cent breach rate
- Group capacity from observed peak concurrent load: min 7, median 17, max 234
- Mean top-1 specialisation share 0.77, so groups genuinely specialise
- Eligible groups per lead range 2 to 49, so constraint C4 binds

### Lead-level models, chronological holdout

| Model | Holdout AUC |
|---|---|
| P(SLA breach) | 0.7399 |
| P(reassigned) | 0.7776 |

Both trained on the first 80 per cent of the timeline only, with causal expanding-window
entity encodings (resolved-before-opened). Consistent with the Model B v3 chronological
result of 0.7544, which is the expected range.

### Baseline sanity check, the correctness gate

This was the gate defined in the plan: if the baselines do not behave sanely, the
objective functions are wrong and no optimiser result means anything.

| Policy | FTR | Breach | Workload sd | Expertise | Delay h |
|---|---|---|---|---|---|
| Random | 0.408 | 0.546 | 1.058 | 0.350 | 478.0 |
| RoundRobin | 0.423 | 0.552 | 0.800 | 0.309 | 579.9 |
| FIFO | 0.223 | 0.669 | 3.219 | 0.444 | 843.9 |
| LeastLoaded | 0.402 | 0.545 | **0.490** | 0.418 | 405.4 |
| GreedyBestOutcome | **0.630** | **0.393** | 2.020 | **0.717** | **282.7** |

**Passed.** LeastLoaded wins workload balance, as it optimises f3 alone. Greedy wins every
outcome objective and pays for it in balance. FIFO is worst overall, as a pure queue
discipline should be. Zero capacity violations anywhere.

### Smoke test of the GA, 1 run, 15 generations, population 30

Too small to conclude anything, recorded only as evidence the pipeline runs end to end.
PAA-NSGA-II beat StandardNSGA2 on hypervolume (0.0193 vs 0.0139) and breach (0.4105 vs
0.5168). `PAA-noSLAMut` showed the highest hypervolume of all, which is an early hint that
the SLA-aware mutation may be hurting rather than helping. Flagged to check against the
full protocol rather than assumed either way.

### Bugs hit

Five, all recorded in `mistakes.md`. The two that mattered: the Anaconda numpy stack was
broken twice by pip installs pulling numpy 2.x, and NSGA-III was silently misconfigured
with 70 reference directions against a population of 60, which would have made the control
look worse than it is.

### Left to do

- Full protocol running: 5 batches, 30 runs, 150 generations, population 60.
- Read the PAA vs Standard Wilcoxon result and report it whichever way it lands.
- Plots: Pareto fronts, hypervolume convergence traces.
- Prototype CRM (Phase 9), deferred by decision.
