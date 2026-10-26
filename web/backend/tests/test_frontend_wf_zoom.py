"""The waveform waits in grey while a zoom loads (M20) instead of drawing blocks.

After a zoom the strip used to draw the previous, wider response (or the whole-file
overview) stretched to the new view, each bin several pixels wide. Now only a response at
least as sharp as the view asks for is drawn, the strip is ``aria-busy`` (and dimmed by
CSS) until the view's own peaks arrive, and a response for a view already left is dropped.
There's no JS runtime in the tests, so these read ``app.js``, ``index.html`` and
``styles.css`` for the pieces that make it work.
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


def test_only_a_response_as_sharp_as_the_view_is_drawn():
    sharp = _function("wfSharp")
    # Raw samples are always sharp; peaks only when their bins are no wider than the ones
    # wfLoad would ask for at this zoom (or they are this view's own response).
    assert 'd.kind === "samples"' in sharp
    assert "wfBins(b - a)" in sharp and "d.values.length >> 1" in sharp
    src = _function("wfSource")
    assert "filter(wfSharp)" in src
    # The old fallback drew the whole-file overview whatever the zoom: that is the blocks.
    assert "return wf.base || d" not in src
    # wfLoad asks for the bins wfSharp compares against.
    assert "const bins = lead ? lead.bins : wfBins(b - a)" in _function("wfLoad")


def test_the_strip_is_busy_until_the_views_peaks_cover_it():
    draw = _function("wfDraw")
    assert "const { src, full } = wfSource()" in draw
    assert "wfBusy(!full && !wf.failed)" in draw
    busy = _function("wfBusy")
    assert '$("wf-stage")' in busy and '"aria-busy", "true"' in busy
    assert 'removeAttribute("aria-busy")' in busy


def test_a_failed_load_stops_looking_busy():
    failed = _function("wfFailed")
    assert "wf.failed = true" in failed and "wfDraw()" in failed
    load = _function("wfLoad")
    assert 'wfFailed("Couldn\'t load the waveform.")' in load
    assert "wfFailed(`No waveform:" in load
    # A failed overview build is a failure too; a running one keeps the strip busy.
    assert "wf.failed = failed" in load


def test_only_the_latest_view_is_drawn_and_older_requests_are_cancelled():
    cancel = _function("wfCancel")
    assert "wf.req++" in cancel and "wf.abort.abort()" in cancel
    # A new view drops what is on its way for the old one before its own load starts.
    assert "wfCancel()" in _function("wfSetView")
    assert "wfCancel()" in _function("wfHide")
    load = _function("wfLoad")
    assert "new AbortController()" in load and "signal: ctl.signal" in load
    assert "if (req !== wf.req || !wf.path) return;" in load


def test_the_veil_dims_only_the_wave_band_and_respects_reduced_motion():
    html = (FRONTEND / "index.html").read_text()
    stage = re.search(r'<div id="wf-stage".*?\n              </div>', html, re.S).group(0)
    # Under the playhead and handles (later in the stage), so they stay on top and usable.
    assert stage.index('class="wf-veil" aria-hidden="true"') < stage.index('id="wf-head"')
    assert stage.index('class="wf-veil"') < stage.index('id="wf-in"')
    css = (FRONTEND / "styles.css").read_text()
    veil = re.search(r"^\.wf-veil \{(.*?)^\}", css, re.M | re.S).group(1)
    assert "pointer-events: none" in veil and "opacity: 0" in veil
    assert re.search(r'\.wf-stage\[aria-busy="true"\] \.wf-veil \{ opacity: 0\.\d+; \}', css)
    assert re.search(r"prefers-reduced-motion: reduce\) \{ \.wf-veil \{ transition: none; \} \}", css)


def test_a_response_clamped_to_the_files_end_covers_the_view():
    # The server clamps `end` to the file's duration, which can fall short of the probed
    # one the view runs to: reaching the file's end is covering, or the veil stays on.
    src = _function("wfSource")
    assert "d.duration ? Math.min(b, d.duration) : b" in src and "d.end >= reach(d)" in src
    assert "Math.min(wf.dur, data.duration || wf.dur)" in _function("wfLoad")  # the base, too
