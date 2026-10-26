"""The waveform's Playhead button and Follow playback switch (M20).

**Playhead** (or P) centres the view on the playhead at the current zoom. **Follow
playback** scrolls the view while the file plays so the playhead stays in the middle; the
sound ahead comes from a fetch that reaches a few views ahead, so a continuous scroll doesn't
cancel every load the way a run of wfSetView calls would. Your own move on the strip pauses
following until the next play or the button. Under reduced motion it turns pages instead.
There's no JS runtime in the tests, so these read ``app.js``, ``index.html`` and the command
registry for the pieces that make it work.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from quipclipper import commands as reg

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
    html = _html()
    zoom = re.search(r'aria-label="Waveform view">(.*?)</div>', html, re.S).group(1)
    button = re.search(r'<button[^>]*id="wf-go-head"[^>]*>([^<]*)</button>', zoom)
    assert button, "the Playhead button belongs in the zoom group"
    assert button.group(1) == "Playhead"
    assert 'aria-keyshortcuts="P"' in button.group(0) and "(P)" in button.group(0)
    js = _js()
    assert '$("wf-go-head").onclick = wfGoToHead;' in js
    assert "p: wfGoToHead, P: wfGoToHead," in js


def test_the_playhead_button_centres_the_view_and_resumes_following():
    go = _function("wfGoToHead")
    assert "wf.held = false" in go
    assert "wfSetView(t - span / 2, t + span / 2)" in go


def test_the_follow_switch_sits_beside_the_playhead_button_and_is_remembered():
    html = _html()
    # In the view row with Playhead, not beside the snap switches, where it read as a snap.
    view = re.search(r'aria-label="Waveform view">(.*?)</div>', html, re.S).group(1)
    assert view.index('id="wf-go-head"') < view.index('id="wf-follow"')
    tag = re.search(r'<input id="wf-follow"[^>]*>', html).group(0)
    assert 'class="switch"' in tag and 'type="checkbox"' in tag and "checked" not in tag  # off by default
    # The script has a "Follow playback" switch too: this one's name says which it is.
    assert 'aria-label="Follow playback on the waveform"' in tag
    js = _js()
    assert 'const WF_FOLLOW_KEY = "qc_wf_follow";' in js
    assert '$("wf-follow").checked = readPref(WF_FOLLOW_KEY) === "1";' in js
    assert 'writePref(WF_FOLLOW_KEY, e.target.checked ? "1" : null)' in js
    # A browser-only view preference: exempt from CLI parity, with its reason.
    assert reg.EXEMPT_UI_SETTINGS["qc_wf_follow"].startswith("presentation:")


def test_following_scrolls_with_the_playhead_in_the_middle():
    tick = _function("wfTick")
    assert "const follow = playing && !wf.drag && !wf.held && $(\"wf-follow\").checked" in tick
    assert "!wfReducedMotion.matches && span >= WF_FOLLOW_MIN_SPAN" in tick
    assert "wfGlide(t - span / 2)" in tick
    # Reduced motion (or too close to scroll): a page at a time, from the playhead.
    assert "wfSetView(t, t + span)" in tick
    # Off, or paused by your own move: the page turn as before.
    assert "wfSetView(b, b + span);  // the playhead ran off the right edge" in tick
    assert 'matchMedia("(prefers-reduced-motion: reduce)")' in _js()


def test_a_play_picks_following_up_again_but_an_audition_does_not():
    tick = _function("wfTick")
    assert "if (playing && !wf.wasPlaying && wf.auditionEnd == null) wf.held = false;" in tick
    hold = _function("wfHold")
    assert "wf.held = true" in hold


@pytest.mark.parametrize("where, marker", [
    ("wheel pan", "wfHold();\n    wfSetView(wf.view[0] + dt, wf.view[1] + dt);"),
    ("pointer pan", 'wfHold();\n    $("wf-stage").classList.add("wf-panning");'),
    ("handle drag", "handle.focus();\n    wfHold();"),
    ("drag-select", "d.moved = true;\n      wfHold();"),
    ("fit selection", "wfHold(); wfFollowSelection();"),
])
def test_your_own_move_on_the_strip_pauses_following(where, marker):
    assert marker in _js(), where


@pytest.mark.parametrize("name", ["wfHandleKey", "wfJumpTo", "wfFocusCue"])
def test_keyboard_moves_on_the_strip_pause_following(name):
    assert "wfHold();" in _function(name)


def test_a_zoom_does_not_pause_following():
    # + / - zoom around the playhead when it's in view, and it is: in the middle.
    assert "wfHold" not in _function("wfZoom")
    assert "wfHold" not in _function("wfSetView")


def test_the_glide_keeps_the_fetch_ahead_instead_of_cancelling_it():
    glide = _function("wfGlide")
    # It moves the view itself: wfSetView would cancel the fetch in flight every frame.
    assert "wfSetView" not in glide and "wfCancel" not in glide
    assert "wf.view = [a, a + span]" in glide and "wfFollowFetch()" in glide


def test_the_fetch_ahead_reaches_a_few_views_and_waits_for_a_quick_run():
    fetch = _function("wfFollowFetch")
    # Not while a quick run of zooms settles, nor again after a failure.
    assert "wf.loadTimer || wf.failed" in fetch
    # Nothing new while the sound on hand, or on its way, reaches far enough ahead.
    assert "wfSharp(d) && has(d.start, d.end)" in fetch
    assert "wf.lead && has(wf.lead[0], wf.lead[1])" in fetch
    assert "wfLoad({ start: a, end, bins:" in fetch and "WF_MAX_BINS" in fetch
    lead = _function("wfLeadSpans")
    assert "Math.max(2, Math.min(4, n))" in lead
    assert "WF_KEYFRAME_SPAN / span" in lead  # on video, keyframes still come with it
    load = _function("wfLoad")
    assert "const [a, b] = lead ? [lead.start, lead.end] : wf.view;" in load
    assert "if (lead) wf.lead = [a, b];" in load
    assert "wf.lead = null" in _function("wfCancel")


def test_the_quick_run_debounce_hands_over_to_the_fetch_ahead():
    soon = _function("wfLoadSoon")
    assert "wf.loadTimer = 0;" in soon
    assert "if (wf.gliding) wfFollowFetch(); else wfLoad();" in soon
    assert "wfLoadSoon(120)" in _function("wfSetView")
    assert "wf.loadTimer = 0;" in _function("wfHide")


def test_a_scrolling_view_draws_a_bin_per_css_pixel_and_reloads_once_still():
    sharp = _function("wfSharp")
    assert "if (wf.gliding) bin = Math.max(bin, (b - a) / wfWidth());" in sharp
    tick = _function("wfTick")
    assert "if (!wfSource().full) wfLoadSoon(120);" in tick
