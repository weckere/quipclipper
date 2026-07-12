from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from quipclipper.clip import (
    ClipRange,
    _argv_path,
    _ffmpeg_args,
    _split_codec,
    _timestamp_slug,
    compute_range,
    cut_clip,
    group_category,
    group_channels,
    output_extension,
    render_clip_srt,
    vaapi_h264_available,
)
from quipclipper.models import Cue, Match


def make_match(start=10.0, end=12.0):
    cue = Cue(index=0, start=start, end=end, text="line")
    return Match(score=100.0, cues=(cue,), text="line")


def test_match_end_uses_latest_cue_end_with_overlap():
    # Overlapping cues: the LAST cue in order isn't the one that ends latest.
    # Match.end must take the max so a clip window never truncates the tail.
    early_long = Cue(index=0, start=10.0, end=20.0, text="long line")
    later_short = Cue(index=1, start=11.0, end=12.0, text="short overlap")
    m = Match(score=100.0, cues=(early_long, later_short), text="x")
    assert m.end == 20.0  # not later_short.end (12.0)


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


def test_ffmpeg_args_default_subtitle_disposition():
    """B17c: the selected subtitle output stream is flagged default, others cleared."""
    args = _ffmpeg_args(
        source=Path("in.mkv"), rng=ClipRange(0.0, 4.0), kind="video",
        out=Path("out.mkv"), lossless=True, fps=15, width=480,
        default_sub_index=1, sub_count=3,
    )
    assert args[args.index("-disposition:s:0") + 1] == "0"
    assert args[args.index("-disposition:s:1") + 1] == "default"
    assert args[args.index("-disposition:s:2") + 1] == "0"


def test_ffmpeg_args_no_disposition_without_default_sub():
    args = _ffmpeg_args(
        source=Path("in.mkv"), rng=ClipRange(0.0, 4.0), kind="video",
        out=Path("out.mkv"), lossless=True, fps=15, width=480,
    )
    assert not any(a.startswith("-disposition") for a in args)


def test_ffmpeg_args_reencode_video_uses_libx264():
    args = _ffmpeg_args(
        source=Path("in.mkv"), rng=ClipRange(0.0, 4.0), kind="video",
        out=Path("out.mp4"), lossless=False, fps=15, width=480,
    )
    assert "libx264" in args
    assert "copy" not in args


def test_ffmpeg_args_reencode_video_vaapi_uses_quick_sync():
    """A hardware re-encode emits the VAAPI encoder + device + hwupload filter,
    not libx264."""
    args = _ffmpeg_args(
        source=Path("in.mkv"), rng=ClipRange(0.0, 4.0), kind="video",
        out=Path("out.mp4"), lossless=False, fps=15, width=480,
        video_encoder="h264_vaapi", vaapi_device="/dev/dri/renderD128",
    )
    assert "h264_vaapi" in args
    assert "libx264" not in args
    assert "-vaapi_device" in args and "/dev/dri/renderD128" in args
    assert "format=nv12,hwupload" in args
    # the device must be initialised before the input
    assert args.index("-vaapi_device") < args.index("-i")


# --- URL sources + DASH audio pair (YouTube) ----------------------------------

def test_ffmpeg_args_aux_audio_lossless_maps_dash_pair():
    """A separate audio URL becomes input 1, seeked like the video, mapped
    explicitly — and 0:s? is dropped (a video-only stream has no subs)."""
    args = _ffmpeg_args(
        source="https://v.example/video", rng=ClipRange(5.0, 8.0), kind="video",
        out=Path("out.mkv"), lossless=True, fps=15, width=480,
        aux_audio="https://a.example/audio",
    )
    assert args.count("-i") == 2
    assert args.count("-ss") == 2  # both inputs seeked to the same spot
    i_video, i_audio = [i for i, a in enumerate(args) if a == "-i"]
    assert args[i_video + 1] == "https://v.example/video"
    assert args[i_audio + 1] == "https://a.example/audio"
    maps = [args[i + 1] for i, a in enumerate(args) if a == "-map"]
    assert maps == ["0:v:0", "1:a:0"]
    assert "0:s?" not in args
    assert args[args.index("-c") + 1] == "copy"
    # network inputs carry reconnect flags
    assert "-reconnect" in args


