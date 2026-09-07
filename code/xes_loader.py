"""Stream a XES event log into a case-level table.

Written rather than using pm4py for three reasons. The logs here run to 1.2M events and a
streaming parse keeps memory flat; pm4py is not in the pinned requirements and adding it
would disturb an environment that took real effort to make reproducible; and the case-level
projection wanted here is a dozen lines, where pm4py would return a full event log object
that then has to be collapsed anyway.

The projection deliberately keeps only what the admissibility protocol needs:

    case id, first event time, last event time, the trace-level attributes, the ordered
    activity names, and the resource on each event.

Everything the protocol calls INADMISSIBLE is derivable from the event sequence, and is
derived explicitly at feature-construction time rather than being smuggled in here.

    from xes_loader import load_xes_cases
    cases, events = load_xes_cases("logs_raw/BPI Challenge 2017.xes.gz")
"""
from __future__ import annotations

import gzip
import os
from collections import OrderedDict

import pandas as pd
from lxml import etree

XES_NS = "{http://www.xes-standard.org/}"


def _strip(tag):
    return tag.split("}")[-1] if "}" in tag else tag


def _open(path):
    return gzip.open(path, "rb") if path.endswith(".gz") else open(path, "rb")


def load_xes_cases(path, max_traces=None, verbose=True):
    """Return (cases_df, events_df).

    cases_df  one row per trace: case id, trace attributes, n_events, t_open, t_end
    events_df one row per event: case id, position, activity, timestamp, resource

    Timestamps are parsed as UTC and returned tz-naive, because the two logs in this study
    carry different offsets and a mixed-offset column silently becomes object dtype.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(path)

    cases, ev_case, ev_pos, ev_act, ev_ts, ev_res = [], [], [], [], [], []
    n_traces = 0

    ctx = etree.iterparse(_open(path), events=("end",), tag=(XES_NS + "trace", "trace"))
    for _, trace in ctx:
        attrs = OrderedDict()
        events = []
        for child in trace:
            tag = _strip(child.tag)
            if tag == "event":
                e = {}
                for f in child:
                    k = f.get("key")
                    if k:
                        e[k] = f.get("value")
                events.append(e)
            elif tag in ("string", "date", "int", "float", "boolean", "id"):
                k = child.get("key")
                if k:
                    attrs[k] = child.get("value")

        cid = attrs.get("concept:name")
        if cid is None:
            trace.clear()
            continue

        row = {("case:" + k if not k.startswith("case:") else k): v
               for k, v in attrs.items() if k != "concept:name"}
        row["case_id"] = cid
        row["n_events"] = len(events)
        cases.append(row)

        # Several published logs (Sepsis, Hospital Billing, Road Traffic Fines) carry NO
        # trace-level attributes beyond concept:name: every case-descriptive field is written
        # onto the FIRST event instead. Those fields are lifted here with a first: prefix so a
        # spec can name them. Lifting is not the same as admitting them: a field can sit on
        # the first event and still be written retrospectively (isClosed, closeCode), so each
        # one is classified explicitly in log_schemas.py rather than admitted wholesale.
        if events:
            for k, v in events[0].items():
                if k not in ("concept:name", "time:timestamp", "lifecycle:transition"):
                    row["first:" + k] = v

        for i, e in enumerate(events):
            ev_case.append(cid)
            ev_pos.append(i)
            ev_act.append(e.get("concept:name"))
            ev_ts.append(e.get("time:timestamp"))
            ev_res.append(e.get("org:resource") or e.get("org:group"))

        n_traces += 1
        trace.clear()
        while trace.getprevious() is not None:
            del trace.getparent()[0]
        if verbose and n_traces % 5000 == 0:
            print("  %d traces, %d events" % (n_traces, len(ev_case)))
        if max_traces and n_traces >= max_traces:
            break

    events_df = pd.DataFrame({
        "case_id": ev_case, "pos": ev_pos, "activity": ev_act,
        "timestamp": pd.to_datetime(pd.Series(ev_ts), format="ISO8601", utc=True, errors="coerce"),
        "resource": ev_res,
    })
    events_df["timestamp"] = events_df["timestamp"].dt.tz_localize(None)

    cases_df = pd.DataFrame(cases)
    agg = events_df.groupby("case_id")["timestamp"].agg(["min", "max"])
    cases_df = cases_df.merge(agg.rename(columns={"min": "t_open", "max": "t_end"}),
                              left_on="case_id", right_index=True, how="left")

    if verbose:
        print("  parsed %d traces, %d events" % (len(cases_df), len(events_df)))
    return cases_df, events_df
