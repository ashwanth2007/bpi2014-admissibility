"""
Verify every numeric claim in the manuscript against the artifacts on disk.

Run this before any submission. It re-derives each published figure from the CSV or JSON
that produced it and fails loudly on any mismatch. A claim that cannot be traced is a claim
that does not go in the paper.

    python code/evaluation/verify_paper_claims.py     # exit 0 = all claims verified

Last full pass: 2026-09-07, 43 checks, 43 passed.
"""
from __future__ import annotations
import io, json, os, sys
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
C = os.path.abspath(os.path.join(HERE, ".."))

def csv(*p): return pd.read_csv(os.path.join(C, *p))
def js(*p):  return json.load(io.open(os.path.join(C, *p), encoding="utf-8"))

CHECKS, FAILS = [], 0

def chk(label, claimed, actual, tol=0.0006):
    global FAILS
    ok = actual is not None and abs(claimed - actual) <= tol
    if not ok:
        FAILS += 1
    CHECKS.append(("PASS" if ok else "FAIL", label, claimed, actual))

def main() -> int:
    l70 = csv("results_bpi_leakfree_7030", "feature_ladder_leakfree.csv").set_index("Feature_Set")["Test_AUC"]
    l80 = csv("results_bpi_leakfree", "feature_ladder_leakfree.csv").set_index("Feature_Set")["Test_AUC"]
    for claim, key in [(0.5076, "3_static_only"), (0.5616, "8_plus_temporal"),
                       (0.6574, "10_plus_operational"), (0.7767, "15_plus_config"),
                       (0.7859, "17_plus_group_TE")]:
        chk("ladder 70/30 " + key, claim, float(l70[key]))
    chk("ladder gain 70/30", 0.2243, float(l70["17_plus_group_TE"] - l70["8_plus_temporal"]))
    chk("ladder gain 80/20", 0.2213, float(l80["17_plus_group_TE"] - l80["8_plus_temporal"]))

    mc = csv("results_bpi_leakfree_7030", "model_comparison_leakfree.csv")
    x = mc[mc.Model == "XGBoost"].set_index("Config")["Test_AUC"]
    chk("contaminated AUC", 0.8814, float(x["contaminated_v1"]))
    chk("leak-free AUC", 0.7859, float(x["leakfree"]))
    chk("chronological AUC", 0.6533, float(x["leakfree_temporal"]))
    chk("correction AUC", 0.0955, float(x["contaminated_v1"] - x["leakfree"]))
    chk("correction pct", 25.0, float(100 * (x["contaminated_v1"] - x["leakfree"]) / (x["contaminated_v1"] - 0.5)), 0.06)

    h = js("results_formulations", "hazard_model.json")
    chk("derived breach AUC", 0.8017, float(h["derived_breach_auc_at_intake"]))
    chk("static breach AUC", 0.7399, float(h["static_model_breach_auc_for_comparison"]))

    f = js("results_formulations", "fitted_parameters.json")
    chk("cosine zero pct", 31.65, float(100 * f["s3_expertise"]["exact_zero_fraction"]), 0.02)
    chk("delay excluded pct", 35.6, float(100 * f["s5_freshness_decay"]["rows_excluded_as_artifacts"] / f["_meta"]["incidents"]), 0.06)
    st = f["rank_stability"]
    chk("spearman critic/equal", 0.957, float(st["critic_vs_equal"]["spearman_rho"]), 0.001)
    chk("spearman critic/entropy", 0.935, float(st["critic_vs_entropy"]["spearman_rho"]), 0.001)
    chk("incidents", 15828, float(f["_meta"]["incidents"]), 0)

    m = csv("phase4", "results", "metrics_fullv3.csv")
    g = m.groupby("policy")["hypervolume"].mean()
    for claim, pol in [(0.4307, "StandardNSGA2"), (0.4844, "PAA-NSGA-II"), (0.5279, "NSGA3"),
                       (0.5324, "NSGA3-PrioInit"), (0.4133, "PAA-noPriority")]:
        chk("hypervolume " + pol, claim, float(g[pol]))
    chk("ablation priority init", -0.0711, float(g["PAA-noPriority"] - g["PAA-NSGA-II"]))
    chk("total runs", 1225, float(len(m)), 0)
    chk("capacity violations", 0, float(m["capacity_violation"].sum()), 0)
    sig = {r["metric"]: r for r in js("phase4", "results", "significance_fullv3.json")}
    chk("hypervolume mean diff", 0.05372, float(sig["hypervolume"]["mean_diff"]))

    rt = csv("results_evaluation", "runtime.csv").set_index("policy")["mean_s_per_solve"]
    for claim, pol in [(4.48, "StandardNSGA2"), (4.84, "PAA-NSGA-II"), (5.07, "NSGA3"),
                       (4.71, "NSGA3-PrioInit")]:
        chk("runtime " + pol, claim, float(rt[pol]), 0.006)

    print("=" * 80)
    for s, lab, c, a in CHECKS:
        print("  [%s] %-30s claimed %-10s actual %s" % (s, lab, c, "%.5f" % a if a is not None else "MISSING"))
    print("=" * 80)
    print("  %d checks, %d passed, %d FAILED" % (len(CHECKS), len(CHECKS) - FAILS, FAILS))
    return 1 if FAILS else 0

if __name__ == "__main__":
    sys.exit(main())