def test_ffmpeg_args_aux_audio_reencode_maps_dash_pair():
    args = _ffmpeg_args(
        source="https://v.example/video", rng=ClipRange(0.0, 4.0), kind="video",
        out=Path("out.mp4"), lossless=False, fps=15, width=480,
        aux_audio="https://a.example/audio",
    )
    maps = [args[i + 1] for i, a in enumerate(args) if a == "-map"]
    assert maps == ["0:v:0", "1:a:0"]
    assert "libx264" in args


def test_ffmpeg_args_aux_audio_shifts_embed_subs_input(tmp_path):
    srt = tmp_path / "subs.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n")
    args = _ffmpeg_args(
        source="https://v.example/video", rng=ClipRange(0.0, 4.0), kind="video",
        out=Path("out.mkv"), lossless=True, fps=15, width=480,
        aux_audio="https://a.example/audio", embed_subs=srt,
    )
    # inputs: 0 = video URL, 1 = audio URL, 2 = the SRT — mapped as 2:0
    assert args.count("-i") == 3
    maps = [args[i + 1] for i, a in enumerate(args) if a == "-map"]
    assert maps == ["0:v:0", "1:a:0", "2:0"]


def test_ffmpeg_args_local_file_has_no_reconnect_flags():
    args = _ffmpeg_args(
        source=Path("in.mkv"), rng=ClipRange(0.0, 4.0), kind="video",
        out=Path("out.mkv"), lossless=True, fps=15, width=480,
    )
    assert "-reconnect" not in args


