from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from quipclipper.clip import ClipRange
from quipclipper.mkv import (
    audio_track_ids,
    build_mkvmerge_args,
    cut_with_mkvmerge,
    estimate_remux_bytes,
    human_size,
    is_matroska,
    subtitle_track_ids,
)

TRACKS = [
    {"id": 0, "type": "video"},
    {"id": 1, "type": "audio"},
    {"id": 2, "type": "audio"},
    {"id": 3, "type": "subtitles"},
]


def test_is_matroska():
    assert is_matroska("movie.mkv")
    assert is_matroska("clip.MKA")
    assert not is_matroska("movie.mp4")


def test_audio_track_ids_all_by_default():
    assert audio_track_ids(TRACKS, None) == [1, 2]


def test_audio_track_ids_maps_relative_index_to_global_id():
    # a:1 (second audio) -> mkvmerge global id 2
    assert audio_track_ids(TRACKS, [1]) == [2]
    assert audio_track_ids(TRACKS, [0, 1]) == [1, 2]


def test_audio_track_ids_rejects_out_of_range():
    # A requested a:N that names no audio stream is an error, not a silent drop.
    with pytest.raises(RuntimeError):
        audio_track_ids(TRACKS, [5])
    # A mix of valid + invalid still errors (rather than keeping only the valid one).
    with pytest.raises(RuntimeError):
        audio_track_ids(TRACKS, [0, 5])


def _video_args(**overrides):
    kw = dict(
        source=Path("in.mkv"), rng=ClipRange(5.0, 7.7), kind="video",
        out=Path("out.mkv"), audio_ids=[1, 2], all_audio=True,
        keep_subs=True, keep_chapters=True, embed_subs=None,
    )
    kw.update(overrides)
    return build_mkvmerge_args(**kw)


def test_build_args_video_all_tracks_uses_split_parts():
    args = _video_args()
    assert args[:2] == ["mkvmerge", "-o"]
    assert "--split" in args
    assert "parts:00:00:05.000-00:00:07.700" in args
    assert "in.mkv" in args
    # all audio kept -> no -a selection, subs kept -> no -S, chapters kept -> no --no-chapters
    assert "-a" not in args and "-S" not in args and "--no-chapters" not in args


def test_build_args_video_selected_audio_and_no_subs():
    args = _video_args(audio_ids=[2], all_audio=False, keep_subs=False)
    assert "-a" in args and args[args.index("-a") + 1] == "2"
    assert "-S" in args


def test_build_args_no_chapters_flag():
    assert "--no-chapters" in _video_args(keep_chapters=False)
    assert "--no-chapters" not in _video_args(keep_chapters=True)


def test_build_args_audio_only_drops_video_and_subs():
    args = build_mkvmerge_args(
        source=Path("in.mkv"), rng=ClipRange(0.0, 2.0), kind="audio",
        out=Path("out.mka"), audio_ids=[1], all_audio=False,
        keep_subs=True, keep_chapters=True, embed_subs=None,
    )
    assert "-D" in args and "-S" in args
    assert "-a" in args and args[args.index("-a") + 1] == "1"


def test_build_args_video_embeds_sidecar_as_extra_input():
    args = _video_args(audio_ids=[1], embed_subs=Path("subs.srt"))
    # sidecar appended after the source input
    assert args.index("subs.srt") > args.index("in.mkv")


def test_build_args_guards_leading_dash_in_source_and_out():
    # A leading-dash source/out/sidecar would be parsed as an mkvmerge option.
    args = build_mkvmerge_args(
        source=Path("-weird.mkv"), rng=ClipRange(5.0, 7.7), kind="video",
        out=Path("-out.mkv"), audio_ids=[1], all_audio=True,
        keep_subs=True, keep_chapters=True, embed_subs=Path("-subs.srt"),
    )
    assert "./-weird.mkv" in args and "-weird.mkv" not in args
    assert "./-out.mkv" in args and "-out.mkv" not in args
    assert "./-subs.srt" in args and "-subs.srt" not in args


def test_subtitle_track_ids():
    tracks = TRACKS + [{"id": 4, "type": "subtitles"}]
    assert subtitle_track_ids(tracks) == [3, 4]


def test_build_args_default_subtitle_flag():
    """B17c: the chosen subtitle id is flagged default (1), the rest cleared (0),
    before the source input."""
    args = _video_args(default_sub_id=4, sub_ids=[3, 4])
    i_src = args.index("in.mkv")
    assert "--default-track-flag" in args
    # both flags precede the source
    flags = [args[i + 1] for i, a in enumerate(args[:i_src]) if a == "--default-track-flag"]
    assert "3:0" in flags and "4:1" in flags


def test_build_args_no_default_flag_without_selection():
    assert "--default-track-flag" not in _video_args(sub_ids=[3, 4])


def _run_cut(kind, tmp_path):
    """Run cut_with_mkvmerge with mkvmerge/identify/keyframe-probe stubbed,
    returning (mkvmerge argv, keyframe-probe mock)."""
    src = tmp_path / "in.mkv"
    src.write_bytes(b"x")
    out = tmp_path / ("o.mka" if kind == "audio" else "o.mkv")
    captured = {}

    def fake_run(args, **kw):
        captured["args"] = args
        out.write_bytes(b"x")  # pretend mkvmerge produced the file
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("quipclipper.mkv.mkvmerge_available", return_value=True), \
         patch("quipclipper.mkv.identify", return_value=[]), \
         patch("quipclipper.mkv._keyframe_at_or_before", return_value=10.0) as kf, \
         patch("quipclipper.mkv.subprocess.run", side_effect=fake_run):
        cut_with_mkvmerge(src, ClipRange(20.0, 23.0), kind=kind, out=out)
    split = next(a for a in captured["args"] if a.startswith("parts:"))
    return split, kf


def test_audio_cut_is_not_snapped_to_a_video_keyframe(tmp_path):
    """An audio clip is cut at the exact requested time — it must NOT snap its
    start back to a video keyframe (which bloated short clips on long-GOP files)."""
    split, kf = _run_cut("audio", tmp_path)
    assert split == "parts:00:00:20.000-00:00:23.000"  # exact start (20s)
    kf.assert_not_called()


def test_video_cut_snaps_start_back_to_keyframe(tmp_path):
    """A video clip still snaps its start back to the prior keyframe (a stream
    copy can only begin there)."""
    split, kf = _run_cut("video", tmp_path)
    assert split == "parts:00:00:10.000-00:00:23.000"  # snapped to keyframe (10s)
    kf.assert_called_once()


def test_human_size():
    assert human_size(0) == "0 B"
    assert human_size(512) == "512 B"
    assert human_size(1536) == "1.5 KB"
    assert human_size(5 * 1024**3) == "5.0 GB"


def test_estimate_remux_bytes_sums_source_and_extras(tmp_path):
    src = tmp_path / "movie.mkv"
    src.write_bytes(b"x" * 1000)
    sub = tmp_path / "movie.srt"
    sub.write_bytes(b"y" * 50)
    assert estimate_remux_bytes(src) == 1000
    assert estimate_remux_bytes(src, [sub]) == 1050


def test_estimate_remux_bytes_ignores_missing_extras(tmp_path):
    src = tmp_path / "movie.mkv"
    src.write_bytes(b"x" * 1000)
    assert estimate_remux_bytes(src, [tmp_path / "nope.srt"]) == 1000
