"""
Verify every numeric claim added in the deep-evaluation and formulation sections.

Companion to verify_paper_claims.py, which covers the original claim set. This one covers
the tables introduced with the extended results: dataset statistics, the per-priority
breakdown, the four-learner cost table, the full metric set, confusion matrices, the feature
ladder metrics, the learning curve, cross-validation folds, the threshold sweep, the
chronological arm, calibration, and every constant in F1 and F2.

Each check re-reads the artifact on disk and compares it to the number printed in the
manuscript. A claim that cannot be traced is a claim that does not go in the paper.

    python code/evaluation/verify_deep_claims.py     # exit 0 = all claims verified
"""

from __future__ import annotations
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import _openmp_first  # noqa: F401  MUST precede sklearn/xgboost/catboost, see module docstring

import io
import json
import os
import sys

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
C = os.path.abspath(os.path.join(HERE, ".."))


def csv(*p):
    return pd.read_csv(os.path.join(C, *p))


def js(*p):
    return json.load(io.open(os.path.join(C, *p), encoding="utf-8"))


CHECKS = []
FAILS = 0


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
    ok = actual is not None and abs(float(claimed) - float(actual)) <= tol
    if not ok:
        FAILS += 1
    CHECKS.append(("PASS" if ok else "FAIL", label, claimed, actual))



def _cost_of_admissibility():
    """The admissibility cost, read from the artifact instead of typed into this line.

    It was written here as the literal 0.0955, which is the pre-correction value, inside a
    ratio whose whole point is to compare a tuning gain against it. A stale denominator makes
    the ratio wrong while both of its inputs look fine, which is the hardest kind of stale
    number to notice.
    """
    m = csv("results_bpi_leakfree_7030", "model_comparison_leakfree.csv")
    x = m[m.Model == "XGBoost"].set_index("Config")["Test_AUC"]
    return float(x["contaminated_v1"]) - float(x["leakfree"])

