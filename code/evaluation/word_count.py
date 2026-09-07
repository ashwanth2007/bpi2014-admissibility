"""Count the manuscript's words, three ways, with the method stated for each.

The paper carried a word count of 5,323 with no recorded derivation, which is the same defect
as any other number nobody can re-derive: it cannot be checked, and it was almost certainly
measured on a draft that no longer exists. A journal's limit is usually stated without saying
what it counts either, so the honest thing is to report the count under several defensible
definitions and say which is which, rather than to pick one and present it as the number.

The three counts:

  body        LaTeX source, body prose only. Comments removed, then the abstract, the
              bibliography, every float (table and figure), and the declarations dropped.
              Commands are stripped, their bracketed arguments kept where they carry prose
              (\\emph, \\textbf, \\citeauthor) and dropped where they do not (\\label,
              \\includegraphics, \\num, \\SI, \\ref, \\cite). Display maths counts as one word.
              This is the count a "words of body text" limit means.

  body+float  the same, plus captions and table cells. Some venues count these.

  pdf         every word in the rendered PDF, including references and headers. This is the
              upper bound and the one that needs no interpretation of the source.

Run:  python evaluation/word_count.py
"""
from __future__ import annotations

import glob
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CODE = os.path.dirname(HERE)
PAPER = os.path.join(os.path.dirname(CODE), "paper")

DRIVER = "paper.tex"

# Dropped whole: their content is not body prose.
DROP_ENV = ["abstract", "thebibliography", "table", "table*", "figure", "figure*",
            "tabular", "algorithm"]
# Commands whose braced argument is prose and should be kept.
KEEP_ARG = ["emph", "textbf", "textit", "texttt", "citeauthor", "textsc", "underline"]
# Commands whose braced argument is not prose.
DROP_ARG = ["label", "ref", "eqref", "cite", "citep", "citet", "includegraphics", "input",
            "num", "SI", "si", "caption", "vspace", "hspace", "newcolumntype", "bibliography",
            "bibliographystyle", "usepackage", "documentclass", "graphicspath", "setlength"]


def strip_comments(s):
    return re.sub(r"(?<!\\)%.*", "", s)


def drop_environments(s, names):
    for n in names:
        s = re.sub(r"\\begin\{" + re.escape(n) + r"\}.*?\\end\{" + re.escape(n) + r"\}",
                   " ", s, flags=re.S)
    return s


def strip_commands(s):
    s = re.sub(r"\$\$.*?\$\$", " EQ ", s, flags=re.S)
    s = re.sub(r"\\begin\{equation\*?\}.*?\\end\{equation\*?\}", " EQ ", s, flags=re.S)
    s = re.sub(r"\\begin\{align\*?\}.*?\\end\{align\*?\}", " EQ ", s, flags=re.S)
    s = re.sub(r"\$[^$]*\$", " EQ ", s)
    for c in DROP_ARG:
        s = re.sub(r"\\" + c + r"\s*(\[[^\]]*\])?\s*\{[^{}]*\}", " ", s)
        s = re.sub(r"\\" + c + r"\s*(\[[^\]]*\])?\s*\{[^{}]*\{[^{}]*\}[^{}]*\}", " ", s)
    for c in KEEP_ARG:
        s = re.sub(r"\\" + c + r"\s*\{([^{}]*)\}", r"\1", s)
    s = re.sub(r"\\[A-Za-z@]+\*?", " ", s)
    s = re.sub(r"[{}\\&~^_]", " ", s)
    return s


def words(s):
    return [w for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9'\-]*", s) if w]


def expand(driver):
    """The driver with every \\input inlined, so one string is the whole manuscript."""
    src = open(os.path.join(PAPER, driver), encoding="utf-8").read()
    for _ in range(6):
        def sub(m):
            t = m.group(1).strip()
            for cand in (t, t + ".tex"):
                p = os.path.join(PAPER, cand)
                if os.path.exists(p):
                    return open(p, encoding="utf-8").read()
            return ""
        new = re.sub(r"\\input\{([^}]+)\}", sub, src)
        if new == src:
            break
        src = new
    return src


def pdf_words(path):
    try:
        from pypdf import PdfReader
    except ImportError:
        return None
    if not os.path.exists(path):
        return None
    r = PdfReader(path)
    t = "\n".join((p.extract_text() or "") for p in r.pages)
    for a, b in [("\ufb00", "ff"), ("\ufb01", "fi"), ("\ufb02", "fl"),
                 ("\ufb03", "ffi"), ("\ufb04", "ffl")]:
        t = t.replace(a, b)
    return len(words(t)), len(r.pages)


def main():
    src = strip_comments(expand(DRIVER))

    # declarations are a fixed administrative block, not body prose
    body_src = re.sub(r"\\input\{sections_declarations\}", " ", src)
    for marker in ("Declaration of competing interest", "Declarations"):
        i = body_src.find(marker)
        if i > len(body_src) * 0.5:
            body_src = body_src[:i]

    body = strip_commands(drop_environments(body_src, DROP_ENV))
    n_body = len(words(body))

    with_floats = strip_commands(drop_environments(body_src, ["abstract", "thebibliography"]))
    n_floats = len(words(with_floats))

    abstract = ""
    m = re.search(r"\\begin\{abstract\}(.*?)\\end\{abstract\}", src, re.S)
    if m:
        abstract = strip_commands(m.group(1))
    n_abs = len(words(abstract))

    print("word count, %s" % DRIVER)
    print("=" * 66)
    print("  abstract                          %6d" % n_abs)
    print("  body prose only                   %6d   floats, refs and abstract removed"
          % n_body)
    print("  body plus captions and tables     %6d" % n_floats)
    for name in ("paper.pdf", "paper_ieee.pdf"):
        r = pdf_words(os.path.join(PAPER, name))
        if r:
            print("  %-32s %6d   every word rendered, %d pages" % (name, r[0], r[1]))
    print("=" * 66)
    print("  Method is in this file's docstring. Quote a count WITH its definition; the")
    print("  three differ by more than a third and a limit that does not say which it")
    print("  means can be met or missed by choosing.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
