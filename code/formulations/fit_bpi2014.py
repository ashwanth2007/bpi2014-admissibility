"""
Fit F1 and F2 on the real BPI Challenge 2014 log.
=================================================

Everything here is fitted on the TRAINING PERIOD ONLY, defined as the earliest 80 per cent of
the timeline by incident open time. Nothing reads a value the future would supply. That is the
same admissibility rule the static model uses, applied to the decision layer, and it is the
claim the whole paper rests on.

Outputs, all under code/results_formulations/:
  fitted_parameters.json      every constant with its value, its 95% interval and its n
  decision_matrix_stats.csv   per-criterion spread and conflict, the CRITIC inputs
  weights.csv                 CRITIC, entropy and equal weights side by side
  p_sweep.csv                 mean/min assignment score and degeneracy count per exponent p
  rank_stability.csv          Spearman rho between weighting schemes
  hazard_model.json           F2 discrete-time hazard, held-out AUC and the frozen threshold
  workload_metrics.csv        Jain index AND standard deviation, both reported, never swapped

Run:  python formulations/fit_bpi2014.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "phase4"))
sys.path.insert(0, str(HERE))

from sim.replay import BPI2014Replay                      # noqa: E402
from scoring import Scorer                                # noqa: E402
from scores import (cosine_expertise, jain_fairness, ExponentialDecay,   # noqa: E402
                    LogisticUrgency, geometric_importance, AssignmentScore,
                    critic_weights, entropy_weights, equal_weights, power_mean)
from sla_hazard import (build_person_period, DiscreteTimeHazard,        # noqa: E402
                        breach_risk_from_resolution)

OUT = HERE.parent / "results_formulations"
OUT.mkdir(exist_ok=True)
SEED = 42
TRAIN_FRAC = 0.8
np.random.seed(SEED)


def main() -> None:
    t_start = time.time()
    print("=" * 100)
    print("  Fitting F1 and F2 on BPI Challenge 2014, training period only")
    print("=" * 100)

    r = BPI2014Replay(HERE.parent / "phase4" / "bpi2014", batch_size=40, seed=SEED)
    df = r.df
    n = len(df)
    cut = int(TRAIN_FRAC * n)
    order = np.argsort(r.t_open)
    train_pos = order[:cut]
    test_pos = order[cut:]
    is_train = np.zeros(n, dtype=bool)
    is_train[train_pos] = True

    print("\nincidents: %d   groups: %d   categories: %d" % (n, len(r.groups), len(r.categories)))
    print("training period: %d incidents, evaluation period: %d" % (cut, n - cut))
    print("breach rate overall %.4f | train %.4f | eval %.4f"
          % (r.breached.mean(), r.breached[train_pos].mean(), r.breached[test_pos].mean()))

    fitted = {"_meta": {"dataset": "BPI Challenge 2014",
                        "incidents": int(n),
                        "groups": int(len(r.groups)),
                        "categories": int(len(r.categories)),
                        "train_fraction": TRAIN_FRAC,
                        "train_n": int(cut),
                        "seed": SEED,
                        "rule": "every parameter fitted on the earliest 80 per cent by open time"}}

    # ---------------------------------------------------------------- s5 freshness decay
    # Favourable outcome is "did not breach". Fitted against assignment delay, which is the
    # speed-to-lead lever the framework exists to act on.
    delay_raw = pd.to_numeric(df["Assignment_Delay_Hours"], errors="coerce").to_numpy()

    # The recorded first-assignment timestamp is corrupted in the upper tail: median 16.2 hours
    # but the 90th percentile is 5,684 hours, which is 7.8 months, and P(no breach) RISES again
    # across that tail instead of falling. Those rows are activity-log ordering artifacts, not
    # slow assignments. Fitting a decay across them returns lambda = 0 with a NaN interval,
    # which is exactly what the first run produced and is why this cap exists. Rows above the
    # cap are excluded from the s5 and s6 fits and the exclusion is reported.
    DELAY_CAP_HOURS = 168.0
    delay = np.where((delay_raw >= 0) & (delay_raw <= DELAY_CAP_HOURS), delay_raw, np.nan)
    n_excluded = int(np.isfinite(delay_raw).sum() - np.isfinite(delay).sum())
    print("\nassignment delay capped at %.0f h: %d of %d finite rows excluded as log artifacts (%.1f%%)"
          % (DELAY_CAP_HOURS, n_excluded, int(np.isfinite(delay_raw).sum()),
             100.0 * n_excluded / max(int(np.isfinite(delay_raw).sum()), 1)))

    good = 1 - r.breached
    ok = np.isfinite(delay) & is_train
    dec = ExponentialDecay().fit(delay[ok], good[ok])
    print("\ns5 freshness decay   lambda = %.6f per hour   95%% CI [%.6f, %.6f]   n = %d"
          % (dec.lam, dec.ci95[0], dec.ci95[1], dec.n_obs))
    fitted["s5_freshness_decay"] = {"lambda_per_hour": dec.lam, "ci95": list(dec.ci95),
                                    "n": dec.n_obs, "fitted_on": "training period only",
                                    "delay_cap_hours": DELAY_CAP_HOURS,
                                    "rows_excluded_as_artifacts": n_excluded,
                                    "why_capped": "first-assignment timestamp corrupted above ~1 week; uncapped fit returns lambda=0 with a NaN interval"}

    # ---------------------------------------------------------------- s6 urgency ramp
    # Hours remaining at first assignment, against the per-priority SLA target that the
    # replay itself uses to define the label. Computed the same way so the scales agree.
    ht = pd.to_numeric(df["Handle_Time_Hours"], errors="coerce").to_numpy()
    pm = pd.Series(ht[train_pos]).groupby(df["Priority"].to_numpy()[train_pos]).median()
    thr = df["Priority"].map(pm).to_numpy(dtype=float)
    hours_remaining = thr - np.clip(delay, 0, None)
    ok2 = np.isfinite(hours_remaining) & is_train
    urg = LogisticUrgency().fit(hours_remaining[ok2], r.breached[ok2])
    print("s6 urgency ramp      k = %.6f   tau0 = %.4f hours   n = %d"
          % (urg.k, urg.tau0, urg.n_obs))
    fitted["s6_urgency_ramp"] = {"k": urg.k, "tau0_hours": urg.tau0, "n": urg.n_obs,
                                 "sla_target_source": "per-priority median handle time, training period",
                                 "fitted_on": "training period only"}

    # ---------------------------------------------------------------- s3 cosine expertise
    cat = df["Category_Id"].to_numpy()
    gid = df["Observed_Group_Id"].to_numpy()
    n_groups, n_cats = len(r.groups), len(r.categories)
    spec = np.zeros((n_groups, n_cats))
    np.add.at(spec, (gid[train_pos], cat[train_pos]), 1.0)     # training period counts only
    req = np.zeros((n, n_cats))
    req[np.arange(n), cat] = 1.0
    E_eval = cosine_expertise(req[test_pos], spec)
    zero_cells = int((E_eval == 0).sum())
    print("s3 cosine expertise  eval matrix %s   exact zeros %d (%.2f%%)   groups with no history %d"
          % (E_eval.shape, zero_cells, 100.0 * zero_cells / E_eval.size,
             int((spec.sum(axis=1) == 0).sum())))
    fitted["s3_expertise"] = {"metric": "cosine similarity",
                              "eval_shape": list(E_eval.shape),
                              "exact_zero_cells": zero_cells,
                              "exact_zero_fraction": float(zero_cells / E_eval.size),
                              "groups_with_no_training_history": int((spec.sum(axis=1) == 0).sum()),
                              "note": "exact zeros are why the power mean needs a floor for p <= 0"}

    # ---------------------------------------------------------------- s4 workload
    # BOTH the Jain index and the standard deviation are reported. The optimiser's objective
    # f3 is the standard deviation and it stays that way. Swapping it after observing that the
    # proposed method loses on it would be fitting the metric to the result.
    load = np.bincount(gid[train_pos], minlength=n_groups).astype(float)
    wl = {"jain_index": float(jain_fairness(load)),
          "std_dev": float(load.std()),
          "coefficient_of_variation": float(load.std() / max(load.mean(), 1e-9)),
          "n_groups": int(n_groups)}
    print("s4 workload          Jain %.4f | sd %.2f | CV %.4f  (both reported, neither substituted)"
          % (wl["jain_index"], wl["std_dev"], wl["coefficient_of_variation"]))
    pd.DataFrame([wl]).to_csv(OUT / "workload_metrics.csv", index=False)
    fitted["s4_workload"] = wl

    # ---------------------------------------------------------------- s1, s2 model outputs
    print("\ntraining the two lead-level models via the existing Scorer ...")
    sc = Scorer(r, train_frac=TRAIN_FRAC)
    print("  P(breach) holdout AUC   %.4f" % sc.tables.auc_breach)
    print("  P(reassign) holdout AUC %.4f" % sc.tables.auc_reassign)
    fitted["s1_s2_models"] = {"auc_breach": float(sc.tables.auc_breach),
                              "auc_reassign": float(sc.tables.auc_reassign)}

    # ---------------------------------------------- decision matrix on the evaluation period
    sample = test_pos[: min(4000, len(test_pos))]
    F1m, F2m, F4m, F5m = sc.objective_matrices(sample)
    Ee = cosine_expertise(req[sample], spec)
    fresh = np.tile(dec(np.nan_to_num(delay[sample], nan=0.0))[:, None], (1, n_groups))
    urgv = np.tile(urg(np.nan_to_num(hours_remaining[sample], nan=0.0))[:, None], (1, n_groups))
    imp_parts = np.stack([
        1.0 - (df["Priority"].to_numpy(dtype=float)[sample] - 1) / 4.0,
        1.0 - (pd.to_numeric(df["Impact"], errors="coerce").fillna(3).to_numpy()[sample] - 1) / 4.0,
        1.0 - (pd.to_numeric(df["Urgency"], errors="coerce").fillna(3).to_numpy()[sample] - 1) / 4.0,
    ], axis=-1)
    imp = np.tile(np.clip(geometric_importance(imp_parts), 0, 1)[:, None], (1, n_groups))
    jain_post = np.tile(jain_fairness(load + np.eye(n_groups))[None, :], (len(sample), 1))

    S = np.stack([F1m, 1.0 - F2m, Ee, jain_post, fresh, urgv, imp], axis=-1)
    names = ["s1_outcome", "s2_sla_survival", "s3_expertise", "s4_workload",
             "s5_freshness", "s6_urgency", "s7_importance"]
    flat = S.reshape(-1, S.shape[-1])
    print("\ndecision matrix: %s  (%d lead-group pairs x %d criteria)"
          % (S.shape, flat.shape[0], flat.shape[1]))

    stats_rows = []
    Z = (flat - flat.min(axis=0)) / np.maximum(flat.max(axis=0) - flat.min(axis=0), 1e-12)
    R = np.nan_to_num(np.corrcoef(Z, rowvar=False), nan=0.0)
    for i, nm in enumerate(names):
        stats_rows.append({"criterion": nm, "mean": float(flat[:, i].mean()),
                           "std": float(flat[:, i].std(ddof=1)),
                           "min": float(flat[:, i].min()), "max": float(flat[:, i].max()),
                           "exact_zeros": int((flat[:, i] == 0).sum()),
                           "critic_conflict": float((1.0 - R[i]).sum())})
    pd.DataFrame(stats_rows).to_csv(OUT / "decision_matrix_stats.csv", index=False)

    W = {"critic": critic_weights(flat), "entropy": entropy_weights(flat),
         "equal": equal_weights(flat)}
    pd.DataFrame({"criterion": names, **{k: v for k, v in W.items()}}).to_csv(
        OUT / "weights.csv", index=False)
    print("\nweights:")
    for k, v in W.items():
        print("  %-8s %s" % (k, np.array2string(v, precision=4, suppress_small=True)))

    # ------------------------------------------------------------------------ p sweep
    A = AssignmentScore(p=1.0)
    A.weights_ = W
    rows = []
    for p in [-4.0, -2.0, -1.0, -0.5, 0.0, 0.25, 0.5, 1.0, 2.0, 4.0, 8.0]:
        v = power_mean(S, W["critic"], p)
        rows.append({"p": p, "mean": float(v.mean()), "min": float(v.min()),
                     "max": float(v.max()), "exact_zero_cells": int((v == 0).sum()),
                     "floored": p <= 0})
    pd.DataFrame(rows).to_csv(OUT / "p_sweep.csv", index=False)
    print("\np sweep written (%d exponents, floor applied for p <= 0)" % len(rows))

    stab = A.rank_stability(S)
    pd.DataFrame([{"pair": k, **v} for k, v in stab.items()]).to_csv(
        OUT / "rank_stability.csv", index=False)
    print("rank stability:")
    for k, v in stab.items():
        print("  %-22s spearman rho = %.4f" % (k, v["spearman_rho"]))
    fitted["rank_stability"] = stab
    fitted["weights"] = {k: [float(x) for x in v] for k, v in W.items()}
    fitted["criteria"] = names

    # ------------------------------------------------------------------------ F2 hazard
    print("\nfitting F2 discrete-time RESOLUTION hazard on real durations ...")
    print("  modelling breach DIRECTLY is degenerate on this log: past its own per-priority")
    print("  threshold a case has measured breach rate 1.0000, so elapsed time plus priority")
    print("  determines the label and a breach-hazard model reads the answer (AUC 0.94).")
    print("  The event modelled here is RESOLUTION, which elapsed time does not determine.")
    static = pd.DataFrame({
        "priority": df["Priority"].to_numpy(dtype=float),
        "impact": pd.to_numeric(df["Impact"], errors="coerce").fillna(3).to_numpy(),
        "urgency": pd.to_numeric(df["Urgency"], errors="coerce").fillna(3).to_numpy(),
        "assignment_delay": np.nan_to_num(delay, nan=float(np.nanmedian(delay))),
        "category_id": cat.astype(float),
    })
    edges = np.array([0, 1, 2, 4, 8, 16, 24, 48, 96, 168, 1e9])
    dur = np.nan_to_num(ht, nan=0.0)

    # EVENT = "the case was RESOLVED in this bin". Measured resolution hazard rises smoothly
    # from 0.126 at 1h to 0.423 at 24h, so it is a genuine random event given elapsed time.
    resolved = np.ones(len(dur), dtype=int)
    pp = build_person_period(dur, resolved, static, edges)
    pp_train = pp[pp["_case"].isin(set(train_pos.tolist()))]
    pp_eval = pp[pp["_case"].isin(set(test_pos.tolist()))]
    feats = ["priority", "impact", "urgency", "assignment_delay", "category_id",
             "bin_idx", "bin_start"]
    hz = DiscreteTimeHazard(bin_edges=edges).fit(pp_train, feats)
    hz.tune_threshold(pp_train, label="training period (earliest 80 per cent)")
    from sklearn.metrics import roc_auc_score
    auc_res = float(roc_auc_score(pp_eval["y"], hz.hazard(pp_eval)))
    curves = hz.survival_curve(pp_eval)
    mono = bool(curves.groupby("_case")["S"].apply(
        lambda s_: s_.diff().dropna().le(1e-12).all()).all())

    # Breach is DERIVED, exactly as it is in the real process:
    #   P(breach | alive at t) = P(not resolved by threshold | alive at t) = S(thr) / S(t)
    curves = breach_risk_from_resolution(curves, thr, edges)
    first = curves.sort_values(["_case", "bin_idx"]).groupby("_case").head(1)
    y_true = r.breached[first["_case"].to_numpy()]
    auc_breach_derived = (float(roc_auc_score(y_true, first["breach_risk"].to_numpy()))
                          if len(np.unique(y_true)) > 1 else float("nan"))

    print("  person-period rows %d from %d cases (%.2fx expansion)" % (len(pp), n, len(pp) / n))
    print("  bin-level RESOLUTION rate %.4f" % pp["y"].mean())
    print("  held-out resolution-hazard AUC %.4f" % auc_res)
    print("  DERIVED breach risk at intake, held-out AUC %.4f" % auc_breach_derived)
    print("  static breach model for comparison        %.4f" % sc.tables.auc_breach)
    print("  survival monotonic within every case: %s" % mono)
    haz = {"event_modelled": "resolution, not breach",
           "why": ("breach is a deterministic function of duration versus the per-priority "
                   "threshold; measured breach rate past threshold is 1.0000, so a "
                   "breach-hazard model with elapsed time as a feature reads the label and "
                   "scored 0.9418 while learning nothing. Verified empirically, not assumed."),
           "person_period_rows": int(len(pp)), "cases": int(n),
           "expansion": float(len(pp) / n),
           "bin_resolution_rate": float(pp["y"].mean()),
           "heldout_resolution_hazard_auc": auc_res,
           "derived_breach_auc_at_intake": auc_breach_derived,
           "static_model_breach_auc_for_comparison": float(sc.tables.auc_breach),
           "threshold": float(hz.threshold_),
           "threshold_tuned_on": hz.threshold_tuned_on_,
           "survival_monotonic": mono,
           "bin_edges_hours": [float(x) for x in edges[:-1]] + ["inf"],
           "features": feats}
    json.dump(haz, open(OUT / "hazard_model.json", "w"), indent=2)
    fitted["f2_hazard"] = haz

    fitted["_meta"]["runtime_seconds"] = round(time.time() - t_start, 1)
    json.dump(fitted, open(OUT / "fitted_parameters.json", "w"), indent=2)
    print("\n" + "=" * 100)
    print("  wrote %d artifacts to %s in %.1fs" % (len(list(OUT.glob("*"))), OUT, time.time() - t_start))
    print("=" * 100)


if __name__ == "__main__":
    main()
