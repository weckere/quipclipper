"""Machine transcription (M21): ranges, whisper output, merging, the store, sidecars,
and a run against a fake whisper-cli."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from quipclipper import transcribe as tr
from quipclipper.idle import lower_priority
from quipclipper.models import Cue, Match
from quipclipper.search import search
from quipclipper.subtitles import find_sidecar, is_machine_sidecar, load_subtitles

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")


def real(*spans):
    return [Cue(i, s, e, t) for i, (s, e, t) in enumerate(spans)]


def machine(*spans):
    return [Cue(i, s, e, t, source=tr.MACHINE) for i, (s, e, t) in enumerate(spans)]


# --- ranges ---------------------------------------------------------------------------

def test_union_and_subtract():
    assert tr.union([(5, 6), (0, 2), (1, 3)]) == [(0, 3), (5, 6)]
    assert tr.union([(0, 1), (1.04, 2)], join=0.05) == [(0, 2)]
    assert tr.subtract([(0, 10)], [(2, 4), (6, 7)]) == [(0, 2), (4, 6), (7, 10)]
    assert tr.subtract([(0, 10)], [(0, 10)]) == []
    assert tr.subtract([(0, 10)], [(0.2, 10)]) == []  # a 0.2 s sliver isn't worth a run


def test_find_gaps_ignores_machine_lines_and_short_gaps():
    cues = real((5, 8, "a"), (9, 10, "b"), (20, 22, "c")) + machine((11, 19, "sung"))
    assert tr.find_gaps(cues, 30) == [(0, 5), (10, 20), (22, 30)]
    assert tr.find_gaps([], 12) == [(0, 12)]


def test_chunks_fold_a_short_tail():
    assert tr.chunks(0, 400, 180) == [(0, 180), (180, 400)]
    assert tr.chunks(0, 100, 180) == [(0, 100)]
    assert tr.chunks(0, 580, 180) == [(0, 180), (180, 360), (360, 580)]


def test_plan_ranges_skips_done_unless_redo():
    cues = real((10, 12, "a"))
    assert tr.plan_ranges("range", real=cues, duration=60, done=[(0, 5)], start=2, end=8) == [(5, 8)]
    assert tr.plan_ranges("range", real=cues, duration=60, done=[(0, 5)], start=2, end=8,
                          redo=True) == [(2, 8)]
    assert tr.plan_ranges("gaps", real=cues, duration=20, done=[]) == [(0, 10), (12, 20)]
    assert tr.plan_ranges("file", real=[], duration=20, done=[(0, 20)]) == []
    with pytest.raises(ValueError):
        tr.plan_ranges("range", real=[], duration=20, done=[], start=5, end=5)
    with pytest.raises(ValueError):
        tr.plan_ranges("everything", real=[], duration=20, done=[])


# --- whisper output -------------------------------------------------------------------

def _seg(a, b, text):
    return {"offsets": {"from": int(a * 1000), "to": int(b * 1000)}, "text": text}


def test_parse_whisper_json_offsets_and_filters():
    data = {"transcription": [
        _seg(0, 2, " (dramatic music)"),
        _seg(2, 4, " ♪ Driving way too fast ♪"),
        _seg(4, 5, " ♪ Driving way too fast ♪"),          # a repeat
        _seg(5, 6, " ♪ ♪"),                               # notes only
        _seg(6, 6.5, " ♪ Mmm ♪"),                        # a sound, not words
        _seg(6, 9, " Thanks for watching!"),              # stock outro
        _seg(9, 12, " - Cause the temperature's too high"),
        _seg(30, 31, " outside the kept range"),
    ]}
    cues = tr.parse_whisper_json(data, offset=100.0, keep=(100, 120))
    assert [(c.start, c.end, c.text) for c in cues] == [
        (102.0, 104.0, "♪ Driving way too fast ♪"),
        (109.0, 112.0, "Cause the temperature's too high"),
    ]
    assert all(c.machine and c.source == "machine" for c in cues)


def test_merge_keeps_only_what_subtitles_lack():
    subs = real((10, 13, "Why won't she be mine?"), (20, 23, "Must have been insane"))
    heard = machine(
        (10.2, 12.8, "Why won't she be mine"),         # under a subtitle: dropped
        (14, 17, "I'm a one-track lover"),             # in a gap: kept
        (19.5, 23.5, "Must have been insane"),         # mostly covered: dropped
        (24, 26, "Driving way too fast"),              # gap: kept
    )
    merged = tr.merge_cues(subs, heard)
    assert [c.text for c in merged] == ["Why won't she be mine?", "I'm a one-track lover",
                                        "Must have been insane", "Driving way too fast"]
    assert [c.index for c in merged] == [0, 1, 2, 3]
    assert [c.machine for c in merged] == [False, True, False, True]


def test_merge_drops_a_fuzzy_duplicate_beside_a_subtitle():
    subs = real((10, 12, "There's a bad atmosphere in here."))
    heard = machine((12.1, 14, "there's a bad atmosphere in here"))
    assert [c.text for c in tr.merge_cues(subs, heard)] == ["There's a bad atmosphere in here."]


def test_filter_machine_modes():
    cues = tr.merge_cues(real((0, 1, "a")), machine((5, 6, "b")))
    assert [c.text for c in tr.filter_machine(cues, "include")] == ["a", "b"]
    assert [c.text for c in tr.filter_machine(cues, "exclude")] == ["a"]
    assert [c.text for c in tr.filter_machine(cues, "only")] == ["b"]


def test_search_finds_machine_lines_and_a_real_line_wins_a_tie():
    cues = tr.merge_cues(real((0, 2, "way too fast")), machine((50, 52, "way too fast")))
    hits = search("way too fast", cues)
    assert [h.score for h in hits] == [100, 100]
    assert [h.machine for h in hits] == [False, True]
    assert isinstance(hits[1], Match) and hits[1].start == 50


# --- the store ------------------------------------------------------------------------

def test_store_add_merge_and_staleness(tmp_path):
    media = tmp_path / "ep.mkv"
    media.write_bytes(b"x" * 10)
    store = tr.TranscriptStore(tmp_path / "transcripts")
    assert store.load(media) is None
    store.add(media, (0, 10), machine((1, 2, "one"), (5, 6, "two")), model="small.en")
    t = store.add(media, (20, 30), machine((21, 22, "three")), model="medium.en")
    assert t.ranges == [(0, 10), (20, 30)]
    assert [c.text for c in t.cues] == ["one", "two", "three"]
    assert t.models == ["small.en", "medium.en"]
    # A re-run of 0-10 replaces the lines inside it.
    t = store.add(media, (0, 10), machine((1, 2, "won")), model="medium.en")
    assert [c.text for c in t.cues] == ["won", "three"]
    assert store.load(media).summary()["covered"] == 20.0
    # Replacing the media retires the transcript.
    media.write_bytes(b"y" * 11)
    assert store.load(media) is None
    assert store.clear(media) is True and store.clear(media) is False


# --- sidecars --------------------------------------------------------------------------

def test_machine_sidecar_name_and_recognition(tmp_path):
    media = tmp_path / "S01E06 The Creeping Moss.mkv"
    media.write_bytes(b"")
    assert tr.machine_sidecar_path(media).name == "S01E06 The Creeping Moss.en.whisper.srt"
    assert is_machine_sidecar("x.en.whisper.srt") and not is_machine_sidecar("x.en.srt")
    # The plain sidecar of a file whose stem ends in "Whisper" is not machine-made.
    assert not is_machine_sidecar("The.Whisper.srt", "The.Whisper")
    assert is_machine_sidecar("The.Whisper.en.whisper.srt", "The.Whisper")


def test_machine_sidecar_is_never_the_files_subtitles(tmp_path):
    media = tmp_path / "ep.mkv"
    media.write_bytes(b"")
    side = tr.write_sidecar(media, machine((1, 2.5, "Driving way too fast")))
    assert side.name == "ep.en.whisper.srt"
    assert find_sidecar(media) is None  # a machine sidecar alone is no subtitle file
    (tmp_path / "ep.en.srt").write_text("1\n00:00:05,000 --> 00:00:06,000\nhello\n")
    assert find_sidecar(media).name == "ep.en.srt"
    assert [c.text for c in load_subtitles(side)] == ["Driving way too fast"]
    got = tr.machine_cues(media, None)
    assert [(c.text, c.machine) for c in got] == [("Driving way too fast", True)]


def test_machine_cues_dedupes_store_and_library_copy(tmp_path):
    media = tmp_path / "ep.mkv"
    media.write_bytes(b"")
    store = tr.TranscriptStore(tmp_path / "t")
    store.add(media, (0, 10), machine((1, 2, "way too fast")), model="small.en")
    tr.write_sidecar(media, store.load(media).cues)
    assert [c.text for c in tr.machine_cues(media, store)] == ["way too fast"]
    assert tr.machine_cues(media, store, library=False)[0].machine
    assert tr.machine_cues(media, store, stored=False)[0].text == "way too fast"


def test_write_sidecar_refuses_an_empty_transcript(tmp_path):
    with pytest.raises(ValueError):
        tr.write_sidecar(tmp_path / "a.mkv", [])


def test_cues_to_srt():
    srt = tr.cues_to_srt(machine((61.5, 63.25, "hi")))
    assert srt == "1\n00:01:01,500 --> 00:01:03,250\nhi\n"


# --- configuration and argv -------------------------------------------------------------

def test_config_from_env_caps_threads_and_names_models():
    cfg = tr.WhisperConfig.from_env({
        "QC_WHISPER_MODELS": "small.en=/m/ggml-small.en.bin,medium.en=/m/ggml-medium.en.bin",
        "QC_WHISPER_MODEL": "medium.en", "QC_WHISPER_THREADS": "6"})
    assert cfg.threads == 3  # never more than MAX_THREADS
    assert cfg.default_model == "medium.en"
    assert cfg.model_path("small.en") == "/m/ggml-small.en.bin"
    with pytest.raises(ValueError):
        cfg.model_path("large-v3")
    one = tr.WhisperConfig.from_env({"QC_WHISPER_MODEL": "/m/ggml-small.en.bin"})
    assert one.models == {"small.en": "/m/ggml-small.en.bin"} and one.default_model == "small.en"
    assert tr.WhisperConfig.from_env({}).threads == 2


def test_whisper_argv_and_idle_wrapper():
    cfg = tr.WhisperConfig(binary="whisper-cli", models={"small.en": "/m.bin"}, threads=9,
                           vad_model="/vad.bin")
    argv = tr.whisper_argv(cfg, "/m.bin", "/a.wav", "/out", prompt="Dagless", vad=True)
    assert argv[argv.index("-t") + 1] == "3"
    assert ["-oj", "-of", "/out"] == argv[argv.index("-oj"):argv.index("-oj") + 3]
    assert "--vad" in argv and "--prompt" in argv
    assert tr.idle_argv(["x"]) == [sys.executable, "-m", "quipclipper.idle", "x"]


def test_extract_argv_is_an_anchored_16k_wav():
    argv = tr.extract_argv("/in.mkv", "/a.wav", start=353.0, duration=40, audio=1,
                           src_rate=48000)
    assert argv[argv.index("-ss") + 1] == "349.000000"  # the anchor, as the waveform seeks
    assert "atrim=start_sample=192000" in argv[argv.index("-af") + 1]
    assert argv[-6:] == ["-f", "wav", "-acodec", "pcm_s16le", "-y", "/a.wav"]
    assert "0:a:1" in argv and "-ar" not in argv
    assert argv[argv.index("-af") + 1].endswith(",aresample=16000")


@needs_ffmpeg
def test_extract_lands_on_the_second_asked_for(tmp_path):
    """An anchored extraction of AC3 in Matroska starts where it should. With -ar after the
    chain's aresample=first_pts=0 it started seconds late (M21, live on Darkplace S01E06)."""
    mkv = tmp_path / "burst.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                    "aevalsrc='if(between(t,52,53),0.5*sin(2*PI*440*t),0)':s=48000:d=70",
                    "-c:a", "ac3", "-y", str(mkv)], check=True)
    wav = tmp_path / "a.wav"
    subprocess.run(tr.extract_argv(mkv, wav, start=51.0, duration=6, src_rate=48000), check=True)
    out = subprocess.run(["ffmpeg", "-v", "info", "-i", str(wav), "-af",
                          "silencedetect=n=-40dB:d=0.3", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    import re as _re
    first = float(_re.search(r"silence_end: ([0-9.]+)", out).group(1))
    assert abs(first - 1.0) < 0.05, out


@pytest.mark.skipif(not hasattr(os, "SCHED_IDLE"), reason="Linux scheduling only")
def test_idle_launcher_sets_idle_scheduling():
    out = subprocess.run(
        [sys.executable, "-m", "quipclipper.idle", sys.executable, "-c",
         "import os; print(os.sched_getscheduler(0) == os.SCHED_IDLE, os.nice(0))"],
        capture_output=True, text=True, env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
        check=True)
    assert out.stdout.split() == ["True", "19"]


def test_idle_env_carries_the_import_path(monkeypatch):
    monkeypatch.delenv("PYTHONPATH", raising=False)
    env = tr.idle_env()
    out = subprocess.run(tr.idle_argv([sys.executable, "-c", "import quipclipper.transcribe"]),
                         env=env, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr


def test_lower_priority_reports_what_it_did():
    code = "from quipclipper.idle import lower_priority; print(','.join(lower_priority()))"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}, check=True)
    assert "nice19" in out.stdout


# --- a whole run with a fake whisper-cli ----------------------------------------------------

FAKE_WHISPER = textwrap.dedent('''\
    #!{python}
    import json, os, sys, wave
    a = sys.argv[1:]
    out = a[a.index("-of") + 1]
    wav = a[a.index("-f") + 1]
    with wave.open(wav) as w:
        secs = w.getnframes() / w.getframerate()
        rate = w.getframerate()
    log = os.environ.get("FAKE_WHISPER_LOG")
    if log:
        with open(log, "a") as fh:
            fh.write(json.dumps({{"argv": a, "secs": secs, "rate": rate, "nice": os.nice(0)}}) + "\\n")
    segs = [{{"offsets": {{"from": 2500, "to": 4000}}, "text": " ♪ Driving way too fast ♪"}},
            {{"offsets": {{"from": 4000, "to": 5000}}, "text": " [Music]"}}]
    json.dump({{"transcription": segs}}, open(out + ".json", "w"))
''')


@pytest.fixture
def fake_whisper(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    exe = bindir / "whisper-cli"
    exe.write_text(FAKE_WHISPER.format(python=sys.executable))
    exe.chmod(0o755)
    log = tmp_path / "whisper.log"
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_WHISPER_LOG", str(log))
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(sys.path))
    model = tmp_path / "ggml-small.en.bin"
    model.write_bytes(b"model")
    return tr.WhisperConfig(models={"small.en": str(model)}), log


def _tone(path: Path, seconds: float = 12.0) -> Path:
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                    f"sine=frequency=440:duration={seconds}", "-ar", "48000", "-y", str(path)],
                   check=True)
    return path


