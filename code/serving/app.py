"""FastLead model service: serves the two trained XGBoost models to the CRM.

WHY A SEPARATE SERVICE. XGBoost is a Python library. The CRM is a Next.js app running
on Node. There is no honest way to run the trained booster inside the Node runtime, so
the models are served over HTTP and the app calls them. `web/lib/scoring.ts` falls back
to its calibrated functions when this service is not running, and every prediction it
writes records WHICH path produced it, so a demo can never silently pass off a fallback
score as a model score.

THE FEATURE ADAPTER, STATED PLAINLY. The models were trained on public datasets whose
columns are not the CRM's columns. Each adapter below maps every CRM field that has a
genuine counterpart, and fills the rest with the training-set median held in the
artifact. `/health` and every response report exactly how many features came from real
request data versus how many were held at the median, so the honesty is machine
readable and not just a comment.

WHAT THAT COSTS, MEASURED. Clamping the unfillable columns is not free, and pretending
otherwise was the single unanimous objection from a 7-model review on 2026-09-02. So
`export_models.py` measures it directly: it re-scores the same held-out set with exactly
these columns clamped. Model A falls from 0.8185 to 0.6885. Model B falls from 0.7927 to
0.7118. Those adapter figures, not the headline ones, are what this service can claim.

Run it:
    python -m uvicorn app:app --port 8000
"""

from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import joblib
import numpy as np
import pandas as pd
import xgboost as xgb
from fastapi import FastAPI
from pydantic import BaseModel, Field

ART = Path(__file__).parent / "artifacts"
SMOOTH = 20  # identical to training, so a rep with few closed leads pulls to the prior

# These two artifacts are produced by `export_models.py` in this same directory and are
# never fetched from anywhere else, so joblib's pickle load is not reading untrusted input.
A = joblib.load(ART / "model_a.joblib")
B = joblib.load(ART / "model_b.joblib")
REGISTRY = joblib.os.fspath(ART / "registry.json")

app = FastAPI(title="FastLead model service", version="1.0")


# ------------------------------------------------------------------ helpers
def _contributions(model, row: pd.DataFrame, top: int = 5):
    """Exact TreeSHAP contributions in log-odds space, largest magnitude first."""
    booster = model.get_booster()
    dm = xgb.DMatrix(row, feature_names=list(row.columns))
    shap = booster.predict(dm, pred_contribs=True)[0][:-1]  # drop the bias term
    order = np.argsort(-np.abs(shap))[:top]
    return [{"feature": str(row.columns[i]), "effect": round(float(shap[i]), 4)} for i in order]


def _ordinal(artifact, column: str, value: str) -> float:
    """Encode one categorical value with the OrdinalEncoder fitted at training time."""
    idx = artifact["cat_cols"].index(column)
    cats = list(artifact["encoder"].categories_[idx])
    return float(cats.index(value)) if value in cats else float(
        artifact["medians"].get(column, 0.0))


# ------------------------------------------------------------------ Model A
# The 12 occupation buckets in the UCI dataset. A CRM job title is matched to one of
# them by keyword; anything unmatched becomes "unknown", which is a real category the
# model saw during training rather than a missing value.
JOB_KEYWORDS = [
    ("management", ["ceo", "cto", "coo", "cfo", "founder", "director", "head", "vp",
                    "president", "manager", "lead", "chief", "partner", "owner"]),
    ("admin.", ["admin", "assistant", "coordinator", "clerk", "secretary", "operations"]),
    ("technician", ["engineer", "developer", "technician", "architect", "analyst",
                    "scientist", "programmer", "devops", "sre"]),
    ("services", ["sales", "support", "service", "account", "customer", "success",
                  "marketing", "recruiter"]),
    ("self-employed", ["freelance", "consultant", "contractor", "self"]),
    ("entrepreneur", ["entrepreneur", "co-founder", "cofounder"]),
    ("student", ["student", "intern", "trainee"]),
    ("retired", ["retired"]),
    ("unemployed", ["unemployed", "between roles"]),
    ("blue-collar", ["driver", "technicianfield", "operator", "worker", "labour", "labor"]),
    ("housemaid", ["housemaid", "domestic"]),
]


def _job_bucket(job_role: str | None) -> str:
    if not job_role:
        return "unknown"
    t = job_role.lower()
    for bucket, words in JOB_KEYWORDS:
        if any(w in t for w in words):
            return bucket
    return "unknown"


class ConversionRequest(BaseModel):
    """Everything the CRM knows about a lead at the moment it arrives."""
    job_role: str | None = None
    has_phone: bool = False
    has_email: bool = False
    created_at: datetime
    # Prior engagement with this lead, which is what the campaign history columns encode.
    prior_contacts: int = Field(0, ge=0, description="activities logged before now")
    prior_outcome: Literal["success", "failure", "nonexistent"] = "nonexistent"
    days_since_last_contact: int | None = None


MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
DOWS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

# The eight columns the adapter genuinely drives from request data. Everything else in
# the 19-feature vector is held at its training median, which is reported per response.
A_DRIVEN = ["job", "contact", "month", "day_of_week", "campaign", "pdays", "previous", "poutcome"]


