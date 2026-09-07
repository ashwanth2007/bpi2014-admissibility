"""
Publication figures for the BPI 2014 admissibility paper.
=========================================================

Every figure is rendered TWICE from the same axes:

  * PDF, vector. Scientific Reports and the Nature portfolio prefer vector EPS or PDF for
    line art and charts, and explicitly do not list PNG among accepted formats.
  * PNG at 600 DPI. Deepika J's instruction on 2026-09-04, verbatim: "When I write in Python,
    I will set 600 DPI... Then only the clarity will be good. If it is not in PNG format, it
    will break."

Those two instructions conflict, so both are satisfied rather than one being chosen. Costs
nothing but disk.

Every figure reads a real artifact from disk. Nothing is drawn from a number typed by hand,
and any figure whose inputs are missing is SKIPPED with a warning rather than faked.

Run:  python figures/make_figures.py
"""
from __future__ import annotations

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

DPI = 600
# Colour-blind safe (Okabe-Ito). Never red/green alone to carry meaning.
C = {"blue": "#0072B2", "orange": "#E69F00", "green": "#009E73", "red": "#D55E00",
     "purple": "#CC79A7", "yellow": "#F0E442", "sky": "#56B4E9", "grey": "#666666"}

plt.rcParams.update({
    "font.family": "serif",
    "font.size": 9,
    "axes.titlesize": 10,
    "axes.labelsize": 9,
    "legend.fontsize": 8,
    "xtick.labelsize": 8,
    "ytick.labelsize": 8,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "figure.constrained_layout.use": True,
})

MADE, SKIPPED = [], []


def save(fig, name: str) -> None:
    pdf, png = OUT / f"{name}.pdf", OUT / f"{name}.png"
    fig.savefig(pdf, format="pdf", bbox_inches="tight")
    fig.savefig(png, format="png", dpi=DPI, bbox_inches="tight")
    plt.close(fig)
    MADE.append((name, pdf.stat().st_size, png.stat().st_size))
    print(f"  [ok] {name:38s} pdf {pdf.stat().st_size/1024:7.1f} KB   png {png.stat().st_size/1024:8.1f} KB")


def need(*paths: Path) -> bool:
    missing = [p for p in paths if not p.exists()]
    if missing:
        SKIPPED.append((paths[0].stem, [str(m.name) for m in missing]))
        print(f"  [skip] missing: {', '.join(m.name for m in missing)}")
        return False
    return True


# ---------------------------------------------------------------- 1. admissibility ladder
def fig_admissibility_ladder():
    a = CODE / "results_bpi_leakfree_7030" / "feature_ladder_leakfree.csv"
    b = CODE / "results_bpi_leakfree" / "feature_ladder_leakfree.csv"
    if not need(a, b):
        return
    d70, d80 = pd.read_csv(a), pd.read_csv(b)
    fig, ax = plt.subplots(figsize=(5.0, 3.2))
    ax.plot(d70["Num_Features"], d70["Test_AUC"], "o-", color=C["blue"],
            lw=1.8, ms=5, label="70/30 split")
    ax.plot(d80["Num_Features"], d80["Test_AUC"], "s--", color=C["orange"],
            lw=1.5, ms=4.5, label="80/20 split")
    ax.axhline(0.5, color=C["grey"], lw=0.8, ls=":", zorder=0)
    ax.text(19.6, 0.504, "chance", fontsize=7, color=C["grey"], ha="right", va="bottom")

    g70 = d70["Test_AUC"].iloc[-1] - d70["Test_AUC"].iloc[1]
    ax.annotate("", xy=(17, d70["Test_AUC"].iloc[-1]), xytext=(17, d70["Test_AUC"].iloc[1]),
                arrowprops=dict(arrowstyle="<->", color=C["green"], lw=1.4))
    ax.text(17.4, (d70["Test_AUC"].iloc[-1] + d70["Test_AUC"].iloc[1]) / 2,
            f"+{g70:.4f}\nAUC", fontsize=8, color=C["green"], va="center")

    ax.set_xlabel("Number of decision-time admissible features")
    ax.set_ylabel("Held-out test AUC")
    ax.set_title("Admissibility ladder: every feature observable at assignment")
    ax.set_xticks(d70["Num_Features"])
    ax.set_xlim(2, 20.5)
    ax.legend(frameon=False, loc="lower right")
    save(fig, "fig01_admissibility_ladder")


