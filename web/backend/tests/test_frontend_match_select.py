"""A click on a match in the workspace's Matches tab selects its line(s) for playback (M20).

The selection is a line selection, so the transport's ▶ and Loop play just those lines with
Before / After; the click also plays from the match, as it did. ``selectCueRange`` takes the
cues that start and end exactly where the match does (a two-cue match selects both), and
falls back to the nearest ones. It runs under node here.
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


def test_a_match_click_selects_then_plays_and_says_so():
    body = _function("doSearch")
    click = body[body.index("li.onclick = () => {"):]
    click = click[:click.index("};")]
    # Select first, so the seek lands inside the new selection, which then holds playback.
    assert click.index("selectCueRange(m.start, m.end, { say: true })") < click.index("seekTo(")
    assert "if (say) announceSelection()" in _function("selectCueRange")
    assert 'makeActivatable(li, "Select this line and play it' in body


def test_a_match_clicked_before_the_file_is_ready_still_selects_and_plays():
    # Bambi (1080p, DTS over HLS): the match showed 89 ms after opening, with the script
    # still loading, and the click selected nothing. The selection now waits for the script...
    body = _function("selectCueRange")
    assert "if (currentItem) pendingCueSelect = { start, end, say }" in body
    assert "selectCueRange(start, end, { say })" in _js()
    # ...and a seek before the element has metadata is done again once it has.
    seek = _function("seekTo")
    assert "if (player.readyState < 1)" in seek and "if (currentItem !== item) return;" in seek


def test_a_library_hit_opens_its_file_with_the_lines_selected():
    js = _js()
    assert "markRange: { start: hit.start, end: hit.end }" in js
    assert "pendingCueSelect = markRange || null" in js
    assert "selectCueRange(start, end, { say })" in js


def test_the_manual_and_changelog_say_so():
    root = FRONTEND.parents[1]
    assert "click a match" in (root / "docs" / "MANUAL.md").read_text()
    log = (root / "CHANGELOG.md").read_text()
    m20 = log[log.index("### UI follow-ups (M20)"):]
    assert "Matches" in m20[:m20.index("\n### ")]


NODE = shutil.which("node")
HARNESS = r"""
const src = require("fs").readFileSync(process.argv[2], "utf8");
const grab = (name) => src.match(new RegExp(`^function ${name}\\(.*?^}`, "ms"))[0];
let scriptCues = [
  { start: 0, end: 0, synthetic: true },
  { start: 10, end: 12 },
  { start: 11.5, end: 13 },      // overlaps the line before it
  { start: 13.2, end: 15 },
  { start: 20, end: 22 },
];
let clipFirst = -1, clipLast = -1, pendingCueSelect = null, said = 0;
const currentItem = { path: "/m/film.mkv" };
const updateClipUI = () => {}, scriptTabStop = () => {}, scriptCenter = () => {};
const announceSelection = () => { said++; };
eval(["nearestCueIndex", "selectCueRange"].map(grab).join("\n"));
const out = [];
const loaded = scriptCues;
scriptCues = [];                                   // the script still loading
selectCueRange(11.5, 13, { say: true });
out.push([clipFirst, pendingCueSelect]);
scriptCues = loaded;
for (const [a, b] of [[11.5, 13], [11.5, 15], [10, 22], [13.21, 14.9]]) {
  selectCueRange(a, b);
  out.push([clipFirst, clipLast]);
}
out.push(said);
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(NODE is None, reason="needs node")
def test_select_cue_range_in_node(tmp_path):
    script = tmp_path / "range.js"
    script.write_text(HARNESS)
    run = subprocess.run([NODE, str(script), str(FRONTEND / "app.js")], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    # Exact edges pick the match's own cues (not the overlapping line that ends nearer);
    # a range off the edges falls back to the nearest ones.
    out = json.loads(run.stdout)
    # Before the script loads, the click waits as a pending selection (said once it lands).
    assert out[0] == [-1, {"start": 11.5, "end": 13, "say": True}]
    assert out[1:5] == [[2, 2], [2, 3], [1, 4], [3, 3]]
    assert out[5] == 0
