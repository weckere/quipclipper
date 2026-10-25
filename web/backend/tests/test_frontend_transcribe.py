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
    assert 'sttSay("")' in _function("renderSttBar")


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
let avail = true;
const sttAvailable = () => avail, sttModels = () => ["small.en"], sttDefaultModel = () => "small.en";
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
eval(["sttHere", "sttUnfollow", "sttSay", "sttStatus", "renderSttBar", "followSttJob", "sttAction"].map(grab).join("\n"));
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
  // FE-1: the slim row ellipsizes it, so its hover text and the open body have it whole.
  out.errorReadable = $("stt-job-text").title === $("stt-job-text").textContent
    && $("stt-msg").textContent === $("stt-job-text").textContent && !$("stt-msg").hidden;
  renderSttBar(T, "b", true);
  out.clearedForAnother = $("stt-job").hidden && $("stt-job-text").textContent === ""
    && $("stt-msg").hidden && $("stt-msg").textContent === "";
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
  // A job gone (404) takes its progress away, in the body too.
  currentItem = { path: "y" };
  timers.length = 0;
  jobAnswer = Object.assign(new Error("gone"), { status: 404 });
  followSttJob("jobY", "y");
  out.progressShown = !$("stt-msg").hidden && $("stt-msg").textContent.startsWith("Transcribing");
  while (timers.length) await timers.shift()();
  out.goneCleared = $("stt-job").hidden && $("stt-msg").hidden;
  // FE-8a: a click opens the bar, and a server without whisper offers nothing to transcribe.
  out.tipSaysOpen = $("stt-tip-do").textContent.startsWith("Click to open: transcribe");
  avail = false;
  renderSttBar(T, "y", true);
  out.tipWithoutWhisper = $("stt-tip-do").textContent.startsWith("Click to open")
    && !$("stt-tip-do").textContent.includes("transcribe");
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
    assert result == {k: True for k in result} and len(result) == 11, result


def _html() -> str:
    return (FRONTEND / "index.html").read_text()


def test_the_transcript_bar_is_one_slim_row_until_opened():
    # wiz: the bar took three script lines' worth of space for something rarely needed.
    html = _html()
    bar = html[html.index('id="stt-bar"'):html.index("<!-- The dialogue cues. -->")]
    head = bar[bar.index('class="stt-head"'):bar.index('id="stt-body"')]
    # The slim row: a disclosure button, the state in a few words, and a job's progress or
    # the last message with Cancel. Everything else is in the body, closed by default.
    assert re.search(r'<button[^>]*id="stt-toggle"[^>]*aria-expanded="false"[^>]*aria-controls="stt-body"', head)
    assert 'id="stt-summary"' in head and 'id="stt-job"' in head and 'id="stt-cancel"' in head
    assert re.search(r'id="stt-body"[^>]*hidden', bar)
    body = bar[bar.index('id="stt-body"'):]
    for control in ("stt-gaps", "stt-file", "stt-model", "stt-copy", "stt-uncopy", "stt-export",
                    "stt-clear", "stt-readonly", "stt-detail"):
        assert f'id="{control}"' in body, control
    # Hover or keyboard focus explains it (a tooltip, not a title), and the button's name
    # starts with the visible "auto" (SC 2.5.3).
    assert re.search(r'id="stt-tip" class="tip tip-below" role="tooltip">Lines whisper heard', head)
    assert 'aria-describedby="stt-tip"' in head
    assert re.search(r'id="stt-toggle"[^>]*><span class="auto-chip">auto</span> <span class="stt-title">Machine transcript', head)
    css = (FRONTEND / "styles.css").read_text()
    assert re.search(r"\.stt-head \{[^}]*flex-wrap: nowrap", css)
    assert re.search(r"\.stt-summary, \.stt-job-text \{[^}]*text-overflow: ellipsis", css)
    # Never squeezed over the script: the pane is a flex column (wiz's Bambi screenshot).
    assert re.search(r"\.ws-side \.stt-bar \{[^}]*flex-shrink: 0", css)


