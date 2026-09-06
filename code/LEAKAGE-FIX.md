# Phase 3: target leakage found and fixed

**Date:** 2026-09-01
**Applies to:** `bpi2014_ml_pipeline.py` (v1)
**Replacement:** `bpi2014_pipeline_leakfree.py` (v2), outputs in `results_bpi_leakfree/`

The v1 pipeline structure is sound. The 5/8/12/26 feature ladder, 5-fold stratified CV,
calibration with a Brier score, and SHAP are the right instincts and all of it carries over. The
problem is two specific defects that inflate the score. Both are ordinary and both are fixed here.

---

## The two defects

### 1. Post-hoc features

The label is a function of total case handle time:

```python
inc["SLA_Breached"] = (inc["Handle_Time_Hours"] > inc["SLA_Threshold_Hours"]).astype(int)
```

Anything that grows as a case runs longer is therefore a proxy for the label. Six of the 26
features are measured across the **whole closed case**, so none of them exist at the moment the
incident is assigned, which is the decision point the framework is supposed to optimise:

| Feature | Why it leaks |
|---|---|
| `Total_Activity_Events` | Count of every activity event over the case's whole life. Longest cases have the most events. |
| `# Reassignments` | Total reassignments over the case's life. |
| `Num_Groups_Touched` | Distinct groups over the case's whole life. |
| `Has_Reopen` | A reopen happens late in a case. |
| `Interaction_Count`, `FCR_Rate` | Aggregated from the interaction log across the full case. |
| `Num_Related_*` | Final tallies on the closed record, not the value visible at open. |

### 2. Target encoding computed before the split

At line 163 of v1:

```python
group_stats = df.groupby("First_Assignment_Group").agg(
    Group_Breach_Rate=("SLA_Breached", "mean"),           # mean of the target
    Group_Avg_Handle_Time=("Handle_Time_Hours", "mean"),  # mean of what defines the target
    ...)
df = df.merge(group_stats, on="First_Assignment_Group", how="left")
```

The train/test split does not happen until line 330. So `Group_Breach_Rate` is the mean of the
target computed over the **entire dataset, including the test rows and including each row's own
label**. That is test contamination and self-inclusion at the same time.

---

## What the correction is worth

Same target, same models, same hyperparameters, same seed. Only the feature admissibility and the
encoding order change.

| Configuration | Features | CV-AUC | Test-AUC | F1 | Precision | Recall |
|---|---|---|---|---|---|---|
| Contaminated (v1 style) | 20 | 0.8833 | **0.8833** | 0.7382 | 0.6870 | 0.7977 |
| Leak-free, random split | 17 | 0.7881 | **0.7877** | 0.6381 | 0.6046 | 0.6754 |
| Leak-free, chronological holdout | 17 | n/a | **0.6533** | 0.5072 | 0.3553 | 0.8858 |

Reproducing v1's exact 26-feature configuration gives 0.8973, matching its published 0.8958 to
within 0.0015, so this is a like-for-like comparison and not a reimplementation artifact.

**About 25 per cent of all signal above chance came from the leak.**

The honest headline for this task is **0.7877 AUC**, and the honest forward-looking number is
**0.6533**.

---

## What v2 does differently

**Admissible features only.** 15 attributes that exist on the record at open or are observable at
the assignment moment: Priority, Impact, Urgency, five temporal features, `Queue_Length_At_Open`
(computed from other cases only), `Assignment_Delay_Hours` (elapsed wait before routing, which is
observable at assignment and is not total handle time), and four configuration and category codes.

**Group statistics fitted inside the training fold.** `Group_Breach_Rate_TE` is smoothed target
encoding with a prior weight of 20, refitted separately inside every CV fold and again on the outer
training set. This is the legitimate form of the Group B representative-performance signal the
specification asks for. It is worth about 0.009 AUC when computed correctly, versus the much larger
apparent gain when computed wrongly.

**A chronological holdout.** Event log data is temporal, and a random split lets the model see 2014
cases while training. Training on the earliest 80 per cent of arrivals and testing on the latest 20
gives 0.6533. Two things are corrected there so the test is causal: the SLA threshold is recomputed
from training-period medians only, and the operating cut point is tuned on a validation slice held
out from the end of the training period, never on test.

**Per-attribute justifications.** `results_bpi_leakfree/feature_justifications_leakfree.csv` gives a
written justification for every included attribute plus a `Known_at_assignment_time` column, and
lists every excluded attribute with the reason. Ma'am asked for exactly this on 24 August.

---

## The distribution shift finding, worth raising with ma'am

The gap between 0.7877 (random split) and 0.6533 (chronological) is not a bug. The Rabobank process
genuinely got faster: median handle time falls from 4.73 hours across the 2012 to 2013 training
period to 3.37 hours in the 2014 test period, and the breach rate drops from 36.6 to 30.8 per cent.

Two consequences:

1. **AUC is threshold-free, so the drop from 0.788 to 0.653 is real ranking degradation under
   drift.** A model trained on 2012 to 2013 ranks 2014 incidents materially worse.
2. **The F1 collapse is separate and fixable.** At the default p>0.5 cut point, F1 on the future was
   0.19 with recall 0.11. Retuning the cut point on training-period data alone lifts it to 0.51 with
   recall 0.89. So the operating threshold must be recalibrated as the process changes, and that is
   a real operational finding, not a failure.

This is a genuinely useful result for the paper. It is the kind of validation ma'am asked for on
24 August, and it says something about deployed CRM and ITSM models that a random-split number hides.

---

## Environment note

`shap` 0.45.1 cannot parse XGBoost 3.2.0's model JSON: it reads `base_score` as the string
`"[5E-1]"` and fails on float conversion. This is a library version incompatibility, not a data
problem. `shap >= 0.46` fixes it but requires numpy 2, which breaks scipy in this Anaconda
environment. v2 therefore runs the explainability layer on LightGBM, which is the same
gradient-boosted tree family and scores within 0.006 AUC of XGBoost here, so the attributions are
representative. Top drivers: `Group_Breach_Rate_TE`, `WBS_Encoded`, `Assignment_Delay_Hours`,
`Group_Volume_TE`, `Queue_Length_At_Open`.

---

## Two things this does not fix

**The 26 attributes are still not the specification's 41.** The spec's Group A customer features do
not exist in BPI 2014 at all, and Groups B and C are only partly derivable.

**This is Phase 3, Model B only.** Model A (lead conversion), Model C (representative success), and
all of Phase 4 PAA-NSGA-II, which is the declared novel core of the project, are still unwritten.
