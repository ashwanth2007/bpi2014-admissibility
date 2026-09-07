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


def chk(label, claimed, actual, tol=0.00051):
    global FAILS
    ok = actual is not None and abs(float(claimed) - float(actual)) <= tol
    if not ok:
        FAILS += 1
    CHECKS.append(("PASS" if ok else "FAIL", label, claimed, actual))


def main() -> int:
    D = "results_deep_7030"
    F = "results_formulations"

    # ------------------------------------------------- Table: dataset statistics
    st = csv(D, "dataset_stats.csv").set_index("Property")["Value"]
    for label, claim in [("Raw incident records", 31238),
                         ("Raw activity events", 210837),
                         ("Raw interaction records", 68358),
                         ("Incidents in the modelling matrix", 31236),
                         ("Distinct first-assignment groups", 117),
                         ("Distinct configuration-item types", 13),
                         ("Distinct configuration-item subtypes", 58),
                         ("Distinct service components (WBS)", 259),
                         ("Breach cases (positive class)", 11444),
                         ("Non-breach cases (negative class)", 19792),
                         ("Training rows (random split)", 21865),
                         ("Test rows (random split)", 9371),
                         ("Training rows (chronological)", 24988),
                         ("Test rows (chronological)", 6248),
                         ("Observation window (days)", 1055)]:
        chk("dataset: " + label, claim, float(st[label]), tol=0.5)
    chk("dataset: positive class rate", 0.3664, float(st["Positive class rate"]))

    # ------------------------------------------------- Table: per-priority breakdown
    pri = csv(D, "priority_breakdown.csv").set_index("Priority")
    for p, n, med, thr, br in [(2, 521, 3.68, 7.36, 0.3359),
                               (3, 4347, 1.58, 3.17, 0.3759),
                               (4, 14810, 4.05, 8.10, 0.3629),
                               (5, 11558, 8.37, 16.74, 0.3686)]:
        chk("priority %d n" % p, n, float(pri.loc[p, "n"]), tol=0.5)
        chk("priority %d median handle" % p, med, float(pri.loc[p, "median_handle_hours"]), tol=0.006)
        chk("priority %d threshold" % p, thr, float(pri.loc[p, "threshold_hours"]), tol=0.006)
        chk("priority %d breach rate" % p, br, float(pri.loc[p, "breach_rate"]))

    # ------------------------------------------------- Table: cost of admissibility
    m = csv(D, "full_metrics.csv").set_index(["Config", "Model"])
    pt = csv(D, "paired_tests.csv")
    cost = pt[pt.Config == "cost_of_admissibility"].set_index("Model")
    for mdl, unc, adm, lo, hi, share in [
            ("XGBoost", 0.8814, 0.7859, 0.0838, 0.1074, 25.0),
            ("LightGBM", 0.8795, 0.7825, 0.0850, 0.1087, 25.6),
            ("CatBoost", 0.8737, 0.7701, 0.0918, 0.1165, 27.7),
            ("RandomForest", 0.8720, 0.7727, 0.0876, 0.1115, 26.7)]:
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
            ("contaminated_v1", "XGBoost", 0.7915, 0.6873, 0.7906, 0.7920, 0.7353, 0.5684, 0.8814, 0.8242, 0.1386),
            ("contaminated_v1", "LightGBM", 0.7867, 0.6804, 0.7876, 0.7861, 0.7301, 0.5594, 0.8795, 0.8208, 0.1403),
            ("contaminated_v1", "CatBoost", 0.7853, 0.6796, 0.7833, 0.7865, 0.7277, 0.5558, 0.8737, 0.8138, 0.1438),
            ("contaminated_v1", "RandomForest", 0.7856, 0.6872, 0.7614, 0.7996, 0.7224, 0.5504, 0.8720, 0.8068, 0.1430),
            ("leakfree", "XGBoost", 0.7213, 0.6074, 0.6761, 0.7474, 0.6399, 0.4152, 0.7859, 0.6888, 0.1861),
            ("leakfree", "LightGBM", 0.7166, 0.6020, 0.6679, 0.7447, 0.6333, 0.4048, 0.7825, 0.6843, 0.1881),
            ("leakfree", "CatBoost", 0.7015, 0.5818, 0.6589, 0.7262, 0.6179, 0.3766, 0.7701, 0.6686, 0.1939),
            ("leakfree", "RandomForest", 0.7185, 0.6155, 0.6170, 0.7772, 0.6162, 0.3940, 0.7727, 0.6672, 0.1900)]:
        r = m.loc[(cfg, mdl)]
        for name, claim, col in [("acc", acc, "Accuracy"), ("prec", prec, "Precision"),
                                 ("rec", rec, "Recall"), ("spec", spec, "Specificity"),
                                 ("f1", f1, "F1"), ("mcc", mcc, "MCC"), ("auc", auc, "AUC"),
                                 ("ap", ap, "AveragePrecision"), ("brier", brier, "Brier")]:
            chk("full %s/%s %s" % (cfg, mdl, name), claim, float(r[col]))

    # ------------------------------------------------- Table: confusion matrices
    cm = csv(D, "confusion_matrices.csv").set_index(["Config", "Model"])
    for cfg, mdl, tn, fp, fn, tp, npv in [
            ("contaminated_v1", "XGBoost", 4703, 1235, 719, 2714, 0.8674),
            ("contaminated_v1", "LightGBM", 4668, 1270, 729, 2704, 0.8649),
            ("contaminated_v1", "CatBoost", 4670, 1268, 744, 2689, 0.8626),
            ("contaminated_v1", "RandomForest", 4748, 1190, 819, 2614, 0.8529),
            ("leakfree", "XGBoost", 4438, 1500, 1112, 2321, 0.7996),
            ("leakfree", "LightGBM", 4422, 1516, 1140, 2293, 0.7950),
            ("leakfree", "CatBoost", 4312, 1626, 1171, 2262, 0.7864),
            ("leakfree", "RandomForest", 4615, 1323, 1315, 2118, 0.7782)]:
        r = cm.loc[(cfg, mdl)]
        for name, claim, col in [("TN", tn, "TN"), ("FP", fp, "FP"),
                                 ("FN", fn, "FN"), ("TP", tp, "TP")]:
            chk("cm %s/%s %s" % (cfg, mdl, name), claim, float(r[col]), tol=0.5)
        chk("cm %s/%s NPV" % (cfg, mdl), npv, float(r["NPV"]))
    # derived statements made in prose
    chk("prose: breaches lost to admissibility",
        393, float(cm.loc[("contaminated_v1", "XGBoost"), "TP"])
        - float(cm.loc[("leakfree", "XGBoost"), "TP"]), tol=0.5)
    chk("prose: extra false alarms",
        265, float(cm.loc[("leakfree", "XGBoost"), "FP"])
        - float(cm.loc[("contaminated_v1", "XGBoost"), "FP"]), tol=0.5)

    # ------------------------------------------------- Table: feature ladder
    lad = csv("results_bpi_leakfree_7030", "feature_ladder_leakfree.csv").set_index("Feature_Set")
    for key, cv, auc, f1, acc, prec, rec in [
            ("3_static_only", 0.5111, 0.5076, 0.4376, 0.5006, 0.3725, 0.5304),
            ("8_plus_temporal", 0.5576, 0.5616, 0.4440, 0.5555, 0.4098, 0.4844),
            ("10_plus_operational", 0.6516, 0.6574, 0.5015, 0.6188, 0.4814, 0.5234),
            ("15_plus_config", 0.7764, 0.7767, 0.6327, 0.7075, 0.5859, 0.6877),
            ("17_plus_group_TE", 0.7850, 0.7859, 0.6399, 0.7213, 0.6074, 0.6761)]:
        r = lad.loc[key]
        chk("ladder %s CV" % key, cv, float(r["CV_AUC"]))
        chk("ladder %s AUC" % key, auc, float(r["Test_AUC"]))
        chk("ladder %s F1" % key, f1, float(r["Test_F1"]))
        chk("ladder %s acc" % key, acc, float(r["Test_Accuracy"]))
        chk("ladder %s prec" % key, prec, float(r["Test_Precision"]))
        chk("ladder %s rec" % key, rec, float(r["Test_Recall"]))
    chk("ladder rung3 to rung4 jump", 0.1193,
        float(lad.loc["15_plus_config", "Test_AUC"] - lad.loc["10_plus_operational", "Test_AUC"]))
    chk("ladder rung4 to rung5 jump", 0.0092,
        float(lad.loc["17_plus_group_TE", "Test_AUC"] - lad.loc["15_plus_config", "Test_AUC"]))

    # ------------------------------------------------- Table: learning curve
    lc = csv(D, "learning_curve.csv").set_index("Train_fraction")
    for frac, n, tr, te in [(0.05, 1093, 0.9977, 0.6723), (0.10, 2186, 0.9868, 0.7075),
                            (0.20, 4373, 0.9535, 0.7394), (0.30, 6559, 0.9282, 0.7580),
                            (0.40, 8746, 0.9120, 0.7644), (0.50, 10932, 0.9042, 0.7723),
                            (0.60, 13119, 0.8903, 0.7773), (0.70, 15305, 0.8807, 0.7785),
                            (0.85, 18585, 0.8718, 0.7822), (1.00, 21865, 0.8647, 0.7844)]:
        r = lc.loc[frac]
        chk("lc %.2f n" % frac, n, float(r["Train_n"]), tol=0.5)
        chk("lc %.2f train AUC" % frac, tr, float(r["Train_AUC"]))
        chk("lc %.2f test AUC" % frac, te, float(r["Test_AUC"]))
    chk("lc gap at full size", 0.0804, float(lc.loc[1.00, "Gap"]))
    chk("lc gap at smallest size", 0.3254, float(lc.loc[0.05, "Gap"]))

    # ------------------------------------------------- Table: cross-validation folds
    cv = csv(D, "cv_folds.csv")
    cvl = cv[cv.Config == "leakfree"]
    for mdl, folds, mean, sd in [
            ("XGBoost", [0.7840, 0.7869, 0.7938, 0.7844, 0.7759], 0.7850, 0.0064),
            ("LightGBM", [0.7785, 0.7859, 0.7890, 0.7840, 0.7741], 0.7823, 0.0060),
            ("RandomForest", [0.7694, 0.7785, 0.7788, 0.7768, 0.7603], 0.7728, 0.0079),
            ("CatBoost", [0.7672, 0.7723, 0.7781, 0.7713, 0.7647], 0.7707, 0.0051)]:
        s = cvl[cvl.Model == mdl].sort_values("Fold")["AUC"].values
        for k, f in enumerate(folds, start=1):
            chk("cv %s fold %d" % (mdl, k), f, float(s[k - 1]))
        chk("cv %s mean" % mdl, mean, float(s.mean()))
        chk("cv %s sd" % mdl, sd, float(pd.Series(s).std(ddof=1)))

    # ------------------------------------------------- Table: threshold sweep
    sw = csv(D, "threshold_sweep.csv").set_index("Threshold")
    for t, prec, rec, f1, acc, flag in [
            (0.20, 0.4203, 0.9729, 0.5870, 0.4985, 0.8480),
            (0.30, 0.4691, 0.9053, 0.6180, 0.5900, 0.7070),
            (0.40, 0.5378, 0.7955, 0.6418, 0.6746, 0.5419),
            (0.45, 0.5766, 0.7387, 0.6477, 0.7056, 0.4693),
            (0.50, 0.6074, 0.6761, 0.6399, 0.7213, 0.4077),
            (0.60, 0.6715, 0.5435, 0.6008, 0.7354, 0.2966),
            (0.70, 0.7398, 0.4026, 0.5214, 0.7293, 0.1993),
            (0.80, 0.8227, 0.2284, 0.3575, 0.6993, 0.1017),
            (0.90, 0.9197, 0.0801, 0.1474, 0.6604, 0.0319)]:
        r = sw.loc[t]
        chk("sweep %.2f prec" % t, prec, float(r["Precision"]))
        chk("sweep %.2f rec" % t, rec, float(r["Recall"]))
        chk("sweep %.2f f1" % t, f1, float(r["F1"]))
        chk("sweep %.2f acc" % t, acc, float(r["Accuracy"]))
        chk("sweep %.2f flagged" % t, flag, float(r["Flagged_fraction"]))
    chk("sweep best F1", 0.6477, float(sw["F1"].max()))

    # ------------------------------------------------- Table: chronological arm
    tm = csv(D, "temporal_metrics.csv").set_index(["Model", "Threshold_rule"])
    for mdl, rule, cut, acc, prec, rec, f1, auc in [
            ("XGBoost", "default_0.5", 0.50, 0.7033, 0.6308, 0.0913, 0.1596, 0.6533),
            ("XGBoost", "tuned_on_train", 0.13, 0.4691, 0.3553, 0.8858, 0.5072, 0.6533),
            ("LightGBM", "default_0.5", 0.50, 0.7076, 0.6471, 0.1142, 0.1941, 0.6456),
            ("LightGBM", "tuned_on_train", 0.16, 0.4854, 0.3589, 0.8500, 0.5047, 0.6456),
            ("CatBoost", "default_0.5", 0.50, 0.7079, 0.6564, 0.1111, 0.1900, 0.6387),
            ("CatBoost", "tuned_on_train", 0.21, 0.4627, 0.3448, 0.8246, 0.4863, 0.6387),
            ("RandomForest", "default_0.5", 0.50, 0.6977, 0.7714, 0.0280, 0.0541, 0.6310),
            ("RandomForest", "tuned_on_train", 0.22, 0.4595, 0.3429, 0.8210, 0.4837, 0.6310)]:
        r = tm.loc[(mdl, rule)]
        chk("temporal %s/%s cut" % (mdl, rule), cut, float(r["Threshold"]), tol=0.006)
        chk("temporal %s/%s acc" % (mdl, rule), acc, float(r["Accuracy"]))
        chk("temporal %s/%s prec" % (mdl, rule), prec, float(r["Precision"]))
        chk("temporal %s/%s rec" % (mdl, rule), rec, float(r["Recall"]))
        chk("temporal %s/%s f1" % (mdl, rule), f1, float(r["F1"]))
        chk("temporal %s/%s auc" % (mdl, rule), auc, float(r["AUC"]))

    dr = js(D, "drift_summary.json")
    chk("drift train median handle", 4.73, dr["train_median_handle_hours"], tol=0.006)
    chk("drift eval median handle", 3.37, dr["eval_median_handle_hours"], tol=0.006)
    chk("drift train breach rate", 0.366, dr["train_breach_rate_causal"], tol=0.001)
    chk("drift eval breach rate", 0.308, dr["eval_breach_rate_causal"], tol=0.001)

    # ------------------------------------------------- calibration
    cal = js(D, "calibration_summary.json")
    chk("calibration Brier raw", 0.1861, cal["brier_raw"])
    chk("calibration Brier isotonic", 0.1769, cal["brier_isotonic"])
    chk("calibration AUC raw", 0.7859, cal["auc_raw"])
    chk("calibration AUC isotonic", 0.7858, cal["auc_isotonic"])
    chk("calibration Brier relative gain pct", 4.9,
        100.0 * (cal["brier_raw"] - cal["brier_isotonic"]) / cal["brier_raw"], tol=0.06)

    # ------------------------------------------------- F1 fitted constants
    fp = js(F, "fitted_parameters.json")
    chk("F1 lambda", 0.0191, fp["s5_freshness_decay"]["lambda_per_hour"], tol=0.00006)
    chk("F1 lambda CI low", 0.0161, fp["s5_freshness_decay"]["ci95"][0], tol=0.00006)
    chk("F1 lambda CI high", 0.0226, fp["s5_freshness_decay"]["ci95"][1], tol=0.00006)
    chk("F1 lambda n", 7041, fp["s5_freshness_decay"]["n"], tol=0.5)
    chk("F1 delay rows excluded", 5627, fp["s5_freshness_decay"]["rows_excluded_as_artifacts"], tol=0.5)
    chk("F1 urgency k", 0.0248, fp["s6_urgency_ramp"]["k"], tol=0.00006)
    chk("F1 urgency tau0", -5.63, fp["s6_urgency_ramp"]["tau0_hours"], tol=0.006)
    chk("F1 expertise zero fraction", 0.3165, fp["s3_expertise"]["exact_zero_fraction"])
    chk("F1 expertise zero cells", 50102, fp["s3_expertise"]["exact_zero_cells"], tol=0.5)
    chk("F1 expertise cells total", 158300,
        fp["s3_expertise"]["eval_shape"][0] * fp["s3_expertise"]["eval_shape"][1], tol=0.5)
    chk("F1 jain index", 0.3758, fp["s4_workload"]["jain_index"])
    chk("F1 workload sd", 326.4, fp["s4_workload"]["std_dev"], tol=0.06)
    chk("F1 workload cv", 1.289, fp["s4_workload"]["coefficient_of_variation"], tol=0.0006)
    hl = 0.6931471805599453 / fp["s5_freshness_decay"]["lambda_per_hour"]
    chk("F1 freshness half-life hours", 36.3, hl, tol=0.06)

    # ------------------------------------------------- criteria, weights, rank stability
    dm = csv(F, "decision_matrix_stats.csv").set_index("criterion")
    for crit, mean, sd, zeros, conflict in [
            ("s1_outcome", 0.5276, 0.3168, 0, 5.363),
            ("s2_sla_survival", 0.6208, 0.2738, 4093, 5.371),
            ("s3_expertise", 0.3664, 0.4247, 50102, 6.010),
            ("s4_workload", 0.3758, 0.0000, 0, 6.024),
            ("s5_freshness", 0.9251, 0.1741, 0, 6.457),
            ("s6_urgency", 0.4501, 0.0922, 0, 7.107),
            ("s7_importance", 0.1898, 0.1710, 0, 5.691)]:
        chk("criterion %s mean" % crit, mean, float(dm.loc[crit, "mean"]))
        chk("criterion %s sd" % crit, sd, float(dm.loc[crit, "std"]))
        chk("criterion %s zeros" % crit, zeros, float(dm.loc[crit, "exact_zeros"]), tol=0.5)
        chk("criterion %s conflict" % crit, conflict, float(dm.loc[crit, "critic_conflict"]), tol=0.0006)

    w = csv(F, "weights.csv").set_index("criterion")
    for crit, cr, en in [("s1_outcome", 0.1605, 0.1320), ("s2_sla_survival", 0.1391, 0.0756),
                         ("s3_expertise", 0.2409, 0.4583), ("s4_workload", 0.1226, 0.0000),
                         ("s5_freshness", 0.1107, 0.0146), ("s6_urgency", 0.1036, 0.0107),
                         ("s7_importance", 0.1226, 0.3087)]:
        chk("weight critic %s" % crit, cr, float(w.loc[crit, "critic"]))
        chk("weight entropy %s" % crit, en, float(w.loc[crit, "entropy"]))

    rs = csv(F, "rank_stability.csv").set_index("pair")["spearman_rho"]
    chk("rho critic vs entropy", 0.935, float(rs["critic_vs_entropy"]), tol=0.0006)
    chk("rho critic vs equal", 0.957, float(rs["critic_vs_equal"]), tol=0.0006)
    chk("rho entropy vs equal", 0.837, float(rs["entropy_vs_equal"]), tol=0.0006)

    # ------------------------------------------------- power-mean sweep
    ps = csv(F, "p_sweep.csv").set_index("p")
    for p, mean, mn, mx in [(-4.0, 0.0978, 0.0013, 0.5645), (-2.0, 0.1185, 0.0017, 0.6613),
                            (-1.0, 0.1412, 0.0027, 0.7230), (-0.5, 0.1672, 0.0055, 0.7532),
                            (0.0, 0.2598, 0.0256, 0.7813), (0.25, 0.3173, 0.0396, 0.7945),
                            (0.5, 0.3909, 0.0834, 0.8068), (1.0, 0.4777, 0.1663, 0.8291),
                            (2.0, 0.5760, 0.2859, 0.8642), (4.0, 0.6873, 0.3905, 0.9069),
                            (8.0, 0.7921, 0.4667, 0.9473)]:
        chk("psweep p=%s mean" % p, mean, float(ps.loc[p, "mean"]))
        chk("psweep p=%s min" % p, mn, float(ps.loc[p, "min"]))
        chk("psweep p=%s max" % p, mx, float(ps.loc[p, "max"]))
    mono = ps["mean"].is_monotonic_increasing
    CHECKS.append(("PASS" if mono else "FAIL", "psweep mean monotone in p", True, mono))
    if not mono:
        globals()["FAILS"] = FAILS + 1

    # ------------------------------------------------- F2 hazard
    hz = js(F, "hazard_model.json")
    chk("F2 person-period rows", 69867, hz["person_period_rows"], tol=0.5)
    chk("F2 cases", 15828, hz["cases"], tol=0.5)
    chk("F2 expansion factor", 4.41, hz["expansion"], tol=0.006)
    chk("F2 bin resolution rate", 0.2260, hz["bin_resolution_rate"])
    chk("F2 hazard held-out AUC", 0.7344, hz["heldout_resolution_hazard_auc"])
    chk("F2 derived breach AUC", 0.8017, hz["derived_breach_auc_at_intake"])
    chk("F2 direct classifier AUC", 0.7399, hz["static_model_breach_auc_for_comparison"])
    chk("F2 improvement", 0.0618,
        hz["derived_breach_auc_at_intake"] - hz["static_model_breach_auc_for_comparison"])
    chk("F2 frozen threshold", 0.5305, hz["threshold"])
    CHECKS.append(("PASS" if hz["survival_monotonic"] else "FAIL",
                   "F2 survival monotone", True, hz["survival_monotonic"]))

    # ------------------------------------------------- SHAP attribution
    sh = csv("results_bpi_leakfree_7030", "shap_importance_leakfree.csv").set_index("Feature")["Mean_Abs_SHAP"]
    chk("shap group breach rate", 0.4366, float(sh["Group_Breach_Rate_TE"]), tol=0.0006)
    chk("shap WBS", 0.2859, float(sh["WBS_Encoded"]), tol=0.0006)
    chk("shap assignment delay", 0.1962, float(sh["Assignment_Delay_Hours"]), tol=0.0006)
    chk("shap alert status is zero", 0.0, float(sh["Alert_Status_Encoded"]))

    # ------------------------------------------------- Table: tuning and ensembling
    tu = csv("results_bpi_leakfree_7030", "model_b_tuned_ensemble.csv").set_index("Model")
    for name, auc, acc, prec, rec, f1, brier in [
            ("XGBoost_untuned", 0.7859, 0.7213, 0.6074, 0.6761, 0.6399, 0.1861),
            ("LightGBM_untuned", 0.7825, 0.7166, 0.6020, 0.6679, 0.6333, 0.1881),
            ("CatBoost_untuned", 0.7701, 0.7015, 0.5818, 0.6589, 0.6179, 0.1939),
            ("RandomForest_untuned", 0.7727, 0.7185, 0.6155, 0.6170, 0.6162, 0.1900),
            ("XGBoost_tuned", 0.7907, 0.7291, 0.6183, 0.6805, 0.6479, 0.1830),
            ("LightGBM_tuned", 0.7865, 0.7232, 0.6089, 0.6834, 0.6440, 0.1858),
            ("SoftVote(XGBt,LGBMt,CatBoost)", 0.7876, 0.7213, 0.6076, 0.6752, 0.6396, 0.1856),
            ("SoftVote(4 models)", 0.7863, 0.7208, 0.6093, 0.6633, 0.6351, 0.1857),
            ("Stacking(LR meta)", 0.7904, 0.7367, 0.6679, 0.5596, 0.6090, 0.1759)]:
        r = tu.loc[name]
        chk("tuning %s AUC" % name, auc, float(r["Test_AUC"]))
        chk("tuning %s accuracy" % name, acc, float(r["Accuracy"]))
        chk("tuning %s precision" % name, prec, float(r["Precision"]))
        chk("tuning %s recall" % name, rec, float(r["Recall"]))
        chk("tuning %s F1" % name, f1, float(r["F1"]))
        chk("tuning %s Brier" % name, brier, float(r["Brier"]))

    sg = csv("results_bpi_leakfree_7030", "model_b_significance.csv").set_index("Model")
    for name, delta, lo, hi, sig in [
            ("XGBoost_tuned", 0.0048, 0.0027, 0.0068, True),
            ("LightGBM_tuned", 0.0005, -0.0017, 0.0028, False),
            ("SoftVote_3", 0.0016, 0.0001, 0.0032, True),
            ("SoftVote_4", 0.0004, -0.0014, 0.0021, False),
            ("Stacking", 0.0044, 0.0027, 0.0061, True)]:
        r = sg.loc[name]
        chk("tuning sig %s delta" % name, delta, float(r["Diff_vs_untuned"]))
        chk("tuning sig %s CI low" % name, lo, float(r["CI_low"]))
        chk("tuning sig %s CI high" % name, hi, float(r["CI_high"]))
        CHECKS.append(("PASS" if bool(r["Significant"]) == sig else "FAIL",
                       "tuning sig %s significant" % name, sig, bool(r["Significant"])))
        if bool(r["Significant"]) != sig:
            globals()["FAILS"] = FAILS + 1
    chk("tuning best gain vs admissibility cost ratio", 20.0,
        0.0955 / float(sg.loc["XGBoost_tuned", "Diff_vs_untuned"]), tol=0.6)

    # ------------------------------------------------- report
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
