"""
Predictive-layer figures: class balance, confusion, ROC, PR, learning curves,
calibration, threshold behaviour, attribution and fold spread.

Same contract as make_figures.py. Every figure is rendered twice, vector PDF
for the journal and 600 DPI PNG for the faculty instruction, and every figure
reads an artifact written by evaluation/deep_metrics.py. Nothing is drawn from
a number typed by hand.

Run:  python figures/make_ml_figures.py
"""

from __future__ import annotations
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import _openmp_first  # noqa: F401  MUST precede sklearn/xgboost/catboost, see module docstring

import json
import warnings
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
CODE = HERE.parent
OUT = CODE / "figures_out"
OUT.mkdir(exist_ok=True)
DEEP = CODE / "results_deep_7030"
LEAK = CODE / "results_bpi_leakfree_7030"

DPI = 600
C = {"blue": "#0072B2", "orange": "#E69F00", "green": "#009E73", "red": "#D55E00",
     "purple": "#CC79A7", "yellow": "#F0E442", "sky": "#56B4E9", "grey": "#666666"}
MODEL_C = {"XGBoost": C["blue"], "LightGBM": C["orange"],
           "CatBoost": C["green"], "RandomForest": C["purple"]}

plt.rcParams.update({
    "font.family": "serif", "font.size": 9, "axes.titlesize": 10, "axes.labelsize": 9,
    "legend.fontsize": 8, "xtick.labelsize": 8, "ytick.labelsize": 8,
    "axes.spines.top": False, "axes.spines.right": False,
    "figure.constrained_layout.use": True,
})

MADE, SKIPPED = [], []


def save(fig, name):
    pdf, png = OUT / (name + ".pdf"), OUT / (name + ".png")
    fig.savefig(pdf, format="pdf", bbox_inches="tight")
    fig.savefig(png, format="png", dpi=DPI, bbox_inches="tight")
    plt.close(fig)
    MADE.append(name)
    print("  [ok] %-38s pdf %7.1f KB   png %8.1f KB"
          % (name, pdf.stat().st_size / 1024, png.stat().st_size / 1024))


def need(*paths):
    missing = [p for p in paths if not p.exists()]
    if missing:
        SKIPPED.append((paths[0].stem, [m.name for m in missing]))
        print("  [skip] missing: " + ", ".join(m.name for m in missing))
        return False
    return True


# ------------------------------------------------------- 10. class balance and priority mix
def fig_class_balance():
    a, b = DEEP / "dataset_stats.csv", DEEP / "priority_breakdown.csv"
    if not need(a, b):
        return
    st = pd.read_csv(a).set_index("Property")["Value"]
    pri = pd.read_csv(b)
    pos = int(st["Breach cases (positive class)"])
    neg = int(st["Non-breach cases (negative class)"])

    fig, ax = plt.subplots(1, 2, figsize=(7.2, 2.9))
    ax[0].bar(["No breach", "Breach"], [neg, pos], color=[C["sky"], C["red"]], width=0.55)
    for i, v in enumerate([neg, pos]):
        ax[0].text(i, v, format(v, ",") + "\n%.1f%%" % (100 * v / (pos + neg)),
                   ha="center", va="bottom", fontsize=8)
    ax[0].set_ylabel("Incidents")
    ax[0].set_ylim(0, max(neg, pos) * 1.22)
    ax[0].set_title("(a) Class balance, %s incidents" % format(pos + neg, ","))

    x = np.arange(len(pri))
    ax[1].bar(x, pri["n"], color=C["grey"], width=0.55, label="Incidents")
    ax[1].set_xticks(x)
    ax[1].set_xticklabels(["P" + str(int(p)) for p in pri["Priority"]])
    ax[1].set_ylabel("Incidents")
    ax2 = ax[1].twinx()
    ax2.plot(x, 100 * pri["breach_rate"], "o-", color=C["red"], label="Breach rate")
    ax2.set_ylabel("Breach rate (%)", color=C["red"])
    ax2.set_ylim(0, 50)
    ax2.spines["right"].set_visible(True)
    ax[1].set_title("(b) Volume and breach rate by priority")
    save(fig, "fig10_class_balance")


