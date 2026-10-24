"""Clicking a subtitle line's label on the waveform selects that line (M20).

There's no browser in the tests, so these read ``app.js``, ``index.html`` and
``styles.css`` for the pieces that make it work, and run the hit test and the selection
logic under node: a click (not a drag) on a label selects its line as the script does,
Shift-click widens the selection, and the lane is one tab stop for the keyboard.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
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


def _listener(target: str, event: str) -> str:
    m = re.search(rf'^\$\("{target}"\)\.addEventListener\("{event}", \(e?\) => \{{.*?^\}}\);',
                  _js(), re.M | re.S)
    assert m, f"no {target} {event} listener"
    return m.group(0)


def test_only_a_click_without_movement_selects_the_line():
    down = _listener("wf-stage", "pointerdown")
    assert "cue: wfCueAt(wfPointerX(e), wfPointerY(e)), shift: e.shiftKey" in down
    end = _function("wfPointerEnd")
    # A drag that starts on a label is still a drag-select: only the no-movement branch picks the line.
    click = end[end.index("!d.moved && !d.held && !d.middle"):]
    assert "if (d.cue >= 0) wfSelectCue(d.cue, d.shift)" in click and "else seekTo(" in click
    assert end.index('d.kind === "select" && d.moved') < end.index("wfSelectCue(")


def test_the_selection_is_a_line_selection_and_the_view_stays():
    body = _function("wfSelectCue")
    assert "updateClipUI()" in body and "announceSelection()" in body
    assert "wf.fromHandle = true" in body and "wf.fromHandle = false" in body
    assert "scriptTabStop(i, false)" in body and "scriptCenter(i)" in body
    # A pending snap of an older selection mustn't land on this one.
    assert "wf.snapSeq++" in body


def test_labels_look_clickable_and_drags_drop_the_hover():
    css = (FRONTEND / "styles.css").read_text()
    assert ".wf-stage.wf-on-cue { cursor: pointer; }" in css
    # The pan's grabbing cursor wins over the label's pointer.
    assert css.index(".wf-stage.wf-on-cue") < css.index(".wf-stage.wf-panning")
    assert '"wf-on-cue", i >= 0' in _function("wfHover")
    move = _listener("wf-stage", "pointermove")
    assert "wfHover(free ? wfCueAt(" in move and move.count("if (wf.hover >= 0) wfHover(-1)") == 2
    draw = _function("wfDraw")
    assert "i === wf.hover" in draw and 'lane.matches(":focus-visible")' in draw


def test_the_lane_is_one_named_tab_stop():
    html = (FRONTEND / "index.html").read_text()
    lane = re.search(r'<button type="button" id="wf-cues"[^>]*>', html).group(0)
    assert 'aria-describedby="wf-cues-help"' in lane and "title=" in lane
    assert 'id="wf-cues-help" class="visually-hidden"' in html
    # One element, not one per label: the canvas draws them.
    assert html.count('id="wf-cues"') == 1 and "tabindex" not in lane
    # It sits under the handles and the playhead, which stay grabbable over a label.
    assert html.index('id="wf-cues"') < html.index('id="wf-head"') < html.index('id="wf-in"')
    assert "`Select line: ${wfCueText(cue)}" in _function("wfCueName")
    keys = _listener("wf-cues", "keydown")
    for k in ("ArrowRight", "ArrowLeft", "Home", "End", '"Enter"', '" "', "setClipInAt", "setClipOutAt"):
        assert k in keys
    assert "wfSelectCue(wf.cue, e.shiftKey)" in keys
    # A mouse press stays the strip's; a screen reader's click (detail 0) activates.
    assert 'addEventListener("mousedown", (e) => e.preventDefault())' in _js()
    assert "if (!e.detail && wf.cue >= 0)" in _js()


def test_moving_along_the_lane_is_announced():
    assert "if (say) announce(" in _function("wfFocusCue")
    assert "wfFocusCue(to, true)" in _listener("wf-cues", "keydown")


def test_the_manual_and_changelog_say_so():
    root = FRONTEND.parents[1]
    manual = (root / "docs" / "MANUAL.md").read_text()
    assert "Click a line's label" in manual
    log = (root / "CHANGELOG.md").read_text()
    m20 = log[log.index("### UI follow-ups (M20)"):]
    assert "label" in m20[:m20.index("\n### ")]


NODE = shutil.which("node")
HARNESS = r"""
const src = require("fs").readFileSync(process.argv[2], "utf8");
const grab = (name) => src.match(new RegExp(`^function ${name}\\(.*?^}|^const ${name} = [^\\n]*`, "ms"))[0];
const WF_CUE_LANE = 18;
const scriptCues = [
  { start: 0, end: 0, text: "", synthetic: true },
  { start: 10, end: 12, text: "Winter sure is long, isn't it?" },
  { start: 13, end: 15, text: "It seems long" },
  { start: 14.5, end: 16, text: "Overlapping", source: "machine" },
  { start: 20, end: 22, text: "But it won't last forever" },
];
const wf = { view: [0, 100], cue: -1, snapSeq: 0, fromHandle: false };
const wfT = (x) => x / 10;  // 1000 px across 100 s
let clipFirst = -1, clipLast = -1, free = null, said = [];
const pb = { pause() {} };
const hasClipRange = () => clipFirst >= 0 && clipLast >= clipFirst;
const wfAnchor = () => (hasClipRange() ? [scriptCues[clipFirst].start, scriptCues[clipLast].end] : free);
const wfSel = wfAnchor;
const showFreeRange = (a, b) => { clipFirst = clipLast = -1; free = [a, b]; };
const round6 = (v) => v;
const updateClipUI = () => { if (!wf.fromHandle) throw new Error("the view would jump"); free = null; };
const scriptTabStop = () => {}, scriptCenter = () => {}, wfCueName = () => {};
const announce = (m) => said.push(m), announceSelection = () => said.push("lines");
const formatTimePrecise = (s) => String(s);
eval(["wfCueAt", "wfSelectCue", "wfCueStep"].map(grab).join("\n"));
const out = {};
out.hits = [[105, 5], [140, 5], [155, 5], [170, 5], [105, 30], [0, 5]].map(([x, y]) => wfCueAt(x, y));
wfSelectCue(2, false); out.one = [clipFirst, clipLast];
wfSelectCue(4, true); out.extended = [clipFirst, clipLast];
wfSelectCue(1, true); out.back = [clipFirst, clipLast];
wfSelectCue(2, false); out.replaced = [clipFirst, clipLast];
clipFirst = clipLast = -1; free = [11.5, 12.5];          // a dragged range
wfSelectCue(2, true); out.free = free; out.freeLines = clipFirst;
wfSelectCue(0, false); out.synthetic = [clipFirst, wf.cue];
out.steps = [wfCueStep(-1, 1), wfCueStep(1, -1), wfCueStep(2, 1), wfCueStep(scriptCues.length, -1)];
out.said = said;
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(NODE is None, reason="needs node")
def test_label_clicks_in_node(tmp_path):
    script = tmp_path / "labels.js"
    script.write_text(HARNESS)
    run = subprocess.run([NODE, str(script), str(FRONTEND / "app.js")], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout)
    # On a label, the line under it (the later of two overlapping lines); in the wave, none.
    assert out["hits"] == [1, 2, 3, -1, -1, -1]
    assert out["one"] == [2, 2]
    assert out["extended"] == [2, 4] and out["back"] == [1, 4]
    assert out["replaced"] == [2, 2]
    # Shift-click on a dragged range grows it to cover the line, still a free range.
    assert out["free"] == [11.5, 15] and out["freeLines"] == -1
    # The synthetic "start of video" line is never a label.
    assert out["synthetic"] == [-1, 2]
    assert out["steps"] == [1, 1, 3, 4]
    assert out["said"] == ["lines", "lines", "lines", "lines", "Selected 11.5 to 15"]
