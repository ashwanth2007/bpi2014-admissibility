"""
Verify every numeric claim in the manuscript against the artifacts on disk.

Run this before any submission. It re-derives each published figure from the CSV or JSON
that produced it and fails loudly on any mismatch. A claim that cannot be traced is a claim
that does not go in the paper.

    python code/evaluation/verify_paper_claims.py     # exit 0 = all claims verified

This script runs 49 checks. It ran 32 until 2026-09-08, and before that its docstring
and FINAL-STATE.md both claimed 43, a number no run ever produced. The seventeen added
cover every figure in the abstract and the conclusion, which are the two blocks a reader
is most likely to check a claim against and were the last two with no explicit check at
all: nine numbers in them were stale while every automatic screen reported the paper clean.
"""

from __future__ import annotations
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import _openmp_first  # noqa: F401  MUST precede sklearn/xgboost/catboost, see module docstring
import io, json, os, sys
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
C = os.path.abspath(os.path.join(HERE, ".."))

def csv(*p): return pd.read_csv(os.path.join(C, *p))
def js(*p):  return json.load(io.open(os.path.join(C, *p), encoding="utf-8"))

CHECKS, FAILS = [], 0

# Tolerance. Every table in the manuscript prints four decimals, so the largest legitimate
# rounding error is 5e-5. The previous defaults, 6e-4 and 5.1e-4, were ten times that, and
# two real transcription defects lived inside them undetected: a fold mean printed 0.7728
# where every artifact says 0.7727, and a standard deviation printed 0.0051 that matched
# neither the sample nor the population form. A tolerance wider than the printed precision
# is not a tolerance, it is a blindfold.
#
# Checks that legitimately compare across differing precisions pass an explicit tol.
TOL_4DP = 0.00005


def chk(label, claimed, actual, tol=TOL_4DP):
    global FAILS
    ok = actual is not None and abs(claimed - actual) <= tol
    if not ok:
        FAILS += 1
    CHECKS.append(("PASS" if ok else "FAIL", label, claimed, actual))