# ------------------------------------------------------- 11. confusion matrices
def fig_confusion():
    p = DEEP / "confusion_matrices.csv"
    if not need(p):
        return
    cm = pd.read_csv(p)
    panels = [("contaminated_v1", "XGBoost", "(a) Unconstrained, XGBoost"),
              ("leakfree", "XGBoost", "(b) Admissible, XGBoost"),
              ("leakfree", "LightGBM", "(c) Admissible, LightGBM"),
              ("leakfree", "RandomForest", "(d) Admissible, Random Forest")]
    fig, axes = plt.subplots(1, 4, figsize=(7.4, 2.35))
    for ax, (cfg, model, title) in zip(axes, panels):
        r = cm[(cm.Config == cfg) & (cm.Model == model)].iloc[0]
        M = np.array([[r.TN, r.FP], [r.FN, r.TP]], dtype=float)
        Mn = M / M.sum(axis=1, keepdims=True)
        ax.imshow(Mn, cmap="Blues", vmin=0, vmax=1)
        for i in range(2):
            for j in range(2):
                ax.text(j, i, format(int(M[i, j]), ",") + "\n%.1f%%" % (100 * Mn[i, j]),
                        ha="center", va="center", fontsize=7.5,
                        color="white" if Mn[i, j] > 0.55 else "black")
        ax.set_xticks([0, 1]); ax.set_yticks([0, 1])
        ax.set_xticklabels(["Pred. no", "Pred. breach"], fontsize=7)
        ax.set_yticklabels(["No breach", "Breach"], fontsize=7)
        ax.set_title(title, fontsize=8.5)
        for s in ax.spines.values():
            s.set_visible(False)
    save(fig, "fig11_confusion_matrices")


# ------------------------------------------------------- 12. ROC curves
def fig_roc():
    p, m = DEEP / "roc_curves.csv", DEEP / "full_metrics.csv"
    if not need(p, m):
        return
    roc, met = pd.read_csv(p), pd.read_csv(m)
    fig, axes = plt.subplots(1, 2, figsize=(7.2, 3.2))
    for ax, cfg, title in [(axes[0], "leakfree", "(a) Admissible configuration"),
                           (axes[1], "contaminated_v1", "(b) Unconstrained configuration")]:
        for model, g in roc[roc.Config == cfg].groupby("Model"):
            auc = met[(met.Config == cfg) & (met.Model == model)]["AUC"].iloc[0]
            ax.plot(g.FPR, g.TPR, color=MODEL_C[model], lw=1.3,
                    label="%s (%.4f)" % (model, auc))
        ax.plot([0, 1], [0, 1], "--", color=C["grey"], lw=0.9)
        ax.set_xlabel("False positive rate")
        ax.set_ylabel("True positive rate")
        ax.set_title(title)
        ax.legend(loc="lower right", frameon=False, title="AUC")
    save(fig, "fig12_roc_curves")


# ------------------------------------------------------- 13. precision-recall curves
def fig_pr():
    p, m = DEEP / "pr_curves.csv", DEEP / "full_metrics.csv"
    if not need(p, m):
        return
    pr, met = pd.read_csv(p), pd.read_csv(m)
    base = met[met.Config == "leakfree"].iloc[0]
    prevalence = (base.TP + base.FN) / (base.TP + base.FN + base.TN + base.FP)
    fig, axes = plt.subplots(1, 2, figsize=(7.2, 3.2))
    for ax, cfg, title in [(axes[0], "leakfree", "(a) Admissible configuration"),
                           (axes[1], "contaminated_v1", "(b) Unconstrained configuration")]:
        for model, g in pr[pr.Config == cfg].groupby("Model"):
            ap = met[(met.Config == cfg) & (met.Model == model)]["AveragePrecision"].iloc[0]
            g = g.sort_values("Recall")
            ax.plot(g.Recall, g.Precision, color=MODEL_C[model], lw=1.3,
                    label="%s (%.4f)" % (model, ap))
        ax.axhline(prevalence, ls="--", color=C["grey"], lw=0.9)
        ax.text(0.02, prevalence + 0.012, "chance = %.3f" % prevalence, fontsize=7,
                color=C["grey"])
        ax.set_xlabel("Recall")
        ax.set_ylabel("Precision")
        ax.set_ylim(0, 1)
        ax.set_title(title)
        ax.legend(loc="upper right", frameon=False, title="Average precision")
    save(fig, "fig13_pr_curves")


