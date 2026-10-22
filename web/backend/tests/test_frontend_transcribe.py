"""Machine transcript lines in the web UI (M21, D-037).

Machine lines are marked wherever they show: their own colour (``--machine``), the "auto"
chip, hover text and an accessible name that says so. Transcription starts only from the
three controls that ask for it, and the captions get machine lines only from the library.

There's no JS runtime in the tests, so these read ``app.js``, ``index.html`` and the CSS.
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


def test_only_the_three_controls_start_a_transcription():
    js = _js()
    # One place posts to the endpoint, and only the waveform button and the bar's two
    # buttons call it: no timer, no item open, no index run starts one (D-037).
    assert js.count('"/api/items/transcribe"') == 1
    assert "/api/items/transcribe" in _function("startTranscribe")
    callers = re.findall(r"^.*startTranscribe\(\{", js, re.M)
    assert len(callers) == 3, callers
    assert all(re.match(r'\s*(startTranscribe|\$\("stt-(gaps|file)"\)\.onclick)', c) for c in callers)
    assert "startTranscribe" not in _function("openItem")
    assert "startTranscribe" not in _function("renderStreamSelector")


def test_machine_lines_are_marked_in_the_script():
    load = _function("loadScript")
    assert 'cue.source === "machine"' in load
    assert "script-machine" in load and "AUTO_CHIP" in load
    assert '"machine transcript: "' in load  # the accessible name says so
    assert "MACHINE_TITLE" in load


def test_machine_hits_are_marked_in_both_searches_and_the_preview():
    js = _js()
    assert "result-machine" in _function("doSearch") and "m.machine" in _function("doSearch")
    assert "hit-machine" in js and "hit.machine" in js
    assert '" machine"' in _function("focusHit")
    assert js.count('<span class="visually-hidden">Machine transcript: </span>') == 3


def test_the_waveform_cue_lane_draws_machine_lines_in_their_colour():
    draw = _function("wfDraw")
    assert 'cue.source === "machine"' in draw and "c.machine" in draw
    assert 'machine: v("--machine")' in _function("wfColors")


def test_machine_text_has_its_own_colour_and_a_non_colour_cue():
    css = (FRONTEND / "styles.css").read_text()
    for sel in (".script-line.script-machine .script-text", ".search-result.result-machine .result-text",
                ".dialogue-hit.hit-machine .hit-text"):
        assert sel in css
    assert re.search(r"\.preview-line\.machine[^{]*\{[^}]*var\(--machine\)[^}]*italic", css)
    assert re.search(r"\.auto-chip\s*\{[^}]*var\(--machine\)", css, re.S)
    assert "#player::cue(.machine)" in css
    js = _js()
    assert 'class="auto-chip" aria-hidden="true"' in js  # the chip is seen; the name is read


def test_the_transcript_bar_offers_the_d037_actions():
    html = (FRONTEND / "index.html").read_text()
    for el, action in (("wf-transcribe", "item.transcribe"), ("stt-gaps", "item.transcribe"),
                       ("stt-file", "item.transcribe"), ("stt-copy", "item.transcript-copy"),
                       ("stt-uncopy", "item.transcript-uncopy"), ("stt-export", "item.transcript"),
                       ("stt-clear", "item.transcript-clear"), ("stt-cancel", "job.cancel"),
                       ("stt-default-model", "settings.set")):
        assert re.search(rf'id="{el}"[^>]*data-action="{re.escape(action)}"', html), el
    bar = _function("renderSttBar")
    assert "t.writable" in bar  # Copy to library is off for a read-only folder
    assert "sttAvailable()" in bar


def test_a_file_with_only_machine_lines_gets_a_script():
    sel = _function("renderStreamSelector")
    assert "sttHasLines(info.transcript)" in sel
    assert "renderSttBar(info.transcript, path, hasSubs)" in sel


def test_finished_jobs_reload_the_script_and_polling_stops():
    follow = _function("followSttJob")
    assert "jobActive(job)" in follow
    assert "sttJobs.delete(path)" in follow and "sttFollowing = null" in follow
    assert "refreshStt(path, { script: true })" in follow
    # A line selection survives the reload by its times, not its (shifting) cue indexes.
    assert "pendingCueSelect = { start: clipStart(), end: clipEnd() }" in _function("refreshStt")
