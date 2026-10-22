"""Making the first selection on the waveform (M20): a drag, or I / O at the playhead.

There's no JS runtime in the tests, so these read ``app.js`` and ``index.html`` for the
pieces that make it work: which pointer selects and which pans, the guards that keep a
snap from landing on a selection made since, and the keys and buttons with their names.
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
    m = re.search(rf"^(?:async )?function {name}\(.*?^}}", _js(), re.M | re.S)
    assert m, f"no function {name} in app.js"
    return m.group(0)


def _listener(event: str) -> str:
    m = re.search(rf'^\$\("wf-stage"\)\.addEventListener\("{event}", \(e\) => \{{.*?^\}}\);',
                  _js(), re.M | re.S)
    assert m, f"no wf-stage {event} listener"
    return m.group(0)


def test_a_mouse_drag_selects_and_shift_or_touch_pans():
    down = _listener("pointerdown")
    assert 'touch || e.shiftKey || e.button === 1 ? "pan" : "select"' in down
    # On touch, holding still for WF_HOLD_MS turns the pan into a selection.
    assert "WF_HOLD_MS" in down and 'd.kind = "select"' in down


def test_a_drag_replaces_the_selection_with_a_free_range_and_no_padding():
    body = _function("wfSetFreeRange")
    assert '$("clip-before").value = "0"' in body and '$("clip-after").value = "0"' in body
    assert "showFreeRange(" in body
    # The range was made on screen: the view must not jump to it.
    assert "wf.fromHandle = true" in body
    move = _listener("pointermove")
    assert 'd.kind === "select"' in move and "wfCueSnap(" in move and "wfSetFreeRange(" in move


def test_a_click_still_plays_from_there():
    end = _function("wfPointerEnd")
    assert "!d.moved && !d.held" in end and "seekTo(" in end
    assert 'd.kind === "select" && d.moved' in end and "wfSnapRange()" in end


def test_the_range_snap_checks_the_selection_is_still_the_one_dragged():
    body = _function("wfSnapRange")
    assert "++wf.snapSeq" in body and "seq !== wf.snapSeq" in body
    assert "wfIsRange(a, b)" in body
    assert "wfServerSnap(a)" in body and "wfServerSnap(b)" in body
    assert "announce(" in body


def test_i_and_o_set_the_points_at_the_playhead():
    js = _js()
    for key, k in (("i", 0), ("I", 0), ("o", 1), ("O", 1)):
        assert f"{key}: () => wfSetPoint({k})" in js
    body = _function("wfSetPoint")
    assert "playerTime()" in body and "WF_POINT_SPAN" in body
    assert "wfMoveHandle(k, t)" in body and "wfSetFreeRange(a, b)" in body
    # The announcement says where the point landed: "In at 5:53.2".
    assert 'announce(`${name} at ${formatTimePrecise(now[k], 1)}`' in body


def test_set_in_and_set_out_buttons_sit_in_the_transport():
    html = (FRONTEND / "index.html").read_text()
    transport = re.search(r'<div id="playback-controls".*?</div>', html, re.S).group(0)
    for id_, key, text in (("pb-set-in", "I", "Set in"), ("pb-set-out", "O", "Set out")):
        tag = re.search(rf'<button[^>]*id="{id_}"[^>]*>([^<]*)</button>', transport)
        assert tag, id_
        assert f'aria-keyshortcuts="{key}"' in tag.group(0) and tag.group(1) == text
    assert '$("pb-set-in").onclick = () => wfSetPoint(0)' in _js()
    assert '$("pb-set-out").onclick = () => wfSetPoint(1)' in _js()


def test_the_waveform_says_how_to_select():
    html = (FRONTEND / "index.html").read_text()
    assert 'class="wf-hint"' in html and "Drag to select" in html and "Long-press" in html


def test_i_and_o_are_not_taken_by_another_shortcut():
    # The script owns S and E; nothing else may answer I or O.
    js = _js()
    assert not re.search(r"""e\.key === ["'][iIoO]["']""", js)


def test_bookmark_saves_a_free_range_too():
    for name in ("openBookmarkLabel", "saveBookmark"):
        body = _function(name)
        assert "wfAnchor()" in body and "hasClipRange()" not in body
    assert '$("mark-save").disabled = false' in _function("showFreeRange")
