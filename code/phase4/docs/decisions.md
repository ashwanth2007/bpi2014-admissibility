# Phase 4 design decisions

Every entry records what was decided, why, and what it costs. A decision with a cost
that is not written down becomes an overclaim later.

---

## D1. f1 is first-time-right resolution, not conversion

**Date:** 2026-09-01

**The specification says** objective f1 is expected CONVERSIONS, scored by Model A.

**The problem.** Model A is trained on UCI Bank Marketing, Portuguese bank telemarketing.
BPI 2014 is a Dutch IT service desk log. It contains no conversion label and none of
Model A's features. f1 as specified is not computable on this data.

This is the "Frankenstein pipeline" objection raised by the 7-model council review:
presenting two unrelated datasets as one integrated pipeline is the first thing a hostile
reviewer attacks.

**Decision.** Inside the optimiser, f1 is the ITSM analogue of conversion:
**first-time-right resolution**, meaning the case is resolved without ever being
reassigned. This is observable in BPI 2014 (`# Reassignments == 0`), it is operationally
the same decision (did we route it correctly the first time), and its importance is
already established on this exact data: reassigned cases take 8.95x longer to resolve at
the median (n=46,369, Mann-Whitney p<0.001, rank-biserial 0.593).

**Cost.** Phase 4 no longer evaluates Model A. Model A becomes a separately trained and
separately evaluated component for the lead-intake stage of the CRM, not part of the
optimiser experiment. The paper must say this plainly rather than implying a single fused
dataset.

**Benefit.** Phase 4 runs entirely on BPI 2014 and is internally consistent. The
Frankenstein objection does not apply to the optimiser result.

---

## D2. Both f1 and f2 use a rank-1 factorisation, because Model C is unlearnable

**Date:** 2026-09-01

**The problem.** Both objectives want a value conditional on (lead i, group j). A
historical log records only the assignment that actually happened, so `P(outcome | i, j)`
cannot be learned for any j other than the observed one. This is the Model C problem and
it was established in `research/critical-review-2026-08-21.md`.

**Decision.** Factorise explicitly:

```
P(outcome | i, j)  ~=  P(outcome | i)  x  R(j, specialisation(i))
```

where `P(outcome | i)` is a leak-free lead-level model and `R` is the group's smoothed
historical relative performance on that specialisation.

**Cost.** This assumes group suitability is separable from lead identity. It cannot
represent an interaction where group j is uniquely good at a specific *kind* of lead
beyond its specialisation average. That is a real limitation and is stated as one.

**What it is not.** It is not Model C, and the paper must not present it as one.

---

## D3. Specialisation is `CI Subtype (aff)`, not `Category`

**Date:** 2026-09-01

**The problem.** Objective f4 is expertise match. The obvious key, `Category`, has only 4
values in BPI 2014 ("incident", "request for information", "complaint", "request for
change"). Those describe request TYPE, not technical domain, so f4 computed on them was
nearly constant and the constraint C4 was not binding: every lead had 43 to 49 eligible
groups out of 50.

**Decision.** Use `CI Subtype (aff)`, which has 58 values (Server Based Application, Web
Based Application, Desktop Application, Laptop, and so on) and is a genuine technical
specialisation.

**Effect, measured.** Mean top-1 specialisation share per group rose to 0.77, meaning
groups genuinely specialise. Eligible groups per lead dropped to a 2 to 49 range, so C4
now actually binds and expertise match is a meaningful objective.

---

## D4. Integer encoding instead of the binary x_ij in the specification

**Date:** 2026-09-01

The specification writes the decision variable as binary `x_ij`. We use the equivalent
integer encoding `x[i] = group assigned to lead i`. Under constraint C1 (every lead
assigned exactly once) the two are mathematically identical, but the integer form
satisfies C1 by construction, so the GA never spends evaluations on solutions that assign
a lead twice or zero times. No claim changes.

---

## D5. StandardNSGA2 uses the same operator mechanics with the modifications off

**Date:** 2026-09-01

**The problem.** If the control used pymoo's stock operators and PAA used custom ones,
the comparison would confound "our three modifications" with "our operator
implementation".

**Decision.** The control is `AdaptiveCrossover(adaptive=False)` (fixed p=0.9 uniform
crossover), `SLAAwareMutation(sla_aware=False)` (unbiased random resetting), and
`RandomFeasibleSampling`. All three are textbook NSGA-II for an integer encoding. The
only difference from PAA-NSGA-II is the three claimed modifications, which is what makes
the ablation clean.

Both also start from feasible populations. Letting the control start infeasible would
have inflated the apparent benefit of priority-aware seeding.

---

## D6. f5 is log1p-compressed

**Date:** 2026-09-01

Observed median assignment delay per group spans 0.03 to 4248 hours. Raw, it would
dominate crowding distance and the hypervolume indicator purely through scale. Dominance
ranking is scale-invariant so front membership is unchanged; only the geometry used for
diversity and the indicator is affected. Operational reporting converts back with
`expm1`, so the reported delay is in real hours.

---

## D7. NSGA-III uses 35 reference directions, not 70

**Date:** 2026-09-01

`das-dennis` with 5 objectives and `n_partitions=4` produces 70 reference directions,
which exceeds `pop_size=60`. pymoo warns that this makes NSGA-III misbehave. Changed to
`n_partitions=3`, giving 35. Caught by reading the run log rather than by the results
looking wrong, which is the only reason it was caught at all.