def test_cut_clip_url_source_skips_exists_check(monkeypatch, tmp_path):
    """A URL source must reach ffmpeg (no local stat); out is required."""
    import quipclipper.clip as clip_mod

    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd

        class R:
            returncode = 0
            stderr = ""
        return R()

    monkeypatch.setattr(clip_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(clip_mod.shutil, "which", lambda _: "/usr/bin/ffmpeg")
    out = tmp_path / "clip.mkv"
    result = cut_clip(
        "https://v.example/video", ClipRange(1.0, 2.0), kind="video",
        lossless=True, out=out, aux_audio="https://a.example/audio",
    )
    assert result == out
    assert "https://v.example/video" in captured["cmd"]
    assert "https://a.example/audio" in captured["cmd"]


def test_cut_clip_url_source_requires_out(monkeypatch):
    import quipclipper.clip as clip_mod
    monkeypatch.setattr(clip_mod.shutil, "which", lambda _: "/usr/bin/ffmpeg")
    with pytest.raises(ValueError, match="out is required"):
        cut_clip("https://v.example/video", ClipRange(0.0, 1.0), kind="video")


def test_ffmpeg_args_gif_is_always_reencoded():
    args = _ffmpeg_args(
        source=Path("in.mkv"), rng=ClipRange(0.0, 4.0), kind="gif",
        out=Path("out.gif"), lossless=True, fps=12, width=320,
    )
    assert "copy" not in args
    assert any("fps=12" in a and "scale=320" in a for a in args)


def test_argv_path_guards_leading_dash():
    # A relative filename starting with '-' would be read as an option; prefix ./.
    assert _argv_path("-weird.mkv") == "./-weird.mkv"
    assert _argv_path(Path("-weird.mkv")) == "./-weird.mkv"
    # Ordinary and absolute paths are untouched.
    assert _argv_path("movie.mkv") == "movie.mkv"
    assert _argv_path("/abs/-weird.mkv") == "/abs/-weird.mkv"


def test_ffmpeg_args_guards_leading_dash_in_source_and_out():
    # Both the input source and the output path must be dash-guarded so ffmpeg
    # doesn't parse a leading '-' filename as an option.
    args = _ffmpeg_args(
        source=Path("-weird.mkv"), rng=ClipRange(0.0, 2.0), kind="audio",
        out=Path("-out.mka"), lossless=True, fps=15, width=480,
    )
    assert "./-weird.mkv" in args and "-weird.mkv" not in args
    assert "./-out.mka" in args and "-out.mka" not in args


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


def test_vaapi_h264_available_false_when_no_render_node():
    assert vaapi_h264_available("/dev/dri/does-not-exist-xyz") is False


def test_vaapi_h264_available_true_when_probe_succeeds(tmp_path):
    dev = tmp_path / "renderD128"
    dev.write_bytes(b"")  # a path that exists, so the probe runs
    with patch("quipclipper.clip.shutil.which", return_value="/ffmpeg"), \
         patch("quipclipper.clip.subprocess.run",
               return_value=MagicMock(returncode=0)) as run:
        assert vaapi_h264_available(str(dev)) is True
    assert "h264_vaapi" in run.call_args.args[0]


def test_cut_clip_falls_back_to_software_when_hw_encode_fails(tmp_path):
    """If the hardware (VAAPI) re-encode fails, cut_clip retries on libx264."""
    src = tmp_path / "in.mkv"
    src.write_bytes(b"x")
    out = tmp_path / "out.mp4"
    calls = []

    def fake_run(args, **kw):
        calls.append(args)
        rc = 1 if "h264_vaapi" in args else 0   # HW fails, software succeeds
        if rc == 0:
            out.write_bytes(b"x")
        return MagicMock(returncode=rc, stdout="", stderr="hw boom")

    with patch("quipclipper.clip.shutil.which", return_value="/ffmpeg"), \
         patch("quipclipper.clip.subprocess.run", side_effect=fake_run):
        result = cut_clip(
            src, ClipRange(0.0, 4.0), kind="video", lossless=False, out=out,
            video_encoder="h264_vaapi", vaapi_device="/dev/dri/renderD128",
        )
    assert result == out
    assert len(calls) == 2
    assert "h264_vaapi" in calls[0]   # tried the iGPU first
    assert "libx264" in calls[1]      # then fell back to the CPU


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


def test_timestamp_slug_rounds_to_whole_seconds():
    assert _timestamp_slug(57.0) == "00-00-57"
    assert _timestamp_slug(57.4) == "00-00-57"
    assert _timestamp_slug(57.5) == "00-00-58"
    assert _timestamp_slug(3661.4) == "01-01-01"
    assert "_" not in _timestamp_slug(12.345)  # no millisecond suffix


def test_group_category_buckets():
    assert group_category("front") == "front"
    assert group_category("front_center") == "front"
    assert group_category("center") == "center"
    assert group_category("lfe") == "lfe"
    assert group_category("side") == "surround"
    assert group_category("back") == "surround"


def test_split_codec_wav_and_flac():
    assert _split_codec("wav", "in.mkv", 0) == ("pcm_s24le", "wav")
    assert _split_codec("flac", "in.mkv", 0) == ("flac", "flac")


def test_split_codec_rejects_unknown_format():
    with pytest.raises(ValueError):
        _split_codec("mp4", "in.mkv", 0)


def test_split_codec_original_maps_opus_and_vorbis_to_external_encoders():
    # ffmpeg's built-in opus/vorbis/mp3 encoders are experimental/absent, so
    # -c:a opus/vorbis aborts. --split-format original must emit libopus/
    # libvorbis/libmp3lame while keeping the source-codec container extension.
    with patch("quipclipper.clip.probe_audio_streams", return_value=["opus"]):
        assert _split_codec("original", "in.webm", 0) == ("libopus", "opus")
    with patch("quipclipper.clip.probe_audio_streams", return_value=["vorbis"]):
        assert _split_codec("original", "in.ogg", 0) == ("libvorbis", "ogg")
    with patch("quipclipper.clip.probe_audio_streams", return_value=["mp3"]):
        assert _split_codec("original", "in.mp3", 0) == ("libmp3lame", "mp3")


def test_split_codec_original_keeps_native_encoder_for_ac3():
    # ac3/eac3/aac/flac encode fine under their own name — not remapped.
    with patch("quipclipper.clip.probe_audio_streams", return_value=["ac3"]):
        assert _split_codec("original", "in.mkv", 0) == ("ac3", "ac3")


def test_split_codec_original_rejects_uncopyable_codec():
    with patch("quipclipper.clip.probe_audio_streams", return_value=["truehd"]):
        with pytest.raises(RuntimeError):
            _split_codec("original", "in.mkv", 0)


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