@app.post("/score/conversion")
def score_conversion(r: ConversionRequest):
    row = {f: float(A["medians"][f]) for f in A["features"]}

    row["job"] = _ordinal(A, "job", _job_bucket(r.job_role))
    # In the source data, cellular contacts convert markedly better than landline. A
    # lead we can reach on a phone is the closest CRM analogue of a cellular contact.
    row["contact"] = _ordinal(A, "contact", "cellular" if r.has_phone else "telephone")
    row["month"] = _ordinal(A, "month", MONTHS[r.created_at.month - 1])
    row["day_of_week"] = _ordinal(A, "day_of_week", DOWS[min(r.created_at.weekday(), 4)])
    row["campaign"] = float(r.prior_contacts + 1)   # this contact included, as in training
    row["previous"] = float(r.prior_contacts)
    row["pdays"] = float(999 if r.days_since_last_contact is None
                         else min(r.days_since_last_contact, 999))
    row["poutcome"] = _ordinal(A, "poutcome", r.prior_outcome)

    X = pd.DataFrame([row])[A["features"]]
    p = float(A["model"].predict_proba(X)[0, 1])
    return {
        "probability": p,
        "contributions": _contributions(A["model"], X),
        "model_name": "model_a_conversion",
        "model_version": "xgb-tuned-a3-nodur",
        "source": "trained_model",
        "features_from_request": len(A_DRIVEN),
        "features_at_training_median": len(A["features"]) - len(A_DRIVEN),
    }


# ------------------------------------------------------------------ Model B
#
# PRIORITY IS DELIBERATELY NOT PASSED TO THIS MODEL.
#
# BPI 2014 has no contractual SLA, so its breach label is "handle time above twice the
# median for that incident's OWN priority band". A CRM SLA is an absolute clock: 4 hours
# for high, 24 for medium, 72 for low. Those are different constructs, and the effect is
# visible: the trained model gives a CRM high-priority lead a LOWER breach probability
# than operational reality, because in BPI the urgent band is the one with the shortest
# work. That inverted signal would then feed the assignment objectives.
#
# Inverting the mapping to make the number look right would be fabricating a calibration
# that no data supports. Clamping the three priority columns to the training median is
# the honest option, and `export_models.py` measured what it costs: adapter AUC 0.7118
# with priority passed through, 0.7097 with it masked. Two thousandths. The inverted
# signal is not worth two thousandths.
#
# Priority still governs the lead entirely: it sets the SLA deadline itself (4/24/72
# hours) and it is one of the five objectives the optimiser balances. It just does not
# enter this particular model.
B_DRIVEN = ["Open_Hour", "Open_DayOfWeek", "Is_Weekend", "Is_Business_Hours",
            "Open_Month", "Queue_Length_At_Open", "Assignment_Delay_Hours",
            "Group_Breach_Rate_TE", "Group_Volume_TE"]


class SlaRequest(BaseModel):
    # Accepted so the caller does not have to know this model ignores it, and so
    # reinstating it later is a one line change rather than an API change.
    priority: Literal["low", "medium", "high"]
    created_at: datetime
    open_leads_in_system: int = Field(0, ge=0, description="queue depth at this moment")
    # The assigned representative's own history, which stands in for the assignment
    # group target encoding. This is Model B's single strongest feature.
    rep_closed_leads: int = Field(0, ge=0)
    rep_breached_leads: int = Field(0, ge=0)


@app.post("/score/sla")
def score_sla(r: SlaRequest):
    row = {f: float(B["medians"][f]) for f in B["features"]}
    # Priority, Impact and Urgency stay at the training median. See the note above.
    created = r.created_at if r.created_at.tzinfo else r.created_at.replace(tzinfo=timezone.utc)
    row["Open_Hour"] = float(created.hour)
    row["Open_DayOfWeek"] = float(created.weekday())
    row["Is_Weekend"] = float(created.weekday() >= 5)
    row["Is_Business_Hours"] = float(8 <= created.hour <= 18)
    row["Open_Month"] = float(created.month)
    row["Queue_Length_At_Open"] = float(r.open_leads_in_system)

    delay = (datetime.now(timezone.utc) - created).total_seconds() / 3600
    row["Assignment_Delay_Hours"] = float(max(delay, 0.0))

    # Same smoothing as training: a rep with few closed leads is pulled toward the
    # corpus breach rate rather than trusted on a handful of outcomes.
    prior = float(B["group_prior"])
    n = r.rep_closed_leads
    observed = (r.rep_breached_leads / n) if n else prior
    row["Group_Breach_Rate_TE"] = (observed * n + prior * SMOOTH) / (n + SMOOTH)
    row["Group_Volume_TE"] = float(n)

    X = pd.DataFrame([row])[B["features"]]
    p = float(B["model"].predict_proba(X)[0, 1])
    return {
        "probability": p,
        "contributions": _contributions(B["model"], X),
        "model_name": "model_b_sla",
        "model_version": "xgb-tuned-leakfree",
        "source": "trained_model",
        "features_from_request": len(B_DRIVEN),
        "features_at_training_median": len(B["features"]) - len(B_DRIVEN),
        "note": "priority is intentionally not passed to this model, see app.py",
    }


@app.get("/health")
def health():
    import json
    reg = json.loads(Path(REGISTRY).read_text(encoding="utf-8"))
    return {
        "status": "ok",
        "exported_at": reg["exported_at"],
        "claimable_metric": "adapter_auc, the held-out AUC with every column this "
                            "service cannot fill clamped to the training median",
        "models": [
            {**m,
             "features_from_request": len(A_DRIVEN) if m["name"].endswith("conversion") else len(B_DRIVEN)}
            for m in reg["models"]
        ],
    }
