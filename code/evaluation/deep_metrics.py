"""
Deep evaluation metrics for the predictive layer.
=================================================

The headline table in the paper reports AUC. AUC alone does not tell a reader
where a classifier fails, whether it is overfitting, whether its probabilities
mean anything, or whether the gap between two models is larger than run-to-run
noise. This script produces the evidence for all four.

Everything is computed from the same data matrix the main pipeline uses,
imported from bpi2014_data so there is one definition and not two.

Outputs, all under code/results_deep[_tag]/:
  dataset_stats.csv        row counts, class balance, observation window
  priority_breakdown.csv   n, median handle time, threshold and breach rate per priority
  confusion_matrices.csv   TN FP FN TP plus specificity, NPV, MCC, balanced accuracy
  full_metrics.csv         every model x every configuration, fourteen metrics each
  roc_curves.csv           ROC points for every model, both configurations
  pr_curves.csv            precision-recall points for the same
  cv_folds.csv             per-fold cross-validation scores, the mean and sd evidence
  learning_curve.csv       train and test AUC against training-set size
  calibration_bins.csv     reliability-diagram bins, before and after isotonic
  threshold_sweep.csv      precision, recall and F1 against the decision threshold
  bootstrap_ci.csv         2000-resample 95 per cent intervals on test AUC
  paired_tests.csv         paired bootstrap and Wilcoxon comparisons between models

Run:  BPI_TEST_SIZE=0.3 BPI_RUN_TAG=7030 python evaluation/deep_metrics.py
"""

from __future__ import annotations
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import _openmp_first  # noqa: F401  MUST precede sklearn/xgboost/catboost, see module docstring

import json
import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from sklearn.metrics import (accuracy_score, precision_score, recall_score, f1_score,
                             roc_auc_score, brier_score_loss, confusion_matrix,
                             roc_curve, precision_recall_curve, average_precision_score,
                             matthews_corrcoef, balanced_accuracy_score, log_loss)
from sklearn.model_selection import StratifiedKFold
from sklearn.calibration import calibration_curve
from sklearn.isotonic import IsotonicRegression
from scipy import stats

import bpi2014_data as B

SEED = B.SEED
RUN_TAG = os.environ.get("BPI_RUN_TAG", "")
OUT = HERE.parent / ("results_deep" + (("_" + RUN_TAG) if RUN_TAG else ""))
OUT.mkdir(exist_ok=True)
MODELS = ["XGBoost", "LightGBM", "CatBoost", "RandomForest"]
rng = np.random.default_rng(SEED)


def thin(x, y, n=180):
    """Keep n evenly spaced points off a curve so the CSV stays readable."""
    if len(x) <= n:
        return np.asarray(x), np.asarray(y)
    idx = np.unique(np.linspace(0, len(x) - 1, n).astype(int))
    return np.asarray(x)[idx], np.asarray(y)[idx]


def score_block(y, proba, pred):
    tn, fp, fn, tp = confusion_matrix(y, pred).ravel()
    return {
        "TN": int(tn), "FP": int(fp), "FN": int(fn), "TP": int(tp),
        "Accuracy": accuracy_score(y, pred),
        "Precision": precision_score(y, pred, zero_division=0),
        "Recall": recall_score(y, pred, zero_division=0),
        "Specificity": tn / (tn + fp) if (tn + fp) else 0.0,
        "NPV": tn / (tn + fn) if (tn + fn) else 0.0,
        "F1": f1_score(y, pred, zero_division=0),
        "BalancedAcc": balanced_accuracy_score(y, pred),
        "MCC": matthews_corrcoef(y, pred),
        "AUC": roc_auc_score(y, proba),
        "AveragePrecision": average_precision_score(y, proba),
        "Brier": brier_score_loss(y, proba),
        "LogLoss": log_loss(y, np.clip(proba, 1e-9, 1 - 1e-9)),
    }


