"""Per-log specifications for the admissibility protocol.

WRITTEN BEFORE ANY AUC WAS COMPUTED ON LOGS 2 TO 5. That ordering is the point. Choosing a
decision point or a label after seeing what each choice scores is the same defect this paper
is about, and the project has already rejected two such moves (swapping an objective after
observing a loss, and renaming a method to keep a win).

Every log needs four choices, and none of them is mechanical:

  1. THE DECISION POINT t_dec. The instant a decision maker commits a case to a resource.
     Everything observable at that instant is admissible; everything recorded later is not.
  2. THE OUTCOME. A duration threshold, since none of these logs carries a contractual
     service level. Held at the BPI 2014 form throughout: twice the median within a stratum.
  3. THE STRATUM. BPI 2014 has an explicit priority. The others do not, so a substitute is
     named per log and its arbitrariness is stated rather than hidden.
  4. THE GROUP. The entity whose history the Rule 2 encoding accumulates over.

Where a choice is genuinely arbitrary it is marked ARBITRARY and gets a sensitivity arm.
Where a log cannot support the protocol honestly, that is recorded here rather than forced.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class LogSpec:
    slug: str
    name: str
    domain: str
    source: str                      # DOI or URL
    filename: Optional[str]          # under code/logs_raw/, None for the CSV-based BPI 2014

    decision_point: str              # prose, the definition of t_dec
    outcome: str                     # prose, how the label is derived
    stratum: str                     # prose, what the median is taken within
    group: str                       # prose, the Rule 2 encoding key
    group_level: str                 # "team" or "individual"

    # Machine-readable forms of the prose above. The prose is what a reader checks;
    # these are what the code executes. They must agree, and the runner prints both.
    decision_kind: str = "first_event"   # first_event | first_human_resource | after_activity
    decision_activity: str = ""          # used when decision_kind == after_activity
    stratum_col: str = ""                # column to take the median within; blank = global

    # Trace attributes admissible at t_dec. Everything else the log carries is either
    # recorded after t_dec or is a function of the completed case.
    admissible_case_attrs: list = field(default_factory=list)
    # Case-level quantities that ARE recorded but are not knowable at t_dec. These are the
    # contaminated arm: the measurement of what admissibility costs is the gap between a
    # model given these and a model denied them.
    inadmissible_features: list = field(default_factory=list)

    caveats: list = field(default_factory=list)



# ---------------------------------------------------------------------------------------
# STRUCTURAL CORRECTION, 2026-09-07, made BEFORE any AUC was computed on these three logs.
#
# The specs below originally named trace-level attributes (case:Age, case:speciality,
# case:amount). Inspecting the published XES showed that Sepsis, Hospital Billing and Road
# Traffic Fines carry NO trace attributes at all beyond concept:name: every case-descriptive
# field is written onto the FIRST event. The loader now lifts those with a `first:` prefix.
#
# This is a correction to WHERE a field is read from, not to WHICH fields are admissible.
# The admissibility judgement below is unchanged in substance and was still made from the
# log's structure rather than from any measured score.
#
# Hospital Billing turned out to carry a genuine trap that is worth the paper's space:
# isClosed, isCancelled, blocked and state sit on the NEW event, which is the first event of
# the case. A naive reading admits them because they are "available at case creation". They
# are case-outcome fields written retrospectively: a billing case cannot be closed at the
# instant it is created. They are inadmissible, and they are exactly the kind of attribute
# the protocol exists to catch.
# ---------------------------------------------------------------------------------------

BPI2014 = LogSpec(
    slug="bpi2014",
    name="BPI Challenge 2014, Rabobank incident management",
    domain="IT service management",
    source="10.4121/uuid:3cfa2260-f5c5-44be-afe1-b70d35288d6d",
    filename=None,
    decision_point="The first Assignment or Reassignment activity for the incident.",
    outcome="Handle time greater than twice the median handle time within the priority band.",
    stratum="Priority, 1 to 5, recorded on the incident at open time.",
    group="First assignment group.",
    group_level="team",
    admissible_case_attrs=["Priority", "Impact", "Urgency", "CI Type", "CI Subtype",
                           "Service Component WBS", "Category", "Alert Status"],
    inadmissible_features=["# Reassignments", "Num_Groups_Touched", "Total_Activity_Events",
                           "Has_Reopen", "Num_Related_Incidents"],
    caveats=["Alert Status is uniformly 'closed' in the published extract and carries no "
             "usable breach signal, which is why the label is derived from duration.",
             "Assignment groups are teams, not individuals, so representative-level "
             "features are not estimable on this log."],
)

BPIC2017 = LogSpec(
    slug="bpic2017",
    name="BPI Challenge 2017, Dutch financial institute loan applications",
    domain="Financial services, consumer lending",
    source="10.4121/uuid:5f3067df-f10b-45da-b98b-86ae4c7a310b",
    filename="BPI Challenge 2017.xes.gz",
    decision_point=(
        "The first event carrying an org:resource that is not the automated system. This is "
        "the moment the application is first committed to a named human, and it is the "
        "closest analogue in this log to BPI 2014's first assignment. "
        "DELIBERATELY NOT A_Accepted: that activity fires exactly 31,509 times, once in "
        "every single case, so it is a mandatory pass-through state meaning 'admitted for "
        "processing' rather than a decision, and it carries no discriminative information "
        "as an occurrence."),
    outcome=(
        "Case duration, last event minus first event, greater than twice the median duration "
        "within the stratum. Held identical in form to BPI 2014 so the two costs are "
        "comparable. The terminal offer states are NOT used as the outcome: they are the "
        "label of a different task, they are not a clean partition over time because a "
        "cancelled case can be restarted, and using them would make this a replication of "
        "the Teinemaa benchmark rather than of this paper's protocol."),
    decision_kind="first_human_resource",
    stratum_col="case:LoanGoal",
    stratum="case:LoanGoal, 13 values. ARBITRARY: this log has no priority field. Sensitivity arm uses case:ApplicationType.",
    group="org:resource at the decision point. 149 distinct values.",
    group_level="individual",
    admissible_case_attrs=["case:LoanGoal", "case:ApplicationType", "case:RequestedAmount"],
    inadmissible_features=["n_events", "n_offers", "n_resources_touched", "n_workflow_events",
                           "was_returned", "was_incomplete"],
    caveats=["This is the log the Limitations section asks for: it identifies individuals, "
             "not teams, so the Rule 2 encoding runs at representative level here.",
             "Smoothing alpha = 20 was never tuned and is likely too small at 149-resource "
             "cardinality. Report a sensitivity sweep rather than a single value.",
             "Terminal offer states are not mutually exclusive over time (Weytjens and De "
             "Weerdt). Not used as an outcome here, but it rules out the obvious alternative."],
)

SEPSIS = LogSpec(
    slug="sepsis",
    name="Sepsis Cases, hospital emergency care pathways",
    domain="Healthcare, acute treatment",
    source="10.4121/uuid:915d2bfb-7e84-49ad-a286-dc35f063a460",
    filename="Sepsis Cases - Event Log.xes.gz",
    decision_point="The first event after ER Registration, which is the first clinical action taken on the patient.",
    outcome="Case duration greater than twice the median within the stratum.",
    decision_kind="after_activity",
    decision_activity="ER Registration",
    stratum_col="first:Diagnose",
    stratum="first:Diagnose. ARBITRARY: no priority field. Sensitivity arm uses a single global median.",
    group="org:group, the treating unit, surfaced as the event resource.",
    group_level="team",
    admissible_case_attrs=[
        # Recorded at ER Registration, which precedes the decision point by construction.
        "first:Age", "first:Diagnose", "first:InfectionSuspected", "first:DisfuncOrg",
        "first:Hypotensie", "first:Hypoxie", "first:Oligurie", "first:Infusion",
        "first:SIRSCriteria2OrMore", "first:SIRSCritHeartRate", "first:SIRSCritTachypnea",
        "first:SIRSCritTemperature", "first:SIRSCritLeucos",
        "first:DiagnosticBlood", "first:DiagnosticArtAstrup", "first:DiagnosticIC",
        "first:DiagnosticSputum", "first:DiagnosticLiquor", "first:DiagnosticUrinaryCulture",
        "first:DiagnosticUrinarySediment", "first:DiagnosticLacticAcid",
        "first:DiagnosticECG", "first:DiagnosticXthorax", "first:DiagnosticOther",
    ],
    inadmissible_features=["n_events", "n_resources_touched", "n_activities"],
    caveats=["Small log, roughly 1,050 cases. Bootstrap intervals will be wide and that "
             "must be reported rather than smoothed over.",
             "Many trace attributes are clinical booleans recorded at triage; each needs "
             "checking for whether it is knowable at t_dec rather than assumed."],
)

HOSPITAL_BILLING = LogSpec(
    slug="hospital",
    name="Hospital Billing, financial settlement of treatment episodes",
    domain="Healthcare administration",
    source="10.4121/uuid:76c46b83-c930-4798-a1c9-4be94dfeb741",
    filename="Hospital Billing - Event Log.xes.gz",
    decision_point="The NEW activity that opens the billing case.",
    outcome="Case duration greater than twice the median within the stratum.",
    decision_kind="first_event",
    stratum_col="first:speciality",
    stratum="first:speciality. ARBITRARY: no priority field.",
    group="org:resource.",
    group_level="individual",
    admissible_case_attrs=["first:speciality", "first:caseType", "first:diagnosis"],
    inadmissible_features=["n_events", "n_resources_touched", "n_activities",
                           # These four sit on the NEW event, the first event of the case,
                           # and a careless reading admits them. They are written back after
                           # the case resolves: a billing case is not closed at creation.
                           "first_isClosed", "first_isCancelled", "first_blocked", "first_state"],
    caveats=["100,000 cases, the largest log in the set. Runtime, not statistics, is the "
             "constraint here."],
)

TRAFFIC_FINES = LogSpec(
    slug="trafficfines",
    name="Road Traffic Fine Management, Italian local police",
    domain="Public administration",
    source="10.4121/uuid:270fd440-1057-4fb9-89a9-b699b47990f5",
    filename="Road_Traffic_Fine_Management_Process.xes.gz",
    decision_point="The Create Fine activity.",
    outcome="Case duration greater than twice the median within the stratum.",
    decision_kind="first_event",
    stratum_col="first:vehicleClass",
    stratum="first:vehicleClass. ARBITRARY: no priority field; amount is used as a feature rather than as the stratum so the label does not partition on a predictor.",
    group="org:resource on the creating event.",
    group_level="individual",
    admissible_case_attrs=["first:amount", "first:article", "first:vehicleClass", "first:points"],
    inadmissible_features=["n_events", "n_resources_touched", "n_activities",
                           # totalPaymentAmount is a running total over the case and dismissal
                           # is the outcome of an appeal. Both are on the Create Fine event and
                           # both would read the future.
                           "first_totalPaymentAmount", "first_dismissal"],
    caveats=["150,000 cases with very long tails: many fines sit for years awaiting payment. "
             "The duration label will be dominated by administrative dormancy rather than "
             "processing effort, and that limits what a comparison to BPI 2014 means. "
             "State it; do not quietly drop the log if the number is unflattering."],
)

ALL = [BPI2014, BPIC2017, SEPSIS, HOSPITAL_BILLING, TRAFFIC_FINES]
BY_SLUG = {s.slug: s for s in ALL}


def preregistration_markdown():
    """Render the specs as the pre-registration document that goes beside the results."""
    out = ["# Pre-registration: decision point, outcome and stratum per log", "",
           "Written before any AUC was computed on logs 2 to 5. Generated from "
           "`code/log_schemas.py`, which is the single source of truth.", ""]
    for s in ALL:
        out += ["## %s" % s.name, "",
                "- **slug**: `%s`  **domain**: %s  **group level**: %s" % (s.slug, s.domain, s.group_level),
                "- **source**: %s" % s.source,
                "- **decision point**: %s" % s.decision_point,
                "- **outcome**: %s" % s.outcome,
                "- **stratum**: %s" % s.stratum,
                "- **group**: %s" % s.group,
                "- **admissible case attributes**: %s" % ", ".join("`%s`" % a for a in s.admissible_case_attrs),
                "- **inadmissible features**: %s" % ", ".join("`%s`" % a for a in s.inadmissible_features), ""]
        if s.caveats:
            out.append("Caveats:")
            out += ["- %s" % c for c in s.caveats]
            out.append("")
    return "\n".join(out)


if __name__ == "__main__":
    print(preregistration_markdown())
