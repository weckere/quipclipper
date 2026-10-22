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
    assert copied["sidecar"].endswith("song.en.whisper.srt")
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
