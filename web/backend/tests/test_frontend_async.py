"""Stale async state in the frontend (the M18 review; the July reviews found the same class).

There's no JS runtime in the tests, so these read ``app.js`` for the guards that keep a
slow response from writing into whatever the user opened since, and the pollers from
running forever once their job is gone. Each names the function and what it must check.
"""

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
    """The body of a top-level ``function name(`` / ``async function name(``."""
    js = _js()
    m = re.search(rf"^(?:async )?function {name}\(.*?^}}", js, re.M | re.S)
    assert m, f"no function {name} in app.js"
    return m.group(0)


def test_a_script_still_loading_never_lands_in_another_item():
    assert "scriptLoadId++" in _function("openItem")
    assert "scriptLoadId++" in _function("stopPlayer")


@pytest.mark.parametrize("name", ["browse", "dialogueSearch", "librarySearch"])
def test_library_listings_drop_answers_a_newer_listing_replaced(name):
    body = _function(name)
    assert "++librarySearchId" in body and "!== librarySearchId" in body


def test_in_video_search_and_keyframe_probes_check_the_item():
    assert "stale()" in _function("doSearch")
    assert "keyframeProbe" in _js() and "signal.aborted && probe === keyframeProbe" in _js()


@pytest.mark.parametrize("name", ["pollBatch", "pollHitClip", "pollClipJob",
                                  "runLeadStingerBatch", "sendLeadStingerLink"])
def test_job_pollers_stop_when_the_job_is_gone(name):
    assert "404" in _function(name), f"{name} polls a cleared job forever"


def test_background_jobs_refresh_only_the_view_still_on_screen():
    js = _js()
    assert 'if (showingRoute("library", "path", "yt:")) browse("yt:")' in js
    assert 'showingRoute("clips", "folder", currentClipsFolder)' in js


def test_waveform_snaps_and_nudges_die_with_their_item():
    hide = _function("wfHide")
    assert "wf.snapSeq++" in hide and "clearTimeout(wf.nudgeTimer)" in hide


def test_focus_traps_skip_what_tab_skips():
    assert 'getAttribute("tabindex") !== "-1"' in _js()


def test_a_bookmark_range_replaces_the_line_selection():
    body = _function("showFreeRange")
    assert "clipFirst = -1" in body and "clipLast = -1" in body
    assert "showFreeRange(" in _function("useBookmarkForClip")


def test_bookmark_rows_wrap_on_a_phone():
    css = (FRONTEND / "styles.css").read_text()
    rule = re.search(r"\.bookmark-item \{[^}]*\}", css).group(0)
    assert "flex-wrap: wrap" in rule


def test_every_spinner_slows_for_reduced_motion():
    css = (FRONTEND / "styles.css").read_text()
    m = re.search(r"@media \(prefers-reduced-motion: reduce\) \{\s*([^{]*)\{ animation-duration", css)
    assert m and all(s in m.group(1) for s in (".clip-spinner", ".jobs-dot", ".job-mark"))
