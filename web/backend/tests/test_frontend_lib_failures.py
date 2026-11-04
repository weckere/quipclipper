"""The Dialogue index card lists a library index run's failed files (M20): a "Failed files"
disclosure under the last run's summary, grouped by reason, loaded from
``GET /api/search/library/index/failures`` when it opens. These read ``app.js`` and
``index.html`` (no JS runtime in the tests)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
pytestmark = pytest.mark.skipif(not (FRONTEND / "app.js").exists(),
                                reason="web/frontend is not part of this source tree")


def _js() -> str:
    return (FRONTEND / "app.js").read_text()


def _function(name: str) -> str:
    m = re.search(rf"^(?:async )?function {name}\(.*?^}}", _js(), re.M | re.S)
    assert m, f"no function {name} in app.js"
    return m.group(0)


def test_the_card_has_a_failed_files_disclosure_for_the_cli_command():
    html = (FRONTEND / "index.html").read_text()
    m = re.search(r'<details id="lib-index-failures"[^>]*>', html)
    assert m and 'data-action="index.library-failures"' in m.group(0) and " hidden" in m.group(0)
    # Right under the last run's summary line, which keeps its counts.
    assert html.index('id="lib-index-status"') < html.index('id="lib-index-failures"')


def test_it_shows_when_a_run_failed_files_and_only_on_the_library_home():
    refresh = _function("refreshLibIndex")
    assert "libFailuresShow(last ? last.failed || 0 : 0)" in refresh
    assert "libFailuresShow(home ? r.failed || 0 : 0)" in refresh  # a run in progress too
    assert "libFailuresShow(0)" in refresh and "libFailuresShow(0)" in _function("libIndexReset")
    assert "libFailuresCount" in _function("libFailuresShow")


def test_it_loads_the_grouped_record_when_opened_and_builds_it_as_text():
    load = _function("loadLibFailures")
    assert 'getJSON("/api/search/library/index/failures")' in load
    assert "g.count" in load and "g.reason" in load and "f.path" in load
    # File names come from the disk: text nodes, never markup.
    assert "innerHTML" not in load and "textContent" in load
    assert "LIB_FAIL_SHOWN" in load  # a long group is cut, and says how many more
    assert re.search(r'\$\("lib-index-failures"\)\.addEventListener\("toggle"', _js())
