from pathlib import Path

import pytest

from clipper.clip import (
    ClipRange,
    _ffmpeg_args,
    compute_range,
    output_extension,
)
from clipper.models import Cue, Match


def make_match(start=10.0, end=12.0):
    cue = Cue(index=0, start=start, end=end, text="line")
    return Match(score=100.0, cues=(cue,), text="line")


def test_compute_range_pads_both_sides():
    rng = compute_range(make_match(10.0, 12.0), before=5.0, after=3.0)
    assert rng.start == 5.0
    assert rng.end == 15.0
    assert rng.duration == 10.0


def test_compute_range_clamps_negative_start():
    rng = compute_range(make_match(1.0, 2.0), before=5.0, after=0.0)
    assert rng.start == 0.0


def test_output_extension_lossless_audio_matches_codec():
    assert output_extension("audio", lossless=True, audio_codec="aac") == "m4a"
    assert output_extension("audio", lossless=True, audio_codec="opus") == "opus"
    assert output_extension("audio", lossless=True, audio_codec="flac") == "flac"


def test_output_extension_lossless_audio_unknown_codec_falls_back():
    assert output_extension("audio", lossless=True, audio_codec="weirdcodec") == "mka"
    assert output_extension("audio", lossless=True, audio_codec=None) == "mka"


def test_output_extension_lossless_video_is_mkv():
    assert output_extension("video", lossless=True) == "mkv"


def test_output_extension_reencode():
    assert output_extension("audio", lossless=False) == "mp3"
    assert output_extension("video", lossless=False) == "mp4"


def test_output_extension_gif_ignores_lossless():
    assert output_extension("gif", lossless=True) == "gif"
    assert output_extension("gif", lossless=False) == "gif"


def test_ffmpeg_args_lossless_audio_uses_stream_copy():
    args = _ffmpeg_args(
        source=Path("in.mkv"), rng=ClipRange(5.0, 8.0), kind="audio",
        out=Path("out.m4a"), lossless=True, fps=15, width=480,
    )
    assert "-c" in args and args[args.index("-c") + 1] == "copy"
    assert "-map" in args and "0:a:0" in args
    # duration, not absolute end time
    assert "-t" in args and args[args.index("-t") + 1] == "3.000"


def test_ffmpeg_args_lossless_video_copies_all_relevant_streams():
    args = _ffmpeg_args(
        source=Path("in.mkv"), rng=ClipRange(0.0, 4.0), kind="video",
        out=Path("out.mkv"), lossless=True, fps=15, width=480,
    )
    assert args.count("-map") == 2
    assert args[args.index("-c") + 1] == "copy"


def test_ffmpeg_args_reencode_video_uses_libx264():
    args = _ffmpeg_args(
        source=Path("in.mkv"), rng=ClipRange(0.0, 4.0), kind="video",
        out=Path("out.mp4"), lossless=False, fps=15, width=480,
    )
    assert "libx264" in args
    assert "copy" not in args


def test_ffmpeg_args_gif_is_always_reencoded():
    args = _ffmpeg_args(
        source=Path("in.mkv"), rng=ClipRange(0.0, 4.0), kind="gif",
        out=Path("out.gif"), lossless=True, fps=12, width=320,
    )
    assert "copy" not in args
    assert any("fps=12" in a and "scale=320" in a for a in args)


def test_ffmpeg_args_seeks_before_input():
    args = _ffmpeg_args(
        source=Path("in.mkv"), rng=ClipRange(7.0, 9.0), kind="audio",
        out=Path("out.m4a"), lossless=True, fps=15, width=480,
    )
    assert args.index("-ss") < args.index("-i")
