# Model serving

This is what makes the CRM use the **actual trained models** instead of an imitation of them.

## Run it

```
cd "code/serving"
python export_models.py                          # once, writes artifacts/
python -m uvicorn app:app --port 8000            # leave running
```

The Next.js app reads `MODEL_SERVICE_URL` from `prototype/web/.env.local`
(`http://127.0.0.1:8000`). Nothing else needs to change.

## What `export_models.py` does

It refits the winning configuration of each model, checks that it reproduces the
published held-out AUC on the same split and seed, measures what the CRM feature adapter
costs, and persists the estimator.

```
model_a_conversion   full-feature 0.8185  ->  adapter 0.6885   (8 of 19 features live)
model_b_sla          full-feature 0.7927  ->  adapter 0.7097   (9 of 17 features live)
```

Both full-feature figures matched the published ones to four decimal places, so the
served model is provably the one the results tables describe. **The model that is
persisted is the train-split model, not a 100 per cent refit**, so the number reported
and the model running are the same object with no gap to explain.

**The adapter figure is the one this system may claim.** See the next section.

| | Model A, conversion | Model B, SLA breach |
|---|---|---|
| Dataset | UCI Bank Marketing, 41,188 rows | BPI Challenge 2014, 31,236 incidents |
| Config | `A3_tuned_no_duration` | `XGBoost_tuned`, leak-free feature set |
| Full-feature held-out AUC | 0.8185 | 0.7927 |
| **Adapter AUC, what the app can claim** | **0.6885** | **0.7097** |
| CV AUC | 0.7981 | 0.7932 |
| Version string | `xgb-tuned-a3-nodur` | `xgb-tuned-leakfree` |

`duration` is excluded from Model A because call length is only known after the call.
Keeping it reaches 0.9537 and is not deployable. Model B's feature set is the leak-free
one: the 0.8958 from the original pipeline was 25 per cent leakage.

## The adapter AUC, and why it is the only claimable number

A 7-model council review on 2026-09-02 returned one unanimous attack: the CRM cannot
supply every column, the adapter fills the rest with the training median, and the
published AUC therefore describes a model running on inputs the deployed system never
sees. Every seat prescribed the same fix, so `export_models.py` runs it. It re-scores
**the same held-out set** with exactly the columns the adapter cannot fill clamped to
the training median.

| | full-feature AUC | adapter AUC | live features |
|---|---|---|---|
| Model A, conversion | 0.8185 | **0.6885** | 8 of 19 |
| Model B, SLA breach | 0.7927 | **0.7097** | 9 of 17 |

Model A loses 0.130. That is the price of a CRM that does not collect age, marital
status, education, credit history or the five macroeconomic indicators, and it is large.
Model B loses 0.083, because the CRM can fill the features that matter most, including
the assigned rep's own breach history.

**What may be claimed:** the full-feature AUCs describe the models on their own
benchmark data and belong in the model chapter. The adapter AUCs describe what the
prototype does and are the only figures that may be attached to a screenshot of the app.
Quoting 0.8185 as the app's accuracy is the misrepresentation the council named.

## Priority is deliberately not passed to Model B

BPI 2014 has no contractual SLA, so its breach label is handle time above twice the
median for that incident's **own** priority band. A CRM SLA is an absolute clock: 4
hours, 24 hours, 72 hours. Different constructs, and the effect is visible. The trained
model gives a CRM high-priority lead a **lower** breach probability than operational
reality, because in BPI the urgent band is the one with the shortest work. That inverted
signal would then feed the assignment objectives.

Inverting the mapping to make the number look right would be fabricating a calibration
no data supports. So the three priority columns are clamped, and the cost was measured:
adapter AUC **0.7118** with priority passed through, **0.7097** with it masked. Two
thousandths. The inverted signal is not worth two thousandths.

Priority still governs the lead completely. It sets the SLA deadline itself and it is
one of the five objectives the optimiser balances. It simply does not enter this model.

## Target-encoding stability, disclosed

`Group_Breach_Rate_TE` is Model B's strongest feature and it is a target encoding, so it
is only as good as the volume behind it. In the training split there are **114
assignment groups, median 29 cases each, and 42 per cent sit below the k=20 smoothing
threshold**, meaning they are pulled substantially toward the corpus prior. In the CRM a
representative stands in for a group, and a rep with three closed leads is roughly 87
per cent prior. That is the smoothing working as designed, but it means the feature is
**not personalised for a new rep** and should not be described as such.

## The feature adapter, stated plainly

Neither public dataset has the CRM's columns, so each endpoint maps the CRM fields that
have a genuine counterpart and holds the rest at the **training-set median** stored in
the artifact.

**Model A, 8 of 19 features come from the request.** `job` (lead job title bucketed into
the 12 UCI occupation categories), `contact` (cellular when a phone number exists, else
telephone), `month` and `day_of_week` (creation timestamp), `campaign`, `previous`,
`pdays` and `poutcome` (the lead's own activity history). Age, marital status,
education, credit default, housing, loan and the five macroeconomic indicators are held
at the median, because the CRM does not collect them.

**Model B, 9 of 17 features come from the request.** The five temporal features, queue
length, assignment delay, and the two target-encoded group features taken from the
assigned rep's own history with the same k=20 smoothing used in training. CI type, CI
subtype, WBS, category and alert status are held at the median, and so are priority,
impact and urgency, for the reason given above.

Every response returns `features_from_request` and `features_at_training_median`, and
the handbook page in the app displays both. The honesty is machine readable.

## Provenance is in the database

`predictions.model_version` records which path produced every score:

- `xgb-tuned-a3-nodur` or `xgb-tuned-leakfree` means the trained model scored it
- `proto-v1-fallback` means the service was unreachable and the calibrated function in
  `web/lib/scoring.ts` scored it

A screenshot can therefore always be traced back to a real model call or an admitted
approximation. `model_registry` carries the same versions with their AUCs and notes.

## Files

```
serving/
|-- export_models.py   refit, verify against the published AUC, persist
|-- app.py             FastAPI service, the two endpoints and /health
|-- artifacts/         model_a.joblib, model_b.joblib, registry.json
|-- export_run.log     the verification output from the last export
`-- README.md          this file
```

Back to parent: `../../CLAUDE.md`
