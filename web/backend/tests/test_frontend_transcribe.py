"""Machine transcript lines in the web UI (M21, D-037).

Machine lines are marked wherever they show: their own colour (``--machine``), the "auto"
chip, hover text and an accessible name that says so. Transcription starts only from the
three controls that ask for it, and the captions get machine lines only from the library.

There's no JS runtime in the tests, so these read ``app.js``, ``index.html`` and the CSS.
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
    # The accessible name says so, starting with the chip's visible "auto" (SC 2.5.3).
    assert '"auto, machine transcript: "' in load
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


def test_a_folder_transcription_starts_only_from_its_two_buttons_after_a_confirm():
    js = _js()
    assert js.count('"/api/search/folder/transcribe"') == 1
    start = _function("startFolderTranscribe")
    assert "/api/search/folder/transcribe" in start and "confirm(" in start
    assert re.findall(r"startFolderTranscribe\(", js) and len(re.findall(r"startFolderTranscribe\(\"", js)) == 2
    for fn in ("browse", "checkFolderIndex", "renderIndexStatus", "showSttFolder"):
        assert "startFolderTranscribe" not in _function(fn)


def test_the_library_marks_files_with_a_machine_transcript():
    js = _js()
    assert "e.has_transcript" in js
    assert '<span class="visually-hidden">machine transcript</span>' in js


def test_removing_the_last_machine_lines_clears_the_script_and_captions():
    refresh = _function("refreshStt")
    assert 'querySelectorAll("track").forEach((el) => el.remove())' in refresh
    assert "scriptCues = [];" in refresh
    assert '$("stt-job-text").textContent = ""' in _function("renderSttBar")


def test_a_read_only_folder_says_why_copy_is_off():
    html = (FRONTEND / "index.html").read_text()
    assert re.search(r'id="stt-readonly"[^>]*>This folder is read-only', html)
    assert '$("stt-readonly").hidden = copy.hidden || t.writable' in _function("renderSttBar")


def test_the_bars_status_stays_until_another_files_bar_is_drawn():
    bar = _function("renderSttBar")
    # The status line goes when the bar's file changes, not on every redraw: an action's
    # result (a read-only folder's 403 too) and a job's "done" survive the refresh after it.
    assert "const fresh = bar.dataset.path !== path" in bar and "bar.dataset.path = path" in bar
    assert bar.index("bar.dataset.path = path") < bar.index("if (bar.hidden) return")
    assert "else if (!running && fresh)" in bar
    assert "delete $(\"stt-bar\").dataset.path" in _function("openItem")
    action = _function("sttAction")
    assert "announce(msg)" in action and "sttStatus(msg)" in action
    assert "That didn't work" in action


def test_a_file_whose_transcription_still_runs_shows_it_again():
    bar = _function("renderSttBar")
    assert "if (running && (fresh || !sttFollowing || sttFollowing.jobId !== running)) followSttJob(running, path)" in bar
    follow = _function("followSttJob")
    # One poll per job: following again ends the older poll, even mid-request.
    assert "const follow = sttFollowing = { jobId, path }" in follow
    assert follow.count("if (!mine()) return") >= 3


def test_transcript_requests_never_land_in_another_file():
    action = _function("sttAction")
    # The answer, its message and the re-enabled button are the file's that asked.
    assert action.index("if (!sttHere(path)) return null") < action.index("btn.disabled = false;")
    assert "finally" not in action
    start = _function("startTranscribe")
    assert "if (sttHere(path)) followSttJob(r.job_id, path)" in start
    assert "sttJobs.set(path, r.job_id)" in start  # still followed when the file is back
    follow = _function("followSttJob")
    assert follow.index("if (here())") < follow.index("box.hidden = false")
    for name in ("stt-copy", "stt-uncopy", "stt-clear"):
        assert re.search(rf'\$\("{name}"\)\.onclick = async \(\) => \{{[^}}]*const path = await sttAction\(', _js()), name


def test_the_job_poll_stops_when_the_item_view_is_left_or_the_server_is_gone():
    assert "sttUnfollow()" in _function("stopPlayer")
    unfollow = _function("sttUnfollow")
    assert "clearTimeout(sttTimer)" in unfollow and "sttFollowing = null" in unfollow
    follow = _function("followSttJob")
    assert "++failures >= STT_POLL_TRIES" in follow and "failures = 0" in follow


def test_a_folder_transcription_started_from_a_folder_since_left_stays_out_of_the_new_one():
    start = _function("startFolderTranscribe")
    assert "const here = currentBrowsePath === folder" in start
    assert "if (here) followFolderTranscribe(r.job_id, folder)" in start
    assert "if (currentBrowsePath === folder) status.textContent" in start


def test_machine_cue_labels_sit_on_an_opaque_ground():
    draw = _function("wfDraw")
    # The selection's shade must not show through a machine line's tint (it took the text
    # under 4.5:1 in light): raised first, then the tint over it.
    lane = draw[draw.index('const machine = cue.source === "machine"'):]
    assert lane.index("ctx.fillStyle = c.raised") < lane.index("ctx.fillStyle = c.machineSoft")
    assert "machine ? c.machineSoft : c.raised" not in draw


def test_a_machine_hits_match_keeps_the_machine_colour():
    css = (FRONTEND / "styles.css").read_text()
    assert re.search(r"\.dialogue-hit\.hit-machine mark \{[^}]*color: var\(--machine\)", css)
    # The focused row doubles the shade, which takes --machine under 4.5:1: --text there.
    assert re.search(r"\.dialogue-hit\.hit-machine\.focus mark \{[^}]*color: var\(--text\)", css)


NODE = shutil.which("node")
STT_HARNESS = r"""
const src = require("fs").readFileSync(process.argv[2], "utf8");
const grab = (name) => {
  const m = src.match(new RegExp(`^(?:async )?function ${name}\\(.*?^}|^const ${name} = [^\n]*`, "ms"));
  if (!m) throw new Error("no " + name);
  return m[0];
};
const els = {};
const $ = (id) => els[id] || (els[id] = { id, hidden: false, disabled: false, textContent: "", dataset: {}, value: "" });
const sttAvailable = () => true, sttModels = () => ["small.en"], sttDefaultModel = () => "small.en";
const sttHasLines = (t) => !!t && ((t.stored && t.stored.lines > 0) || (t.library || []).length > 0);
const formatTimeWhole = String, plural = (n, w) => `${n} ${w}s`, qp = (p) => `?path=${p}`;
const sttJobs = new Map(), announced = [], timers = [];
let sttFollowing = null, sttTimer = null, currentItem = null, jobAnswer = null, apiAnswer = null;
const STT_POLL_TRIES = 6;
function fillModelSelect() {}
function announce(m) { announced.push(m); }
function setTimeout(fn) { timers.push(fn); return timers.length; }
function clearTimeout() {}
async function getJSON() { if (jobAnswer instanceof Error) throw jobAnswer; return jobAnswer; }
async function apiFetch() { return apiAnswer; }
const jobActive = (j) => j.status === "running" || j.status === "queued";
async function refreshStt() {}
eval(["sttHere", "sttUnfollow", "sttStatus", "renderSttBar", "followSttJob", "sttAction"].map(grab).join("\n"));
const T = { stored: { covered: 60, models: ["small.en"], lines: 12 }, library: [], writable: false };
const out = {};
(async () => {
  currentItem = { path: "a" };
  renderSttBar(T, "a", true);
  apiAnswer = { ok: false, status: 403, json: async () => ({ detail: "read-only" }) };
  await sttAction($("stt-copy"), "/x", "POST", () => "ok");
  renderSttBar(T, "a", true);  // the refresh after the action
  out.errorKept = !$("stt-job").hidden && $("stt-job-text").textContent.includes("read-only");
  out.errorAnnounced = announced.at(-1).includes("read-only");
  renderSttBar(T, "b", true);
  out.clearedForAnother = $("stt-job").hidden && $("stt-job-text").textContent === "";
  sttJobs.set("a", "jobA");
  renderSttBar(T, "a", true);
  currentItem = { path: "b" };
  renderSttBar(T, "b", true);
  currentItem = { path: "a" };
  renderSttBar(T, "a", true);
  out.backOnRunning = !$("stt-job").hidden && !$("stt-cancel").hidden && $("stt-gaps").disabled;
  apiAnswer = { ok: true, status: 200, json: async () => ({}) };
  $("stt-job-text").textContent = "mine";
  const late = sttAction($("stt-copy"), "/x", "POST", () => "done");
  currentItem = { path: "c" };
  $("stt-copy").disabled = true;
  out.staleIgnored = (await late) === null && $("stt-copy").disabled && $("stt-job-text").textContent === "mine";
  timers.length = 0;
  jobAnswer = Object.assign(new Error("down"), { status: 502 });
  followSttJob("jobZ", "z");
  let ticks = 0;
  while (timers.length && ticks < 50) { await timers.shift()(); ticks++; }
  out.pollGivesUp = ticks === STT_POLL_TRIES && sttFollowing === null;
  console.log(JSON.stringify(out));
})();
"""


@pytest.mark.skipif(NODE is None, reason="needs node")
def test_the_transcript_bar_in_node(tmp_path):
    script = tmp_path / "stt.js"
    script.write_text(STT_HARNESS)
    run = subprocess.run([NODE, str(script), str(FRONTEND / "app.js")], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout)
    assert result == {k: True for k in result} and len(result) == 6, result