def main() -> int:
    l70 = csv("results_bpi_leakfree_7030", "feature_ladder_leakfree.csv").set_index("Feature_Set")["Test_AUC"]
    l80 = csv("results_bpi_leakfree", "feature_ladder_leakfree.csv").set_index("Feature_Set")["Test_AUC"]
    for claim, key in [(0.5100, "3_static_only"), (0.5716, "8_plus_temporal"),
                       (0.7225, "10_plus_operational"), (0.8171, "15_plus_config"),
                       (0.8258, "17_plus_group_TE")]:
        chk("ladder 70/30 " + key, claim, float(l70[key]))
    chk("ladder gain 70/30", 0.2542, float(l70["17_plus_group_TE"] - l70["8_plus_temporal"]))
    chk("ladder gain 80/20", 0.2597, float(l80["17_plus_group_TE"] - l80["8_plus_temporal"]))

    mc = csv("results_bpi_leakfree_7030", "model_comparison_leakfree.csv")
    x = mc[mc.Model == "XGBoost"].set_index("Config")["Test_AUC"]
    chk("contaminated AUC", 0.9050, float(x["contaminated_v1"]))
    chk("leak-free AUC", 0.8258, float(x["leakfree"]))
    chk("chronological AUC", 0.8074, float(x["leakfree_temporal"]))
    chk("correction AUC", 0.0792, float(x["contaminated_v1"] - x["leakfree"]))
    chk("correction pct", 19.6, float(100 * (x["contaminated_v1"] - x["leakfree"]) / (x["contaminated_v1"] - 0.5)), 0.06)

    h = js("results_formulations", "hazard_model.json")
    chk("derived breach AUC", 0.8373, float(h["derived_breach_auc_at_intake"]))
    chk("static breach AUC", 0.7879, float(h["static_model_breach_auc_for_comparison"]))

    f = js("results_formulations", "fitted_parameters.json")
    chk("cosine zero pct", 30.04, float(100 * f["s3_expertise"]["exact_zero_fraction"]), 0.02)
    chk("delay excluded pct", 0.4, float(100 * f["s5_freshness_decay"]["rows_excluded_above_cap"] / f["_meta"]["incidents"]), 0.06)
    st = f["rank_stability"]
    chk("spearman critic/equal", 0.959, float(st["critic_vs_equal"]["spearman_rho"]), 0.001)
    chk("spearman critic/entropy", 0.946, float(st["critic_vs_entropy"]["spearman_rho"]), 0.001)
    chk("incidents", 39449, float(f["_meta"]["incidents"]), 0)

    m = csv("phase4", "results", "metrics_fullv3.csv")
    g = m.groupby("policy")["hypervolume"].mean()
    for claim, pol in [(0.0695, "StandardNSGA2"), (0.0762, "PAA-NSGA-II"), (0.0824, "NSGA3"),
                       (0.0850, "NSGA3-PrioInit"), (0.0658, "PAA-noPriority")]:
        chk("hypervolume " + pol, claim, float(g[pol]))
    chk("ablation priority init", -0.0104, float(g["PAA-noPriority"] - g["PAA-NSGA-II"]))
    chk("total runs", 1225, float(len(m)), 0)
    chk("capacity violations", 0, float(m["capacity_violation"].sum()), 0)
    sig = {r["metric"]: r for r in js("phase4", "results", "significance_fullv3.json")}
    chk("hypervolume mean diff", 0.00666, float(sig["hypervolume"]["mean_diff"]))

    rt = csv("results_evaluation", "runtime.csv").set_index("policy")["mean_s_per_solve"]
    for claim, pol in [(0.89, "StandardNSGA2"), (0.96, "PAA-NSGA-II"), (0.99, "NSGA3"),
                       (1.01, "NSGA3-PrioInit")]:
        chk("runtime " + pol, claim, float(rt[pol]), 0.006)

    # ---------------------------------------------- the abstract and the conclusion
    # Nine numbers in these two blocks were stale while every automatic screen reported the
    # paper clean. The stale screen asks whether a printed value matches the OLD run and not
    # the new one, and a value that is merely wrong by 0.0012, or that happens to occur
    # somewhere in the current artifacts, answers no. Only an explicit comparison against the
    # artifact catches that, so the two blocks a reader is most likely to check a claim
    # against now have one per number. They are shared inputs to both drivers, so an error
    # here appears twice.
    import glob as _glob
    import statistics as _st

    x = mc[mc.Model == "XGBoost"].set_index("Config")["Test_AUC"]
    adm70, con70 = float(x["leakfree"]), float(x["contaminated_v1"])
    tmp70 = float(x["leakfree_temporal"])
    hz = js("results_formulations", "hazard_model.json")

    chk("abstract ladder at 3", 0.5100, float(l70["3_static_only"]))
    chk("abstract ladder at 17", 0.8258, float(l70["17_plus_group_TE"]))
    chk("abstract hazard derived", 0.8373, float(hz["derived_breach_auc_at_intake"]))
    chk("abstract hazard static", 0.7879, float(hz["static_model_breach_auc_for_comparison"]))

    costs = []
    for mdl in ("XGBoost", "LightGBM", "CatBoost", "RandomForest"):
        y = mc[mc.Model == mdl].set_index("Config")["Test_AUC"]
        costs.append(float(y["contaminated_v1"]) - float(y["leakfree"]))
    chk("abstract cost range low", 0.0782, min(costs))
    chk("abstract cost range high", 0.0823, max(costs))

    con_all, adm_all = [], []
    for d in sorted(_glob.glob(os.path.join(C, "results_*"))):
        p = os.path.join(d, "summary.json")
        if os.path.exists(p):
            s = json.load(io.open(p, encoding="utf-8"))
            adm_all.append(float(s["best_admissible_auc"]))
            con_all.append(float(s["best_admissible_auc"]) + float(s["cost_best"]))
    adm_all.append(adm70)
    con_all.append(con70)
    chk("abstract contaminated min", 0.824, min(con_all), 0.0005)
    chk("abstract contaminated max", 0.979, max(con_all), 0.0005)
    chk("abstract contaminated sd", 0.058, _st.stdev(con_all), 0.0005)
    chk("abstract admissible min", 0.613, min(adm_all), 0.0005)
    chk("abstract admissible max", 0.904, max(adm_all), 0.0005)

    chk("conclusion cost", 0.0792, con70 - adm70)
    chk("conclusion contaminated band", 0.155, max(con_all) - min(con_all), 0.0005)
    chk("conclusion admissible band", 0.291, max(adm_all) - min(adm_all), 0.0005)
    chk("conclusion ladder gain", 0.2542,
        float(l70["17_plus_group_TE"] - l70["8_plus_temporal"]))
    chk("conclusion separation", 0.8258, adm70)
    chk("conclusion drift loss", 0.0184, adm70 - tmp70)

    print("=" * 80)
    if "--dump" in sys.argv:
        for s, lab, c, a in CHECKS:
            print("CHECK	%s	%s	%s	%s" % (s, lab, c, a))
    for s, lab, c, a in CHECKS:
        print("  [%s] %-30s claimed %-10s actual %s" % (s, lab, c, "%.5f" % a if a is not None else "MISSING"))
    print("=" * 80)
    print("  %d checks, %d passed, %d FAILED" % (len(CHECKS), len(CHECKS) - FAILS, FAILS))
    return 1 if FAILS else 0

if __name__ == "__main__":
    sys.exit(main())