@needs_ffmpeg
def test_run_transcribes_in_chunks_and_saves_each(tmp_path, fake_whisper, monkeypatch):
    cfg, log = fake_whisper
    media = _tone(tmp_path / "song.flac", 12.0)
    store = tr.TranscriptStore(tmp_path / "store")
    monkeypatch.setattr(tr, "CHUNK", 4.0)
    seen = []
    t = tr.run(store, media, [(1.0, 9.0)], cfg=cfg, duration=12.0,
               progress=lambda d, n: seen.append((round(d, 1), round(n, 1))))
    assert seen == [(0.0, 8.0), (4.0, 8.0), (8.0, 8.0)]
    assert t.ranges == [(1.0, 9.0)]
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(calls) == 2
    assert all(c["rate"] == 16000 for c in calls)
    assert all(c["argv"][c["argv"].index("-t") + 1] == "2" for c in calls)
    assert all(c["nice"] == 19 for c in calls)  # whisper ran at idle priority
    # Chunk 1 decodes from 0 (1 s - 2 s of context, floored at 0): its line lands at 2.5 s;
    # chunk 2 decodes from 3 s: its line, at 3 + 2.5 = 5.5 s, is inside 5-9 and stays.
    assert [(c.start, c.text) for c in t.cues] == [(2.5, "♪ Driving way too fast ♪"),
                                                    (5.5, "♪ Driving way too fast ♪")]