def test_the_transcript_bar_opens_closes_and_is_remembered():
    js = _js()
    set_open = _function("sttSetOpen")
    assert '$("stt-body").hidden = !open' in set_open
    assert 'setAttribute("aria-expanded", String(open))' in set_open
    assert "writePref(STT_OPEN_KEY" in set_open  # localStorage, inside writePref's try/catch
    assert 'sttSetOpen(readPref(STT_OPEN_KEY) === "1", false)' in js
    assert '$("stt-toggle").onclick = () => sttSetOpen($("stt-body").hidden)' in js
    esc = js[js.index('$("stt-bar").addEventListener("keydown"'):]
    esc = esc[:esc.index("});")]
    assert '"Escape"' in esc and "sttSetOpen(false)" in esc and '$("stt-toggle").focus()' in esc
    # A slim row still says what happened: the brief state and the status line are drawn
    # whether or not the bar is open, and messages go to the live region too.
    render = _function("renderSttBar")
    assert '$("stt-summary").textContent = brief.join(" · ")' in render
    assert '$("stt-detail").textContent = parts.join(" · ")' in render
    assert "announce(msg)" in _function("sttAction")
    assert "announce(`Couldn't start the transcription" in _function("startTranscribe")


def _rule(css: str, selector: str) -> str:
    m = re.search(rf"(?:^|\n){re.escape(selector)} \{{([^}}]*)\}}", css)
    assert m, f"no rule {selector}"
    return m.group(1)


def test_cancel_and_the_toggle_are_never_cut_off_in_the_slim_row():
    # FE-1: Cancel sat inside the ellipsized span and was clipped off the row in every job
    # state (still Tab-focusable), its focus ring with it, and long messages were unreadable.
    html = _html()
    head = html[html.index('class="stt-head"'):html.index('id="stt-body"')]
    job = re.search(r'<span id="stt-job" class="stt-job" hidden>(.*?)</button></span>', head, re.S)
    assert job, "Cancel lives in #stt-job"
    text = re.search(r'<span id="stt-job-text" class="stt-job-text" role="status"></span>', job.group(1))
    assert text and 'id="stt-cancel"' in job.group(1)[text.end():], "Cancel sits beside the text, not in it"
    css = (FRONTEND / "styles.css").read_text()
    job_rule = _rule(css, ".stt-job")
    assert "display: flex" in job_rule and "min-width: 0" in job_rule
    assert "overflow" not in job_rule, "nothing around Cancel clips it or its focus ring"
    assert "overflow: hidden" not in _rule(css, ".stt-head") and "overflow" not in _rule(css, ".stt-bar")
    assert "flex-shrink: 0" in _rule(css, ".stt-job .reindex-link")
    assert "text-overflow: ellipsis" in _rule(css, ".stt-summary, .stt-job-text")
    # The whole message: the open body (not a second live region), and the hover text.
    body = html[html.index('id="stt-body"'):html.index("<!-- The dialogue cues. -->")]
    msg = re.search(r'<p id="stt-msg" class="stt-msg"[^>]*>', body)
    assert msg and "role=" not in msg.group(0) and "aria-live" not in msg.group(0)
    say = _function("sttSay")
    assert "text.title = msg" in say and "full.textContent = msg" in say and "full.hidden = !msg" in say
    # Every write to the status line goes through it.
    js = _js()
    assert js.count('$("stt-job-text")') == 1 and "text.textContent" not in _function("followSttJob")
    # FE-2: the flex item in the row is the toggle's wrapper; it doesn't shrink under the text.
    assert "flex-shrink: 0" in _rule(css, ".stt-head > .tip-wrap")


def test_the_toggle_tooltip_says_a_click_opens_the_bar():
    # FE-8a: a click opens the panel; it doesn't transcribe anything.
    html = _html()
    tip = html[html.index('id="stt-tip"'):]
    tip = tip[:tip.index("</span></span>")]
    assert "Click to transcribe" not in tip
    assert '<span id="stt-tip-do">Click to open: ' in tip
    assert '$("stt-tip-do").textContent = avail ?' in _function("renderSttBar")


def test_auto_chips_on_buttons_start_their_accessible_name():
    # FE-8b (SC 2.5.3 label in name): a visible "auto" on a button is part of its name, like the
    # toggle's; aria-hidden took it out of the name while it still showed.
    html = _html()
    buttons = re.findall(r"<button[^>]*>.*?</button>", html, re.S)
    with_chip = [b for b in buttons if "auto-chip" in b]
    assert len(with_chip) >= 4
    for b in with_chip:
        assert re.search(r'>\s*<span class="auto-chip">auto</span> [<\w]', b), b
        assert 'class="auto-chip" aria-hidden' not in b


