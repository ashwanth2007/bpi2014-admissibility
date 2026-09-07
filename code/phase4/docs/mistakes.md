# Phase 4 mistakes

Bugs hit during the build, their root cause, and the fix. Written in the format of
`knowledge/engineering/mistakes/`. The point is that the next person does not repeat them.

---

## M1. Anaconda's numpy was broken twice by pip installs

**Symptom.** `ImportError: numpy.core.multiarray failed to import`, then every scipy,
lightgbm and catboost import failing.

**Root cause.** `pip install shap` and later `pip install tabpfn` both pulled numpy 2.x
as a dependency. Anaconda's scipy, and every compiled extension in the base environment,
were built against numpy 1.x. The upgrade silently broke the whole stack.

**Second occurrence, worse.** An attempt to isolate TabPFN in a `python -m venv` failed
partway (Anaconda's venv module could not copy `python.exe` into `Scripts/`), and the
subsequent `pip install` silently ran under **anaconda's** interpreter instead of the
venv's. That would have broken numpy a third time. Caught by inspecting the running
process command line before it finished, and killed.

**Fix.** Pin numpy explicitly in every install: `pip install <pkg> "numpy==1.26.4"`. Run
`pip install --dry-run` first and confirm numpy is not in the "Would install" list. For
genuinely isolated environments use `uv venv`, not `python -m venv`, which works
correctly here.

**Lesson.** After any install in this environment, immediately verify:
`python -c "import numpy,scipy,xgboost,lightgbm,catboost; print(numpy.__version__)"`.

---

## M2. Piping a long-running script to `head` kills it

**Symptom.** `model_b_v3.py` ran for over 25 minutes accumulating CPU time but produced
zero output, and would have crashed on its next print.

**Root cause.** It was launched as `python -u model_b_v3.py 2>&1 | head -40`. `head` exits
after 40 lines and closes the pipe. The Python process then gets SIGPIPE / BrokenPipeError
on its next write. Worse, until that next write it looks perfectly healthy: it has a PID
and is burning CPU.

**Fix.** Redirect long runs to a file (`> run.log 2>&1`) and tail the file. Never pipe a
long-running job to `head`.

---

## M3. `ChoiceRandomMutation` requires a Choice-typed pymoo problem

**Symptom.** `AttributeError: 'LeadAssignmentProblem' object has no attribute 'vars'`.

**Root cause.** pymoo's `ChoiceRandomMutation` is for problems declared with `Choice`
variables. Our problem uses a plain integer encoding (`vtype=int`), which has no `.vars`.

**Fix.** Use our own `SLAAwareMutation(sla_aware=False)` as the control's mutation. This
turned out better than a workaround: because the control and PAA now share the same
mutation mechanics and differ only in the bias, the ablation isolates exactly the claimed
modification instead of confounding it with a different operator implementation.

---

## M4. pymoo passes `random_state`, not `seed`, into operators

**Symptom.** Custom operators produced identical output across runs and ignored the seed.

**Root cause.** The operators read `kwargs.get("seed")`. pymoo 0.6.2 passes a numpy
`Generator` under `random_state`.

**Fix.** A `_rng(kwargs)` helper in `operators.py` that accepts a `Generator`, an int, or
nothing, and returns a usable generator in each case.

---

## M5. NSGA-III silently misconfigured: 70 reference directions, population 60

**Symptom.** A wall of `WARNING: pop_size=60 is less than the number of reference
directions ref_dirs=70. This might cause unwanted behavior of the algorithm.`

**Root cause.** `get_reference_directions("das-dennis", 5, n_partitions=4)` produces
C(8,4) = 70 directions for 5 objectives. NSGA-III needs the population to be at least the
number of reference directions.

**Fix.** `n_partitions=3` gives 35 directions, comfortably inside a population of 60.

**Lesson.** This produced no error and no obviously wrong number. It was only caught by
reading the run log. A control algorithm that is quietly misconfigured makes the treatment
look better than it is, which is exactly the failure mode that matters here.

---

## M6. `Category` was the wrong specialisation key and made a constraint non-binding

**Symptom.** Every lead had 43 to 49 eligible groups out of 50, and the expertise
objective barely varied.

**Root cause.** `Category` in BPI 2014 has 4 values describing request type, not technical
domain. Using it meant constraint C4 (expertise eligibility) never actually excluded
anything, so an objective and a constraint were both effectively dead.

**Fix.** Switched to `CI Subtype (aff)`, 58 values, genuine technical domains. Eligible
groups per lead dropped to a 2 to 49 range and mean top-1 specialisation share rose to
0.77.

**Lesson.** Check that a constraint actually binds. A constraint that never excludes
anything is not a constraint, and an objective computed from it is not an objective.
