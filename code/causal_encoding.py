"""Rule 2 of the admissibility protocol, implemented as the equation actually states it.

The manuscript defines the admissible past set as

    P(i,g) = { j : g_j = g, t_res_j < t_open_i }

and the encoding as

    theta_g(i) = ( sum_{j in P(i,g)} y_j + alpha * ybar_P ) / ( |P(i,g)| + alpha )

with alpha = 20 and ybar_P the prior breach rate over the admissible past.

Nothing in this repository computed that. The headline pipeline used `fit_group_encoding`,
which groups over the TRAINING ROWS. Under a stratified random split a test case opened in
2012 is encoded using cases that resolved in 2014, which is not P(i,g). The manuscript
bridged the gap with one sentence, "operationally this means the encoding is refitted inside
every cross-validation fold", and fold refitting is not what the equation says. The claim is
load-bearing: the abstract sells "every group-level target encoding to be estimated from past
cases only", and RQ1 repeats it.

`model_b_v3.py` came closer with a searchsorted past set, but took its prior over the whole
dataset rather than over the past set, so it did not satisfy the equation either.

This module computes it exactly, in O(n log n), with no reference to any split. Because the
encoding for case i depends only on cases resolved before case i opened, it is admissible by
construction and needs no refitting inside folds: there is no train/test boundary for it to
leak across. That is a stronger property than the fold-refit approximation, not a weaker one.

`encode_causal` is the implementation. `encode_causal_bruteforce` is the O(n^2) definition
transcribed directly from the equation, used only to prove the fast path correct on samples.
"""
from __future__ import annotations

import numpy as np

SMOOTHING_DEFAULT = 20.0


def _as_int64(ts):
    """Datetime64 to int64 nanoseconds, with NaT mapped to +inf (never resolved)."""
    arr = np.asarray(ts)
    out = np.empty(len(arr), dtype=np.int64)
    isnat = np.isnat(arr)
    out[~isnat] = arr[~isnat].astype("datetime64[ns]").astype(np.int64)
    out[isnat] = np.iinfo(np.int64).max
    return out, isnat


def encode_causal(t_open, t_res, groups, y, alpha=SMOOTHING_DEFAULT):
    """Exact Rule 2 encoding.

    Returns (theta, count) aligned with the input order.

      theta  the smoothed breach rate over P(i,g)
      count  |P(i,g)|, the admissible group volume

    A case that never resolves is never a member of any past set, which is correct: at
    t_open_i nothing about its outcome is known. A case with an empty past set falls back
    to the global admissible prior at that instant, and to 0.5 when even that is empty
    (the very first cases in the log, where nothing has resolved yet).
    """
    t_open = np.asarray(t_open, dtype=np.int64)
    res_i, _ = (t_res, None) if t_res.dtype == np.int64 else _as_int64(t_res)
    y = np.asarray(y, dtype=np.float64)
    groups = np.asarray(groups)
    n = len(t_open)

    # ---- global admissible prior: mean y over all cases resolved before t_open_i
    order = np.argsort(res_i, kind="mergesort")
    res_sorted = res_i[order]
    y_cum = np.concatenate([[0.0], np.cumsum(y[order])])
    k_global = np.searchsorted(res_sorted, t_open, side="left")
    prior = np.where(k_global > 0, y_cum[k_global] / np.maximum(k_global, 1), 0.5)

    # ---- per-group past set, same construction restricted to the group
    theta = np.empty(n, dtype=np.float64)
    count = np.zeros(n, dtype=np.float64)
    for g in np.unique(groups):
        m = np.flatnonzero(groups == g)
        gr = res_i[m]
        go = np.argsort(gr, kind="mergesort")
        gr_sorted = gr[go]
        gy_cum = np.concatenate([[0.0], np.cumsum(y[m][go])])
        k = np.searchsorted(gr_sorted, t_open[m], side="left")
        s = gy_cum[k]
        count[m] = k
        theta[m] = (s + alpha * prior[m]) / (k + alpha)
    return theta, count


def encode_causal_bruteforce(t_open, t_res, groups, y, alpha=SMOOTHING_DEFAULT):
    """The equation transcribed literally. O(n^2). For verification only."""
    t_open = np.asarray(t_open, dtype=np.int64)
    res_i, _ = (t_res, None) if t_res.dtype == np.int64 else _as_int64(t_res)
    y = np.asarray(y, dtype=np.float64)
    groups = np.asarray(groups)
    n = len(t_open)
    theta = np.empty(n)
    count = np.zeros(n)
    for i in range(n):
        past = res_i < t_open[i]
        prior = y[past].mean() if past.any() else 0.5
        sel = past & (groups == groups[i])
        k = int(sel.sum())
        theta[i] = (y[sel].sum() + alpha * prior) / (k + alpha)
        count[i] = k
    return theta, count
