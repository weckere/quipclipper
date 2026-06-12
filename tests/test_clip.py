from pathlib import Path

import pytest

from quipclipper.clip import (
    ClipRange,
    _ffmpeg_args,
    _split_codec,
    compute_range,
    cut_clip,
    group_channels,
    output_extension,
    render_clip_srt,
)
from quipclipper.models import Cue, Match


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


def test_output_extension_single_audio_stream_matches_codec():
    assert output_extension("audio", lossless=True, audio_codecs=["aac"]) == "m4a"
    assert output_extension("audio", lossless=True, audio_codecs=["opus"]) == "opus"
    assert output_extension("audio", lossless=True, audio_codecs=["ac3"]) == "ac3"
    assert output_extension("audio", lossless=True, audio_codecs=["eac3"]) == "eac3"


def test_output_extension_multiple_audio_streams_uses_mka():
    # e.g. a 5.1 EAC3 track plus a stereo commentary -> Matroska holds both
    assert output_extension("audio", lossless=True, audio_codecs=["eac3", "aac"]) == "mka"
    assert output_extension("audio", lossless=True, audio_codecs=["ac3", "ac3", "aac"]) == "mka"


def test_output_extension_lossless_audio_unknown_codec_falls_back():
    assert output_extension("audio", lossless=True, audio_codecs=["weirdcodec"]) == "mka"
    assert output_extension("audio", lossless=True, audio_codecs=None) == "mka"
    assert output_extension("audio", lossless=True, audio_codecs=[]) == "mka"


def test_output_extension_lossless_video_is_mkv():
    assert output_extension("video", lossless=True) == "mkv"


def test_output_extension_reencode():
    assert output_extension("audio", lossless=False) == "mp3"
    assert output_extension("video", lossless=False) == "mp4"


def test_output_extension_gif_ignores_lossless():
    assert output_extension("gif", lossless=True) == "gif"
    assert output_extension("gif", lossless=False) == "gif"


def test_ffmpeg_args_lossless_audio_copies_all_audio_streams():
    args = _ffmpeg_args(
        source=Path("in.mkv"), rng=ClipRange(5.0, 8.0), kind="audio",
        out=Path("out.mka"), lossless=True, fps=15, width=480,
    )
    assert "-c" in args and args[args.index("-c") + 1] == "copy"
    # maps every audio stream, not just the first
    assert "-map" in args and "0:a" in args
    assert "0:a:0" not in args
    # duration, not absolute end time
    assert "-t" in args and args[args.index("-t") + 1] == "3.000"


def test_ffmpeg_args_lossless_video_copies_all_av_and_subtitle_streams():
    args = _ffmpeg_args(
        source=Path("in.mkv"), rng=ClipRange(0.0, 4.0), kind="video",
        out=Path("out.mkv"), lossless=True, fps=15, width=480,
    )
    assert args.count("-map") == 3
    assert {"0:v?", "0:a?", "0:s?"} <= set(args)
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


def test_ffmpeg_args_audio_track_selection_maps_chosen_streams():
    args = _ffmpeg_args(
        source=Path("in.mkv"), rng=ClipRange(0.0, 2.0), kind="audio",
        out=Path("out.mka"), lossless=True, fps=15, width=480,
        audio_indices=[0, 2],
    )
    # explicit per-stream maps, not the catch-all 0:a
    assert "0:a:0" in args and "0:a:2" in args
    assert args.count("-map") == 2


def test_ffmpeg_args_video_embeds_sidecar_subtitle():
    args = _ffmpeg_args(
        source=Path("in.mkv"), rng=ClipRange(0.0, 2.0), kind="video",
        out=Path("out.mkv"), lossless=True, fps=15, width=480,
        embed_subs=Path("subs.srt"),
    )
    # second input is the subtitle file, and it is mapped in
    assert args.count("-i") == 2
    assert "subs.srt" in args
    assert "1:0" in args


def test_ffmpeg_args_video_no_embed_when_not_requested():
    args = _ffmpeg_args(
        source=Path("in.mkv"), rng=ClipRange(0.0, 2.0), kind="video",
        out=Path("out.mkv"), lossless=True, fps=15, width=480,
    )
    assert args.count("-i") == 1


