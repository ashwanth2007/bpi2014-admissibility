"""Check that the two builds of the manuscript actually say the same thing.

paper.tex and paper_ieee.tex are two presentations of ONE manuscript. Before 2026-09-07
they were not: the abstract, the conclusion and the declarations were duplicated inline in
both drivers, and they silently diverged. The IEEE build shipped a code-availability
sentence carrying a GitHub URL that the single-column build did not, and both PDFs went
out that way. Nothing caught it, because the claim verifiers read CSV artifacts and never
look at the manuscript at all.

Two things this deliberately does NOT do, because both produce false alarms rather than
signal:

  * It does not compare sentences. A one-column article and a two-column IEEE paper wrap
    and hyphenate differently, and pdf extraction interleaves columns, so identical prose
    yields different fragments.
  * It does not compare the bibliography. `unsrtnat` prints full author forenames and
    DOIs; `IEEEtranN` abbreviates and omits them. Those differences are correct.

What it does instead: assert that every passage of SHARED SOURCE renders in both PDFs.
Those are the passages that can silently diverge, and the ones a reviewer reads first.

Exit 0 if every shared passage is present in both builds.

    python evaluation/verify_pdf_parity.py
"""
import os
import re
import sys

from pypdf import PdfReader

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
PAPER = os.path.normpath(os.path.join(ROOT, "paper"))

LIGATURES = {chr(0xFB00): "ff", chr(0xFB01): "fi", chr(0xFB02): "fl",
             chr(0xFB03): "ffi", chr(0xFB04): "ffl"}
QUOTES = {chr(0x2018): "'", chr(0x2019): "'", chr(0x201C): '"', chr(0x201D): '"'}
DASHES = [chr(0x2013), chr(0x2014), chr(0x2212)]

# Files whose text is shared by both drivers. If a passage lives here it MUST render
# identically in both builds.
SHARED = ["sections_abstract.tex", "sections_conclusion.tex", "sections_declarations.tex"]

CHUNK = 90   # letters, roughly a line and a half of prose


def tokens(t):
    """The single tokeniser. Probe and haystack MUST both go through it.

    Filtering on one side only guarantees a false failure: a probe that has dropped its
    one-letter words can never be a substring of a haystack that kept them. That mistake
    cost a debugging round on 2026-09-07.

    Digits are dropped on purpose. siunitx renders numeric macros with a thin space and
    the DOI line-break macro splits identifiers at arbitrary points, so numeric text never
    matches the source token for token. Every published number is already checked against
    its artifact by verify_paper_claims.py and verify_deep_claims.py; this script is about
    prose that can go missing.
    """
    for a, b in LIGATURES.items():
        t = t.replace(a, b)
    for a, b in QUOTES.items():
        t = t.replace(a, b)
    for d in DASHES:
        t = t.replace(d, "-")
    # Reduce to a continuous stream of letters: no spaces, no digits, no punctuation.
    # This is deliberately cruder than word matching, and it is the right crudeness here.
    # A two-column build hyphenates "precision-recall" across a line break, so the pdf
    # yields "precisionrecall" where the source yields "precision recall"; IEEE numbers its
    # headings, so "Conclusion" arrives as "V. Conclusion". Both break word alignment while
    # changing nothing a reader would notice. A letter stream is immune to both, and still
    # catches the failure that actually happened: a whole sentence present in one build and
    # absent from the other.
    return re.sub(r"[^a-z]+", "", t.lower())


def pdf_text(path):
    reader = PdfReader(path)
    return tokens("\n".join((p.extract_text() or "") for p in reader.pages))


def strip_latex(t):
    t = re.sub(r"(?m)^%.*$", " ", t)                 # comments
    t = re.sub(r"\\(href|url)\{[^}]*\}", " ", t)     # link targets are not printed prose
    t = re.sub(r"\\[a-zA-Z]+\*?", " ", t)            # control sequences
    t = re.sub(r"[{}$&~\\]", " ", t)
    return t


def passages(src):
    stream = tokens(strip_latex(src))
    if len(stream) <= CHUNK:
        return [stream] if stream else []
    return [stream[i:i + CHUNK] for i in range(0, len(stream) - CHUNK + 1, CHUNK)]


def main():
    a_path = os.path.join(PAPER, "paper.pdf")
    b_path = os.path.join(PAPER, "paper_ieee.pdf")
    for p in (a_path, b_path):
        if not os.path.exists(p):
            print("MISSING: %s" % p)
            return 1

    ta, tb = pdf_text(a_path), pdf_text(b_path)
    failures = 0

    for fname in SHARED:
        fpath = os.path.join(PAPER, fname)
        if not os.path.exists(fpath):
            print("%-30s MISSING SOURCE" % fname)
            failures += 1
            continue
        chunks = passages(open(fpath, encoding="utf-8").read())
        miss_a = [c for c in chunks if c not in ta]
        miss_b = [c for c in chunks if c not in tb]
        ok = not miss_a and not miss_b
        if not ok:
            failures += 1
        print("%-30s %3d probes   paper:%-10s ieee:%-10s %s"
              % (fname, len(chunks),
                 "OK" if not miss_a else "%d missing" % len(miss_a),
                 "OK" if not miss_b else "%d missing" % len(miss_b),
                 "OK" if ok else "FAIL"))
        for c in (miss_a + miss_b)[:4]:
            print("      %s" % c[:110])

    print()
    if failures:
        print("PARITY FAILED: %d shared block(s) do not render in both builds." % failures)
        return 1
    print("PARITY OK: every shared passage renders in both builds.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
