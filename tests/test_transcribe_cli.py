"""The CLI runs machine transcription locally when no server is set (M21)."""

from __future__ import annotations

import json
import shutil

import pytest
from typer.testing import CliRunner

import quipclipper.cli as cli
from quipclipper import transcribe as tr
from test_transcribe import _tone, fake_whisper  # noqa: F401  (fixture)

runner = CliRunner()
needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")


@pytest.fixture(autouse=True)
def no_switch(monkeypatch):
    # Transcription turns on by itself where whisper-cli and a model are installed (D-053);
    # test_transcribe_off.py covers it off.
    monkeypatch.delenv("QC_TRANSCRIBE", raising=False)


@pytest.fixture
def local_whisper(fake_whisper, monkeypatch, tmp_path):  # noqa: F811
    cfg, log = fake_whisper
    monkeypatch.setenv("QC_WHISPER_MODEL", cfg.models["small.en"])
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    return log


def _json(result):
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


@needs_ffmpeg
def test_local_transcribe_then_search_export_copy_clear(local_whisper, tmp_path):
    media = _tone(tmp_path / "song.flac", 12.0)
    (tmp_path / "song.en.srt").write_text("1\n00:00:00,500 --> 00:00:01,500\nWhy won't she be mine\n")
    got = _json(runner.invoke(cli.app, ["--json", "item.transcribe", str(media)]))
    assert got["transcribed"] == [[1.5, 12.0]]  # the gap after the one subtitle line
    assert got["new_lines"][0]["text"] == "♪ Driving way too fast ♪"
    assert (tmp_path / "cache" / "quipclipper" / "transcripts").is_dir()
    # A second run has nothing new to do.
    again = _json(runner.invoke(cli.app, ["--json", "item.transcribe", str(media)]))
    assert again["transcribed"] == [] and "Nothing new" in again["note"]

    hits = _json(runner.invoke(cli.app, ["--json", "search", "way too fast", "--video", str(media)]))
    assert hits["matches"][0]["machine"] is True
    real = runner.invoke(cli.app, ["--json", "search", "way too fast", "--video", str(media),
                                   "--machine", "exclude"])
    assert real.exit_code == 1  # only the subtitle line is left, and it doesn't match
    plain = runner.invoke(cli.app, ["search", "way too fast", "--video", str(media)])
    assert "[auto] ♪ Driving way too fast ♪" in plain.stdout

    srt = runner.invoke(cli.app, ["item.transcript", str(media), "--fmt", "srt"])
    assert "Driving way too fast" in srt.stdout and "-->" in srt.stdout

    assert runner.invoke(cli.app, ["--safe", "item.transcript-copy", str(media)]).exit_code == 5
    copied = _json(runner.invoke(cli.app, ["--json", "item.transcript-copy", str(media)]))
    assert copied["sidecar"] == "song.en.whisper.srt"
    shown = _json(runner.invoke(cli.app, ["--json", "item.transcript", str(media)]))
    assert shown["library"] == ["song.en.whisper.srt"]
    assert all(line["source"] == "machine" for line in shown["lines"])

    assert _json(runner.invoke(cli.app, ["--json", "item.transcript-clear", str(media)]))["deleted"]
    # The library copy still answers the search after the transcript is gone.
    hits = _json(runner.invoke(cli.app, ["--json", "search", "way too fast", "--video", str(media)]))
    assert hits["matches"][0]["machine"] is True
    removed = _json(runner.invoke(cli.app, ["--json", "item.transcript-uncopy", str(media)]))
    assert removed["removed"] == ["song.en.whisper.srt"]


