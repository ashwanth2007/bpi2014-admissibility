"""The two figures the manuscript was missing.

Deepika J asked for an architecture diagram. The paper had eighteen figures and every one
was a results plot: nothing showed the mechanism or the system.

Two are drawn here, and deliberately not the third that was suggested. The generic
data-to-preprocessing-to-model-to-evaluation flowchart is not drawn, because it would be
the only figure in the paper that supports no claim. Every other figure carries evidence,
and one decorative box diagram among them reads as filler to exactly the reader whose
opinion matters.

  fig19  the admissibility mechanism. A case timeline with the decision boundary on it,
         what is observable at that instant above the line and what is only recorded later
         below it, and the two rules cutting the feature vector and the encoding window.
         This is the paper's actual contribution and no existing figure shows it.

  fig20  the system pipeline, in the shape a supervisor expects: five logs, one protocol,
         the learners, the two scoring formulations, the assignment layer, evaluation.

Same conventions as every other figure: the Okabe-Ito colour-blind-safe palette, serif at
9pt, vector PDF plus 600 DPI PNG through the shared save helper.

    python figures/make_architecture_figures.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch, Rectangle

from pathlib import Path

CODE = Path(__file__).resolve().parent.parent
OUT = CODE / "figures_out"
OUT.mkdir(exist_ok=True)
DPI = 600

C = {"blue": "#0072B2", "orange": "#E69F00", "green": "#009E73", "red": "#D55E00",
     "purple": "#CC79A7", "yellow": "#F0E442", "sky": "#56B4E9", "grey": "#666666"}

plt.rcParams.update({
    "font.family": "serif", "font.size": 9, "axes.titlesize": 10,
    "axes.labelsize": 9, "xtick.labelsize": 8, "ytick.labelsize": 8,
    "axes.spines.top": False, "axes.spines.right": False,
})


def save(fig, name):
    pdf, png = OUT / (name + ".pdf"), OUT / (name + ".png")
    fig.savefig(pdf, format="pdf", bbox_inches="tight")
    fig.savefig(png, format="png", dpi=DPI, bbox_inches="tight")
    plt.close(fig)
    print("  [ok] %-40s pdf %7.1f KB   png %8.1f KB"
          % (name, pdf.stat().st_size / 1024, png.stat().st_size / 1024))


def box(ax, x, y, w, h, text, fc, ec=None, fs=8, tc="black", lw=1.0):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.012,rounding_size=0.02",
                                linewidth=lw, edgecolor=ec or fc, facecolor=fc, zorder=2))
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center",
            fontsize=fs, color=tc, zorder=3, linespacing=1.35)


def arrow(ax, xy, xytext, color="#333333", lw=1.1, style="-|>"):
    ax.add_patch(FancyArrowPatch(xytext, xy, arrowstyle=style, mutation_scale=11,
                                 linewidth=lw, color=color, zorder=4,
                                 shrinkA=1.5, shrinkB=1.5))


# =====================================================================  fig19
def fig_mechanism():
    fig, ax = plt.subplots(figsize=(7.4, 4.5))
    ax.set_xlim(0, 10); ax.set_ylim(0, 6.4); ax.axis("off")

    # the case timeline
    ax.annotate("", xy=(9.5, 3.2), xytext=(0.5, 3.2),
                arrowprops=dict(arrowstyle="-|>", color="#333333", linewidth=1.4))
    # The decision label sits to the RIGHT of its tick: centred, it collides with the dashed
    # boundary line that shares the same x. Checked by rendering, not by reading the code.
    for x, lab, ha, dx in [(1.2, "case\nopens", "center", 0.0),
                           (4.2, "decision\n$t_{dec}$", "left", 0.18),
                           (8.6, "case\nresolves", "center", 0.0)]:
        ax.plot([x], [3.2], marker="o", ms=6, color=C["grey"], zorder=5)
        ax.text(x + dx, 2.92, lab, ha=ha, va="top", fontsize=8, color="#333333")

    # the boundary
    ax.plot([4.2, 4.2], [2.62, 6.05], color=C["red"], linewidth=1.6, linestyle="--", zorder=1)
    ax.text(4.32, 6.12, "the decision boundary", fontsize=8.5, color=C["red"], va="bottom")

    ax.text(2.35, 5.72, "OBSERVABLE at $t_{dec}$", ha="center", fontsize=8.5,
            color=C["green"], weight="bold")
    for i, t in enumerate(["priority, impact, urgency",
                           "configuration item, category",
                           "arrival hour, weekday",
                           "queue length at $t_{dec}$",
                           "group history over resolved cases"]):
        box(ax, 0.55, 5.28 - i * 0.42, 3.35, 0.34, t, "#E8F5F0", C["green"], fs=7.6)

    ax.text(7.0, 5.72, "RECORDED, but only LATER", ha="center", fontsize=8.5,
            color=C["red"], weight="bold")
    for i, t in enumerate(["reassignment count",
                           "groups touched",
                           "total activity events",
                           "reopen flag",
                           "related incident count"]):
        box(ax, 4.75, 5.28 - i * 0.42, 3.35, 0.34, t, "#FDEEE6", C["red"], fs=7.6)

    # the two rules
    box(ax, 0.55, 1.15, 3.9, 1.15,
        "Rule 1, feature admissibility\n"
        r"$x_i$ must be $\mathcal{F}_{t_{dec}}$-measurable",
        "#EAF2FA", C["blue"], fs=8)
    box(ax, 5.05, 1.15, 4.2, 1.15,
        "Rule 2, encoding admissibility\n"
        r"$\mathcal{P}(i,g)=\{j: g_j=g,\ t^{res}_j < t^{open}_i\}$",
        "#EAF2FA", C["blue"], fs=8)

    arrow(ax, (2.5, 2.30), (2.35, 3.05), color=C["blue"])
    arrow(ax, (7.15, 2.30), (5.6, 3.05), color=C["blue"])

    ax.text(5.0, 0.62,
            "Enforcing both is what the paper measures the cost of. On BPI 2014 it removes a "
            "fifth of all\nabove-chance signal; across five logs the share ranges far more widely "
            "than that.",
            ha="center", va="center", fontsize=7.6, color="#333333")
    save(fig, "fig19_admissibility_mechanism")


# =====================================================================  fig20
def fig_pipeline():
    fig, ax = plt.subplots(figsize=(7.4, 5.0))
    ax.set_xlim(0, 10); ax.set_ylim(0, 7.0); ax.axis("off")

    ax.text(5.0, 6.72, "One protocol, five event logs, four domains",
            ha="center", fontsize=9.5, weight="bold", color="#222222")

    logs = [("BPI 2014\nIT service mgmt", C["blue"]),
            ("BPI 2017\nlending", C["orange"]),
            ("Sepsis\nacute care", C["green"]),
            ("Hospital Billing\nhealth admin", C["purple"]),
            ("Traffic Fines\npublic admin", C["sky"])]
    for i, (t, col) in enumerate(logs):
        box(ax, 0.30 + i * 1.90, 5.55, 1.72, 0.78, t, "white", col, fs=7.2)
        arrow(ax, (1.16 + i * 1.90, 5.10), (1.16 + i * 1.90, 5.52), color=col)

    box(ax, 0.30, 4.30, 9.40, 0.78,
        "Case projection: decision point $t_{dec}$, outcome, stratum, group\n"
        "fixed per log BEFORE any score was computed",
        "#F2F2F2", C["grey"], fs=8)
    arrow(ax, (5.0, 3.85), (5.0, 4.27))

    box(ax, 0.30, 3.05, 4.55, 0.78,
        "Rule 1: admissible feature set", "#EAF2FA", C["blue"], fs=8)
    box(ax, 5.15, 3.05, 4.55, 0.78,
        "Rule 2: past-set target encoding", "#EAF2FA", C["blue"], fs=8)
    arrow(ax, (5.0, 2.60), (5.0, 3.02))

    box(ax, 0.30, 1.80, 4.55, 0.78,
        "Four learners\nXGBoost, LightGBM, CatBoost, Random Forest",
        "white", C["grey"], fs=7.6)
    box(ax, 5.15, 1.80, 4.55, 0.78,
        "Contaminated control arm\nsame learners, inadmissible attributes restored",
        "#FDEEE6", C["red"], fs=7.6)
    arrow(ax, (2.55, 1.35), (2.55, 1.77))
    arrow(ax, (7.45, 1.35), (7.45, 1.77))

    box(ax, 0.30, 0.85, 9.40, 0.48,
        "The cost of admissibility = contaminated AUC minus admissible AUC",
        "#FFF9E0", C["yellow"], fs=8.2)

    box(ax, 0.30, 0.05, 4.55, 0.62,
        "Application, BPI 2014 only\nF1 assignment score, F2 SLA hazard",
        "white", C["green"], fs=7.4)
    box(ax, 5.15, 0.05, 4.55, 0.62,
        "Application, BPI 2014 only\nmulti-objective assignment study",
        "white", C["green"], fs=7.4)
    save(fig, "fig20_system_pipeline")


if __name__ == "__main__":
    print("architecture figures")
    fig_mechanism()
    fig_pipeline()
    print("  written to", OUT)