# ------------------------------------------------------- 14. learning curve
def fig_learning_curve():
    p = DEEP / "learning_curve.csv"
    if not need(p):
        return
    lc = pd.read_csv(p)
    fig, ax = plt.subplots(1, 2, figsize=(7.2, 3.0))
    ax[0].plot(lc.Train_n, lc.Train_AUC, "o-", color=C["orange"], lw=1.4, label="Training AUC")
    ax[0].plot(lc.Train_n, lc.Test_AUC, "s-", color=C["blue"], lw=1.4, label="Held-out AUC")
    ax[0].fill_between(lc.Train_n, lc.Test_AUC, lc.Train_AUC, color=C["grey"], alpha=0.13)
    ax[0].set_xscale("log")
    ax[0].set_xlabel("Training incidents (log scale)")
    ax[0].set_ylabel("ROC-AUC")
    ax[0].set_title("(a) Learning curve, admissible XGBoost")
    ax[0].legend(frameon=False, loc="center right")

    ax[1].plot(lc.Train_n, lc.Gap, "o-", color=C["red"], lw=1.4)
    ax[1].set_xscale("log")
    ax[1].set_xlabel("Training incidents (log scale)")
    ax[1].set_ylabel("Training AUC minus held-out AUC")
    ax[1].set_title("(b) Generalisation gap")
    ax[1].axhline(0, color=C["grey"], lw=0.8)
    save(fig, "fig14_learning_curve")


# ------------------------------------------------------- 15. calibration
def fig_calibration():
    p, s = DEEP / "calibration_bins.csv", DEEP / "calibration_summary.json"
    if not need(p, s):
        return
    cal = pd.read_csv(p)
    su = json.load(open(s))
    fig, ax = plt.subplots(figsize=(3.7, 3.4))
    ax.plot([0, 1], [0, 1], "--", color=C["grey"], lw=0.9, label="Perfect")
    ax.plot(cal.mean_predicted_raw, cal.observed_raw, "o-", color=C["blue"], lw=1.3,
            label="Raw (Brier %.4f)" % su["brier_raw"])
    ax.plot(cal.mean_predicted_isotonic, cal.observed_isotonic, "s-", color=C["green"], lw=1.3,
            label="Isotonic (Brier %.4f)" % su["brier_isotonic"])
    ax.set_xlabel("Mean predicted probability")
    ax.set_ylabel("Observed breach fraction")
    ax.set_title("Reliability, admissible XGBoost")
    ax.legend(frameon=False, loc="upper left")
    save(fig, "fig15_calibration")


# ------------------------------------------------------- 16. threshold sweep
def fig_threshold():
    p = DEEP / "threshold_sweep.csv"
    if not need(p):
        return
    sw = pd.read_csv(p)
    best = sw.loc[sw.F1.idxmax()]
    fig, ax = plt.subplots(figsize=(4.4, 3.2))
    ax.plot(sw.Threshold, sw.Precision, "o-", color=C["blue"], lw=1.3, ms=3, label="Precision")
    ax.plot(sw.Threshold, sw.Recall, "s-", color=C["red"], lw=1.3, ms=3, label="Recall")
    ax.plot(sw.Threshold, sw.F1, "^-", color=C["green"], lw=1.5, ms=3, label="$F_1$")
    ax.axvline(0.5, ls=":", color=C["grey"], lw=1.0)
    ax.annotate("default 0.50\n$F_1$=%.4f" % sw[sw.Threshold == 0.5].F1.iloc[0],
                xy=(0.5, 0.18), fontsize=7, ha="left", color=C["grey"])
    ax.axvline(best.Threshold, ls="--", color=C["purple"], lw=1.0)
    ax.annotate("best %.2f\n$F_1$=%.4f" % (best.Threshold, best.F1),
                xy=(best.Threshold - 0.02, 0.05), fontsize=7, ha="right", color=C["purple"])
    ax.set_xlabel("Decision threshold")
    ax.set_ylabel("Score")
    ax.set_title("Operating point, admissible XGBoost")
    ax.legend(frameon=False, loc="upper center", ncol=3, columnspacing=1.0)
    ax.set_ylim(0, 1.12)
    save(fig, "fig16_threshold_sweep")


