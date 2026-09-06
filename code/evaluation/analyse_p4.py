"""
Phase 4 evaluation: the negative result, its ablation, complexity and runtime.
==============================================================================

Reads only artifacts that exist on disk. If the run has not finished, this exits with a
clear message rather than reporting partial numbers as final.

INPUTS   code/phase4/results/{summary,metrics}_" + TAG + ".csv, significance_" + TAG + ".json,
         " + TAG + "_run.log (for measured wall time per variant)
OUTPUTS  code/results_evaluation/    tables as CSV, one JSON summary
         code/figures_out/           fig07 ablation, fig08 pareto, fig09 runtime

Run:  python evaluation/analyse_p4.py
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
CODE = HERE.parent
RES = CODE / "phase4" / "results"
OUT = CODE / "results_evaluation"
FIGS = CODE / "figures_out"
OUT.mkdir(exist_ok=True)
FIGS.mkdir(exist_ok=True)

DPI = 600
C = {"blue": "#0072B2", "orange": "#E69F00", "green": "#009E73", "red": "#D55E00",
     "purple": "#CC79A7", "sky": "#56B4E9", "grey": "#666666"}
plt.rcParams.update({
    "font.family": "serif", "font.size": 9, "axes.titlesize": 10, "axes.labelsize": 9,
    "legend.fontsize": 8, "xtick.labelsize": 8, "ytick.labelsize": 8,
    "axes.spines.top": False, "axes.spines.right": False,
    "figure.constrained_layout.use": True,
})

PROPOSED = "PAA-NSGA-II"
CONTROL = "StandardNSGA2"
ORDER = ["Random", "RoundRobin", "FIFO", "LeastLoaded", "GreedyBestOutcome",
         CONTROL, "NSGA3", PROPOSED, "PAA-noPriority", "PAA-noAdaptive",
         "PAA-noSLAMut", "PAA-AdaptMut", "NSGA3-PrioInit"]


def save(fig, name):
    fig.savefig(FIGS / f"{name}.pdf", format="pdf", bbox_inches="tight")
    fig.savefig(FIGS / f"{name}.png", format="png", dpi=DPI, bbox_inches="tight")
    plt.close(fig)
    print(f"  [fig] {name}")


TAG = "fullv3"


def require():
    need = [RES / ("summary_%s.csv" % TAG), RES / ("metrics_%s.csv" % TAG),
            RES / ("significance_%s.json" % TAG)]
    missing = [p.name for p in need if not p.exists()]
    if missing:
        print("NOT READY. Missing: " + ", ".join(missing))
        print("The full protocol has not written its outputs yet. Nothing is reported.")
        sys.exit(2)
    return [pd.read_csv(need[0]), pd.read_csv(need[1]),
            json.load(open(need[2], encoding="utf-8"))]


def measured_runtime():
    """Per-solve wall time, from the instrumented timing run.

    NOT derived from the run log's "done" timestamps. Once the reference-point fix moved all
    search into a first pass, those timestamps all print within a second of each other and the
    derived figures were meaningless (most variants read 0.0 s). `experiment.py` now records
    `solve_seconds` per (variant, run) at the point the search actually executes.
    """
    src = RES / "metrics_timing.csv"
    if not src.exists():
        src = RES / ("metrics_%s.csv" % TAG)
    if not src.exists():
        return pd.DataFrame()
    d = pd.read_csv(src)
    if "solve_seconds" not in d.columns:
        print("  [warn] no solve_seconds column; runtime NOT reported")
        return pd.DataFrame()
    g = (d.groupby("policy")["solve_seconds"]
           .agg(["mean", "std", "count"]).reset_index()
           .rename(columns={"mean": "mean_s_per_solve", "std": "sd_s", "count": "solves"}))
    # Operational baselines do no search, so they have no solve time. Drop them rather than
    # printing NaN rows that look like missing data.
    g = g[g["solves"] > 0].reset_index(drop=True)
    base = g.loc[g.policy == CONTROL, "mean_s_per_solve"]
    if len(base):
        g["pct_vs_standard_nsga2"] = 100.0 * (g["mean_s_per_solve"] - base.iloc[0]) / base.iloc[0]
    g["source"] = src.name
    return g


def main():
    print("=" * 96)
    print("  Phase 4 evaluation: negative result, ablation, complexity, runtime")
    print("=" * 96)
    summ, metrics, sig = require()

    key = ["hypervolume", "f1_ftr", "f2_breach", "f3_workload_sd", "f4_expertise",
           "workload_jain", "first_time_right", "sla_breach_rate", "expertise_match",
           "matched_fraction", "matched_real_breach_rate", "capacity_violation"]
    have = [c for c in key if c in metrics.columns]
    agg = metrics.groupby("policy")[have].mean()
    agg = agg.reindex([p for p in ORDER if p in agg.index])
    agg.to_csv(OUT / "policy_means.csv")
    print("\n%-18s %10s %8s %8s %9s %8s" % ("policy", "hypervol", "f1 FTR", "f2 brch", "f3 sd", "jain"))
    for p, r in agg.iterrows():
        print("%-18s %10.4f %8.4f %8.4f %9.4f %8.4f" % (
            p, r.get("hypervolume", np.nan), r.get("f1_ftr", np.nan),
            r.get("f2_breach", np.nan), r.get("f3_workload_sd", np.nan),
            r.get("workload_jain", np.nan)))

    # ---------------------------------------------------------------- the headline claim
    sg = pd.DataFrame(sig)
    sg.to_csv(OUT / "significance.csv", index=False)
    hv = sg[sg.metric == "hypervolume"]
    verdict = {}
    if len(hv):
        row = hv.iloc[0]
        verdict["hypervolume_p"] = float(row["p_value"])
        verdict["hypervolume_mean_diff"] = float(row["mean_diff"])
        verdict["proposed_beats_control_on_hypervolume"] = bool(
            row["favours_PAA"] and row["p_value"] < 0.05)
    if PROPOSED in agg.index and "NSGA3" in agg.index:
        a, b = agg.loc[PROPOSED, "hypervolume"], agg.loc["NSGA3", "hypervolume"]
        verdict["nsga3_hypervolume"] = float(b)
        verdict["proposed_hypervolume"] = float(a)
        verdict["nsga3_beats_proposed"] = bool(b > a)
        verdict["nsga3_advantage_pct"] = float(100 * (b - a) / a) if a else float("nan")

    # ------------------------------------------------------------------------- ablation
    abl = {}
    for v in ["PAA-noPriority", "PAA-noAdaptive", "PAA-noSLAMut"]:
        if v in agg.index and PROPOSED in agg.index:
            delta = agg.loc[v, "hypervolume"] - agg.loc[PROPOSED, "hypervolume"]
            comp = {"PAA-noPriority": "priority-aware initialisation",
                    "PAA-noAdaptive": "adaptive crossover",
                    "PAA-noSLAMut": "SLA-aware mutation"}[v]
            abl[comp] = {"hypervolume_without": float(agg.loc[v, "hypervolume"]),
                         "delta_vs_full": float(delta),
                         "component_helps": bool(delta < 0)}
    verdict["ablation"] = abl
    print("\nAblation (removing a component; NEGATIVE delta means the component helps):")
    for comp, d in abl.items():
        print("  %-32s HV without = %.4f   delta = %+.4f   helps = %s"
              % (comp, d["hypervolume_without"], d["delta_vs_full"], d["component_helps"]))

    # exploratory repairs
    for v in ["PAA-AdaptMut", "NSGA3-PrioInit"]:
        if v in agg.index and PROPOSED in agg.index:
            verdict[f"exploratory_{v}"] = {
                "hypervolume": float(agg.loc[v, "hypervolume"]),
                "vs_proposed": float(agg.loc[v, "hypervolume"] - agg.loc[PROPOSED, "hypervolume"]),
                "vs_nsga3": float(agg.loc[v, "hypervolume"] - agg.loc["NSGA3", "hypervolume"])
                if "NSGA3" in agg.index else None}

    # ------------------------------------------------------------------------- runtime
    rt = measured_runtime()
    if len(rt):
        rt = rt.set_index("policy").reindex([p for p in ORDER if p in rt["policy"].values]
                                            if "policy" in rt else rt.index).reset_index()
        rt.to_csv(OUT / "runtime.csv", index=False)
        print("\nMeasured wall time per batch (CPU only, no GPU):")
        for _, r in rt.iterrows():
            print("  %-18s %7.2f s  sd %.2f  n=%d  %+6.1f%%" % (r["policy"], r["mean_s_per_solve"], r["sd_s"], r["solves"], r.get("pct_vs_standard_nsga2", float("nan"))))

    # ---------------------------------------------------------------------- complexity
    # Asymptotic cost per generation. N population, M objectives, n leads, m groups.
    complexity = [
        {"policy": "Random / RoundRobin / FIFO", "per_decision": "O(n)",
         "notes": "single pass, no search"},
        {"policy": "LeastLoaded", "per_decision": "O(n log m)",
         "notes": "heap over group loads"},
        {"policy": "GreedyBestOutcome", "per_decision": "O(n m)",
         "notes": "scan every eligible group per lead"},
        {"policy": "NSGA-II family", "per_generation": "O(M N^2)",
         "notes": "fast non-dominated sorting dominates; Deb et al. 2002"},
        {"policy": "NSGA-III", "per_generation": "O(M N^2 + N H)",
         "notes": "sorting plus association to H reference directions; Deb and Jain 2014"},
        {"policy": "CapacityRepair", "per_evaluation": "O(n m) worst case",
         "notes": "at most 6 passes, bounded"},
    ]
    pd.DataFrame(complexity).to_csv(OUT / "complexity.csv", index=False)

    json.dump(verdict, open(OUT / "verdict.json", "w"), indent=2)

    # ------------------------------------------------------------------------- figures
    if "hypervolume" in agg.columns:
        gas = [p for p in agg.index if p not in
               ("Random", "RoundRobin", "FIFO", "LeastLoaded", "GreedyBestOutcome")]
        d = agg.loc[gas, "hypervolume"]
        cols = [C["green"] if p == "NSGA3" else
                (C["blue"] if p == PROPOSED else
                 (C["purple"] if p in ("PAA-AdaptMut", "NSGA3-PrioInit") else C["grey"]))
                for p in d.index]
        fig, ax = plt.subplots(figsize=(6.0, 3.3))
        ax.bar(range(len(d)), d.values, color=cols)
        if PROPOSED in d.index:
            ax.axhline(d[PROPOSED], color=C["blue"], ls="--", lw=0.9)
        ax.set_xticks(range(len(d)))
        ax.set_xticklabels(d.index, rotation=30, ha="right")
        ax.set_ylabel("Hypervolume (higher is better)")
        ax.set_title("Multi-objective search quality: the proposed method does not win")
        save(fig, "fig07_hypervolume_by_variant")

        if abl:
            fig, ax = plt.subplots(figsize=(5.2, 3.0))
            names = list(abl.keys())
            deltas = [abl[k]["delta_vs_full"] for k in names]
            cols2 = [C["green"] if v < 0 else C["red"] for v in deltas]
            ax.barh(names, deltas, color=cols2, height=0.55)
            ax.axvline(0, color="black", lw=0.9)
            ax.set_xlabel("Change in hypervolume when the component is REMOVED")
            ax.set_title("Ablation: green helps, red hurts")
            for i, v in enumerate(deltas):
                ax.text(v + (0.0004 if v >= 0 else -0.0004), i, f"{v:+.4f}",
                        va="center", ha="left" if v >= 0 else "right", fontsize=8)
            save(fig, "fig08_ablation")

    if len(rt):
        fig, ax = plt.subplots(figsize=(5.6, 3.0))
        r2 = rt.sort_values("mean_s_per_solve")
        ax.barh(r2["policy"], r2["mean_s_per_solve"], xerr=r2["sd_s"], color=C["sky"], height=0.6, error_kw={"lw":0.8,"ecolor":C["grey"]})
        ax.set_xlabel("Mean wall-clock seconds per solve (CPU), error bars are 1 sd")
        ax.set_title("Measured cost: search quality is not free")
        save(fig, "fig09_runtime")

    print("\n" + "=" * 96)
    print("  tables -> %s" % OUT)
    print("  figures -> %s" % FIGS)
    print("=" * 96)


if __name__ == "__main__":
    main()