def main() -> int:
    D = "results_deep_7030"
    F = "results_formulations"

    # ------------------------------------------------- Table: dataset statistics
    st = csv(D, "dataset_stats.csv").set_index("Property")["Value"]
    for label, claim in [("Raw incident records", 46606),
                         ("Raw activity events", 466737),
                         ("Raw interaction records", 147004),
                         ("Incidents in the modelling matrix", 46605),
                         ("Distinct first-assignment groups", 180),
                         ("Distinct configuration-item types", 13),
                         ("Distinct configuration-item subtypes", 65),
                         ("Distinct service components (WBS)", 274),
                         ("Breach cases (positive class)", 17120),
                         ("Non-breach cases (negative class)", 29485),
                         ("Training rows (random split)", 32623),
                         ("Test rows (random split)", 13982),
                         ("Training rows (chronological)", 37284),
                         ("Test rows (chronological)", 9321),
                         ("Observation window (days)", 785)]:
        chk("dataset: " + label, claim, float(st[label]), tol=0.5)
    chk("dataset: positive class rate", 0.3673, float(st["Positive class rate"]))

    # ------------------------------------------------- Table: per-priority breakdown
    pri = csv(D, "priority_breakdown.csv").set_index("Priority")
    for p, n, med, thr, br in [(2, 697, 3.40, 6.79, 0.3242),
                               (3, 6703, 1.48, 2.96, 0.3698),
                               (4, 22717, 3.58, 7.17, 0.3612),
                               (5, 16485, 7.63, 15.25, 0.3766)]:
        chk("priority %d n" % p, n, float(pri.loc[p, "n"]), tol=0.5)
        chk("priority %d median handle" % p, med, float(pri.loc[p, "median_handle_hours"]), tol=0.006)
        chk("priority %d threshold" % p, thr, float(pri.loc[p, "threshold_hours"]), tol=0.006)
        chk("priority %d breach rate" % p, br, float(pri.loc[p, "breach_rate"]))

    # ------------------------------------------------- Table: cost of admissibility
    m = csv(D, "full_metrics.csv").set_index(["Config", "Model"])
    pt = csv(D, "paired_tests.csv")
    cost = pt[pt.Config == "cost_of_admissibility"].set_index("Model")
    for mdl, unc, adm, lo, hi, share in [
            ("XGBoost", 0.9050, 0.8258, 0.0703, 0.0875, 19.5),
            ("LightGBM", 0.9048, 0.8239, 0.0723, 0.0894, 20.0),
            ("CatBoost", 0.8989, 0.8166, 0.0732, 0.0909, 20.6),
            ("RandomForest", 0.8994, 0.8212, 0.0702, 0.0870, 19.6)]:
        a_unc = float(m.loc[("contaminated_v1", mdl), "AUC"])
        a_adm = float(m.loc[("leakfree", mdl), "AUC"])
        chk("cost: %s unconstrained AUC" % mdl, unc, a_unc)
        chk("cost: %s admissible AUC" % mdl, adm, a_adm)
        chk("cost: %s CI low" % mdl, lo, float(cost.loc[mdl, "CI_low"]))
        chk("cost: %s CI high" % mdl, hi, float(cost.loc[mdl, "CI_high"]))
        chk("cost: %s share of above-chance signal" % mdl, share,
            100.0 * (a_unc - a_adm) / (a_unc - 0.5), tol=0.06)

    # ------------------------------------------------- Table: full metric set
    for cfg, mdl, acc, prec, rec, spec, f1, mcc, auc, ap, brier in [
            ("contaminated_v1", "XGBoost", 0.8277, 0.7326, 0.8363, 0.8227, 0.7810, 0.6438, 0.9050, 0.8491, 0.1231),
            ("contaminated_v1", "LightGBM", 0.8266, 0.7291, 0.8401, 0.8188, 0.7807, 0.6429, 0.9048, 0.8498, 0.1235),
            ("contaminated_v1", "CatBoost", 0.8193, 0.7200, 0.8316, 0.8122, 0.7718, 0.6280, 0.8989, 0.8417, 0.1281),
            ("contaminated_v1", "RandomForest", 0.8213, 0.7290, 0.8172, 0.8236, 0.7706, 0.6277, 0.8994, 0.8392, 0.1268),
            ("leakfree", "XGBoost", 0.7433, 0.6280, 0.7389, 0.7459, 0.6790, 0.4718, 0.8258, 0.7346, 0.1694),
            ("leakfree", "LightGBM", 0.7397, 0.6214, 0.7459, 0.7362, 0.6780, 0.4681, 0.8239, 0.7299, 0.1709),
            ("leakfree", "CatBoost", 0.7311, 0.6085, 0.7512, 0.7194, 0.6724, 0.4557, 0.8166, 0.7215, 0.1750),
            ("leakfree", "RandomForest", 0.7389, 0.6239, 0.7282, 0.7451, 0.6720, 0.4610, 0.8212, 0.7270, 0.1707)]:
        r = m.loc[(cfg, mdl)]
        for name, claim, col in [("acc", acc, "Accuracy"), ("prec", prec, "Precision"),
                                 ("rec", rec, "Recall"), ("spec", spec, "Specificity"),
                                 ("f1", f1, "F1"), ("mcc", mcc, "MCC"), ("auc", auc, "AUC"),
                                 ("ap", ap, "AveragePrecision"), ("brier", brier, "Brier")]:
            chk("full %s/%s %s" % (cfg, mdl, name), claim, float(r[col]))

    # ------------------------------------------------- Table: confusion matrices
    cm = csv(D, "confusion_matrices.csv").set_index(["Config", "Model"])
    for cfg, mdl, tn, fp, fn, tp, npv in [
            ("contaminated_v1", "XGBoost", 7278, 1568, 841, 4295, 0.8964),
            ("contaminated_v1", "LightGBM", 7243, 1603, 821, 4315, 0.8982),
            ("contaminated_v1", "CatBoost", 7185, 1661, 865, 4271, 0.8925),
            ("contaminated_v1", "RandomForest", 7286, 1560, 939, 4197, 0.8858),
            ("leakfree", "XGBoost", 6598, 2248, 1341, 3795, 0.8311),
            ("leakfree", "LightGBM", 6512, 2334, 1305, 3831, 0.8331),
            ("leakfree", "CatBoost", 6364, 2482, 1278, 3858, 0.8328),
            ("leakfree", "RandomForest", 6591, 2255, 1396, 3740, 0.8252)]:
        r = cm.loc[(cfg, mdl)]
        for name, claim, col in [("TN", tn, "TN"), ("FP", fp, "FP"),
                                 ("FN", fn, "FN"), ("TP", tp, "TP")]:
            chk("cm %s/%s %s" % (cfg, mdl, name), claim, float(r[col]), tol=0.5)
        chk("cm %s/%s NPV" % (cfg, mdl), npv, float(r["NPV"]))
    # derived statements made in prose
    chk("prose: breaches lost to admissibility",
        500, float(cm.loc[("contaminated_v1", "XGBoost"), "TP"])
        - float(cm.loc[("leakfree", "XGBoost"), "TP"]), tol=0.5)
    chk("prose: extra false alarms",
        680, float(cm.loc[("leakfree", "XGBoost"), "FP"])
        - float(cm.loc[("contaminated_v1", "XGBoost"), "FP"]), tol=0.5)

    # ------------------------------------------------- Table: feature ladder
    lad = csv("results_bpi_leakfree_7030", "feature_ladder_leakfree.csv").set_index("Feature_Set")
    for key, cv, auc, f1, acc, prec, rec in [
            ("3_static_only", 0.5112, 0.5100, 0.3838, 0.5441, 0.3811, 0.3865),
            ("8_plus_temporal", 0.5698, 0.5716, 0.4450, 0.5613, 0.4157, 0.4788),
            ("10_plus_operational", 0.7254, 0.7225, 0.5684, 0.6510, 0.5207, 0.6258),
            ("15_plus_config", 0.8177, 0.8171, 0.6745, 0.7331, 0.6109, 0.7529),
            ("17_plus_group_TE", 0.8269, 0.8258, 0.6790, 0.7433, 0.6280, 0.7389)]:
        r = lad.loc[key]
        chk("ladder %s CV" % key, cv, float(r["CV_AUC"]))
        chk("ladder %s AUC" % key, auc, float(r["Test_AUC"]))
        chk("ladder %s F1" % key, f1, float(r["Test_F1"]))
        chk("ladder %s acc" % key, acc, float(r["Test_Accuracy"]))
        chk("ladder %s prec" % key, prec, float(r["Test_Precision"]))
        chk("ladder %s rec" % key, rec, float(r["Test_Recall"]))
    chk("ladder rung3 to rung4 jump", 0.0946,
        float(lad.loc["15_plus_config", "Test_AUC"] - lad.loc["10_plus_operational", "Test_AUC"]))
    chk("ladder rung4 to rung5 jump", 0.0087,
        float(lad.loc["17_plus_group_TE", "Test_AUC"] - lad.loc["15_plus_config", "Test_AUC"]))

    # ------------------------------------------------- Table: learning curve
    lc = csv(D, "learning_curve.csv").set_index("Train_fraction")
    for frac, n, tr, te in [(0.05, 1631, 0.9999, 0.7765), (0.10, 3262, 0.9899, 0.7872),
                            (0.20, 6524, 0.9601, 0.8007), (0.30, 9786, 0.9421, 0.8076),
                            (0.40, 13049, 0.9246, 0.8178), (0.50, 16311, 0.9130, 0.8171),
                            (0.60, 19573, 0.9017, 0.8196), (0.70, 22836, 0.8972, 0.8241),
                            (0.85, 27729, 0.8878, 0.8243), (1.00, 32623, 0.8822, 0.8259)]:
        r = lc.loc[frac]
        chk("lc %.2f n" % frac, n, float(r["Train_n"]), tol=0.5)
        chk("lc %.2f train AUC" % frac, tr, float(r["Train_AUC"]))
        chk("lc %.2f test AUC" % frac, te, float(r["Test_AUC"]))
    chk("lc gap at full size", 0.0563, float(lc.loc[1.00, "Gap"]))
    chk("lc gap at smallest size", 0.2234, float(lc.loc[0.05, "Gap"]))

    # ------------------------------------------------- Table: cross-validation folds
    cv = csv(D, "cv_folds.csv")
    cvl = cv[cv.Config == "leakfree"]
    for mdl, folds, mean, sd in [
            ("XGBoost", [0.8201, 0.8269, 0.8276, 0.8270, 0.8331], 0.8269, 0.0046),
            ("LightGBM", [0.8172, 0.8227, 0.8238, 0.8262, 0.8315], 0.8243, 0.0052),
            ("RandomForest", [0.8144, 0.8206, 0.8251, 0.8216, 0.8288], 0.8221, 0.0054),
            ("CatBoost", [0.8121, 0.8172, 0.8207, 0.8180, 0.8258], 0.8188, 0.0050)]:
        s = cvl[cvl.Model == mdl].sort_values("Fold")["AUC"].values
        for k, f in enumerate(folds, start=1):
            chk("cv %s fold %d" % (mdl, k), f, float(s[k - 1]))
        chk("cv %s mean" % mdl, mean, float(s.mean()))
        chk("cv %s sd" % mdl, sd, float(pd.Series(s).std(ddof=1)))

    # ------------------------------------------------- Table: threshold sweep
    sw = csv(D, "threshold_sweep.csv").set_index("Threshold")
    for t, prec, rec, f1, acc, flag in [
            (0.20, 0.4713, 0.9593, 0.6320, 0.5897, 0.7477),
            (0.30, 0.5190, 0.9165, 0.6627, 0.6573, 0.6487),
            (0.40, 0.5671, 0.8477, 0.6796, 0.7064, 0.5491),
            (0.45, 0.5950, 0.7991, 0.6821, 0.7264, 0.4933),
            (0.50, 0.6280, 0.7389, 0.6790, 0.7433, 0.4322),
            (0.60, 0.6889, 0.6207, 0.6530, 0.7577, 0.3310),
            (0.70, 0.7557, 0.4764, 0.5844, 0.7511, 0.2316),
            (0.80, 0.8233, 0.3049, 0.4450, 0.7206, 0.1360),
            (0.90, 0.9206, 0.1287, 0.2258, 0.6759, 0.0514)]:
        r = sw.loc[t]
        chk("sweep %.2f prec" % t, prec, float(r["Precision"]))
        chk("sweep %.2f rec" % t, rec, float(r["Recall"]))
        chk("sweep %.2f f1" % t, f1, float(r["F1"]))
        chk("sweep %.2f acc" % t, acc, float(r["Accuracy"]))
        chk("sweep %.2f flagged" % t, flag, float(r["Flagged_fraction"]))
    chk("sweep best F1", 0.6821, float(sw["F1"].max()))

    # ------------------------------------------------- Table: chronological arm
    tm = csv(D, "temporal_metrics.csv").set_index(["Model", "Threshold_rule"])
    for mdl, rule, cut, acc, prec, rec, f1, auc in [
            ("XGBoost", "default_0.5", 0.50, 0.7383, 0.5529, 0.6770, 0.6087, 0.8074),
            ("XGBoost", "tuned_on_train", 0.38, 0.6960, 0.4965, 0.8101, 0.6157, 0.8074),
            ("LightGBM", "default_0.5", 0.50, 0.7353, 0.5481, 0.6806, 0.6072, 0.8061),
            ("LightGBM", "tuned_on_train", 0.35, 0.6899, 0.4908, 0.8380, 0.6190, 0.8061),
            ("CatBoost", "default_0.5", 0.50, 0.7323, 0.5422, 0.7045, 0.6128, 0.8076),
            ("CatBoost", "tuned_on_train", 0.46, 0.7221, 0.5264, 0.7548, 0.6202, 0.8076),
            ("RandomForest", "default_0.5", 0.50, 0.7441, 0.5687, 0.6160, 0.5914, 0.8021),
            ("RandomForest", "tuned_on_train", 0.41, 0.7164, 0.5189, 0.7805, 0.6233, 0.8021)]:
        r = tm.loc[(mdl, rule)]
        chk("temporal %s/%s cut" % (mdl, rule), cut, float(r["Threshold"]), tol=0.005)
        chk("temporal %s/%s acc" % (mdl, rule), acc, float(r["Accuracy"]))
        chk("temporal %s/%s prec" % (mdl, rule), prec, float(r["Precision"]))
        chk("temporal %s/%s rec" % (mdl, rule), rec, float(r["Recall"]))
        chk("temporal %s/%s f1" % (mdl, rule), f1, float(r["F1"]))
        chk("temporal %s/%s auc" % (mdl, rule), auc, float(r["AUC"]))

    dr = js(D, "drift_summary.json")
    chk("drift train median handle", 4.28, dr["train_median_handle_hours"], tol=0.006)
    chk("drift eval median handle", 2.79, dr["eval_median_handle_hours"], tol=0.006)
    chk("drift train breach rate", 0.366, dr["train_breach_rate_causal"], tol=0.001)
    chk("drift eval breach rate", 0.301, dr["eval_breach_rate_causal"], tol=0.001)

    # ------------------------------------------------- calibration
    cal = js(D, "calibration_summary.json")
    chk("calibration Brier raw", 0.1694, cal["brier_raw"])
    chk("calibration Brier isotonic", 0.1619, cal["brier_isotonic"])
    chk("calibration AUC raw", 0.8258, cal["auc_raw"])
    chk("calibration AUC isotonic", 0.8253, cal["auc_isotonic"])
    chk("calibration Brier relative gain pct", 4.4,
        100.0 * (cal["brier_raw"] - cal["brier_isotonic"]) / cal["brier_raw"], tol=0.06)

    # ------------------------------------------------- F1 fitted constants
    fp = js(F, "fitted_parameters.json")
    chk("F1 lambda", 0.0174, fp["s5_freshness_decay"]["lambda_per_hour"], tol=0.00006)
    chk("F1 lambda CI low", 0.0161, fp["s5_freshness_decay"]["ci95"][0], tol=0.00006)
    chk("F1 lambda CI high", 0.0188, fp["s5_freshness_decay"]["ci95"][1], tol=0.00006)
    chk("F1 lambda n", 31394, fp["s5_freshness_decay"]["n"], tol=0.5)
    chk("F1 delay rows excluded", 171, fp["s5_freshness_decay"]["rows_excluded_above_cap"], tol=0.5)
    chk("F1 urgency k", 0.0228, fp["s6_urgency_ramp"]["k"], tol=0.00006)
    chk("F1 urgency tau0", -12.03, fp["s6_urgency_ramp"]["tau0_hours"], tol=0.006)
    chk("F1 expertise zero fraction", 0.3004, fp["s3_expertise"]["exact_zero_fraction"])
    chk("F1 expertise zero cells", 206214, fp["s3_expertise"]["exact_zero_cells"], tol=0.5)
    chk("F1 expertise cells total", 686430,
        fp["s3_expertise"]["eval_shape"][0] * fp["s3_expertise"]["eval_shape"][1], tol=0.5)
    chk("F1 jain index", 0.3301, fp["s4_workload"]["jain_index"])
    chk("F1 workload sd", 516.7, fp["s4_workload"]["std_dev"], tol=0.06)
    chk("F1 workload cv", 1.424, fp["s4_workload"]["coefficient_of_variation"], tol=0.0006)  # 3dp claim
    hl = 0.6931471805599453 / fp["s5_freshness_decay"]["lambda_per_hour"]
    chk("F1 freshness half-life hours", 39.9, hl, tol=0.06)

    # ------------------------------------------------- criteria, weights, rank stability
    dm = csv(F, "decision_matrix_stats.csv").set_index("criterion")
    for crit, mean, sd, zeros, conflict in [
            ("s1_outcome", 0.5036, 0.2958, 0, 5.397),
            ("s2_sla_survival", 0.5629, 0.2904, 16352, 5.349),
            ("s3_expertise", 0.3608, 0.4225, 109012, 5.996),
            ("s4_workload", 0.3301, 0.0000, 0, 6.063),
            ("s5_freshness", 0.9470, 0.1289, 0, 6.529),
            ("s6_urgency", 0.4169, 0.0659, 0, 6.960),
            ("s7_importance", 0.1984, 0.1690, 0, 5.576)]:
        chk("criterion %s mean" % crit, mean, float(dm.loc[crit, "mean"]))
        chk("criterion %s sd" % crit, sd, float(dm.loc[crit, "std"]))
        chk("criterion %s zeros" % crit, zeros, float(dm.loc[crit, "exact_zeros"]), tol=0.5)
        chk("criterion %s conflict" % crit, conflict, float(dm.loc[crit, "critic_conflict"]), tol=0.0006)  # 3dp claim

    w = csv(F, "weights.csv").set_index("criterion")
    for crit, cr, en in [("s1_outcome", 0.1628, 0.1283), ("s2_sla_survival", 0.1589, 0.1032),
                         ("s3_expertise", 0.2582, 0.4687), ("s4_workload", 0.1223, 0.0000),
                         ("s5_freshness", 0.0913, 0.0071), ("s6_urgency", 0.0783, 0.0065),
                         ("s7_importance", 0.1283, 0.2861)]:
        chk("weight critic %s" % crit, cr, float(w.loc[crit, "critic"]))
        chk("weight entropy %s" % crit, en, float(w.loc[crit, "entropy"]))

    rs = csv(F, "rank_stability.csv").set_index("pair")["spearman_rho"]
    chk("rho critic vs entropy", 0.946, float(rs["critic_vs_entropy"]), tol=0.0006)  # 3dp claim
    chk("rho critic vs equal", 0.959, float(rs["critic_vs_equal"]), tol=0.0006)  # 3dp claim
    chk("rho entropy vs equal", 0.856, float(rs["entropy_vs_equal"]), tol=0.0006)  # 3dp claim

    # ------------------------------------------------- power-mean sweep
    ps = csv(F, "p_sweep.csv").set_index("p")
    for p, mean, mn, mx in [(-4.0, 0.0949, 0.0012, 0.5190), (-2.0, 0.1135, 0.0014, 0.6361),
                            (-1.0, 0.1336, 0.0018, 0.7114), (-0.5, 0.1565, 0.0031, 0.7488),
                            (0.0, 0.2390, 0.0116, 0.7828), (0.25, 0.2918, 0.0209, 0.7982),
                            (0.5, 0.3631, 0.0558, 0.8123), (1.0, 0.4495, 0.1387, 0.8371),
                            (2.0, 0.5494, 0.2553, 0.8743), (4.0, 0.6659, 0.3602, 0.9162),
                            (8.0, 0.7787, 0.4480, 0.9504)]:
        chk("psweep p=%s mean" % p, mean, float(ps.loc[p, "mean"]))
        chk("psweep p=%s min" % p, mn, float(ps.loc[p, "min"]))
        chk("psweep p=%s max" % p, mx, float(ps.loc[p, "max"]))
    mono = ps["mean"].is_monotonic_increasing
    CHECKS.append(("PASS" if mono else "FAIL", "psweep mean monotone in p", True, mono))
    if not mono:
        globals()["FAILS"] = FAILS + 1

    # ------------------------------------------------- F2 hazard
    hz = js(F, "hazard_model.json")
    chk("F2 person-period rows", 161720, hz["person_period_rows"], tol=0.5)
    chk("F2 cases", 39449, hz["cases"], tol=0.5)
    chk("F2 expansion factor", 4.10, hz["expansion"], tol=0.006)
    chk("F2 bin resolution rate", 0.2430, hz["bin_resolution_rate"])
    chk("F2 hazard held-out AUC", 0.7640, hz["heldout_resolution_hazard_auc"])
    chk("F2 derived breach AUC", 0.8373, hz["derived_breach_auc_at_intake"])
    chk("F2 direct classifier AUC", 0.7879, hz["static_model_breach_auc_for_comparison"])
    chk("F2 improvement", 0.0494,
        hz["derived_breach_auc_at_intake"] - hz["static_model_breach_auc_for_comparison"])
    chk("F2 frozen threshold", 0.5250, hz["threshold"])
    CHECKS.append(("PASS" if hz["survival_monotonic"] else "FAIL",
                   "F2 survival monotone", True, hz["survival_monotonic"]))

    # ------------------------------------------------- SHAP attribution
    sh = csv("results_bpi_leakfree_7030", "shap_importance_leakfree.csv").set_index("Feature")["Mean_Abs_SHAP"]
    chk("shap group breach rate", 0.6559, float(sh["Group_Breach_Rate_TE"]), tol=0.0006)  # 3dp claim
    chk("shap WBS", 0.1606, float(sh["WBS_Encoded"]), tol=0.0006)  # 3dp claim
    chk("shap assignment delay", 0.4909, float(sh["Assignment_Delay_Hours"]), tol=0.0006)  # 3dp claim
    chk("shap alert status is zero", 0.0, float(sh["Alert_Status_Encoded"]))

    # ------------------------------------------------- Table: tuning and ensembling
    tu = csv("results_bpi_leakfree_7030", "model_b_tuned_ensemble.csv").set_index("Model")
    for name, auc, acc, prec, rec, f1, brier in [
            ("XGBoost_untuned", 0.8297, 0.7460, 0.6306, 0.7444, 0.6828, 0.1675),
            ("LightGBM_untuned", 0.8266, 0.7414, 0.6243, 0.7432, 0.6786, 0.1694),
            ("CatBoost_untuned", 0.8226, 0.7372, 0.6172, 0.7488, 0.6767, 0.1719),
            ("RandomForest_untuned", 0.8253, 0.7427, 0.6276, 0.7364, 0.6777, 0.1689),
            ("XGBoost_tuned", 0.8329, 0.7525, 0.6417, 0.7383, 0.6866, 0.1649),
            ("LightGBM_tuned", 0.8316, 0.7490, 0.6358, 0.7416, 0.6846, 0.1661),
            ("SoftVote(XGBt,LGBMt,CatBoost)", 0.8325, 0.7489, 0.6349, 0.7444, 0.6853, 0.1658),
            ("SoftVote(4 models)", 0.8322, 0.7486, 0.6348, 0.7430, 0.6847, 0.1659),
            ("Stacking(LR meta)", 0.8332, 0.7643, 0.6983, 0.6308, 0.6628, 0.1592)]:
        r = tu.loc[name]
        chk("tuning %s AUC" % name, auc, float(r["Test_AUC"]))
        chk("tuning %s accuracy" % name, acc, float(r["Accuracy"]))
        chk("tuning %s precision" % name, prec, float(r["Precision"]))
        chk("tuning %s recall" % name, rec, float(r["Recall"]))
        chk("tuning %s F1" % name, f1, float(r["F1"]))
        chk("tuning %s Brier" % name, brier, float(r["Brier"]))

    sg = csv("results_bpi_leakfree_7030", "model_b_significance.csv").set_index("Model")
    for name, delta, lo, hi, sig in [
            ("XGBoost_tuned", 0.0032, 0.0019, 0.0046, True),
            ("LightGBM_tuned", 0.0019, 0.0001, 0.0037, True),
            ("SoftVote_3", 0.0028, 0.0018, 0.0038, True),
            ("SoftVote_4", 0.0025, 0.0015, 0.0035, True),
            ("Stacking", 0.0035, 0.0024, 0.0046, True)]:
        r = sg.loc[name]
        chk("tuning sig %s delta" % name, delta, float(r["Diff_vs_untuned"]))
        chk("tuning sig %s CI low" % name, lo, float(r["CI_low"]))
        chk("tuning sig %s CI high" % name, hi, float(r["CI_high"]))
        CHECKS.append(("PASS" if bool(r["Significant"]) == sig else "FAIL",
                       "tuning sig %s significant" % name, sig, bool(r["Significant"])))
        if bool(r["Significant"]) != sig:
            globals()["FAILS"] = FAILS + 1
    chk("tuning best gain vs admissibility cost ratio", 24.8,
        _cost_of_admissibility() / float(sg.loc["XGBoost_tuned", "Diff_vs_untuned"]),
        tol=0.6)

    # ------------------------------------------------- report
    #
    # --dump prints EVERY check, passing and failing, in the order chk was called. That order
    # is what lets refresh_claims.py line the reported checks up one-to-one with the claim
    # literals it finds in this file's source. Matching failures by their value alone is not
    # enough: a value like 0.19 or 0.85 appears in a dozen places, and the run that motivated
    # this flag silently rewrote the wrong occurrence of several of them.
    if "--dump" in sys.argv:
        for status, label, claimed, actual in CHECKS:
            print("CHECK\t%s\t%s\t%s\t%s" % (status, label, claimed, actual))

    width = max(len(c[1]) for c in CHECKS)
    for status, label, claimed, actual in CHECKS:
        if status == "FAIL":
            print("  FAIL  %-*s  paper=%s  artifact=%s" % (width, label, claimed, actual))
    print("=" * 72)
    print("  %d checks, %d passed, %d FAILED" % (len(CHECKS), len(CHECKS) - FAILS, FAILS))
    print("=" * 72)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