ORPHANS_HARNESS = r"""
const src = require("fs").readFileSync(process.argv[2], "utf8");
const grab = (re) => { const m = src.match(re); if (!m) throw new Error("no " + re); return m[0]; };
const els = {};
const $ = (id) => els[id] || (els[id] = { id, hidden: true, disabled: false, textContent: "" });
const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;
const appConfig = { transcribe: {} };
const announced = [];
let count = 3, gets = 0, cleanAnswer = null;
function announce(m) { announced.push(m); }
function confirm() { return true; }
async function getJSON(url) { gets++; return { count }; }
async function postJSON() { const a = await cleanAnswer; if (a instanceof Error) throw a; return a; }
let sttOrphansCheckId = 0;
eval(grab(/^async function refreshSttOrphans\(.*?^}/ms));
eval(grab(/^\$\("stt-orphans-btn"\)\.onclick = async \(\) => \{.*?^};/ms));
const settle = () => new Promise((r) => setTimeout(r, 0));
const out = {};
(async () => {
  await refreshSttOrphans();
  const line = $("stt-orphans"), btn = $("stt-orphans-btn");
  out.countShown = !line.hidden && !btn.hidden && /^3 stored machine transcripts aren't needed/.test(line.textContent);
  out.countNotAnnounced = announced.length === 0;
  // Success: the result, said once, then the count again (none left: the button goes).
  cleanAnswer = { count: 3 }; count = 0; announced.length = 0;
  await btn.onclick(); await settle();
  out.successRefreshed = line.textContent === "Deleted 3 orphaned transcripts." && !line.hidden && btn.hidden;
  out.saidOnce = announced.length === 1 && announced[0] === "Deleted 3 orphaned transcripts.";
  // A failure half-way: the error, and the count of what's left, not the stale one.
  await (count = 3, refreshSttOrphans());
  cleanAnswer = Promise.resolve(new Error("500")); count = 1; announced.length = 0;
  await btn.onclick(); await settle();
  out.failureRefreshed = line.textContent.startsWith("Couldn't delete them: 500 1 stored machine transcript isn't needed")
    && !btn.hidden && !btn.disabled && announced.length === 1;
  // Navigation while it runs: the answer isn't shown, said or counted again.
  let release; cleanAnswer = new Promise((r) => { release = r; }); announced.length = 0;
  const run = btn.onclick();
  sttOrphansCheckId++; line.hidden = btn.hidden = true;  // libIndexReset
  const before = gets;
  release({ count: 1 }); await run; await settle();
  out.navigationIgnored = line.hidden && announced.length === 0 && gets === before && !btn.disabled;
  console.log(JSON.stringify(out));
})();
"""


@pytest.mark.skipif(NODE is None, reason="needs node")
def test_removing_orphaned_transcripts_in_node(tmp_path):
    # FE-4, FE-5, FE-8c.
    script = tmp_path / "orphans.js"
    script.write_text(ORPHANS_HARNESS)
    run = subprocess.run([NODE, str(script), str(FRONTEND / "app.js")], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout)
    assert result == {k: True for k in result} and len(result) == 6, result


def test_the_orphan_line_is_true_for_every_reason_and_said_once():
    # FE-4: the count also holds unreadable files and a save's leftovers, not only files gone.
    refresh, js = _function("refreshSttOrphans"), _js()
    assert "no longer in the library" not in refresh and "unreadable or unfinished" in refresh
    click = js[js.index('$("stt-orphans-btn").onclick'):]
    click = click[:click.index("\n};") + 3]
    assert "gone from the library?" not in click and "unreadable or unfinished)?" in click
    html = _html()
    btn = re.search(r'<button[^>]*id="stt-orphans-btn"[^>]*>', html).group(0)
    assert "can't be read" in btn
    # FE-5: the count comes back after a delete, failed or not, unless the view changed.
    fin = click[click.index("} finally {"):]
    assert "refreshSttOrphans(note)" in fin and "myId === sttOrphansCheckId" in fin
    # FE-8c: not a live region (the count isn't read on every visit); the result is announced once.
    line = re.search(r'<p id="stt-orphans"[^>]*>', html).group(0)
    assert "role=" not in line and "aria-live" not in line
    assert click.count("announce(") == 1
