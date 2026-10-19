"""The frontend's script and markup agree on element ids.

``app.js`` addresses about a hundred elements by id (``$("clip-btn")``). Moving or renaming one in
``index.html`` while leaving the script alone would only fail at runtime, as a null dereference
in a browser nobody is watching. This test fails first: every id the script looks up must exist
in ``index.html`` or be created by the script itself, and ``index.html`` must not repeat an id.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
# The Nix build packages web/backend alone, so the frontend isn't next to the tests there.
needs_frontend = pytest.mark.skipif(not (FRONTEND / "app.js").exists(),
                                    reason="web/frontend is not part of this source tree")

_ID_ATTR = re.compile(r'\bid="([A-Za-z][\w-]*)"')
_LOOKUPS = (
    re.compile(r"""\$\(\s*["']([A-Za-z][\w-]*)["']\s*\)"""),
    re.compile(r"""getElementById\(\s*["']([A-Za-z][\w-]*)["']\s*\)"""),
    re.compile(r"""querySelector(?:All)?\(\s*["']#([A-Za-z][\w-]*)["']\s*\)"""),
)
_ID_ASSIGN = re.compile(r"""\.id\s*=\s*["']([A-Za-z][\w-]*)["']""")


def _looked_up(js: str) -> set[str]:
    return {m for pat in _LOOKUPS for m in pat.findall(js)}


def _created_by_script(js: str) -> set[str]:
    """Ids the script puts on elements it builds (template strings and ``el.id = ...``)."""
    return set(_ID_ATTR.findall(js)) | set(_ID_ASSIGN.findall(js))


def test_scanners():
    js = ('$("a-b"); document.getElementById(\'c\'); x.querySelector("#d"); '
          'el.innerHTML = `<i id="e">`; node.id = "f";')
    assert _looked_up(js) == {"a-b", "c", "d"}
    assert _created_by_script(js) == {"e", "f"}


@needs_frontend
def test_every_looked_up_id_exists():
    html = (FRONTEND / "index.html").read_text()
    js = (FRONTEND / "app.js").read_text()
    known = set(_ID_ATTR.findall(html)) | _created_by_script(js)
    missing = sorted(_looked_up(js) - known)
    assert not missing, f"app.js looks up ids that index.html does not define: {missing}"


@needs_frontend
def test_index_html_has_no_duplicate_ids():
    ids = Counter(_ID_ATTR.findall((FRONTEND / "index.html").read_text()))
    assert not [i for i, n in ids.items() if n > 1], f"duplicate ids: {[i for i, n in ids.items() if n > 1]}"


@needs_frontend
def test_hls_js_leaves_our_subtitle_track_alone():
    """hls.js's text-track controllers clear the cues of every labelled text track on attach,
    manifest load and detach, so a <track> whose VTT loaded before hls.js attached showed no
    subtitles (Firefox on deckhand, 2026-10-19). The player must build hls.js without them."""
    js = (FRONTEND / "app.js").read_text()
    calls = re.findall(r"new window\.Hls\((.*?)\)", js, re.S)
    assert calls, "app.js no longer constructs hls.js; move this guard with it"
    for args in calls:
        for key in ("timelineController", "subtitleTrackController", "subtitleStreamController"):
            assert re.search(rf"\b{key}\s*:\s*null", args), f"new Hls(...) must set {key}: null"