@needs_ffmpeg
def test_local_transcribe_a_range_with_the_model_setting(local_whisper, tmp_path, monkeypatch):
    media = _tone(tmp_path / "song.flac", 12.0)
    assert runner.invoke(cli.app, ["settings.set", "stt_model", "medium.en"]).exit_code == 0
    bad = runner.invoke(cli.app, ["--json", "item.transcribe", str(media), "--start", "1",
                                  "--end", "5"])
    assert bad.exit_code != 0 and "medium.en" in bad.output  # this machine only has small.en
    assert runner.invoke(cli.app, ["settings.set", "stt_model", ""]).exit_code == 0
    got = _json(runner.invoke(cli.app, ["--json", "item.transcribe", str(media), "--start", "1",
                                        "--end", "5"]))
    assert got["transcribed"] == [[1.0, 5.0]] and got["model"] == "small.en"


def test_local_transcribe_without_whisper_says_how(tmp_path, monkeypatch):
    media = tmp_path / "a.mkv"
    media.write_bytes(b"")
    monkeypatch.setenv("QC_WHISPER_BIN", "no-such-whisper-cli")
    out = runner.invoke(cli.app, ["item.transcribe", str(media)])
    assert out.exit_code != 0 and "whisper.cpp" in out.output


def test_server_job_results_print(capsys):
    # A server reports new_lines as a count and the transcript as "stored" (DOC-2).
    job = {"id": "j", "status": "done", "label": "Transcribe",
           "result": {"path": "/m/a.mkv", "mode": "gaps", "model": "small.en", "new_lines": 3,
                      "transcribed": [[0, 9]],
                      "transcript": {"lines": 3, "covered": 9.0, "models": ["small.en"]}}}
    cli._fmt_transcribe(job)
    assert "3 new machine lines" in capsys.readouterr().out
    cli._fmt_transcript({"path": "/m/a.mkv", "library": [], "lines": [],
                         "stored": {"lines": 3, "covered": 9.0, "models": ["small.en"]}})
    out = capsys.readouterr().out
    assert "No machine transcript" not in out and "3 lines" in out


def test_machine_only_with_a_subtitle_file_is_a_usage_error(tmp_path, monkeypatch):
    monkeypatch.setenv("QC_TRANSCRIBE", "1")  # no whisper needed: forced on (D-053)
    subs = tmp_path / "a.srt"
    subs.write_text("1\n00:00:01,000 --> 00:00:02,000\nhello\n")
    out = runner.invoke(cli.app, ["search", "hello", "--subs", str(subs), "--machine", "only"])
    assert out.exit_code == 2 and "--machine only" in out.output


def test_local_copy_writes_what_the_server_would(tmp_path, monkeypatch):
    monkeypatch.setenv("QC_TRANSCRIBE", "1")  # no whisper needed: forced on (D-053)
    # Only machine lines the subtitles don't already have, and the sidecar's name (EN-3).
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    media = tmp_path / "ep.mkv"
    media.write_bytes(b"")
    (tmp_path / "ep.en.srt").write_text("1\n00:00:01,000 --> 00:00:03,000\nWhy won't she be mine\n")
    tr.TranscriptStore(tr.default_store_dir()).add(
        media, (0, 20), [tr.Cue(0, 1.0, 3.0, "why won't she be mine", source=tr.MACHINE),
                         tr.Cue(1, 8.0, 10.0, "Driving way too fast", source=tr.MACHINE)],
        model="small.en")
    got = _json(runner.invoke(cli.app, ["--json", "item.transcript-copy", str(media)]))
    assert got == {"path": str(media), "sidecar": "ep.en.whisper.srt", "lines": 1}
    assert "way too fast" in (tmp_path / "ep.en.whisper.srt").read_text()
    assert "mine" not in (tmp_path / "ep.en.whisper.srt").read_text()