# ------------------------------------------------------------- 2. cost of admissibility
def fig_leakage_waterfall():
    p = CODE / "results_bpi_leakfree_7030" / "model_comparison_leakfree.csv"
    if not need(p):
        return
    d = pd.read_csv(p)
    x = d[d.Model == "XGBoost"].set_index("Config")["Test_AUC"]
    labels = ["Unconstrained\n(default practice)", "Admissible\n(random split)",
              "Admissible\n(chronological)"]
    vals = [x.get("contaminated_v1"), x.get("leakfree"), x.get("leakfree_temporal")]
    if any(v is None or (isinstance(v, float) and np.isnan(v)) for v in vals):
        SKIPPED.append(("fig02", ["config rows"]))
        return
    fig, ax = plt.subplots(figsize=(4.4, 3.2))
    bars = ax.bar(labels, vals, color=[C["red"], C["blue"], C["purple"]], width=0.6)
    for bar, v in zip(bars, vals):
        ax.text(bar.get_x() + bar.get_width() / 2, v + 0.008, f"{v:.4f}",
                ha="center", fontsize=8.5)
    ax.axhline(0.5, color=C["grey"], lw=0.8, ls=":")
    drop = vals[0] - vals[1]
    pct = 100 * drop / (vals[0] - 0.5)
    ax.set_ylim(0.45, 1.0)
    ax.set_ylabel("Held-out test AUC, XGBoost")
    ax.set_title(f"Cost of admissibility: {drop:.4f} AUC, {pct:.1f}% of\nabove-chance signal was leakage",
                 fontsize=9.5)
    save(fig, "fig02_cost_of_admissibility")


# ---------------------------------------------------------------- 3. model comparison
def fig_model_comparison():
    p = CODE / "results_bpi_leakfree_7030" / "model_comparison_leakfree.csv"
    if not need(p):
        return
    d = pd.read_csv(p)
    cfgs = ["contaminated_v1", "leakfree", "leakfree_temporal"]
    names = ["Unconstrained", "Admissible", "Admissible chronological"]
    models = ["XGBoost", "LightGBM", "CatBoost", "RandomForest"]
    fig, ax = plt.subplots(figsize=(5.6, 3.2))
    w, xs = 0.26, np.arange(len(models))
    for i, (cfg, nm, col) in enumerate(zip(cfgs, names, [C["red"], C["blue"], C["purple"]])):
        sub = d[d.Config == cfg].set_index("Model")["Test_AUC"]
        ax.bar(xs + (i - 1) * w, [sub.get(m, np.nan) for m in models], w, label=nm, color=col)
    ax.axhline(0.5, color=C["grey"], lw=0.8, ls=":")
    ax.axhline(0.8, color=C["green"], lw=1.0, ls="--")
    ax.text(3.45, 0.806, "0.80", fontsize=7, color=C["green"])
    ax.set_xticks(xs); ax.set_xticklabels(models)
    ax.set_ylim(0.45, 0.95)
    ax.set_ylabel("Held-out test AUC")
    ax.set_title("All four learners, all three admissibility configurations")
    ax.legend(frameon=False, ncol=3, loc="upper center", bbox_to_anchor=(0.5, -0.16))
    save(fig, "fig03_model_comparison")


# ------------------------------------------------------------------- 4. p sensitivity
def fig_p_sweep():
    p = CODE / "results_formulations" / "p_sweep.csv"
    if not need(p):
        return
    d = pd.read_csv(p)
    fig, ax = plt.subplots(figsize=(5.0, 3.2))
    ax.plot(d["p"], d["mean"], "o-", color=C["blue"], lw=1.8, ms=4.5, label="mean score")
    ax.fill_between(d["p"], d["min"], d["max"], color=C["blue"], alpha=0.15,
                    label="min to max")
    ax.axvline(0, color=C["grey"], lw=0.8, ls=":")
    ax.axvline(1, color=C["green"], lw=0.9, ls="--")
    ax.text(1.08, 0.05, "arithmetic\n(fully compensatory)", fontsize=7, color=C["green"])
    ax.text(-3.9, 0.86, "non-compensatory\n(worst criterion dominates)", fontsize=7,
            color=C["grey"])
    ax.set_xlabel("Power mean exponent $p$")
    ax.set_ylabel("Assignment score $A(i,j)$")
    ax.set_title("Compensation sensitivity: $p$ controls how much a strong\ncriterion may offset a weak one")
    ax.legend(frameon=False, loc="upper left")
    save(fig, "fig04_power_mean_sensitivity")