@needs_ffmpeg
def test_run_cancel_keeps_finished_chunks(tmp_path, fake_whisper, monkeypatch):
    cfg, _ = fake_whisper
    media = _tone(tmp_path / "song.flac", 12.0)
    store = tr.TranscriptStore(tmp_path / "store")
    monkeypatch.setattr(tr, "CHUNK", 4.0)
    calls = []

    def progress(done, total):
        calls.append(done)

    with pytest.raises(tr.Cancelled):
        tr.run(store, media, [(1.0, 9.0)], cfg=cfg, duration=12.0, progress=progress,
               cancelled=lambda: len(calls) >= 2)
    assert store.load(media).ranges == [(1.0, 5.0)]


def test_run_refuses_an_unknown_model(tmp_path):
    cfg = tr.WhisperConfig(models={"small.en": "/m.bin"})
    with pytest.raises(ValueError):
        tr.run(tr.TranscriptStore(tmp_path), tmp_path / "x.mkv", [(0, 1)], cfg=cfg,
               model="large")


def test_cues_to_vtt():
    assert tr.cues_to_vtt(machine((1, 2, "hi"))) == "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nhi\n"


def test_clean_text_drops_subtitle_markup_whisper_learnt():
    assert tr.clean_text(" <i>Cause the temperature's too high</i>") == "Cause the temperature's too high"


def test_vad_only_for_whole_files():
    assert tr.use_vad("file") and not tr.use_vad("gaps") and not tr.use_vad("range")
