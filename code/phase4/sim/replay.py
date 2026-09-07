"""
BPI 2014 arrival replay: the simulation environment for Phase 4.
================================================================
Phase 7 of the faculty specification, built here because Phase 4 cannot be scored
without it.

WHAT IS REAL AND WHAT IS SIMULATED. Stated up front because this is the single
biggest reviewer attack on any optimisation paper, and the nearest prior work
(arXiv 2606.01857) names it as its own open limitation.

  REAL, taken directly from the Rabobank log:
    - arrival timestamps (Open Time)
    - priority, impact, urgency, category, configuration item
    - the set of assignment groups and which categories each actually handled
    - group capacity, derived from each group's observed peak concurrent open load
    - the historically observed assignment for each incident

  SIMULATED (model-scored, NOT observed):
    - the OUTCOME of a counterfactual assignment. If a policy routes incident i to
      group j and the log shows it actually went to group k, no ground truth exists
      for what would have happened. Model B scores it.

  The honest control: `matched_fraction` reports how often a policy's chosen group
  equals the historically observed group. On those cases the real outcome IS known,
  so `matched_real_breach_rate` is a genuine, non-simulated signal. It is reported
  alongside every simulated result.

The replay is deterministic given a seed and a window.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from pathlib import Path
from dataclasses import dataclass, field

# BPI 2014 timestamp formats. BOTH files are day-first; the incident file uses "/" and the
# activity file uses "-". This was previously parsed with format="mixed", dayfirst=False on the
# incident file, which silently swapped day and month on the 41 per cent of rows where both
# fields are <= 12. Verified from the raw bytes on 2026-09-07: across 46,606 non-blank Open Time
# values, 27,994 have a first field > 12 and ZERO have a second field > 12, so month-first is
# arithmetically impossible. Never use format="mixed" on an ambiguous numeric date.
INCIDENT_TS_FORMAT = "%d/%m/%Y %H:%M:%S"
ACTIVITY_TS_FORMAT = "%d-%m-%Y %H:%M:%S"


DAY_NS = 86_400_000_000_000


@dataclass
class ReplayWindow:
    """One batch of leads arriving together, plus the state of the world at that moment."""
    batch_id: int
    lead_idx: np.ndarray          # row indices into the incident frame
    t_start: int                  # ns epoch of the first arrival in the batch
    group_ids: np.ndarray         # eligible group ids (global index space)
    capacity: np.ndarray          # remaining capacity per group at t_start
    available: np.ndarray         # 1 if group had any activity in the trailing window
    eligible: np.ndarray          # (n_leads, n_groups) boolean, constraint C4
    observed_group: np.ndarray    # historically observed group per lead, -1 if unknown


class BPI2014Replay:
    """Loads BPI 2014, rebuilds decision-time state, and yields arrival batches."""

    def __init__(self, data_dir: Path, batch_size: int = 40, seed: int = 42,
                 min_group_cases: int = 50):
        self.data_dir = Path(data_dir)
        self.batch_size = batch_size
        self.seed = seed
        self.min_group_cases = min_group_cases
        self.rng = np.random.default_rng(seed)
        self._load()

    # ------------------------------------------------------------------ loading
    def _load(self) -> None:
        d = self.data_dir
        inc = pd.read_csv(d / "Detail_Incident.csv", sep=";", encoding="latin1")

        # 203 completely empty trailing rows in the published file carry a null Incident ID and
        # collide as duplicate NaN keys under set_index. Real incidents: 46,606 of 46,809 raw rows.
        inc = inc[inc["Incident ID"].notna()].copy()
        # One row carries Urgency = "5 - Very Low" rather than "5", which makes the column object
        # dtype and causes XGBoost to reject the matrix. Take the leading integer.
        for _c in ["Priority", "Impact", "Urgency"]:
            if _c in inc.columns:
                inc[_c] = pd.to_numeric(
                    inc[_c].astype(str).str.extract(r"^\s*(\d+)", expand=False), errors="coerce"
                ) if inc[_c].dtype == object else pd.to_numeric(inc[_c], errors="coerce")
        act = pd.read_csv(d / "Detail_Incident_Activity.csv", sep=";", encoding="latin1")

        inc["Handle_Time_Hours"] = pd.to_numeric(
            inc["Handle Time (Hours)"].astype(str).str.replace(",", "."), errors="coerce")
        for c in ["Open Time", "Resolved Time", "Close Time"]:
            inc[c] = pd.to_datetime(inc[c], format=INCIDENT_TS_FORMAT, errors="coerce")
        act["DateStamp"] = pd.to_datetime(act["DateStamp"], format=ACTIVITY_TS_FORMAT, errors="coerce")

        # Same SLA label as Model B, so simulated outcomes are on the trained scale.
        pm = inc.groupby("Priority")["Handle_Time_Hours"].median()
        thr = {p: m * 2.0 for p, m in pm.items()}
        inc["SLA_Breached"] = (inc["Handle_Time_Hours"] > inc["Priority"].map(thr)).astype(int)

        df = inc[inc["Handle_Time_Hours"].notna()
                 & inc["Priority"].notna()
                 & inc["Open Time"].notna()].copy()

        ae = act[act["IncidentActivity_Type"].isin(["Assignment", "Reassignment"])]
        first_grp = (ae.sort_values("DateStamp").groupby("Incident ID")["Assignment Group"]
                     .first().reset_index()
                     .rename(columns={"Assignment Group": "Observed_Group"}))
        first_time = (ae.sort_values("DateStamp").groupby("Incident ID")["DateStamp"]
                      .first().reset_index()
                      .rename(columns={"DateStamp": "First_Assignment_Time"}))
        df = df.merge(first_grp, on="Incident ID", how="left")
        df = df.merge(first_time, on="Incident ID", how="left")
        df["Observed_Group"] = df["Observed_Group"].fillna("unknown").astype(str)
        df["Assignment_Delay_Hours"] = (
            (df["First_Assignment_Time"] - df["Open Time"]).dt.total_seconds() / 3600
        ).clip(lower=0).fillna(0)

        df = df.sort_values("Open Time").reset_index(drop=True)

        # Keep only groups with enough history for their statistics to mean anything.
        counts = df["Observed_Group"].value_counts()
        keep = set(counts[counts >= self.min_group_cases].index) - {"unknown"}
        df = df[df["Observed_Group"].isin(keep)].reset_index(drop=True)

        self.groups = sorted(keep)
        self.gidx = {g: i for i, g in enumerate(self.groups)}
        df["Observed_Group_Id"] = df["Observed_Group"].map(self.gidx).astype(int)

        # SPECIALISATION KEY. "Category" in BPI 2014 has only 4 values
        # ("incident", "request for information", ...), which is a request TYPE, not a
        # technical domain, so it cannot express expertise. "CI Subtype (aff)" has 58
        # values (Server Based Application, Web Based Application, Laptop, ...) and is a
        # genuine technical specialisation. Objective f4 uses this.
        df["Spec"] = df["CI Subtype (aff)"].fillna("unknown").astype(str)
        self.categories = sorted(df["Spec"].unique())
        self.cidx = {c: i for i, c in enumerate(self.categories)}
        df["Category_Id"] = df["Spec"].map(self.cidx).astype(int)
        df["Category"] = df["Category"].fillna("unknown").astype(str)

        self.df = df
        self.t_open = df["Open Time"].values.astype("datetime64[ns]").astype(np.int64)
        res = df["Resolved Time"].values
        self.t_res = np.where(np.isnat(res),
                              np.iinfo(np.int64).max,
                              res.astype("datetime64[ns]").astype(np.int64))
        self.breached = df["SLA_Breached"].values.astype(int)

        self._build_group_state()
        self._build_eligibility()

    # ------------------------------------------------- group capacity and skill
    def _build_group_state(self) -> None:
        """Capacity = observed peak concurrent open load per group. Real, not invented."""
        n_g = len(self.groups)
        peak = np.zeros(n_g, dtype=int)
        gid = self.df["Observed_Group_Id"].values
        for g in range(n_g):
            m = np.where(gid == g)[0]
            if len(m) == 0:
                continue
            ot = np.sort(self.t_open[m])
            rt = np.sort(self.t_res[m])
            load = (np.searchsorted(ot, self.t_open[m], side="left")
                    - np.searchsorted(rt, self.t_open[m], side="left"))
            peak[g] = max(1, int(load.max()))
        self.group_capacity = peak

        # Category competence: share of a group's cases in each category, and its
        # causal breach rate there. Both are historical, both are decision-time safe.
        n_c = len(self.categories)
        cat = self.df["Category_Id"].values
        comp = np.zeros((n_g, n_c))
        for g in range(n_g):
            m = np.where(gid == g)[0]
            if len(m) == 0:
                continue
            cnt = np.bincount(cat[m], minlength=n_c)
            comp[g] = cnt / max(cnt.sum(), 1)
        self.group_category_share = comp

        prior = self.breached.mean()
        succ = np.full((n_g, n_c), 1.0 - prior)
        for g in range(n_g):
            for c in np.where(comp[g] > 0)[0]:
                m = np.where((gid == g) & (cat == c))[0]
                if len(m) == 0:
                    continue
                succ[g, c] = 1.0 - (self.breached[m].mean() * len(m) + prior * 20) / (len(m) + 20)
        self.group_category_success = succ

    def _build_eligibility(self) -> None:
        """C4: a group is eligible for a category it has demonstrably handled before."""
        self.eligible_gc = self.group_category_share > 0

    # ------------------------------------------------------------------ batches
    def batches(self, start_frac: float = 0.8, max_batches: int | None = None):
        """Yield arrival batches from the tail of the log (the held-out period)."""
        n = len(self.df)
        lo = int(n * start_frac)
        idx = np.arange(lo, n)
        n_g = len(self.groups)
        out, bid = [], 0
        for s in range(0, len(idx) - self.batch_size + 1, self.batch_size):
            sel = idx[s:s + self.batch_size]
            t0 = int(self.t_open[sel[0]])

            gid = self.df["Observed_Group_Id"].values
            load = np.zeros(n_g, dtype=int)
            avail = np.zeros(n_g, dtype=int)
            for g in range(n_g):
                m = np.where(gid == g)[0]
                if len(m) == 0:
                    continue
                ot = np.sort(self.t_open[m]); rt = np.sort(self.t_res[m])
                load[g] = (np.searchsorted(ot, t0, side="left")
                           - np.searchsorted(rt, t0, side="left"))
                # available if the group saw any arrival in the trailing 30 days
                avail[g] = int(((self.t_open[m] >= t0 - 30 * DAY_NS)
                                & (self.t_open[m] < t0)).sum() > 0)

            cap = np.maximum(self.group_capacity - load, 0)
            cats = self.df["Category_Id"].values[sel]
            elig = self.eligible_gc[:, cats].T & (avail[None, :].astype(bool))

            # Never emit a batch where some lead has no eligible group.
            dead = ~elig.any(axis=1)
            if dead.any():
                elig[dead] = avail.astype(bool)
            if not elig.any(axis=1).all():
                continue

            out.append(ReplayWindow(
                batch_id=bid, lead_idx=sel, t_start=t0,
                group_ids=np.arange(n_g), capacity=cap, available=avail,
                eligible=elig, observed_group=self.df["Observed_Group_Id"].values[sel],
            ))
            bid += 1
            if max_batches and bid >= max_batches:
                break
        return out


if __name__ == "__main__":
    import sys
    d = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parents[1] / "bpi2014"
    r = BPI2014Replay(d, batch_size=40)
    print(f"incidents kept      : {len(r.df):,}")
    print(f"assignment groups   : {len(r.groups)}")
    print(f"specialisations     : {len(r.categories)}")
    import numpy as _np
    _sh=r.group_category_share
    print(f"spec concentration  : mean top-1 share {_np.max(_sh,axis=1).mean():.2f} "
          f"(1.0 = every group does exactly one thing, low = no specialisation)")
    print(f"breach rate         : {r.breached.mean():.1%}")
    print(f"capacity  min/med/max: {r.group_capacity.min()} / "
          f"{int(np.median(r.group_capacity))} / {r.group_capacity.max()}")
    b = r.batches(max_batches=5)
    print(f"batches built       : {len(b)}")
    for w in b[:3]:
        print(f"  batch {w.batch_id}: {len(w.lead_idx)} leads, "
              f"eligible groups per lead min={w.eligible.sum(1).min()} "
              f"max={w.eligible.sum(1).max()}, free capacity total={w.capacity.sum()}")