# ------------------------------------------------------- 17. SHAP attribution
def fig_shap():
    p = LEAK / "shap_importance_leakfree.csv"
    if not need(p):
        return
    sh = pd.read_csv(p).sort_values("Mean_Abs_SHAP", ascending=True)
    sh = sh[sh.Mean_Abs_SHAP > 0]
    fig, ax = plt.subplots(figsize=(4.6, 3.8))
    ax.barh(sh.Feature.str.replace("_", " "), sh.Mean_Abs_SHAP, color=C["blue"], height=0.68)
    for y, v in enumerate(sh.Mean_Abs_SHAP):
        ax.text(v + 0.005, y, "%.3f" % v, va="center", fontsize=6.5)
    ax.set_xlabel("Mean absolute SHAP value")
    ax.set_title("Attribution, admissible LightGBM")
    ax.set_xlim(0, sh.Mean_Abs_SHAP.max() * 1.18)
    save(fig, "fig17_shap_importance")


# ------------------------------------------------------- 18. fold spread and intervals
def fig_folds():
    p, q = DEEP / "cv_folds.csv", DEEP / "bootstrap_ci.csv"
    if not need(p, q):
        return
    cv, bs = pd.read_csv(p), pd.read_csv(q)
    order = ["XGBoost", "LightGBM", "CatBoost", "RandomForest"]
    fig, ax = plt.subplots(1, 2, figsize=(7.2, 3.0))

    data = [cv[(cv.Config == "leakfree") & (cv.Model == m)].AUC.values for m in order]
    bp = ax[0].boxplot(data, tick_labels=order, widths=0.55, patch_artist=True)
    for patch, m in zip(bp["boxes"], order):
        patch.set_facecolor(MODEL_C[m]); patch.set_alpha(0.45)
    for med in bp["medians"]:
        med.set_color("black")
    for i, m in enumerate(order, start=1):
        v = cv[(cv.Config == "leakfree") & (cv.Model == m)].AUC.values
        ax[0].scatter(np.full(len(v), i), v, s=9, color=MODEL_C[m], zorder=3)
    ax[0].set_ylabel("Cross-validation AUC")
    ax[0].set_title("(a) Five-fold spread, admissible")
    ax[0].tick_params(axis="x", labelrotation=12)

    y = np.arange(len(order))
    for cfg, off, col, lab in [("leakfree", -0.16, C["blue"], "Admissible"),
                               ("contaminated_v1", 0.16, C["red"], "Unconstrained")]:
        sub = bs[bs.Config == cfg].set_index("Model").loc[order]
        ax[1].errorbar(sub.AUC, y + off,
                       xerr=[sub.AUC - sub.CI_low, sub.CI_high - sub.AUC],
                       fmt="o", color=col, ms=4, capsize=2.5, lw=1.2, label=lab)
    ax[1].set_yticks(y)
    ax[1].set_yticklabels(order)
    ax[1].invert_yaxis()
    ax[1].set_xlabel("Held-out AUC with 95% bootstrap interval")
    ax[1].set_title("(b) Test AUC, 2000 resamples")
    ax[1].legend(frameon=False, loc="lower center")
    save(fig, "fig18_fold_spread_and_intervals")


def main():
    print("=" * 92)
    print("  Rendering predictive-layer figures (vector PDF + 600 DPI PNG)")
    print("=" * 92)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for fn in [fig_class_balance, fig_confusion, fig_roc, fig_pr, fig_learning_curve,
                   fig_calibration, fig_threshold, fig_shap, fig_folds]:
            try:
                fn()
            except Exception as e:
                print("  [FAIL] %s: %s: %s" % (fn.__name__, type(e).__name__, e))
                SKIPPED.append((fn.__name__, [str(e)[:90]]))
    print("=" * 92)
    print("  %d figures written to %s" % (len(MADE), OUT))
    for n, why in SKIPPED:
        print("    skipped %s: %s" % (n, why))
    print("=" * 92)


if __name__ == "__main__":
    main()