# ----------------------------------------------------------------------- 5. weights
def fig_weights():
    p = CODE / "results_formulations" / "weights.csv"
    s = CODE / "results_formulations" / "rank_stability.csv"
    if not need(p, s):
        return
    d, st = pd.read_csv(p), pd.read_csv(s)
    short = [c.replace("s1_", "").replace("s2_", "").replace("s3_", "").replace("s4_", "")
             .replace("s5_", "").replace("s6_", "").replace("s7_", "").replace("_", " ")
             for c in d["criterion"]]
    fig, (ax, ax2) = plt.subplots(1, 2, figsize=(7.2, 3.0),
                                  gridspec_kw={"width_ratios": [2.3, 1]})
    xs, w = np.arange(len(short)), 0.26
    for i, (k, col) in enumerate(zip(["critic", "entropy", "equal"],
                                     [C["blue"], C["orange"], C["grey"]])):
        ax.bar(xs + (i - 1) * w, d[k], w, label=k.upper() if k == "critic" else k.capitalize(),
               color=col)
    ax.set_xticks(xs); ax.set_xticklabels(short, rotation=35, ha="right")
    ax.set_ylabel("Criterion weight")
    ax.set_title("Three weighting schemes, reported together")
    ax.set_ylim(0, max(d[["critic", "entropy", "equal"]].to_numpy().max() * 1.28, 0.5))
    ax.legend(frameon=False, ncol=3, fontsize=7.5, loc="upper left")

    pairs = [r.replace("_vs_", " vs\n") for r in st["pair"]]
    ax2.barh(pairs, st["spearman_rho"], color=C["green"], height=0.55)
    for i, v in enumerate(st["spearman_rho"]):
        ax2.text(v - 0.02, i, f"{v:.3f}", va="center", ha="right",
                 fontsize=8, color="white", fontweight="bold")
    ax2.set_xlim(0, 1.0)
    ax2.set_xlabel(r"Spearman $\rho$")
    ax2.set_title("Induced orderings agree")
    save(fig, "fig05_weights_and_rank_stability")


# ------------------------------------------------------------------ 6. criterion spread
def fig_criterion_stats():
    p = CODE / "results_formulations" / "decision_matrix_stats.csv"
    if not need(p):
        return
    d = pd.read_csv(p)
    short = [c.split("_", 1)[1].replace("_", " ") for c in d["criterion"]]
    fig, ax = plt.subplots(figsize=(5.2, 3.0))
    ax.bar(short, d["std"], color=C["sky"], label="standard deviation")
    ax.set_ylabel("Spread across lead-group pairs")
    ax.set_xticklabels(short, rotation=35, ha="right")
    ax2 = ax.twinx()
    ax2.plot(short, d["exact_zeros"], "o-", color=C["red"], lw=1.5, ms=5,
             label="exact zeros")
    ax2.set_ylabel("Exact-zero cells", color=C["red"])
    ax2.tick_params(axis="y", colors=C["red"])
    ax2.spines["right"].set_visible(True)
    ax.set_title("Criterion spread drives CRITIC; exact zeros force the\npower-mean floor for $p \\leq 0$")
    save(fig, "fig06_criterion_spread_and_zeros")


# ------------------------------------------------------------------------ main
def main():
    print("=" * 92)
    print("  Rendering publication figures (vector PDF + 600 DPI PNG)")
    print("=" * 92)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for fn in [fig_admissibility_ladder, fig_leakage_waterfall, fig_model_comparison,
                   fig_p_sweep, fig_weights, fig_criterion_stats]:
            try:
                fn()
            except Exception as e:
                print(f"  [FAIL] {fn.__name__}: {type(e).__name__}: {e}")
                SKIPPED.append((fn.__name__, [str(e)[:80]]))
    print("=" * 92)
    print(f"  {len(MADE)} figures written to {OUT}")
    if SKIPPED:
        print(f"  {len(SKIPPED)} skipped or failed:")
        for n, why in SKIPPED:
            print(f"    {n}: {why}")
    print("=" * 92)


if __name__ == "__main__":
    main()
