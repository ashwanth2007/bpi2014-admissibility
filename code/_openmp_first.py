"""Import this FIRST, before sklearn, xgboost or catboost.

Two separate Windows problems are fixed here, and both must be fixed before any other
machine-learning library is loaded into the process.

1. THREAD COUNT PINNING (measured 2026-09-07)
---------------------------------------------
The published numbers are quoted to four decimals and checked against a tolerance of
0.00005. They are not stable to that precision unless the OpenMP thread count is fixed,
because the order in which LightGBM and XGBoost reduce their per-thread histogram
partials depends on how many threads there are, and floating-point addition is not
associative. Measured on this machine, one script, one seed, one dataset, varying only
the thread count:

    OMP_NUM_THREADS=2       holdout AUC 0.7866
    OMP_NUM_THREADS=4       holdout AUC 0.7879
    OMP_NUM_THREADS=8       holdout AUC 0.7874
    unset (all cores)       holdout AUC 0.7875

A spread of 0.0013 AUC, twenty-six times the verification tolerance, from a variable
nobody had written down. Repeated runs at the SAME thread count are bit-identical, so
this is not randomness and no seed can absorb it.

Pinned to 4 below. Four is portable: a machine with fewer cores still creates four
threads and therefore still reduces in four partials. What this does NOT promise is
bit-identity across different CPU architectures, since vector width also changes the
summation order within a thread. It removes the one source that varies on the same
machine, which is the one that was silently moving our own numbers between runs.

Override with BPI_THREADS if a run genuinely needs a different count; the value used is
recorded in the run log so any published number can be traced to it.

2. OPENMP RUNTIME CONFLICT (diagnosed 2026-09-07)
--------------------------------------------------
lightgbm ships `lib_lightgbm.dll` with its own bundled OpenMP runtime. scikit-learn and
xgboost each bring another one. When one of those is loaded into the process first, a
subsequent `LGBMClassifier.fit` dies inside `Dataset.set_field` with

    OSError: exception: access violation reading 0x0000000000000000

on any input, including 200 rows of random noise. Reproduced deterministically:

    import lightgbm                    -> fit OK
    import sklearn; import lightgbm    -> fit FAILS
    import xgboost;  import lightgbm   -> fit FAILS
    import lightgbm; import sklearn, xgboost -> fit OK

Importing lightgbm first makes its runtime the one already resident, and every library then
binds to it. This does NOT reproduce in the Anaconda base environment because conda's numpy
pulls in Intel's `libiomp5md.dll` before anything else, which both libraries then share. The
pip-installed venv has no MKL, so nothing arbitrates.

The usual workaround, KMP_DUPLICATE_LIB_OK=TRUE, is deliberately NOT used here. Intel documents
it as unsafe and capable of producing silently incorrect results, which is unacceptable in a
pipeline whose output is published numbers. Import order costs nothing and is deterministic.
"""
import os as _os

THREADS = _os.environ.get("BPI_THREADS", "4")

# Every one of these must be set BEFORE the library that reads it is imported. They are
# read once, at DLL load time, so setting them later in the process is a silent no-op.
for _v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
           "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
    _os.environ[_v] = THREADS

import lightgbm as _lightgbm  # noqa: F401,E402  MUST be the first ML import in the process
