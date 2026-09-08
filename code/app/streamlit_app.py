"""FastLead: decision-time SLA triage for IT service management.

Phase 9 of the VTOP Special Project. The prototype that shows what the paper measures.

WHAT THIS IS. An incident arrives. Someone has to route it within seconds. This app scores
the breach risk at that moment, using only what is knowable at that moment, and recommends a
group. The distinguishing feature is not that it predicts. It is that it shows you, on every
single incident, exactly which attributes it was allowed to look at and which ones the log
records but the decision-maker does not have yet.

WHY THAT IS THE POINT. A model given the closed case scores 0.9099. The same learner on the
same rows, restricted to the assignment moment, scores 0.8319. The difference is not a
modelling failure, it is the price of only using what exists when the decision is taken, and
a deployed model pays it whether or not the paper reporting it did.

Run:
    cd code
    streamlit run app/streamlit_app.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import altair as alt
import numpy as np
import pandas as pd
import streamlit as st

HERE = Path(__file__).resolve().parent
CODE = HERE.parent
ART = HERE / "artifacts"
RESULTS = CODE / "results_bpi_leakfree_7030"
DEEP = CODE / "results_deep_7030"

st.set_page_config(page_title="FastLead, decision-time triage", layout="wide",
                   initial_sidebar_state="expanded")

INK = "#12141a"
MUTED = "#6b7280"
OK = "#1f9d76"
WARN = "#d1791f"
BAD = "#c0392b"
LINE = "#e5e7eb"

st.markdown("""
<style>
  .block-container {padding-top: 2.2rem; padding-bottom: 3rem; max-width: 1500px;}
  h1, h2, h3 {letter-spacing: -0.015em;}
  [data-testid="stMetricValue"] {font-size: 1.55rem;}
  [data-testid="stMetricLabel"] {font-size: .78rem; text-transform: uppercase;
                                 letter-spacing: .06em; color: #6b7280;}
  .pill {display:inline-block; padding:.14rem .55rem; border-radius:999px;
         font-size:.72rem; font-weight:600; letter-spacing:.02em;}
  .obs  {background:#e8f5f0; color:#1f6f57; border:1px solid #bfe3d6;}
  .late {background:#f4f4f5; color:#71717a; border:1px solid #e4e4e7;}
  .note {color:#6b7280; font-size:.86rem; line-height:1.5;}
  .attr {padding:.42rem .1rem; border-bottom:1px solid #f1f1f3; font-size:.9rem;}
  .attr b {font-weight:600;}
  .val  {float:right; font-variant-numeric:tabular-nums;}
  .strike {text-decoration:line-through; color:#a1a1aa;}
</style>
""", unsafe_allow_html=True)


# --------------------------------------------------------------------------- loading
@st.cache_resource(show_spinner="loading models")
def load_models():
    # These two artifacts are written by app/export_ui_models.py in this same directory and
    # are never fetched from anywhere else, so joblib's pickle load is not reading untrusted
    # input. If that ever stops being true, this becomes arbitrary code execution.
    import joblib
    adm = joblib.load(ART / "ui_admissible.joblib")
    unc = joblib.load(ART / "ui_unconstrained.joblib")
    return adm, unc


@st.cache_data(show_spinner="loading the evaluation slice")
def load_queue():
    p = ART / "ui_queue.parquet"
    q = pd.read_parquet(p) if p.exists() else pd.read_csv(ART / "ui_queue.csv")
    if "Open Time" in q.columns:
        q["Open Time"] = pd.to_datetime(q["Open Time"], errors="coerce")
    return q


@st.cache_data
def load_registry():
    return json.loads((ART / "ui_registry.json").read_text(encoding="utf-8"))


@st.cache_data
def load_groups():
    g = pd.read_csv(ART / "ui_groups.csv")
    gc = pd.read_csv(ART / "ui_group_category.csv")
    return g, gc


@st.cache_data
def load_results():
    """The paper's own artifacts, read not retyped. Missing files degrade, never fabricate."""
    out = {}
    for key, path in (("cost", RESULTS / "model_comparison_leakfree.csv"),
                      ("ladder", RESULTS / "feature_ladder_leakfree.csv"),
                      ("shap", RESULTS / "shap_importance_leakfree.csv"),
                      ("just", RESULTS / "feature_justifications_leakfree.csv")):
        if path.exists():
            out[key] = pd.read_csv(path)
    logs = []
    for d in sorted(CODE.glob("results_*")):
        f = d / "summary.json"
        if f.exists():
            logs.append(json.loads(f.read_text(encoding="utf-8")))
    out["logs"] = logs
    return out


def risk_colour(p):
    return BAD if p >= 0.65 else (WARN if p >= 0.4 else OK)


def band(p):
    return "high" if p >= 0.65 else ("watch" if p >= 0.4 else "low")


# --------------------------------------------------------------------------- shared
reg = load_registry()
queue = load_queue()
adm_art, unc_art = load_models()
ADM_FEATS = adm_art["features"]
UNC_FEATS = unc_art["features"]
LATE_ONLY = [f for f in UNC_FEATS if f not in ADM_FEATS]

PRETTY = {
    "Priority": "Priority", "Impact": "Impact", "Urgency": "Urgency",
    "Open_Hour": "Arrival hour", "Open_DayOfWeek": "Day of week",
    "Is_Weekend": "Weekend arrival", "Is_Business_Hours": "In business hours",
    "Open_Month": "Month", "Queue_Length_At_Open": "Open incidents at arrival",
    "Assignment_Delay_Hours": "Wait before routing (h)",
    "CI_Type_Encoded": "Configuration item type", "CI_Subtype_Encoded": "CI subtype",
    "WBS_Encoded": "Service component", "Category_Encoded": "Category",
    "Alert_Status_Encoded": "Alert status",
    "Group_Breach_Rate_TE": "Group breach rate, past cases only",
    "Group_Volume_TE": "Group volume, past cases only",
    "# Reassignments": "Times reassigned", "Num_Groups_Touched": "Groups touched",
    "Total_Activity_Events": "Activity events logged", "Has_Reopen": "Was reopened",
    "Num_Related_Incidents": "Related incidents",
}


def label(f):
    return PRETTY.get(f, f.replace("_", " "))


with st.sidebar:
    st.markdown("### FastLead")
    st.caption("Decision-time triage, BPI Challenge 2014")
    page = st.radio("Section", ["Queue", "Incident", "Assignment", "Evidence"],
                    label_visibility="collapsed")
    st.divider()
    st.metric("Admissible model", "%.4f" % reg["models"]["admissible"]["holdout_auc"],
              help="Held-out ROC-AUC using only attributes knowable at the assignment moment")
    st.metric("If it could see the closed case",
              "%.4f" % reg["models"]["unconstrained"]["holdout_auc"],
              delta="+%.4f not deployable" % reg["cost_of_admissibility"],
              delta_color="off")
    st.caption("Both trained on the same %s rows with the same learner. They differ only in "
               "which attributes they may see." % f"{reg['train_rows']:,}")
    st.divider()
    st.caption("Rule 2 mode: **%s**  \nThreads pinned: **%s**  \nExported %s"
               % (reg.get("rule2_mode", "?"), reg.get("threads_pinned", "unset"),
                  reg["exported_at"][:16].replace("T", " ")))


# --------------------------------------------------------------------------- queue
if page == "Queue":
    st.title("Incident queue")
    st.markdown('<p class="note">Every incident below is scored as it would have been at the '
                'moment it needed routing. Nothing recorded after that moment reaches the '
                'model.</p>', unsafe_allow_html=True)

    c = st.columns([1, 1, 1, 1, 2])
    prio = c[0].multiselect("Priority", sorted(queue["Priority"].dropna().unique().astype(int)),
                            default=[])
    minrisk = c[1].slider("Minimum risk", 0.0, 1.0, 0.0, 0.05)
    only_bh = c[2].selectbox("Arrival", ["all", "business hours", "out of hours"])
    n_show = c[3].selectbox("Show", [25, 50, 100, 250], index=1)

    q = queue.copy()
    if prio:
        q = q[q["Priority"].isin(prio)]
    q = q[q["breach_risk_admissible"] >= minrisk]
    if only_bh == "business hours" and "Is_Business_Hours" in q.columns:
        q = q[q["Is_Business_Hours"] == 1]
    elif only_bh == "out of hours" and "Is_Business_Hours" in q.columns:
        q = q[q["Is_Business_Hours"] == 0]
    q = q.sort_values("breach_risk_admissible", ascending=False)

    m = st.columns(4)
    m[0].metric("In view", f"{len(q):,}")
    m[1].metric("Flagged high", f"{int((q['breach_risk_admissible'] >= 0.65).sum()):,}",
                help="Risk at or above 0.65")
    m[2].metric("Mean risk", "%.3f" % q["breach_risk_admissible"].mean()
                if len(q) else "n/a")
    m[3].metric("Actual breach rate here",
                "%.1f%%" % (100 * q["actually_breached"].mean()) if len(q) else "n/a",
                help="Ground truth, shown because this is a historical log. A live desk would "
                     "not have this column.")

    st.divider()
    view = q.head(n_show).copy()
    view["risk"] = view["breach_risk_admissible"]
    view["band"] = view["risk"].map(band)
    cols = ["Incident ID", "Open Time", "Priority", "category", "First_Assignment_Group",
            "risk", "band"]
    cols = [c for c in cols if c in view.columns]
    st.dataframe(
        view[cols],
        hide_index=True, width="stretch", height=460,
        column_config={
            "Incident ID": st.column_config.TextColumn("Incident", width="small"),
            "Open Time": st.column_config.DatetimeColumn("Arrived", format="YYYY-MM-DD HH:mm"),
            "Priority": st.column_config.NumberColumn("Pri", width="small"),
            "category": st.column_config.TextColumn("Category", width="medium"),
            "First_Assignment_Group": st.column_config.TextColumn("Routed to", width="medium"),
            "risk": st.column_config.ProgressColumn("Breach risk", min_value=0.0,
                                                    max_value=1.0, format="%.3f"),
            "band": st.column_config.TextColumn("", width="small"),
        })
    st.caption("Pick any incident on the **Incident** page to see what the model was and was "
               "not allowed to look at.")


# --------------------------------------------------------------------------- incident
elif page == "Incident":
    st.title("Incident, at the moment of the decision")

    ids = queue.sort_values("breach_risk_admissible", ascending=False)["Incident ID"].tolist()
    pick = st.selectbox("Incident", ids, index=0,
                        help="Sorted by admissible breach risk, highest first")
    row = queue[queue["Incident ID"] == pick].iloc[0]

    p_adm = float(row["breach_risk_admissible"])
    p_unc = float(row["breach_risk_unconstrained"])

    left, right = st.columns([1.05, 1])

    with left:
        st.markdown("#### %s" % pick)
        st.markdown(
            '<div style="font-size:3.4rem; font-weight:700; line-height:1; color:%s;">%.3f</div>'
            '<div class="note" style="margin-top:.2rem;">breach risk, using only what is '
            'knowable now</div>' % (risk_colour(p_adm), p_adm), unsafe_allow_html=True)

        st.markdown("")
        k = st.columns(3)
        k[0].metric("Band", band(p_adm))
        k[1].metric("Priority", int(row["Priority"]) if pd.notna(row.get("Priority")) else "n/a")
        k[2].metric("Routed to", str(row.get("First_Assignment_Group", "n/a"))[:22])

        st.markdown("")
        st.markdown("**What the closed case would have said**")
        st.markdown(
            '<p class="note">The unconstrained model scores this incident at <b>%.3f</b>. '
            'It is not a better answer, it is a later one: it reads attributes that only exist '
            'once the case is finished. At the moment this decision has to be taken, those '
            'columns are empty.</p>' % p_unc, unsafe_allow_html=True)

        if "actually_breached" in row:
            truth = "breached" if int(row["actually_breached"]) == 1 else "met its target"
            hrs = row.get("handle_hours")
            thr = row.get("threshold_hours")
            extra = ""
            if pd.notna(hrs) and pd.notna(thr):
                extra = " Took %.1f hours against a %.1f hour target." % (hrs, thr)
            st.info("Outcome on the historical record: this incident **%s**.%s" % (truth, extra))

    with right:
        st.markdown("#### The two sets of attributes")
        st.markdown('<span class="pill obs">observable now</span> '
                    '<span class="pill late">recorded later</span>',
                    unsafe_allow_html=True)
        st.markdown('<p class="note" style="margin-top:.6rem;">Rule 1 admits an attribute only '
                    'if its value exists at the decision moment. The greyed rows are real '
                    'columns in the log and they are the ones a leaking model reaches '
                    'for.</p>', unsafe_allow_html=True)

        shown = [f for f in ADM_FEATS if f in queue.columns]
        html = []
        for f in shown:
            v = row.get(f)
            vs = ("%.2f" % v) if isinstance(v, (int, float, np.floating)) and pd.notna(v) else str(v)
            html.append('<div class="attr"><b>%s</b><span class="val">%s</span></div>'
                        % (label(f), vs))
        for f in ADM_FEATS:
            if f not in queue.columns:
                html.append('<div class="attr" style="color:#9ca3af;"><b>%s</b>'
                            '<span class="val">in model</span></div>' % label(f))
        for f in LATE_ONLY:
            html.append('<div class="attr strike"><b>%s</b>'
                        '<span class="val">not knowable yet</span></div>' % label(f))
        st.markdown("".join(html), unsafe_allow_html=True)


# --------------------------------------------------------------------------- assignment
elif page == "Assignment":
    st.title("Which group should take it")
    st.markdown('<p class="note">Candidates are ranked on three criteria computed from '
                '<b>training-period history only</b>. A group profile assembled over the whole '
                'log would describe a group using cases that had not happened yet when this '
                'incident arrived, which is Rule 1 applied to the thing doing the assigning. '
                'The full seven-criterion score is in the paper.</p>', unsafe_allow_html=True)

    groups, gcat = load_groups()
    ids = queue.sort_values("breach_risk_admissible", ascending=False)["Incident ID"].tolist()
    pick = st.selectbox("Incident", ids[:400], index=0)
    row = queue[queue["Incident ID"] == pick].iloc[0]

    st.markdown("")
    w = st.columns(3)
    w_risk = w[0].slider("Weight: low breach history", 0.0, 1.0, 0.4, 0.05)
    w_exp = w[1].slider("Weight: category expertise", 0.0, 1.0, 0.4, 0.05)
    w_load = w[2].slider("Weight: spare capacity", 0.0, 1.0, 0.2, 0.05)
    tot = max(w_risk + w_exp + w_load, 1e-9)

    cand = groups.copy()
    cand["s_risk"] = 1.0 - cand["breach_rate"]
    med = cand["cases"].median()
    cand["s_load"] = 1.0 - (cand["cases"] / cand["cases"].max())
    cat = row.get("Category_Encoded")
    if cat is None and "category" in row:
        cat = None
    exp = gcat.copy()
    if cat is not None and not pd.isna(cat):
        exp = exp[exp["category"] == cat]
        e = exp.set_index("group")["cases"]
        cand["s_exp"] = cand["group"].map(e).fillna(0.0)
        if cand["s_exp"].max() > 0:
            cand["s_exp"] = cand["s_exp"] / cand["s_exp"].max()
    else:
        cand["s_exp"] = 0.0

    cand["score"] = (w_risk * cand["s_risk"] + w_exp * cand["s_exp"]
                     + w_load * cand["s_load"]) / tot
    cand = cand[cand["cases"] >= 50].sort_values("score", ascending=False)

    top = cand.head(8).copy()
    if len(top):
        best = top.iloc[0]
        actual = str(row.get("First_Assignment_Group", ""))
        a, b = st.columns([1, 1])
        a.metric("Recommended", str(best["group"])[:26],
                 help="Highest weighted score among groups with at least 50 training cases")
        b.metric("Actually routed to", actual[:26],
                 delta="same" if actual == str(best["group"]) else "different",
                 delta_color="normal" if actual == str(best["group"]) else "off")

        st.divider()
        show = top[["group", "cases", "breach_rate", "s_exp", "s_load", "score"]]
        st.dataframe(show, hide_index=True, width="stretch",
                     column_config={
                         "group": st.column_config.TextColumn("Group", width="medium"),
                         "cases": st.column_config.NumberColumn("Past cases", width="small"),
                         "breach_rate": st.column_config.NumberColumn("Breach rate",
                                                                     format="%.3f"),
                         "s_exp": st.column_config.ProgressColumn("Category expertise",
                                                                  min_value=0.0, max_value=1.0,
                                                                  format="%.2f"),
                         "s_load": st.column_config.ProgressColumn("Spare capacity",
                                                                   min_value=0.0, max_value=1.0,
                                                                   format="%.2f"),
                         "score": st.column_config.ProgressColumn("Score", min_value=0.0,
                                                                  max_value=1.0, format="%.3f"),
                     })
        st.caption("Expertise is the count of training-period cases that group handled in this "
                   "incident's category, normalised. A zero column means no group in the "
                   "candidate set has three or more past cases in it.")
    else:
        st.warning("No candidate group has 50 or more training cases.")


# --------------------------------------------------------------------------- evidence
else:
    st.title("What this is built on")
    res = load_results()

    m = st.columns(4)
    m[0].metric("Incidents", f"{reg['incidents']:,}")
    m[1].metric("Cost of admissibility", "%.4f" % reg["cost_of_admissibility"],
                help="Held-out AUC lost by restricting the model to the decision moment")
    m[2].metric("Share of above-chance signal", "%.1f%%" % reg["share_of_signal_pct"])
    m[3].metric("Base breach rate", "%.1f%%" % (100 * reg["base_rate"]))

    st.markdown('<p class="note">Everything on this page is read from the artifacts the paper '
                'was verified against. Nothing here is typed by hand, so it cannot drift from '
                'what was published.</p>', unsafe_allow_html=True)
    st.divider()

    t1, t2, t3 = st.tabs(["Across five logs", "What each attribute is worth", "Rule 1 ledger"])

    with t1:
        logs = res.get("logs", [])
        rows = [{"Log": s["name"].split(",")[0], "Domain": s["domain"].split(",")[0],
                 "Cases": s["n_cases"], "Admissible AUC": s["best_admissible_auc"],
                 "Share lost %": s["share_of_signal_pct"]} for s in logs]
        rows.append({"Log": "BPI Challenge 2014", "Domain": "IT service management",
                     "Cases": reg["incidents"],
                     "Admissible AUC": reg["models"]["admissible"]["holdout_auc"],
                     "Share lost %": reg["share_of_signal_pct"]})
        df = pd.DataFrame(rows).sort_values("Share lost %")
        st.dataframe(df, hide_index=True, width="stretch",
                     column_config={"Admissible AUC": st.column_config.NumberColumn(
                                        format="%.4f"),
                                    "Share lost %": st.column_config.NumberColumn(
                                        format="%.1f")})
        ch = alt.Chart(df).mark_bar(size=22, cornerRadius=3).encode(
            x=alt.X("Share lost %:Q", title="share of above-chance signal lost, per cent"),
            y=alt.Y("Log:N", sort="-x", title=None),
            color=alt.value("#2f6f8f"),
            tooltip=["Log", "Domain", "Cases", "Admissible AUC", "Share lost %"])
        st.altair_chart(ch.properties(height=210), width="stretch")
        st.caption("The cost is not a constant. Any single figure, including this project's "
                   "own, is a point on that range and not a general number.")

    with t2:
        lad = res.get("ladder")
        if lad is not None:
            pretty = {"3_static_only": "Static record fields",
                      "8_plus_temporal": "+ arrival-time context",
                      "10_plus_operational": "+ operational congestion",
                      "15_plus_config": "+ configuration and category",
                      "17_plus_group_TE": "+ group encoding (Rule 2)"}
            lad = lad.copy()
            lad["Feature set"] = lad["Feature_Set"].map(lambda k: pretty.get(k, k))
            ch = alt.Chart(lad).mark_line(point=True, color="#2f6f8f").encode(
                x=alt.X("Num_Features:Q", title="admissible attributes"),
                y=alt.Y("Test_AUC:Q", scale=alt.Scale(zero=False), title="held-out AUC"),
                tooltip=["Feature set", "Num_Features", "Test_AUC"])
            st.altair_chart(ch.properties(height=280), width="stretch")
            st.caption("Every rung is admissible. The climb is what the discipline buys, and it "
                       "is why the answer to a leakage finding is not to model with fewer "
                       "attributes but with observable ones.")
        shap = res.get("shap")
        if shap is not None:
            s = shap.head(8).copy()
            s["Attribute"] = s["Feature"].map(label)
            ch = alt.Chart(s).mark_bar(size=16, cornerRadius=3).encode(
                x=alt.X("Mean_Abs_SHAP:Q", title="mean absolute SHAP"),
                y=alt.Y("Attribute:N", sort="-x", title=None),
                color=alt.value("#7a8b99"), tooltip=["Attribute", "Mean_Abs_SHAP"])
            st.altair_chart(ch.properties(height=240), width="stretch")

    with t3:
        just = res.get("just")
        if just is not None:
            j = just.copy()
            j["Verdict"] = j["Known_at_assignment_time"].map(
                lambda v: "admissible" if str(v).strip().lower().startswith("yes")
                else "excluded")
            st.dataframe(j[["Feature", "Verdict", "Justification"]], hide_index=True,
                         width="stretch", height=430,
                         column_config={
                             "Feature": st.column_config.TextColumn(width="medium"),
                             "Verdict": st.column_config.TextColumn(width="small"),
                             "Justification": st.column_config.TextColumn(width="large")})
            st.caption("This table is the machine-checked one. "
                       "`evaluation/verify_rule1.py` fails the build if any attribute marked "
                       "excluded reaches the admissible model, or if any attribute in the "
                       "model is missing a justification.")
        else:
            st.info("Run the pipeline to produce feature_justifications_leakfree.csv.")

st.divider()
st.caption("FastLead prototype, Phase 9. BPI Challenge 2014 (van Dongen, 2014), "
           "%s incidents. Models exported from the same pipeline the paper verifies; "
           "the admissible arm holds %.4f held-out AUC against %.4f for a model allowed to "
           "read the closed case."
           % (f"{reg['incidents']:,}", reg["models"]["admissible"]["holdout_auc"],
              reg["models"]["unconstrained"]["holdout_auc"]))
