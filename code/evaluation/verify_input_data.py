"""Pin every input file by checksum, and check them.

The paper says the canonical incident file was re-downloaded and its checksum recorded. That
was not true when it was written: the file had been verified once, by hand, and the digest
lived in a chat transcript. A checksum nobody stored is a checksum nobody can check, which is
the same defect as a number nobody can re-derive.

It matters more here than for a typical input. The single most expensive error in this work
was running for weeks on a truncated copy of `Detail_Incident.csv`, 31,238 rows against the
canonical 46,606, with nothing anywhere that would have noticed. Every artifact was computed
correctly from the wrong file. A digest recorded on the first day would have caught it on the
first run.

    python evaluation/verify_input_data.py           # check against the recorded digests
    python evaluation/verify_input_data.py --record  # write the digests for what is on disk

Recording is a deliberate, separate action. A tool that silently re-recorded whatever it found
would agree with any file it was given, which is worse than no tool at all.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CODE = os.path.dirname(HERE)
STORE = os.path.join(CODE, "input_checksums.json")

# path relative to code/, and what the file is. Logs downloaded per log_schemas.py DOIs.
INPUTS = [
    ("bpi2014/Detail_Incident.csv", "BPI Challenge 2014 incident header"),
    ("bpi2014/Detail_Incident_Activity.csv", "BPI Challenge 2014 activity log"),
    ("bpi2014/Detail_Interaction.csv", "BPI Challenge 2014 interaction log"),
    ("logs_raw/BPI Challenge 2017.xes.gz", "BPI Challenge 2017"),
    ("logs_raw/Sepsis Cases - Event Log.xes.gz", "Sepsis Cases"),
    ("logs_raw/Hospital Billing - Event Log.xes.gz", "Hospital Billing"),
    ("logs_raw/Road_Traffic_Fine_Management_Process.xes.gz", "Road Traffic Fines"),
]


def digest(path):
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def measure(rel):
    p = os.path.join(CODE, rel)
    if not os.path.exists(p):
        return None
    rec = {"md5": digest(p), "bytes": os.path.getsize(p)}
    if rel.endswith(".csv"):
        with open(p, "rb") as fh:
            rec["lines"] = sum(1 for _ in fh)
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--record", action="store_true")
    args = ap.parse_args()

    known = {}
    if os.path.exists(STORE):
        known = json.load(io.open(STORE, encoding="utf-8")).get("inputs", {})

    if args.record:
        out = {}
        for rel, what in INPUTS:
            m = measure(rel)
            if m is None:
                print("  [skip] %s is not present" % rel)
                continue
            m["what"] = what
            out[rel] = m
            print("  recorded %-52s %s  %d bytes" % (rel, m["md5"], m["bytes"]))
        json.dump({"note": ("Digests of every raw input. Checked by "
                            "evaluation/verify_input_data.py. Recorded deliberately, never "
                            "auto-refreshed: a tool that re-records whatever it finds agrees "
                            "with any file it is given."),
                   "inputs": out},
                  io.open(STORE, "w", encoding="utf-8"), indent=2)
        print("\nwrote %s" % STORE)
        return 0

    if not known:
        print("No recorded digests. Run with --record once, then commit %s"
              % os.path.basename(STORE))
        return 1

    bad, missing, ok = [], [], 0
    for rel, what in INPUTS:
        if rel not in known:
            continue
        m = measure(rel)
        if m is None:
            missing.append(rel)
            continue
        k = known[rel]
        if m["md5"] != k["md5"]:
            bad.append((rel, k, m))
        else:
            ok += 1

    for rel in missing:
        print("  MISSING  %s" % rel)
    for rel, k, m in bad:
        print("  CHANGED  %s" % rel)
        print("           recorded %s  %s bytes%s"
              % (k["md5"], k["bytes"],
                 ", %s lines" % k["lines"] if "lines" in k else ""))
        print("           on disk  %s  %s bytes%s"
              % (m["md5"], m["bytes"],
                 ", %s lines" % m["lines"] if "lines" in m else ""))
    print()
    print("%d input(s) verified, %d changed, %d missing" % (ok, len(bad), len(missing)))
    if bad or missing:
        print()
        print("An input that does not match its digest invalidates every artifact computed")
        print("from it. This project has already lost weeks to exactly that, on a truncated")
        print("copy of the incident file that nothing was checking.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
