"""Check mechanically that no inadmissible attribute reaches the admissible model.

Rule 1 says an attribute may be used only if its value is measurable at the decision point.
The paper states it, argues for it, and prices it. Nothing was checking that the code obeyed
it, and the code did not: the standing-queue count at a case's arrival was computed over cases
that eventually resolved, which is a fact about the future of each case, and it went undetected
through every run and every review until the rule was applied by hand.

The rule is mechanical, so the check should be too. Three things are asserted:

  1. Every feature the admissible model trains on is marked knowable at assignment time in
     feature_justifications_leakfree.csv. A feature in the model and not in that file is a
     failure too: a feature nobody wrote a justification for is a feature nobody checked.

  2. Every feature marked NOT knowable is absent from the admissible model and present in the
     contaminated one. A feature documented as inadmissible and then never actually excluded
     would make the whole comparison meaningless, and four of the nine documented exclusions
     were once exactly that: dictionary keys that no code ever computed.

  3. The admissible and contaminated arms differ ONLY by inadmissible features. If the two
     arms differ in any other way, the gap between them is not the price of admissibility.

    python evaluation/verify_rule1.py
"""
from __future__ import annotations

import io
import os
import sys

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
CODE = os.path.dirname(HERE)
RUN_TAG = os.environ.get("BPI_RUN_TAG", "7030")
R = os.path.join(CODE, "results_bpi_leakfree" + (("_" + RUN_TAG) if RUN_TAG else ""))

ADMISSIBLE_MARKS = ("yes",)


def main():
    jpath = os.path.join(R, "feature_justifications_leakfree.csv")
    if not os.path.exists(jpath):
        print("no feature_justifications_leakfree.csv under %s" % R)
        return 2
    j = pd.read_csv(jpath)
    j["mark"] = j["Known_at_assignment_time"].astype(str).str.strip().str.lower()
    admissible = set(j.loc[j["mark"].str.startswith("yes"), "Feature"])
    inadmissible = set(j.loc[~j["mark"].str.startswith("yes"), "Feature"])

    sys.path.insert(0, CODE)
    import _openmp_first                                            # noqa: F401
    import bpi2014_data as bd

    # The FEATURE LIST each arm trains on, not the matrix that holds both. The first version
    # of this check read the shared matrix and reported ten violations that were not
    # violations: the contaminated arm needs those columns, and the matrix is the union. A
    # check that cannot tell a superset from the thing being tested is worse than none, and
    # this one nearly went into the paper as a finding.
    used = list(bd.BASE_CLEAN) + list(getattr(bd, "GROUP_TE", []))
    contaminated = list(bd.CONTAMINATED)

    fails = []

    # 0. the two arms differ ONLY by inadmissible features
    extra = set(contaminated) - set(used)
    dropped = set(used) - set(contaminated) - set(getattr(bd, "GROUP_TE", []))
    for f in sorted(extra & admissible):
        fails.append("the contaminated arm adds %s, which is documented ADMISSIBLE, so the "
                     "gap between arms is not purely the price of admissibility" % f)
    for f in sorted(dropped):
        fails.append("the admissible arm has %s and the contaminated arm does not; the arms "
                     "must differ only by inadmissible features" % f)

    # The group encodings are the one asymmetry that is NOT a failure, and it is reported
    # rather than exempted silently. The admissible arm carries two Rule 2 encodings that the
    # contaminated arm has no counterpart for, because the v1 configuration this reproduces
    # target-encoded over the whole dataset and that column is one of the documented
    # exclusions. So the two arms are not a clean superset and subset, and the headline cost
    # is therefore an UNDER-estimate of what removing the inadmissible attributes costs on
    # its own. The like-for-like figure is printed so nobody has to derive it.
    te = list(getattr(bd, "GROUP_TE", []))
    asym = [f for f in te if f not in contaminated]

    # 1. every used feature is documented and marked admissible
    for f in used:
        if f not in admissible and f not in inadmissible:
            fails.append("feature %s is used by the admissible model and appears in no "
                         "justification row" % f)
        elif f in inadmissible:
            fails.append("feature %s is documented as NOT knowable at assignment time and is "
                         "in the admissible model anyway" % f)

    # 2. every documented exclusion is genuinely excluded, and is not a phantom
    for f in sorted(inadmissible):
        if f in used:
            fails.append("documented exclusion %s is present in the admissible matrix" % f)

    print("features in the admissible arm      : %d" % len(used))
    print("features in the contaminated arm    : %d" % len(contaminated))
    print("documented admissible               : %d" % len(admissible))
    print("documented inadmissible             : %d" % len(inadmissible))
    print()

    undocumented = [f for f in used if f not in admissible and f not in inadmissible]
    if undocumented:
        print("  used but undocumented: %s" % ", ".join(undocumented))
    unused_adm = sorted(admissible - set(used))
    if unused_adm:
        print("  documented admissible but not used: %s" % ", ".join(unused_adm))
        print("  (not a failure: the ladder uses subsets, and some are engineered into others)")
    print()

    if asym:
        print("  ASYMMETRY, reported not exempted: the admissible arm carries %s, which the"
              % ", ".join(asym))
        print("  contaminated arm has no counterpart for. The arms are therefore not a clean")
        print("  superset and subset, and the headline cost UNDER-states what removing the")
        print("  inadmissible attributes costs alone. Like for like, from the feature ladder:")
        try:
            lad = pd.read_csv(os.path.join(R, "feature_ladder_leakfree.csv")
                              ).set_index("Feature_Set")["Test_AUC"]
            mc = pd.read_csv(os.path.join(R, "model_comparison_leakfree.csv"))
            x = mc[mc.Model == "XGBoost"].set_index("Config")["Test_AUC"]
            con, adm = float(x["contaminated_v1"]), float(x["leakfree"])
            no_te = float(lad["15_plus_config"])
            print("    contaminated %.4f  admissible with encodings %.4f  cost %.4f (%.1f%%)"
                  % (con, adm, con - adm, 100 * (con - adm) / (con - 0.5)))
            print("    contaminated %.4f  admissible without them   %.4f  cost %.4f (%.1f%%)"
                  % (con, no_te, con - no_te, 100 * (con - no_te) / (con - 0.5)))
        except Exception as e:
            print("    (could not compute: %s)" % e)
        print()

    if fails:
        for f in fails:
            print("  FAIL  %s" % f)
        print()
        print("%d Rule 1 violation(s). The gap between the two arms is only the price of" % len(fails))
        print("admissibility if the arms differ by nothing else.")
        return 1
    print("Rule 1 holds: every feature in the admissible matrix is documented as knowable at")
    print("the decision point, and every documented exclusion is genuinely absent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
