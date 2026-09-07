# What moved the headline, and which defect moved it

Written 2026-09-08. Evidence: `code/results_crosslog/attribution.csv`, produced by
`code/evaluation/attribution_run.py`. Every figure below is from that file.

## Why this exists

Four things were corrected at once: a truncated copy of `Detail_Incident.csv` was replaced
with the canonical 46,606-row file, a month-first date parse was fixed, a look-ahead in the
standing queue count was removed, and Rule 2 was implemented as the equation states rather
than as a refit inside every cross-validation fold. The headline admissible AUC moved from
0.7859 to 0.8258 and the chronological arm from 0.6533 to 0.8076.

Correcting four things at once and then telling a story about which one mattered is the same
habit this paper argues against. So each defect was reintroduced on its own, with everything
else held at its corrected value, and the pipeline was run unchanged. Two switches already
existed in the loader; two were added for this study and default off.

## The measurement

| Variant | Incidents | Unconstrained | Admissible | Chronological | Adm. delta | Chron. delta |
|---|---|---|---|---|---|---|
| corrected (what the paper reports) | 46,605 | 0.9050 | 0.8258 | 0.8076 | . | . |
| truncated data only | 31,237 | 0.9032 | 0.8215 | 0.7769 | -0.0043 | -0.0307 |
| month-first dates only | 46,605 | 0.8802 | 0.7869 | 0.7927 | -0.0389 | -0.0149 |
| queue look-ahead only | 46,605 | 0.9051 | 0.8273 | 0.8021 | +0.0015 | -0.0055 |
| Rule 2 as fold refit only | 46,605 | 0.9050 | 0.8301 | 0.8077 | +0.0043 | +0.0001 |
| all four together | 31,237 | 0.8771 | 0.7996 | 0.7235 | -0.0262 | -0.0841 |

## What it says

**The date defect is what moved the random split.** On its own it costs 0.0389 AUC, which is
almost the whole 0.0399 the headline moved. The mechanism is not subtle: `Open_Month`,
`Open_DayOfWeek` and `Is_Weekend` are features, and on the 41 per cent of rows where both
leading date fields are at most twelve they were being computed from a transposed date. The
model was being handed three corrupted attributes and it lost accuracy accordingly.

**Truncation is what moved the chronological arm.** Cutting the incident file to its first
31,237 rows costs 0.0307 on the chronological split against 0.0043 on the random one, a factor
of seven. That is the expected shape rather than a surprise: the file is ordered by incident
ID, which correlates with open time, so a prefix is close to a truncation in time. The
chronological arm trains on the earliest 70 per cent and tests forward, so removing the last
third of the timeline removes most of what it was supposed to be tested on.

**The two leaks behave like leaks, and they are small.** Both the queue look-ahead and the
fold-refitted encoding *raise* the admissible score, by 0.0015 and 0.0043. That is the correct
direction and it is worth stating that it is the correct direction: a leak that lowered the
score would mean the diagnosis was wrong. Their magnitude also matters. Together they are
worth under 0.006 AUC, so neither was ever the reason the headline number was what it was,
and the earlier draft's implication that Rule 2 was load-bearing for the reported figure is
not supported. Rule 2 is load-bearing for the *claim*, because the paper defines admissibility
by that equation and a fold refit does not satisfy it, but it is not load-bearing for the
number.

## What it does not say

**The reconstruction is partial, and the gap is reported rather than closed.** Reintroducing
all four defects gives 0.7996 and 0.7235. The figures actually published by the earlier draft
were 0.7859 and 0.6533. So this study accounts for 0.0262 of the 0.0399 random-split move and
0.0841 of the 0.1543 chronological move: roughly two thirds of the first and just over half of
the second.

Three things are known to differ beyond the four defects and none of them can be recovered by
a switch. The OpenMP thread count was not pinned then and is worth up to 0.0015 on its own
(`thread_determinism.csv`). The earlier run used a different feature-engineering path in
places that have since been rewritten rather than parameterised. And the group-encoding
smoothing was applied over a different candidate set, because the group filter itself moved
when the data was completed: 50 groups became 87.

The honest summary is therefore narrower than a full decomposition. The date defect explains
most of the random-split move, truncation explains most of the chronological move, the two
leaks are real but small, and about a third of the total change belongs to code that no longer
exists in a form that can be re-run. Claiming a clean four-way attribution would be inventing
precision the experiment does not have.

## How to reproduce

```
python evaluation/attribution_run.py
```

Each variant writes to its own `results_bpi_leakfree_attrib_<name>/` directory, so the
canonical `results_bpi_leakfree_7030/` is never touched. The switches are
`BPI_TRUNCATE`, `BPI_LEGACY_DATES`, `BPI_QUEUE_UNRESOLVED` and `BPI_RULE2`, all off by
default and none used by any published run.
