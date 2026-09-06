# Phase 4: PAA-NSGA-II Lead Assignment Optimiser

The multi-objective assignment optimiser from the faculty specification. This is the
declared novelty of the project, and until 2026-09-01 it did not exist.

## What this does, in one paragraph

Incidents arrive from the real BPI Challenge 2014 log (Rabobank Nederland Group ICT).
For each arrival batch, an assignment policy decides which of 50 support groups handles
each incident. Five competing objectives are traded off: first-time-right resolution,
SLA breach risk, workload balance, expertise match, and response delay. PAA-NSGA-II is
a modified genetic algorithm that searches for the Pareto-optimal set of assignments,
and it is compared against five operational baselines and two standard algorithms.

## Quick start

```bash
# fast sanity check, about one minute
python experiment.py --batches 1 --runs 2 --gens 20 --pop 30 --tag smoke

# the full protocol used for the reported results
python experiment.py --batches 5 --runs 30 --gens 150 --pop 60 --tag full
```

Individual components can be run on their own to inspect them:

```bash
python sim/replay.py     # what the simulator built: groups, capacities, batches
python scoring.py        # the two lead-level models and the objective matrices
```

## The files

| File | What it is |
|---|---|
| `sim/replay.py` | Loads BPI 2014, rebuilds decision-time state, yields arrival batches. States plainly what is real and what is simulated. |
| `scoring.py` | Trains the two lead-level models and builds the per-(lead, group) objective matrices. Carries the f1 design decision. |
| `problem.py` | The five-objective problem, the four constraints, and the operational metrics. |
| `baselines.py` | Random, round robin, FIFO, least loaded, greedy. |
| `operators.py` | The three PAA modifications plus the repair operator. **This is the novelty.** |
| `experiment.py` | Runs everything, ablates, and applies the statistics. |
| `results/` | Metrics CSVs, Pareto fronts, significance JSON, run log. |
| `docs/` | Build log, mistakes, decisions. |

## The five objectives

All are expressed as minimisation internally; f1 and f4 are negated.

| | Objective | Direction | Source |
|---|---|---|---|
| f1 | first-time-right resolution probability | maximise | model, factorised |
| f2 | SLA breach probability | minimise | model, factorised |
| f3 | workload imbalance (sd of post-assignment load) | minimise | computed |
| f4 | expertise match | maximise | historical specialisation |
| f5 | expected delay before first touch | minimise | historical, log1p |

## The three claimed modifications

1. **Priority-aware initialisation.** 35 per cent of the initial population is seeded
   greedily in composite-priority order rather than uniformly at random.
2. **Adaptive crossover.** Crossover probability tracks population diversity, rising
   when the front stagnates.
3. **SLA-aware mutation.** Mutation is biased 4x toward leads whose unavoidable breach
   risk exceeds 0.5.

Each is independently switchable, which is what makes the ablation
(`PAA-noPriority`, `PAA-noAdaptive`, `PAA-noSLAMut`) meaningful.

## What is real and what is simulated

**Read this before quoting any number from here.**

Real, taken from the Rabobank log: arrival timestamps, priority, impact, urgency,
configuration item, the set of groups and what each actually handled, group capacity
(observed peak concurrent load), and the historically observed assignment.

Simulated: the OUTCOME of a counterfactual assignment. If a policy routes an incident to
group j and the log shows it went to group k, no ground truth exists for what would have
happened, so the trained models score it. This is the standard objection to optimisation
papers and the nearest prior work (arXiv 2606.01857) names it as its own open limitation.

The honest control: `matched_fraction` reports how often a policy chose the group the log
actually used, and `matched_real_breach_rate` gives the REAL observed breach rate on
exactly those cases. That is the only non-simulated outcome signal available, and it is
reported next to every simulated result.

## Honest limitations

- **f1 is not the specification's f1.** The spec defines it as expected conversions from
  Model A. BPI 2014 has no conversion label, so within the optimiser f1 is the ITSM
  analogue: first-time-right resolution. See the design-decision block at the top of
  `scoring.py`. Model A remains a separately evaluated component and is not used here.
- **Model C does not exist and is not claimed.** Both f1 and f2 use an explicit rank-1
  factorisation `P(outcome | i) x R(group, specialisation)`, which assumes group
  suitability is separable from lead identity. That is an approximation, and it is
  labelled as one.
- **Five objectives is many-objective territory** where NSGA-II is known to degrade
  (Deb and Jain 2014), which is why NSGA-III is included as a control.

## Reproducibility

Every run is seeded and **reproducibility is verified, not asserted**. Two separate
invocations with identical arguments produce bitwise-identical metrics.

**This was broken until 2026-09-07 and the claim above was false.** The seed was derived with
`hash(gname) % 97`, and Python salts string hashing per process, so the same variant drew a
different seed on every invocation: measured at 43, 41 and 32 across three processes for the
same name, with `PYTHONHASHSEED` unset. It is now `zlib.crc32(gname.encode()) % 97`, which is
stable across processes and platforms.

**A second defect was fixed at the same time.** The hypervolume reference point was taken
from whichever run happened to complete first (`if ref is None`), which made every
hypervolume value depend on variant ORDER and made runs with different variant sets
incomparable. It is now the nadir across ALL variants and ALL runs on the batch, computed in
a first pass before any hypervolume is taken. Fronts from that pass are cached and reused, so
the total compute is unchanged.

**Consequence: hypervolume figures from any run before 2026-09-07 are not comparable to
current ones and must not be quoted.**