def test_local_transcript_lines_as_the_server(tmp_path, monkeypatch):
    monkeypatch.setenv("QC_TRANSCRIBE", "1")  # no whisper needed: forced on (D-053)
    # --lines machine|all read the stored transcript only, as GET /api/items/transcript (M22).
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    media = tmp_path / "ep.mkv"
    media.write_bytes(b"")
    (tmp_path / "ep.en.srt").write_text("1\n00:00:01,000 --> 00:00:03,000\nWhy won't she be mine\n")
    (tmp_path / "ep.en.whisper.srt").write_text("1\n00:00:15,000 --> 00:00:16,000\nA copy\n")
    tr.TranscriptStore(tr.default_store_dir()).add(
        media, (0, 20), [tr.Cue(0, 1.0, 3.0, "why won't she be mine", source=tr.MACHINE),
                         tr.Cue(1, 8.0, 10.0, "Driving way too fast", source=tr.MACHINE)],
        model="small.en")

    def texts(*args):
        got = _json(runner.invoke(cli.app, ["--json", "item.transcript", str(media), *args]))
        return [x["text"] for x in got["lines"]]

    assert texts("--lines", "machine") == ["Driving way too fast"]
    assert texts("--lines", "all") == ["why won't she be mine", "Driving way too fast"]
    assert "A copy" in texts()
    srt = runner.invoke(cli.app, ["item.transcript", str(media), "--lines", "machine",
                                  "--fmt", "srt"])
    assert srt.stdout.startswith("1\n00:00:08,000 --> 00:00:10,000\nDriving way too fast")
    bare = tmp_path / "bare.mkv"
    bare.write_bytes(b"")
    none = runner.invoke(cli.app, ["item.transcript", str(bare), "--lines", "all", "--fmt", "srt"])
    assert none.exit_code != 0 and "no machine transcript" in none.output
    jf = runner.invoke(cli.app, ["item.transcript", str(media), "--source", "jellyfin"])
    assert jf.exit_code == 2 and "needs a server" in jf.output


def test_local_orphans_listed_then_cleaned(tmp_path, monkeypatch):
    monkeypatch.setenv("QC_TRANSCRIBE", "1")  # no whisper needed: forced on (D-053)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    keep, gone = tmp_path / "keep.mkv", tmp_path / "gone.mkv"
    store = tr.TranscriptStore(tr.default_store_dir())
    for f in (keep, gone):
        f.write_bytes(b"x")
        store.add(f, (0, 5), [tr.Cue(0, 1, 2, "hi", source=tr.MACHINE)], model="small.en")
    gone.unlink()
    listed = _json(runner.invoke(cli.app, ["--json", "index.transcript-orphans"]))
    assert listed["count"] == 1 and listed["orphans"][0]["path"] == str(gone)
    plain = runner.invoke(cli.app, ["index.transcript-orphans"]).stdout
    assert "Would delete 1 orphaned machine transcript" in plain and "media file deleted" in plain
    # Destructive: --safe refuses it, and a plain run deletes only the orphan.
    assert runner.invoke(cli.app, ["--safe", "index.transcript-clean"]).exit_code == 5
    done = _json(runner.invoke(cli.app, ["--json", "index.transcript-clean"]))
    assert done["count"] == 1 and store.load(keep) is not None and store.load(gone) is None
    assert "No orphaned" in runner.invoke(cli.app, ["index.transcript-orphans"]).stdout


def test_local_gaps_read_subtitles_at_idle_priority_and_never_guess(local_whisper, tmp_path,
                                                                     monkeypatch):
    # BE-9: the subtitles are read at idle priority, as the server does; BE-1's twin: if
    # they can't be read, the gaps are unknown, so nothing is transcribed (not the whole file).
    from quipclipper import subtitles

    seen = []

    def broken(**k):
        seen.append(getattr(subtitles._idle, "on", False))
        raise RuntimeError("ffprobe timed out")

    monkeypatch.setattr(cli, "resolve_subtitles", broken)
    runs = []
    monkeypatch.setattr(tr, "run", lambda *a, **k: runs.append(a))
    media = tmp_path / "film.mkv"
    media.write_bytes(b"")
    (tmp_path / "film.en.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nhello\n")
    out = runner.invoke(cli.app, ["item.transcribe", str(media)])
    assert out.exit_code != 0 and "gaps are unknown" in out.output, out.output
    assert seen == [True] and runs == []
