"""The waveform's Playhead button (M20).

**Playhead** (or P) centres the view on the playhead at the current zoom. While playing,
the view still turns a page when the playhead runs off its right edge; there is no
continuous follow mode (wiz tried one and chose page turns). There's no JS runtime in the
tests, so these read ``app.js`` and ``index.html``.
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


def _html() -> str:
    return (FRONTEND / "index.html").read_text()


def _function(name: str) -> str:
    m = re.search(rf"^(?:async )?function {name}\(.*?^}}", _js(), re.M | re.S)
    assert m, f"no function {name} in app.js"
    return m.group(0)


def test_the_playhead_button_sits_with_the_zoom_buttons_and_has_a_key():
    zoom = re.search(r'aria-label="Waveform zoom">(.*?)</div>', _html(), re.S).group(1)
    button = re.search(r'<button[^>]*id="wf-go-head"[^>]*>([^<]*)</button>', zoom)
    assert button, "the Playhead button belongs in the zoom group"
    assert button.group(1) == "Playhead"
    assert 'aria-keyshortcuts="P"' in button.group(0) and "(P)" in button.group(0)
    js = _js()
    assert '$("wf-go-head").onclick = wfGoToHead;' in js
    assert "p: wfGoToHead, P: wfGoToHead," in js
    assert "<kbd>P</kbd> brings the playhead into view." in _html()


def test_the_playhead_button_centres_the_view_at_this_zoom():
    go = _function("wfGoToHead")
    assert "span = wfSpan()" in go
    assert "wfSetView(t - span / 2, t + span / 2)" in go


def test_playback_turns_pages_and_never_scrolls_continuously():
    tick = _function("wfTick")
    assert "wfSetView(b, b + (b - a));  // the playhead ran off the right edge: next page" in tick
    assert 'id="wf-follow"' not in _html()
    assert "qc_wf_follow" not in _js()
