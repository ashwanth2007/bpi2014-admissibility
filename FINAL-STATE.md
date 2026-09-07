# Final state, 2026-09-08

> Supersedes the 2026-09-07 04:15 version, which was written before the correction round and
> was wrong about almost every number in it. Read
> `research/scope-decision-2026-09-06.md` for the scope, then
> `research/what-moved-and-why-2026-09-07.md` for why the numbers changed.

## What the paper now is

Five public event logs across four domains, one code path, one protocol. BPI Challenge 2014
is the spine and carries the two scoring formulations and the assignment study; BPI Challenge
2017, Sepsis Cases, Hospital Billing and Road Traffic Fine Management carry the admissibility
measurement only.

The manuscript builds as two PDFs from one set of shared sources: `paper.pdf` single column,
`paper_ieee.pdf` two column. Both compile clean under Tectonic.

## The headline results

Every number below is re-derived from an artifact by
`code/evaluation/verify_paper_claims.py` (49 checks) and
`code/evaluation/verify_deep_claims.py` (543 checks). Both exit non-zero on any mismatch.

- Enforcing admissibility costs **0.0792 AUC on BPI 2014, 19.6 per cent of all above-chance
  signal**, and every bootstrap interval on that loss excludes zero. Like for like, denying
  the admissible model its group encodings too, the cost is 0.0879 and 21.7 per cent; the
  headline is the smaller and more conservative of the two, and the paper says so.
- Across the five logs the share lost ranges from **9.7 per cent to 69.5 per cent**. There is
  no constant to quote.
- Contaminated models occupy a band of 0.155 AUC across the five logs; admissible models
  occupy 0.291. **Leakage is a leveller**: it makes unlike problems look alike.
- An admissible feature ladder rises from 0.5100 at three attributes to **0.8258** at
  seventeen, gaining 0.2542 between eight and seventeen with nothing leaking.
- Breach derived from a resolution hazard reaches **0.8373** at intake against **0.7879** for
  a direct classifier, from a model structurally unable to observe its own label.
- The assignment study is a **negative result and is reported as one**. PAA-NSGA-II beats
  standard NSGA-II on hypervolume (+0.00666, p = 1.5e-19) and loses to off-the-shelf NSGA-III
  by 8.2 per cent. One of its three components carries the whole gain; the other two raise the
  objective when removed.

## Six defects found in our own work, all disclosed in the paper

Ordered as the paper orders them: validity first, instrument second.

1. **The incident file was a partial copy.** 31,238 rows against the canonical 46,606, and a
   prefix rather than a sample, so it was close to a truncation in time. Cost 0.0307 on the
   chronological arm against 0.0043 on the random one.
2. **A feature of ours violated our own Rule 1.** The standing-queue count conditioned on
   whether a case eventually resolved. Found by applying the rule mechanically, not by
   intuition. Now checked by `evaluation/verify_rule1.py`.
3. **Rule 2 was not implemented as Rule 2.** The pipeline refitted the encoding inside every
   fold, which is not the equation the paper's contribution is. Now implemented as written in
   `code/causal_encoding.py` and checked against an O(n^2) reference to a maximum absolute
   difference of zero.
4. **The seed was salted per process**, measured at 43, 41 and 32 for one variant name.
5. **The hypervolume reference point depended on execution order.** This one reversed the sign
   of the assignment study's headline comparison.
6. **The OpenMP thread count was never pinned.** Changing only the thread count moves the
   admissible AUC by 0.0015 and the chronological arm by 0.0019, against a stated tolerance of
   0.00005. Repeats at a fixed count are bitwise identical, so no seed can absorb it. Now
   pinned in `code/_openmp_first.py` and measured by `evaluation/thread_determinism.py`.

Separately, a **date-parsing defect** in the loader parsed day-first timestamps month-first on
41 per cent of rows, and two things previously reported as properties of the data were
consequences of it.

## What checks what

| Check | What it catches |
|---|---|
| `verify_paper_claims.py` | 49 published numbers, including every number in the abstract and conclusion |
| `verify_deep_claims.py` | 543 numbers across every table |
| `verify_latex_against_artifacts.py` | any printed number that matches the pre-correction run and not the current one |
| `verify_figures_and_tables.py` | a figure rendered but never shown, an include that does not resolve, an empty float |
| `verify_rule1.py` | an inadmissible attribute reaching the admissible model |
| `verify_input_data.py` | an input file that is not the file the results were computed from |
| `verify_pdf_parity.py` | the two PDFs disagreeing on shared text |
| `thread_determinism.py` | how far the numbers move when only the thread count changes |
| `attribution_run.py` | which defect moved which number |
| `word_count.py` | the word count, three ways, each method stated |

Twenty-two of the manuscript's twenty-four numeric tables are generated from artifacts by
`code/figures/make_tables.py`. The two that are not are a feature-justification table and a
hyperparameter table, neither of which holds a measured number.

## Word count, with the method stated

- abstract 314
- body prose only 13,469, floats and references removed
- body plus captions and tables 16,752
- every rendered word 24,175 over 51 pages single column, 22,610 over 37 pages two column

The previous figure of 5,323 had no recorded derivation and was measured on a 19-page draft
that no longer exists. `evaluation/word_count.py` states the method for each count.

## What still needs Ashwanth

1. **The word count against the brief.** 13,469 body words against Deepika's stated 6,000 to
   7,000. That is roughly double. Cutting a paper in half is a scope decision, not a
   mechanical edit, and the material that would go is real: the five-log study, the two
   scoring formulations and the assignment study could each stand alone.
2. **The journal.** Not recoverable from any file or message.
3. **Whether her "80" means accuracy or AUC.** Open since 2026-08-24.
4. **Author contributions** is still a placeholder.
