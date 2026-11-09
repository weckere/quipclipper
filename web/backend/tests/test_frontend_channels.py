"""The Channels picker picks single channels, shows for stereo, and the waveform follows it (M18).

There's no browser in the tests, so these read ``app.js`` for the pieces that make it work
and run the stream selector's picker under node with a stub DOM: stereo offers Both, Left,
Right; 5.1 offers the groups and then single channels; mono and iOS hide it; and the
waveform's peaks and snap requests carry the picked channel.
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


def test_the_picker_is_built_from_the_servers_channel_choices():
    body = _function("renderStreamSelector")
    assert "const choices = chanChoices(aud);" in body
    # Stereo (Both + two channels) and up; still not on iOS.
    assert '$("ss-chan-field").hidden = choices.length <= 1 || IS_IOS;' in body
    assert "o.value = c.value; o.textContent = c.label;" in body


def test_a_clip_of_one_channel_sends_the_pickers_value():
    js = _js()
    assert "channel_subset: splitCh ? chan : undefined," in js
    assert "chanCategory" not in js  # the old group -> category mapping is gone


def test_the_waveform_follows_the_picker():
    js = _js()
    load = _function("wfLoad")
    assert "const chan = getSelectedChan();" in load
    assert "chan !== wf.chan" in load and "base: null, data: null" in load
    assert "${wfChanParam()}" in load
    snap = _function("wfServerSnap")
    assert "&audio=${wf.audio}${wfChanParam()}" in snap
    assert '$("ss-chan").addEventListener("change", () => { wf.base = null; wfLoad(); });' in js
    assert "chan: getSelectedChan()" in _function("wfReset")


NODE = shutil.which("node")
HARNESS = r"""
const src = require("fs").readFileSync(process.argv[2], "utf8");
const grab = (name) => src.match(new RegExp(`^function ${name}\\(.*?^}|^const ${name} = [^;]*;`, "ms"))[0];
function el() {
  const e = { hidden: false, value: "", children: [], onchange: null };
  Object.defineProperty(e, "innerHTML", { set() { e.children = []; } });
  Object.defineProperty(e, "options", { get() { return e.children; } });
  e.appendChild = (c) => e.children.push(c);
  return e;
}
const els = {};
const $ = (id) => (els[id] = els[id] || el());
const document = { createElement: () => ({}) };
let IS_IOS = false;
const currentItem = null;
const isTextSubtitle = () => true;
const loadSubtitleTrack = () => {}, loadScript = () => {}, renderSttBar = () => {};
const sttHasLines = () => false;
const wf = { chan: "whole" };
eval(["chanChoices", "renderStreamSelector", "getSelectedChan",
      "wfChanParam"].map(grab).map((s) => s.replace(/^const /, "var ")).join("\n"));
const stereo = { kind: "audio", index: 0, codec: "aac", channel_layout: "stereo",
  channel_choices: [{ value: "whole", label: "Both" }, { value: "FL", label: "Left" },
                    { value: "FR", label: "Right" }] };
const surround = { kind: "audio", index: 1, codec: "ac3", channel_layout: "5.1",
  channel_choices: [{ value: "whole", label: "All channels" }, { value: "front", label: "Front (L/R)" },
                    { value: "center", label: "Center" }, { value: "FL", label: "Left" }] };
const mono = { kind: "audio", index: 0, codec: "aac", channel_layout: "mono", channel_choices: [] };
const pick = () => ({ hidden: $("ss-chan-field").hidden, value: getSelectedChan(),
  options: $("ss-chan").options.map((o) => `${o.value}=${o.textContent}`) });
const out = {};
renderStreamSelector({ streams: [stereo] }, "/m/a.mkv", () => {});
out.stereo = pick();
$("ss-chan").value = "FL";
out.left = getSelectedChan();
renderStreamSelector({ streams: [stereo, surround] }, "/m/a.mkv", () => {});
$("ss-audio").value = "1";
$("ss-audio").onchange();
out.surround = pick();
renderStreamSelector({ streams: [mono] }, "/m/a.mkv", () => {});
out.mono = pick();
IS_IOS = true;
renderStreamSelector({ streams: [stereo] }, "/m/a.mkv", () => {});
out.ios = pick();
out.params = ["whole", "FL", "back"].map((c) => { wf.chan = c; return wfChanParam(); });
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(NODE is None, reason="needs node")
def test_the_picker_in_node(tmp_path):
    script = tmp_path / "channels.js"
    script.write_text(HARNESS)
    run = subprocess.run([NODE, str(script), str(FRONTEND / "app.js")],
                         capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout)
    assert out["stereo"] == {"hidden": False, "value": "whole",
                             "options": ["whole=Both", "FL=Left", "FR=Right"]}
    assert out["left"] == "FL"
    # Another audio stream rebuilds the picker from its own choices, back on "whole".
    assert out["surround"]["hidden"] is False and out["surround"]["value"] == "whole"
    assert out["surround"]["options"] == ["whole=All channels", "front=Front (L/R)",
                                          "center=Center", "FL=Left"]
    # Hidden: "whole" for everything that asks.
    assert out["mono"] == {"hidden": True, "value": "whole", "options": []}
    assert out["ios"]["hidden"] is True and out["ios"]["value"] == "whole"
    assert out["params"] == ["", "&chan=FL", "&chan=back"]

