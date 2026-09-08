# FastLead prototype (Streamlit)

Phase 9 of the VTOP Special Project. A decision-time triage interface for IT service
management incidents, built on BPI Challenge 2014.

## Run it

```
cd code
../.venv/Scripts/python.exe -m streamlit run app/streamlit_app.py
```

Opens at http://localhost:8501. No database, no auth, no Node. It reads local artifacts
and nothing else.

## What it does

An incident arrives and someone has to route it within seconds. The app scores the breach
risk at that moment using only what is knowable at that moment, and recommends a group.

The distinguishing feature is not the prediction. It is that every incident shows you which
attributes the model was allowed to look at and which ones the log records but the
decision-maker does not have yet. That is Rule 1 of the paper, made visible.

| Page | What it shows |
|---|---|
| Queue | Every held-out incident scored as of its arrival, sorted by risk, filterable |
| Incident | One incident at its decision moment: observable attributes against the ones recorded later, and what a model reading the closed case would have said instead |
| Assignment | Candidate groups scored on breach history, category expertise and spare capacity, all from training-period history only |
| Evidence | The measurements behind it, read from the artifacts the paper was verified against |

## The two models

Both are trained on the same 37,284 rows with the same learner and the same split. They
differ only in which attributes they may see.

| Model | Attributes | Held-out AUC |
|---|---|---|
| Admissible, Rules 1 and 2 enforced | 17 | 0.8319 |
| Unconstrained, default practice | 20 | 0.9099 |

The gap, 0.0780 AUC or 19.0 per cent of all above-chance signal, is the price of only using
what exists when the decision is taken. Those figures reproduce the paper's 80/20 arm
exactly, because they come from the same pipeline.

## Regenerating the artifacts

```
cd code
../.venv/Scripts/python.exe app/export_ui_models.py
```

Retrains both models from the canonical log and rewrites everything under `app/artifacts/`.
Takes about a minute.

**Do not point this app at `code/serving/artifacts/`.** Those were exported on 2026-09-02,
before the dataset was completed and the date parse corrected, and their registry still
reports a holdout AUC of 0.7927 on 31,236 incidents. Every number in them is superseded.

## Testing

```
cd code
../.venv/Scripts/python.exe app/test_app.py
```

Executes every page headlessly through Streamlit's own test runner and fails on any
exception or empty page. A Streamlit error renders as a red box inside a page that still
returns HTTP 200, so "the server started" is not evidence the interface works.

## If you just cloned this

The two `.joblib` models and the raw BPI 2014 CSVs are NOT in the repo: the models are
build output, and the log is redistributable from its DOI rather than from us. So a fresh
clone gives you the code and the small artifacts, not a runnable app.

To run it you need two things:

1. The three BPI Challenge 2014 CSVs in `code/bpi2014/`, from
   https://doi.org/10.4121/uuid:c3e5d162-0cfd-4bb0-bd82-af5268819c35
2. Then `../.venv/Scripts/python.exe app/export_ui_models.py`, about a minute, which
   writes the models and the queue slice.

`code/input_checksums.json` holds the MD5 of every raw input, and
`evaluation/verify_input_data.py` checks them, so you can confirm you have the same file
we did before you wonder why a number differs.

## Files

```
app/
  streamlit_app.py      the interface
  export_ui_models.py   trains and exports the two models plus the browsing slice
  test_app.py           headless execution test, all four pages
  artifacts/            generated, regenerate rather than edit
```
