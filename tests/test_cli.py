import pytest
import typer
from typer.testing import CliRunner

import quipclipper.cli as cli
from quipclipper.models import Cue, Match
from quipclipper.subtitles import ResolvedSubtitles

runner = CliRunner()


def mk(i: int) -> Match:
    cue = Cue(index=i, start=float(i), end=float(i) + 1, text=f"line{i}")
    return Match(score=100.0, cues=(cue,), text=f"line{i}")


def test_parse_tracks_none():
    assert cli._parse_tracks(None) is None
    assert cli._parse_tracks("") is None


def test_parse_tracks_list():
    assert cli._parse_tracks("0,2") == [0, 2]
    assert cli._parse_tracks(" 1 , 3 ") == [1, 3]


def test_parse_tracks_invalid():
    with pytest.raises(typer.BadParameter):
        cli._parse_tracks("a,b")


def test_select_single_candidate_autoselects(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("should not prompt for a single candidate")

    monkeypatch.setattr(cli.typer, "prompt", boom)
    cands = [mk(0)]
    assert cli._select_matches(cands) == cands


def test_select_parses_indices(monkeypatch):
    cands = [mk(0), mk(1), mk(2), mk(3)]
    monkeypatch.setattr(cli.typer, "prompt", lambda *a, **k: "0,2")
    assert cli._select_matches(cands) == [cands[0], cands[2]]


def test_select_all(monkeypatch):
    cands = [mk(0), mk(1), mk(2)]
    monkeypatch.setattr(cli.typer, "prompt", lambda *a, **k: "all")
    assert cli._select_matches(cands) == cands


def test_select_dedupes_preserving_order(monkeypatch):
    cands = [mk(0), mk(1), mk(2)]
    monkeypatch.setattr(cli.typer, "prompt", lambda *a, **k: "2,1,2,1")
    assert cli._select_matches(cands) == [cands[2], cands[1]]


def test_select_out_of_range_rejected(monkeypatch):
    cands = [mk(0), mk(1)]
    monkeypatch.setattr(cli.typer, "prompt", lambda *a, **k: "5")
    with pytest.raises(typer.BadParameter):
        cli._select_matches(cands)


def test_select_empty_rejected(monkeypatch):
    cands = [mk(0), mk(1)]
    monkeypatch.setattr(cli.typer, "prompt", lambda *a, **k: " , ")
    with pytest.raises(typer.BadParameter):
        cli._select_matches(cands)


# --- --audio-format (full-mix WAV/FLAC) --------------------------------------

def test_clip_audio_format_rejects_bad_value(tmp_path):
    v = tmp_path / "movie.mkv"
    v.write_bytes(b"")
    res = runner.invoke(cli.app, ["clip", "x", "-v", str(v), "-t", "audio", "--audio-format", "mp3"])
    assert res.exit_code == 2
    assert "must be wav or flac" in res.output


def test_clip_audio_format_rejects_video(tmp_path):
    v = tmp_path / "movie.mkv"
    v.write_bytes(b"")
    res = runner.invoke(cli.app, ["clip", "x", "-v", str(v), "-t", "video", "--audio-format", "wav"])
    assert res.exit_code == 2
    assert "only applies to --type audio" in res.output


def test_clip_audio_format_rejects_with_split(tmp_path):
    v = tmp_path / "movie.mkv"
    v.write_bytes(b"")
    res = runner.invoke(
        cli.app,
        ["clip", "x", "-v", str(v), "-t", "audio", "--audio-format", "wav", "--split-channels"],
    )
    assert res.exit_code == 2
    assert "can't be combined with --split-channels" in res.output


def _stub_clip_backends(monkeypatch, tmp_path):
    """Stub resolve/search + both cut backends; return a dict of captured kwargs."""
    cue = Cue(index=0, start=10.0, end=12.0, text="x")
    monkeypatch.setattr(cli, "_resolve", lambda *a, **k: ResolvedSubtitles([cue], None))
    monkeypatch.setattr(cli, "search", lambda *a, **k: [Match(100.0, (cue,), "x")])
    monkeypatch.setattr(cli, "mkvmerge_available", lambda: True)
    cap = {}

    def fake_cut(video, rng, **kw):
        cap["cut_clip"] = kw
        return kw.get("out") or tmp_path / "clip.mp4"

    def fake_mkv(video, rng, **kw):
        cap["mkvmerge"] = kw
        return kw.get("out") or tmp_path / "clip.mkv"

    monkeypatch.setattr(cli, "cut_clip", fake_cut)
    monkeypatch.setattr(cli, "cut_with_mkvmerge", fake_mkv)
    return cap


def test_clip_video_defaults_to_reencode(tmp_path, monkeypatch):
    """Aligned with the web app: a video clip re-encodes (frame-exact) by default."""
    v = tmp_path / "movie.mkv"; v.write_bytes(b"")
    cap = _stub_clip_backends(monkeypatch, tmp_path)
    res = runner.invoke(cli.app, ["clip", "x", "-v", str(v), "-t", "video", "--yes"])
    assert res.exit_code == 0, res.output
    assert cap.get("cut_clip", {}).get("lossless") is False  # re-encode via ffmpeg
    assert "mkvmerge" not in cap


def test_clip_audio_defaults_to_lossless(tmp_path, monkeypatch):
    """Audio stays a lossless copy by default (the web app never re-encodes audio)."""
    v = tmp_path / "movie.mkv"; v.write_bytes(b"")
    cap = _stub_clip_backends(monkeypatch, tmp_path)
    res = runner.invoke(cli.app, ["clip", "x", "-v", str(v), "-t", "audio", "--yes"])
    assert res.exit_code == 0, res.output
    assert "mkvmerge" in cap  # lossless audio → mkvmerge copy, not a re-encode
    assert "cut_clip" not in cap


def test_clip_video_lossless_flag_forces_copy(tmp_path, monkeypatch):
    v = tmp_path / "movie.mkv"; v.write_bytes(b"")
    cap = _stub_clip_backends(monkeypatch, tmp_path)
    res = runner.invoke(cli.app, ["clip", "x", "-v", str(v), "-t", "video", "--lossless", "--yes"])
    assert res.exit_code == 0, res.output
    assert "mkvmerge" in cap  # explicit --lossless → stream copy
    assert "cut_clip" not in cap


def test_clip_audio_format_passes_codec_to_cut_clip(tmp_path, monkeypatch):
    v = tmp_path / "movie.mkv"
    v.write_bytes(b"")
    cue = Cue(index=0, start=10.0, end=12.0, text="i'll be back")
    monkeypatch.setattr(cli, "_resolve", lambda *a, **k: ResolvedSubtitles([cue], None))
    monkeypatch.setattr(cli, "search", lambda *a, **k: [Match(100.0, (cue,), "i'll be back")])
    captured = {}

    def fake_cut(video, rng, **kwargs):
        captured.update(kwargs)
        out = kwargs.get("out") or tmp_path / "clip.wav"
        return out

    monkeypatch.setattr(cli, "cut_clip", fake_cut)
    res = runner.invoke(
        cli.app,
        ["clip", "be back", "-v", str(v), "-t", "audio", "--audio-format", "flac", "--yes"],
    )
    assert res.exit_code == 0, res.output
    assert captured.get("audio_codec") == "flac"
    assert "full-mix lossless flac" in res.output
