"""The transport plays any selection (M20): play/pause, back to the start and Loop selection
hold playback to a free range (a waveform drag, I / O, a bookmark) as well as to lines.

There's no JS runtime in the tests, so these read ``app.js`` for where each control gets
its range from: ``wfSel()`` (lines first, then the free range), never the line-only
``hasClipRange()`` / ``bufStart()`` / ``bufEnd()``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
pytestmark = pytest.mark.skipif(not (FRONTEND / "app.js").exists(),
                                reason="web/frontend is not part of this source tree")

LINE_ONLY = ("bufStart()", "bufEnd()")


def _js() -> str:
    return (FRONTEND / "app.js").read_text()


def _function(name: str) -> str:
    m = re.search(rf"^(?:async )?function {name}\(.*?^}}", _js(), re.M | re.S)
    assert m, f"no function {name} in app.js"
    return m.group(0)


def _pb_blocks() -> list[str]:
    """Each ``pb = { ... };`` controller (the <video> one and the YouTube embed's)."""
    blocks = [m.group(0) for m in re.finditer(r"^( *)pb = \{\n.*?^\1\};", _js(), re.M | re.S)]
    assert len(blocks) == 2, "expected the <video> and embed controllers"
    return blocks


def test_the_anchor_takes_lines_first_then_the_free_range():
    anchor = _function("wfAnchor")
    assert anchor.index("hasClipRange()") < anchor.index("clipRangeStart")
    assert "wfAnchor()" in _function("wfSel")


def test_play_and_back_to_start_use_the_selection_whatever_made_it():
    for block in _pb_blocks():
        assert block.count("playRange()") == 2, block  # restart() and toggle()
        assert "hasClipRange()" not in block
        assert not any(f in block for f in LINE_ONLY)
    assert "wfSel()" in _function("playRange") and "freeRangeArmed = !!r" in _function("playRange")


def test_the_range_loop_stops_or_loops_at_any_selections_end():
    js = _js()
    assert js.count("rangeEnded(") == 3  # the definition, the <video> loop, the embed poll
    ended = _function("rangeEnded")
    # Lines always hold playback; a free range only once the transport has started it.
    assert "hasClipRange() || freeRangeArmed" in ended and "wfSel()" in ended
    loop = re.search(r'addEventListener\("timeupdate", \(\) => \{\n    if \(settingSrc\).*?\}, \{ signal \}\);',
                     js, re.S)
    assert loop and "rangeEnded(absTime" in loop.group(0)
    assert not any(f in loop.group(0) for f in LINE_ONLY)


def test_a_seek_away_or_a_new_range_away_lets_playback_run_on():
    js = _js()
    assert 'addEventListener("seeking", () => rangeSought(' in js
    assert "rangeSought(seconds)" in _function("seekTo")  # the embed has no seeking event
    assert "rangeSought(playerTime())" in _function("showFreeRange")
    sought = _function("rangeSought")
    assert "freeRangeArmed = false" in sought and "freeRangeArmed = true" not in sought


def test_loop_turned_on_inside_a_free_range_holds_playback_to_it():
    m = re.search(r'\$\("pb-loop"\)\.addEventListener\("change", \(e\) => \{.*?^\}\);', _js(), re.M | re.S)
    assert m and "wfSel()" in m.group(0) and "freeRangeArmed = true" in m.group(0)


def test_the_play_buttons_note_shows_any_selection():
    ui = _function("updatePlaybackUI")
    assert "wfSel()" in ui and "hasClipRange()" not in ui
    assert "updatePlaybackUI()" in _function("renderFreeRange")  # handles move a free range


def test_a_new_item_starts_unheld():
    assert "freeRangeArmed = false" in _function("openItem")
