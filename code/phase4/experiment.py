"""
Phase 4 experiment harness: baselines, standard NSGA-II, PAA-NSGA-II, ablations.
================================================================================
Run:  python experiment.py --batches 3 --runs 5 --gens 60      (quick check)
      python experiment.py --batches 5 --runs 30 --gens 150    (full protocol)

The load-bearing comparison is PAA-NSGA-II vs StandardNSGA2. Everything else exists to
make that comparison interpretable. The result is reported whichever way it lands: a
negative ablation is a real finding and must not be dressed up.
"""
from __future__ import annotations

import argparse
import zlib
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import wilcoxon

from pymoo.algorithms.moo.nsga2 import NSGA2
from pymoo.algorithms.moo.nsga3 import NSGA3
from pymoo.optimize import minimize
from pymoo.indicators.hv import HV
from pymoo.util.ref_dirs import get_reference_directions

from sim.replay import BPI2014Replay
from scoring import Scorer
from problem import LeadAssignmentProblem, operational_metrics
from baselines import BASELINES
from operators import (PriorityAwareSampling, AdaptiveCrossover,
                       SLAAwareMutation, CapacityRepair, RandomFeasibleSampling)

BASE = Path(__file__).parent
RESULTS = BASE / "results"
RESULTS.mkdir(exist_ok=True)


# ------------------------------------------------------------------ GA variants
def make_algorithm(name: str, pop: int, seed: int):
    """Every GA variant differs ONLY in the operators, so the ablation is clean."""
    # The control uses the SAME operator mechanics with every modification switched
    # off: fixed-probability uniform crossover and unbiased random-resetting mutation.
    # That is textbook NSGA-II for an integer encoding, and it means the only thing
    # separating it from PAA-NSGA-II is the three claimed modifications.
    if name == "StandardNSGA2":
        return NSGA2(pop_size=pop,
                     sampling=RandomFeasibleSampling(),
                     crossover=AdaptiveCrossover(adaptive=False),
                     mutation=SLAAwareMutation(sla_aware=False),
                     repair=CapacityRepair(enabled=True),
                     eliminate_duplicates=True)

    if name == "NSGA3":
        # das-dennis with 5 objectives: n_partitions=4 gives 70 reference directions,
        # which exceeds pop_size=60 and makes NSGA-III misbehave. n_partitions=3 gives
        # 35, comfortably inside the population.
        rd = get_reference_directions("das-dennis", 5, n_partitions=3)
        return NSGA3(ref_dirs=rd, pop_size=pop,
                     sampling=RandomFeasibleSampling(),
                     crossover=AdaptiveCrossover(adaptive=False),
                     mutation=SLAAwareMutation(sla_aware=False),
                     repair=CapacityRepair(enabled=True),
                     eliminate_duplicates=True)

    # ---- POST-HOC EXPLORATORY VARIANTS -------------------------------------------
    # Both were designed AFTER seeing the first full protocol, and both are labelled as
    # exploratory wherever they are reported. Neither is presented as "the method", because
    # promoting a post-hoc winner to the headline claim would be HARKing.
    #
    # PAA-AdaptMut tests one specific mechanical hypothesis: the ablation showed adaptive
    # CROSSOVER is net negative (removing it raised hypervolume 0.0922 -> 0.0944). Raising
    # crossover on stagnation recombines already-converged parents and produces offspring
    # identical to them. The same adaptive schedule is therefore moved onto mutation, the
    # operator that can actually reintroduce diversity. Adaptive crossover is OFF here.
    if name == "PAA-AdaptMut":
        return NSGA2(
            pop_size=pop,
            sampling=PriorityAwareSampling(0.35),
            crossover=AdaptiveCrossover(adaptive=False),
            mutation=SLAAwareMutation(sla_aware=True, adaptive=True),
            repair=CapacityRepair(enabled=True),
            eliminate_duplicates=True,
        )

    # NSGA3-PrioInit keeps ONLY the modification the ablation supports (priority-aware
    # initialisation) and puts it on the base algorithm that actually won the first study.
    # Reported as an exploratory combination, not as a renamed proposal.
    if name == "NSGA3-PrioInit":
        rd = get_reference_directions("das-dennis", 5, n_partitions=3)
        return NSGA3(ref_dirs=rd, pop_size=pop,
                     sampling=PriorityAwareSampling(0.35),
                     crossover=AdaptiveCrossover(adaptive=False),
                     mutation=SLAAwareMutation(sla_aware=False),
                     repair=CapacityRepair(enabled=True),
                     eliminate_duplicates=True)

    # PAA-NSGA-II and its ablations
    use_prio = name in ("PAA-NSGA-II", "PAA-noAdaptive", "PAA-noSLAMut")
    use_adapt = name in ("PAA-NSGA-II", "PAA-noPriority", "PAA-noSLAMut")
    use_sla = name in ("PAA-NSGA-II", "PAA-noPriority", "PAA-noAdaptive")

    return NSGA2(
        pop_size=pop,
        sampling=(PriorityAwareSampling(0.35) if use_prio else RandomFeasibleSampling()),
        crossover=AdaptiveCrossover(adaptive=use_adapt),
        mutation=SLAAwareMutation(sla_aware=use_sla),
        repair=CapacityRepair(enabled=True),
        eliminate_duplicates=True,
    )


