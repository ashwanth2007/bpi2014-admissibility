# Which numbers in this folder are real

## Use these

- `summary_full.csv` and `metrics_full.csv` are the full protocol: 5 batches, 30 seeded runs,
  150 generations, population 60. Written 2026-09-01 23:07.
- `significance_full.json` is the **only** authoritative statistics file.

## Do NOT use these

- **`full_run.log` contains TWO different runs concatenated, separated by a block of NUL bytes at
  line 74.** Lines 1 to 72 (wall time 1956s, no NSGA-III reference-direction warning) match the CSVs
  on disk. Lines 74 to 501 are a **stale remnant of an earlier run that still had the 70-reference-
  direction NSGA-III misconfiguration**, and its Wilcoxon block reports the OPPOSITE conclusion:
  `hypervolume mean diff -0.00602 p=1.327e-07 -> Standard better SIGNIFICANT`.
  Anyone who tails this log gets the wrong answer from a broken control.
- `*_smoke.csv` are one-minute sanity runs (1 batch, 2 runs, 20 generations). The smoke numbers
  disagree with the full protocol and must never be quoted.
- `code/phase4/docs/build-log.md` still says the full protocol is "left to do". It has run. That
  file is stale on this point.

## The result itself

PAA-NSGA-II does **not** beat standard NSGA-II on hypervolume (mean diff +0.00515, p = 0.149).
NSGA-III, included only as a control, beats it 0.1327 to 0.0922. Two of the three claimed operator
modifications fail ablation: removing adaptive crossover raises hypervolume to 0.0944, removing
SLA-aware mutation raises it to 0.0931. Only priority-aware initialisation earns its place
(removing it drops hypervolume to 0.0819).

This is reported as a negative result. See `research/scope-decision-2026-09-06.md`.
