"""The CLI with machine transcription off (D-053): no whisper-cli and model, or
QC_TRANSCRIBE=0 where they are."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

import quipclipper.cli as cli
from quipclipper import commands as reg
from quipclipper import transcribe as tr

runner = CliRunner()
STT = [c.id for c in reg.COMMANDS if c.transcription]


@pytest.fixture(autouse=True)
def switched_off(monkeypatch, tmp_path):
    monkeypatch.setenv("QC_TRANSCRIBE", "0")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))


def test_the_transcription_commands_are_marked():
    assert sorted(STT) == sorted([
        "item.transcribe", "item.transcript", "item.transcript-clear",
        "item.transcript-copy", "item.transcript-uncopy", "index.transcribe",
        "index.transcript-orphans", "index.transcript-clean"])


def test_hidden_from_help_and_commands_marked_in_docs():
    # The CLI module was imported with no whisper model configured, so off (as the shell of
    # someone without whisper.cpp has it).
    top = runner.invoke(cli.app, ["--help"], terminal_width=200).output
    assert "item.transcribe" not in top and "index.transcribe" not in top
    listed = runner.invoke(cli.app, ["commands"]).output
    assert all(i not in listed for i in STT)
    rows = json.loads(runner.invoke(cli.app, ["--json", "commands"]).stdout)
    assert not {r["id"] for r in rows} & set(STT)
    assert all(p["name"] != "machine" for r in rows for p in r["params"])
    assert "--machine" not in runner.invoke(cli.app, ["search", "--help"]).output
    assert "--machine" not in runner.invoke(cli.app, ["item.script", "--help"]).output
    md = reg.render_markdown()  # docs/CLI.md lists them, marked as needing whisper
    assert all(f"`{i}` ²" in md for i in STT) and "`--machine²`" in md
    settings = json.loads(runner.invoke(cli.app, ["--json", "settings.list"]).stdout)
    assert "stt_model" not in settings["settings"]


@pytest.mark.parametrize("cmd", STT)
def test_refused_locally_with_the_reason(cmd, tmp_path):
    media = tmp_path / "a.mkv"
    media.write_bytes(b"")
    arg = [] if reg.BY_ID[cmd].main_param is None else [
        str(tmp_path if cmd == "index.transcribe" else media)]
    out = runner.invoke(cli.app, [cmd, *arg])
    if cmd == "index.transcribe":  # server-only: it needs a server, as before
        assert out.exit_code != 0
        return
    assert out.exit_code == 2 and "QC_TRANSCRIBE" in out.output, out.output


def test_local_search_leaves_stored_lines_out(tmp_path):
    media = tmp_path / "ep.mkv"
    media.write_bytes(b"")
    (tmp_path / "ep.en.srt").write_text("1\n00:00:01,000 --> 00:00:03,000\nWhy won't she be mine\n")
    tr.TranscriptStore(tr.default_store_dir()).add(
        media, (0, 20), [tr.Cue(0, 8.0, 10.0, "Driving way too fast", source=tr.MACHINE)],
        model="small.en")
    miss = runner.invoke(cli.app, ["--json", "search", "way too fast", "--video", str(media)])
    assert miss.exit_code == 1  # only the subtitle line, and it doesn't match
    only = runner.invoke(cli.app, ["search", "way too fast", "--video", str(media),
                                   "--machine", "only"])
    assert only.exit_code == 2 and "QC_TRANSCRIBE" in only.output


def test_off_by_default_without_whisper_and_on_with_it(tmp_path, monkeypatch):
    monkeypatch.delenv("QC_TRANSCRIBE", raising=False)
    monkeypatch.delenv("QC_WHISPER_MODEL", raising=False)
    monkeypatch.delenv("QC_WHISPER_MODELS", raising=False)
    media = tmp_path / "a.mkv"
    media.write_bytes(b"")
    out = runner.invoke(cli.app, ["item.transcript", str(media)])
    assert out.exit_code == 2 and "whisper" in out.output, out.output
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "whisper-cli").write_text("#!/bin/sh\n")
    (bindir / "whisper-cli").chmod(0o755)
    model = tmp_path / "ggml-small.en.bin"
    model.write_bytes(b"model")
    monkeypatch.setenv("PATH", f"{bindir}:{__import__('os').environ['PATH']}")
    monkeypatch.setenv("QC_WHISPER_MODEL", str(model))
    out = runner.invoke(cli.app, ["--json", "item.transcript", str(media)])
    assert out.exit_code == 0, out.output  # on: no transcript yet, but it answers
    monkeypatch.setenv("QC_TRANSCRIBE", "0")
    out = runner.invoke(cli.app, ["item.transcript", str(media)])
    assert out.exit_code == 2 and "QC_TRANSCRIBE" in out.output, out.output