GA_VARIANTS = ["StandardNSGA2", "NSGA3", "PAA-NSGA-II",
               "PAA-noPriority", "PAA-noAdaptive", "PAA-noSLAMut",
               "PAA-AdaptMut", "NSGA3-PrioInit"]


def knee_point(F: np.ndarray) -> int:
    """Pick one solution from a Pareto front for operational reporting: the point
    closest to the ideal after min-max normalisation."""
    lo, hi = F.min(axis=0), F.max(axis=0)
    Z = (F - lo) / np.where(hi - lo < 1e-12, 1, hi - lo)
    return int(np.argmin(np.linalg.norm(Z, axis=1)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batches", type=int, default=3)
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--gens", type=int, default=60)
    ap.add_argument("--pop", type=int, default=60)
    ap.add_argument("--batch-size", type=int, default=40)
    ap.add_argument("--tag", type=str, default="run")
    args = ap.parse_args()

    print("Loading BPI 2014 replay ...")
    replay = BPI2014Replay(BASE / "bpi2014", batch_size=args.batch_size)
    print(f"  {len(replay.df):,} incidents, {len(replay.groups)} groups, "
          f"{len(replay.categories)} specialisations")
    scorer = Scorer(replay)
    windows = replay.batches(max_batches=args.batches)
    print(f"  {len(windows)} arrival batches of {args.batch_size} leads\n")

    rows, front_rows = [], []
    t_start = time.time()

    for w in windows:
        F1, F2, F4, F5 = scorer.objective_matrices(w.lead_idx)
        prob = LeadAssignmentProblem(F1, F2, F4, F5, w.eligible, w.capacity)
        obs_breach = replay.breached[w.lead_idx]

        # ---------------- baselines: deterministic, evaluated once ----------------
        for pname, fn in BASELINES.items():
            x = fn(prob)
            F = prob.objectives_for(x)
            m = operational_metrics(prob, x, w.observed_group, obs_breach)
            rows.append({"batch": w.batch_id, "policy": pname, "run": 0,
                         "f1_ftr": -F[0], "f2_breach": F[1], "f3_workload_sd": F[2],
                         "f4_expertise": -F[3], "f5_delay_log": F[4],
                         "hypervolume": np.nan, **m})

        # Reference point for hypervolume. Previously this was set from whichever run
        # happened to finish first (`if ref is None`), which made every hypervolume depend
        # on variant ORDER and made runs with different variant sets incomparable. It is now
        # the nadir across ALL variants and ALL runs on this batch, computed in a first pass
        # before any hypervolume is taken, which is the standard construction.
        ref = None
        _front_cache = {}
        _nadir = None
        for _g in GA_VARIANTS:
            for _r in range(args.runs):
                _seed = 1000 * w.batch_id + 17 * _r + zlib.crc32(_g.encode()) % 97
                _t0 = time.perf_counter()
                _res = minimize(prob, make_algorithm(_g, args.pop, _seed),
                                ("n_gen", args.gens), seed=_seed, verbose=False,
                                save_history=False)
                _solve_s = time.perf_counter() - _t0
                if _res.F is None or len(np.atleast_2d(_res.F)) == 0:
                    continue
                _F = np.atleast_2d(_res.F)
                # Per-solve wall time is captured HERE because this first pass is where the
                # search actually runs. Deriving it from the "done" timestamps in the log is
                # wrong now: those all print within a second of each other once the fronts
                # are served from cache.
                _front_cache[(_g, _r)] = (_F, np.atleast_2d(_res.X).astype(int), _solve_s)
                _nadir = _F.max(axis=0) if _nadir is None else np.maximum(_nadir, _F.max(axis=0))
        if _nadir is not None:
            ref = _nadir + 0.1 * (np.abs(_nadir) + 1e-9)
        print(f"  batch {w.batch_id} reference point fixed across all variants: "
              f"{np.array2string(ref, precision=3)}" if ref is not None else "  no fronts")

        for gname in GA_VARIANTS:
            for run in range(args.runs):
                # DETERMINISTIC seed. Python salts str hashing per process, so the
                # previous `hash(gname) % 97` produced a DIFFERENT seed on every run
                # (measured: 43, 41, 32 across three processes for the same name).
                # zlib.crc32 is stable across processes and platforms.
                seed = 1000 * w.batch_id + 17 * run + zlib.crc32(gname.encode()) % 97
                cached = _front_cache.get((gname, run))
                if cached is None:
                    continue
                F, X, solve_s = cached
                hv = HV(ref_point=ref)(F)

                k = knee_point(F)
                m = operational_metrics(prob, X[k], w.observed_group, obs_breach)
                rows.append({"batch": w.batch_id, "policy": gname, "run": run,
                             "f1_ftr": -F[k, 0], "f2_breach": F[k, 1],
                             "f3_workload_sd": F[k, 2], "f4_expertise": -F[k, 3],
                             "f5_delay_log": F[k, 4], "hypervolume": float(hv),
                             "front_size": len(F), "solve_seconds": float(solve_s), **m})
                for f in F:
                    front_rows.append({"batch": w.batch_id, "policy": gname, "run": run,
                                       "f1": f[0], "f2": f[1], "f3": f[2],
                                       "f4": f[3], "f5": f[4]})
            print(f"  batch {w.batch_id} {gname:16s} done "
                  f"({time.time()-t_start:.0f}s elapsed)")

    df = pd.DataFrame(rows)
    df.to_csv(RESULTS / f"metrics_{args.tag}.csv", index=False)
    pd.DataFrame(front_rows).to_csv(RESULTS / f"fronts_{args.tag}.csv", index=False)

    # ------------------------------------------------------------- summary table
    num = df.select_dtypes(include=[np.number]).columns
    summary = df.groupby("policy")[list(num)].mean(numeric_only=True).round(4)
    order = [p for p in list(BASELINES) + GA_VARIANTS if p in summary.index]
    summary = summary.loc[order]
    cols = ["f1_ftr", "f2_breach", "f3_workload_sd", "f4_expertise",
            "delay_hours", "hypervolume", "matched_fraction",
            "matched_real_breach_rate", "capacity_violation"]
    cols = [c for c in cols if c in summary.columns]
    print("\n" + "=" * 118)
    print("  SUMMARY  (f1 and f4 higher is better; f2, f3, delay lower is better)")
    print("=" * 118)
    print(summary[cols].to_string())
    summary.to_csv(RESULTS / f"summary_{args.tag}.csv")

    # ------------------------------------------- the load-bearing significance test
    print("\n" + "=" * 118)
    print("  PAA-NSGA-II vs StandardNSGA2, paired Wilcoxon signed-rank")
    print("=" * 118)
    tests = []
    a = df[df.policy == "PAA-NSGA-II"].sort_values(["batch", "run"])
    b = df[df.policy == "StandardNSGA2"].sort_values(["batch", "run"])
    n = min(len(a), len(b))
    for metric, better in [("hypervolume", "higher"), ("f2_breach", "lower"),
                           ("f1_ftr", "higher"), ("f3_workload_sd", "lower"),
                           ("f4_expertise", "higher")]:
        x, y = a[metric].values[:n], b[metric].values[:n]
        ok = ~(np.isnan(x) | np.isnan(y))
        if ok.sum() < 5 or np.allclose(x[ok], y[ok]):
            print(f"  {metric:18s} insufficient or identical data, skipped")
            continue
        stat, p = wilcoxon(x[ok], y[ok])
        d = float(np.mean(x[ok] - y[ok]))
        win = (d > 0) if better == "higher" else (d < 0)
        tests.append({"metric": metric, "better": better, "mean_diff": round(d, 5),
                      "p_value": float(p), "n_pairs": int(ok.sum()),
                      "favours_PAA": bool(win)})
        print(f"  {metric:18s} mean diff {d:+.5f}  p={p:.4g}  n={ok.sum()}  "
              f"-> {'PAA better' if win else 'Standard better'}")

    # Holm-Bonferroni correction across the metrics tested
    if tests:
        ps = sorted([(t['p_value'], t['metric']) for t in tests])
        m = len(ps)
        print("\n  Holm-Bonferroni corrected at alpha=0.05:")
        for rank, (p, met) in enumerate(ps):
            thr = 0.05 / (m - rank)
            print(f"    {met:18s} p={p:.4g}  threshold={thr:.4g}  "
                  f"{'SIGNIFICANT' if p < thr else 'not significant'}")
        json.dump(tests, open(RESULTS / f"significance_{args.tag}.json", "w"), indent=2)

    print(f"\nTotal wall time {time.time()-t_start:.0f}s")
    print(f"Wrote results to {RESULTS}")


if __name__ == "__main__":
    main()