def _write_headline(out_dir):
    """The cost of admissibility and the share of above-chance signal it represents.

    Both are ratios of numbers that do have artifacts, which is exactly why neither had one:
    a derived quantity is nobody's output. The share was quoted in the abstract, in a section
    heading, in the methods conventions and in three places in the discussion, and when the
    dataset changed it moved in the tables and stayed put in all five sentences.

    Per learner, plus the XGBoost row the paper quotes as the headline.
    """
    src = (HERE.parent / ("results_bpi_leakfree" + (("_" + RUN_TAG) if RUN_TAG else ""))
           / "model_comparison_leakfree.csv")
    if not src.exists():
        print("  [warn] no model_comparison_leakfree.csv; headline.json NOT written")
        return
    m = pd.read_csv(src)
    per = {}
    for mdl in sorted(m.Model.unique()):
        x = m[m.Model == mdl].set_index("Config")["Test_AUC"]
        con, adm = float(x["contaminated_v1"]), float(x["leakfree"])
        per[mdl] = {"unconstrained_auc": con, "admissible_auc": adm,
                    "chronological_auc": float(x["leakfree_temporal"]),
                    "cost_auc": con - adm,
                    "above_chance_auc": con - 0.5,
                    "share_of_signal_pct": 100.0 * (con - adm) / (con - 0.5)}
    head = dict(per["XGBoost"])
    head["learner"] = "XGBoost"
    head["per_learner"] = per
    head["note"] = ("share_of_signal_pct = (unconstrained - admissible) / (unconstrained - 0.5). "
                    "This is the paper's headline share and the section heading that names it.")
    (out_dir / "headline.json").write_text(json.dumps(head, indent=2), encoding="utf-8")
    print("  headline: cost %.4f AUC, %.1f%% of above-chance signal"
          % (head["cost_auc"], head["share_of_signal_pct"]))