def test_ffmpeg_args_fullmix_wav_keeps_all_channels():
    # Full-mix WAV: re-encode one audio stream to pcm_s24le with NO -ac downmix,
    # so a 5.1 source stays 5.1. lossless flag is irrelevant when audio_codec set.
    args = _ffmpeg_args(
        source=Path("in.mkv"), rng=ClipRange(0.0, 2.0), kind="audio",
        out=Path("out.wav"), lossless=False, fps=15, width=480,
        audio_codec="wav",
    )
    assert "-c:a" in args and "pcm_s24le" in args
    assert "-ac" not in args                 # no downmix → channel layout preserved
    assert "-map" in args and "0:a:0" in args  # single stream (WAV holds one)
    assert args[-1] == "out.wav"


def test_ffmpeg_args_fullmix_flac_uses_selected_stream():
    args = _ffmpeg_args(
        source=Path("in.mkv"), rng=ClipRange(0.0, 2.0), kind="audio",
        out=Path("out.flac"), lossless=False, fps=15, width=480,
        audio_indices=[2], audio_codec="flac",
    )
    assert "flac" in args
    assert "0:a:2" in args
    assert "-ac" not in args


def test_cut_clip_rejects_unknown_audio_codec(tmp_path):
    src = tmp_path / "in.mkv"
    src.write_bytes(b"")
    with pytest.raises(ValueError):
        cut_clip(src, ClipRange(0.0, 2.0), kind="audio", audio_codec="mp3")


def test_group_channels_5_1():
    groups = group_channels(["FL", "FR", "FC", "LFE", "BL", "BR"])
    assert groups == [
        ("front", ["FL", "FR"]),
        ("back", ["BL", "BR"]),
        ("center", ["FC"]),
        ("lfe", ["LFE"]),
    ]


def test_group_channels_7_1_has_side_and_back_pairs():
    groups = dict(group_channels(["FL", "FR", "FC", "LFE", "BL", "BR", "SL", "SR"]))
    assert groups["front"] == ["FL", "FR"]
    assert groups["side"] == ["SL", "SR"]
    assert groups["back"] == ["BL", "BR"]
    assert groups["center"] == ["FC"]
    assert groups["lfe"] == ["LFE"]


def test_group_channels_stereo_is_single_front_pair():
    assert group_channels(["FL", "FR"]) == [("front", ["FL", "FR"])]


def test_group_channels_can_drop_lfe():
    groups = dict(group_channels(["FL", "FR", "FC", "LFE", "BL", "BR"], include_lfe=False))
    assert "lfe" not in groups
    assert groups["front"] == ["FL", "FR"]
    assert groups["center"] == ["FC"]


def test_split_codec_wav_and_flac():
    assert _split_codec("wav", "in.mkv", 0) == ("pcm_s24le", "wav")
    assert _split_codec("flac", "in.mkv", 0) == ("flac", "flac")


def test_split_codec_rejects_unknown_format():
    with pytest.raises(ValueError):
        _split_codec("mp4", "in.mkv", 0)


def _cue(i, start, end, text):
    return Cue(index=i, start=start, end=end, text=text)


def test_render_clip_srt_shifts_to_window_start():
    cues = [
        _cue(0, 1.0, 3.0, "before window"),
        _cue(1, 5.5, 7.2, "in window"),
        _cue(2, 20.0, 22.0, "after window"),
    ]
    srt = render_clip_srt(cues, window_start=5.0, window_end=7.7)
    # only the in-window cue, shifted by -window_start
    assert "in window" in srt
    assert "before window" not in srt and "after window" not in srt
    assert "00:00:00,500 --> 00:00:02,200" in srt


def test_render_clip_srt_clamps_overlapping_cue():
    cues = [_cue(0, 4.5, 6.0, "spans the start")]
    srt = render_clip_srt(cues, window_start=5.0, window_end=7.7)
    # starts clamped to 0 (cue began before the window)
    assert srt.startswith("1\n00:00:00,000 --> 00:00:01,000")


def test_render_clip_srt_empty_when_nothing_in_window():
    cues = [_cue(0, 1.0, 2.0, "x")]
    assert render_clip_srt(cues, window_start=5.0, window_end=7.7) == ""
