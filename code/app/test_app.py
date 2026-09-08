"""Run every page of the Streamlit app headlessly and fail on any exception.

Streamlit swallows a runtime error into a red box inside the page, so an app that is
completely broken still serves HTTP 200 and still reports a healthy server. "The server
started" is not evidence that the interface works, in the same way that "the edit applied"
was never evidence that a number was right.

AppTest executes the real script in-process, one run per page, and exposes whatever the page
produced: exceptions, widgets, dataframes, metrics. That is an executed check rather than a
read of the source.

    python app/test_app.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from streamlit.testing.v1 import AppTest  # noqa: E402

PAGES = ["Queue", "Incident", "Assignment", "Evidence"]
SCRIPT = str(HERE / "streamlit_app.py")


def run(page):
    at = AppTest.from_file(SCRIPT, default_timeout=180)
    at.run()
    if at.exception:
        return at, [str(e.value)[:400] for e in at.exception]
    # the sidebar radio selects the page
    if page != "Queue":
        at.radio[0].set_value(page).run()
        if at.exception:
            return at, [str(e.value)[:400] for e in at.exception]
    return at, []


def main():
    failures = 0
    for page in PAGES:
        at, errs = run(page)
        if errs:
            failures += 1
            print("  FAIL  %-11s %s" % (page, errs[0]))
            for e in errs[1:]:
                print("        %s" % e)
            continue
        counts = {
            "metric": len(at.metric), "dataframe": len(at.dataframe),
            "selectbox": len(at.selectbox), "slider": len(at.slider),
            "markdown": len(at.markdown), "tabs": len(at.tabs),
        }
        shape = " ".join("%s=%d" % (k, v) for k, v in counts.items() if v)
        print("  ok    %-11s %s" % (page, shape))

        # a page that renders nothing is a pass that means nothing
        if counts["markdown"] + counts["dataframe"] + counts["metric"] == 0:
            failures += 1
            print("  FAIL  %-11s rendered no content at all" % page)

    print()
    if failures:
        print("%d page(s) failed. The server being up is not the same as the app working."
              % failures)
        return 1
    print("All %d pages execute with no exception and render content." % len(PAGES))
    return 0


if __name__ == "__main__":
    sys.exit(main())