def main():
    t0 = time.time()
    b = B.build()
    d, y_all, g_all = b.d, b.y_all, b.g_all
    idx_tr, idx_te = b.idx_tr, b.idx_te

    inc, act, df = b.inc, b.act, b.df
    try:
        inter = pd.read_csv(HERE.parent / "bpi2014" / "Detail_Interaction.csv",
                            sep=";", encoding="latin1")
        n_inter = len(inter)
    except Exception:
        n_inter = -1

    open_times = d["Open Time"]
    stats_rows = [
        ("Raw incident records", len(inc)),
        ("Raw activity events", len(act)),
        ("Raw interaction records", n_inter),
        ("Incidents with a resolvable handle time and priority", len(df)),
        ("Incidents in the modelling matrix", len(d)),
        ("Distinct first-assignment groups", int(d["First_Assignment_Group"].nunique())),
        ("Distinct configuration-item subtypes", int(d["CI_Subtype_Encoded"].nunique())),
        ("Distinct configuration-item types", int(d["CI_Type_Encoded"].nunique())),
        ("Distinct service components (WBS)", int(d["WBS_Encoded"].nunique())),
        ("Admissible attributes", len(B.BASE_CLEAN) + len(B.GROUP_TE)),
        ("Breach cases (positive class)", int(y_all.sum())),
        ("Non-breach cases (negative class)", int((1 - y_all).sum())),
        ("Positive class rate", round(float(y_all.mean()), 4)),
        ("Training rows (random split)", len(idx_tr)),
        ("Test rows (random split)", len(idx_te)),
        ("Training rows (chronological)", len(b.idx_tr_time)),
        ("Test rows (chronological)", len(b.idx_te_time)),
        ("Test size fraction", b.test_size),
        ("Earliest incident open time", str(open_times.min())),
        ("Latest incident open time", str(open_times.max())),
        ("Observation window (days)", int((open_times.max() - open_times.min()).days)),
    ]
    # The paper's opening paragraph rests on these three numbers and nothing computed them.
    # They were measured once, by hand, on the truncated and mis-parsed copy of the log, and
    # then carried through every draft. The first sentence of a paper about numbers nobody
    # re-derived should not be a number nobody re-derived.
    _re = pd.to_numeric(df["# Reassignments"], errors="coerce")
    _ht = pd.to_numeric(df["Handle_Time_Hours"], errors="coerce")
    _ok = _re.notna() & _ht.notna()
    _med_re = float(_ht[_ok & (_re >= 1)].median())
    _med_no = float(_ht[_ok & (_re == 0)].median())
    stats_rows += [
        ("Incidents reassigned at least once", int((_ok & (_re >= 1)).sum())),
        ("Reassigned fraction", round(float((_re[_ok] >= 1).mean()), 6)),
        ("Median handle hours, reassigned", round(_med_re, 4)),
        ("Median handle hours, not reassigned", round(_med_no, 4)),
        ("Reassignment handle-time ratio", round(_med_re / _med_no, 4)),
    ]

    pd.DataFrame(stats_rows, columns=["Property", "Value"]).to_csv(
        OUT / "dataset_stats.csv", index=False)

    # The headline itself. "A fifth of the signal was not there" is the sentence the paper is
    # remembered by, and the share behind it was the one number in the study that no artifact
    # produced: it is a ratio of two AUCs and lived only in the prose and in a table cell that
    # recomputed it. Written here so it can be checked like everything else.
    _write_headline(OUT)

    pri = (df.groupby("Priority")
             .agg(n=("SLA_Breached", "size"),
                  median_handle_hours=("Handle_Time_Hours", "median"),
                  threshold_hours=("SLA_Threshold_Hours", "first"),
                  breach_rate=("SLA_Breached", "mean"))
             .reset_index())
    pri.to_csv(OUT / "priority_breakdown.csv", index=False)
    print(pri.to_string(index=False))

    configs = {
        "contaminated_v1": (B.CONTAMINATED, False),
        "leakfree": (B.BASE_CLEAN + B.GROUP_TE, True),
    }

    full_rows, cm_rows, roc_rows, pr_rows, cv_rows = [], [], [], [], []
    keep_proba = {}

    for cfg, (feats, use_te) in configs.items():
        base = [f for f in feats if f not in B.GROUP_TE]
        Xtr_raw, Xte_raw = d.loc[idx_tr, base], d.loc[idx_te, base]
        ytr, yte = y_all.loc[idx_tr], y_all.loc[idx_te]
        gtr, gte = g_all.loc[idx_tr], g_all.loc[idx_te]
        if use_te:
            enc = B.fit_group_encoding(gtr, ytr)
            Xtr = B.apply_group_encoding(Xtr_raw, gtr, enc)
            Xte = B.apply_group_encoding(Xte_raw, gte, enc)
        else:
            Xtr, Xte = Xtr_raw, Xte_raw

        for name in MODELS:
            skf = StratifiedKFold(5, shuffle=True, random_state=SEED)
            for k, (f_tr, f_va) in enumerate(skf.split(Xtr_raw, ytr), start=1):
                i_tr, i_va = Xtr_raw.index[f_tr], Xtr_raw.index[f_va]
                if use_te:
                    e = B.fit_group_encoding(g_all.loc[i_tr], y_all.loc[i_tr])
                    A = B.apply_group_encoding(d.loc[i_tr, base], g_all.loc[i_tr], e)
                    C = B.apply_group_encoding(d.loc[i_va, base], g_all.loc[i_va], e)
                else:
                    A, C = d.loc[i_tr, base], d.loc[i_va, base]
                m = B.get_models(y_all.loc[i_tr])[name]
                m.fit(A, y_all.loc[i_tr])
                pv = m.predict_proba(C)[:, 1]
                pl = (pv >= 0.5).astype(int)
                cv_rows.append({"Config": cfg, "Model": name, "Fold": k,
                                "AUC": roc_auc_score(y_all.loc[i_va], pv),
                                "F1": f1_score(y_all.loc[i_va], pl, zero_division=0),
                                "Accuracy": accuracy_score(y_all.loc[i_va], pl),
                                "Brier": brier_score_loss(y_all.loc[i_va], pv)})

            model = B.get_models(ytr)[name]
            model.fit(Xtr, ytr)
            proba = model.predict_proba(Xte)[:, 1]
            pred = model.predict(Xte)
            proba_tr = model.predict_proba(Xtr)[:, 1]
            blk = score_block(yte, proba, pred)
            tr_auc = roc_auc_score(ytr, proba_tr)
            blk.update({"Config": cfg, "Model": name, "Num_Features": Xtr.shape[1],
                        "Train_AUC": tr_auc, "Generalisation_gap": tr_auc - blk["AUC"]})
            full_rows.append(blk)
            cm_rows.append({"Config": cfg, "Model": name,
                            **{k: blk[k] for k in ["TN", "FP", "FN", "TP", "Specificity",
                                                   "NPV", "MCC", "BalancedAcc"]}})
            keep_proba[(cfg, name)] = (yte.values, proba)

            fpr, tpr, _ = roc_curve(yte, proba)
            fpr, tpr = thin(fpr, tpr)
            roc_rows += [{"Config": cfg, "Model": name, "FPR": float(a), "TPR": float(c)}
                         for a, c in zip(fpr, tpr)]
            prec, rec, _ = precision_recall_curve(yte, proba)
            rec2, prec2 = thin(rec, prec)
            pr_rows += [{"Config": cfg, "Model": name, "Recall": float(a), "Precision": float(c)}
                        for a, c in zip(rec2, prec2)]
            print("  %-16s %-13s AUC=%.4f F1=%.4f gap=%+.4f"
                  % (cfg, name, blk["AUC"], blk["F1"], blk["Generalisation_gap"]))

    pd.DataFrame(full_rows).to_csv(OUT / "full_metrics.csv", index=False)
    pd.DataFrame(cm_rows).to_csv(OUT / "confusion_matrices.csv", index=False)
    pd.DataFrame(roc_rows).to_csv(OUT / "roc_curves.csv", index=False)
    pd.DataFrame(pr_rows).to_csv(OUT / "pr_curves.csv", index=False)
    cv = pd.DataFrame(cv_rows)
    cv.to_csv(OUT / "cv_folds.csv", index=False)

    # ------------------------------------------------------------ learning curve
    feats, use_te = configs["leakfree"]
    base = [f for f in feats if f not in B.GROUP_TE]
    ytr, yte = y_all.loc[idx_tr], y_all.loc[idx_te]
    gte = g_all.loc[idx_te]
    lc = []
    for frac in [0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.85, 1.0]:
        n = max(200, int(len(idx_tr) * frac))
        sub = pd.Index(rng.permutation(np.asarray(idx_tr))[:n])
        e = B.fit_group_encoding(g_all.loc[sub], y_all.loc[sub])
        A = B.apply_group_encoding(d.loc[sub, base], g_all.loc[sub], e)
        C = B.apply_group_encoding(d.loc[idx_te, base], gte, e)
        m = B.get_models(y_all.loc[sub])["XGBoost"]
        m.fit(A, y_all.loc[sub])
        lc.append({"Train_fraction": frac, "Train_n": n,
                   "Train_AUC": roc_auc_score(y_all.loc[sub], m.predict_proba(A)[:, 1]),
                   "Test_AUC": roc_auc_score(yte, m.predict_proba(C)[:, 1])})
        print("  learning curve n=%6d train=%.4f test=%.4f"
              % (n, lc[-1]["Train_AUC"], lc[-1]["Test_AUC"]))
    lcdf = pd.DataFrame(lc)
    lcdf["Gap"] = lcdf["Train_AUC"] - lcdf["Test_AUC"]
    lcdf.to_csv(OUT / "learning_curve.csv", index=False)

    # ------------------------------------------------------------ calibration
    yv, pv = keep_proba[("leakfree", "XGBoost")]
    frac_pos, mean_pred = calibration_curve(yv, pv, n_bins=10, strategy="quantile")
    skf = StratifiedKFold(5, shuffle=True, random_state=SEED)
    Xtr_raw = d.loc[idx_tr, base]
    oof = np.zeros(len(idx_tr))
    for f_tr, f_va in skf.split(Xtr_raw, ytr):
        i_tr, i_va = Xtr_raw.index[f_tr], Xtr_raw.index[f_va]
        e = B.fit_group_encoding(g_all.loc[i_tr], y_all.loc[i_tr])
        m = B.get_models(y_all.loc[i_tr])["XGBoost"]
        m.fit(B.apply_group_encoding(d.loc[i_tr, base], g_all.loc[i_tr], e), y_all.loc[i_tr])
        oof[f_va] = m.predict_proba(
            B.apply_group_encoding(d.loc[i_va, base], g_all.loc[i_va], e))[:, 1]
    iso = IsotonicRegression(out_of_bounds="clip")
    iso.fit(oof, ytr.values)
    pv_cal = iso.predict(pv)
    fp_c, mp_c = calibration_curve(yv, pv_cal, n_bins=10, strategy="quantile")
    nb = min(len(mean_pred), len(mp_c))
    pd.DataFrame({"bin": range(1, nb + 1),
                  "mean_predicted_raw": mean_pred[:nb], "observed_raw": frac_pos[:nb],
                  "mean_predicted_isotonic": mp_c[:nb],
                  "observed_isotonic": fp_c[:nb]}).to_csv(OUT / "calibration_bins.csv", index=False)
    with open(OUT / "calibration_summary.json", "w") as f:
        json.dump({"brier_raw": float(brier_score_loss(yv, pv)),
                   "brier_isotonic": float(brier_score_loss(yv, pv_cal)),
                   "auc_raw": float(roc_auc_score(yv, pv)),
                   "auc_isotonic": float(roc_auc_score(yv, pv_cal))}, f, indent=1)

    # ------------------------------------------------------------ threshold sweep
    sweep = []
    for t in np.round(np.arange(0.05, 0.96, 0.05), 2):
        pr = (pv >= t).astype(int)
        sweep.append({"Threshold": float(t),
                      "Precision": precision_score(yv, pr, zero_division=0),
                      "Recall": recall_score(yv, pr, zero_division=0),
                      "F1": f1_score(yv, pr, zero_division=0),
                      "Accuracy": accuracy_score(yv, pr),
                      "Flagged_fraction": float(pr.mean())})
    pd.DataFrame(sweep).to_csv(OUT / "threshold_sweep.csv", index=False)

    # ------------------------------------------------------------ bootstrap intervals
    n_boot = 2000
    boot_rows, boot_store = [], {}
    for (cfg, name), (yv2, pv2) in keep_proba.items():
        idx = rng.integers(0, len(yv2), size=(n_boot, len(yv2)))
        aucs = np.array([roc_auc_score(yv2[i], pv2[i]) for i in idx])
        boot_store[(cfg, name)] = aucs
        boot_rows.append({"Config": cfg, "Model": name,
                          "AUC": roc_auc_score(yv2, pv2),
                          "CI_low": float(np.percentile(aucs, 2.5)),
                          "CI_high": float(np.percentile(aucs, 97.5)),
                          "SE": float(aucs.std(ddof=1))})
    pd.DataFrame(boot_rows).to_csv(OUT / "bootstrap_ci.csv", index=False)

    # ------------------------------------------------------------ paired comparisons
    pt = []
    ref = "XGBoost"
    for cfg in configs:
        a = boot_store[(cfg, ref)]
        for name in MODELS:
            if name == ref:
                continue
            diff = a - boot_store[(cfg, name)]
            pt.append({"Config": cfg, "A": ref, "B": name, "Model": name,
                       "Delta_AUC": float(diff.mean()),
                       "CI_low": float(np.percentile(diff, 2.5)),
                       "CI_high": float(np.percentile(diff, 97.5)),
                       "P_A_better": float((diff > 0).mean())})
    for name in MODELS:
        diff = boot_store[("contaminated_v1", name)] - boot_store[("leakfree", name)]
        pt.append({"Config": "cost_of_admissibility", "A": "contaminated_v1", "B": "leakfree",
                   "Model": name, "Delta_AUC": float(diff.mean()),
                   "CI_low": float(np.percentile(diff, 2.5)),
                   "CI_high": float(np.percentile(diff, 97.5)),
                   "P_A_better": float((diff > 0).mean())})
    for cfg in configs:
        base_scores = cv[(cv.Config == cfg) & (cv.Model == ref)].sort_values("Fold")["AUC"].values
        for name in MODELS:
            if name == ref:
                continue
            other = cv[(cv.Config == cfg) & (cv.Model == name)].sort_values("Fold")["AUC"].values
            w = stats.wilcoxon(base_scores, other)
            pt.append({"Config": cfg + "_cv_wilcoxon", "A": ref, "B": name, "Model": name,
                       "Delta_AUC": float(np.mean(base_scores - other)),
                       "P_value": float(w.pvalue)})
    pd.DataFrame(pt).to_csv(OUT / "paired_tests.csv", index=False)


    # ------------------------------------------------------------ chronological arm
    # Same admissible features, but the label threshold is re-derived from the training
    # period and the decision threshold is tuned on a validation slice taken from the END
    # of the training period. Both the default 0.5 cut point and the tuned one are reported,
    # because the difference between them is a threshold problem and not a ranking problem.
    ht = b.inc.set_index("Incident ID")["Handle_Time_Hours"]
    tr_t, te_t = b.idx_tr_time, b.idx_te_time
    tr_med = (d.loc[tr_t].assign(_h=d.loc[tr_t, "Incident ID"].map(ht))
                .groupby("Priority")["_h"].median())
    thr_causal = d["Priority"].map({pp: mm * 2.0 for pp, mm in tr_med.items()})
    y_causal = pd.Series((d["Incident ID"].map(ht).values > thr_causal.values).astype(int),
                         index=d.index)
    n_val = int(len(tr_t) * 0.2)
    tr_fit, tr_val = tr_t[:-n_val], tr_t[-n_val:]
    e_fit = B.fit_group_encoding(g_all.loc[tr_fit], y_causal.loc[tr_fit])
    e_full = B.fit_group_encoding(g_all.loc[tr_t], y_causal.loc[tr_t])
    trows = []
    for name in MODELS:
        mf = B.get_models(y_causal.loc[tr_fit])[name]
        mf.fit(B.apply_group_encoding(d.loc[tr_fit, base], g_all.loc[tr_fit], e_fit),
               y_causal.loc[tr_fit])
        p_val = mf.predict_proba(
            B.apply_group_encoding(d.loc[tr_val, base], g_all.loc[tr_val], e_fit))[:, 1]
        grid = np.linspace(0.05, 0.95, 91)
        cut = float(grid[int(np.argmax([f1_score(y_causal.loc[tr_val], (p_val >= q).astype(int),
                                                 zero_division=0) for q in grid]))])
        mm2 = B.get_models(y_causal.loc[tr_t])[name]
        mm2.fit(B.apply_group_encoding(d.loc[tr_t, base], g_all.loc[tr_t], e_full),
                y_causal.loc[tr_t])
        pr_te = mm2.predict_proba(
            B.apply_group_encoding(d.loc[te_t, base], g_all.loc[te_t], e_full))[:, 1]
        yv_t = y_causal.loc[te_t]
        for tag, q in [("default_0.5", 0.5), ("tuned_on_train", cut)]:
            blk = score_block(yv_t, pr_te, (pr_te >= q).astype(int))
            blk.update({"Model": name, "Threshold_rule": tag, "Threshold": round(q, 4)})
            trows.append(blk)
        print("  chronological %-13s cut=%.2f AUC=%.4f F1@0.5=%.4f F1@cut=%.4f"
              % (name, cut, trows[-1]["AUC"], trows[-2]["F1"], trows[-1]["F1"]))
    pd.DataFrame(trows).to_csv(OUT / "temporal_metrics.csv", index=False)
    with open(OUT / "drift_summary.json", "w") as f:
        json.dump({"train_median_handle_hours": float(d.loc[tr_t, "Incident ID"].map(ht).median()),
                   "eval_median_handle_hours": float(d.loc[te_t, "Incident ID"].map(ht).median()),
                   "train_breach_rate_causal": float(y_causal.loc[tr_t].mean()),
                   "eval_breach_rate_causal": float(y_causal.loc[te_t].mean()),
                   "train_open_from": str(d.loc[tr_t, "Open Time"].min().date()),
                   "train_open_to": str(d.loc[tr_t, "Open Time"].max().date()),
                   "eval_open_to": str(d.loc[te_t, "Open Time"].max().date())}, f, indent=1)

    print("\nWrote %d artifacts to %s" % (len(list(OUT.glob("*"))), OUT))
    print("Total %.1fs" % (time.time() - t0))


if __name__ == "__main__":
    main()
