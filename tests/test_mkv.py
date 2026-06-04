from pathlib import Path

from clipper.clip import ClipRange
from clipper.mkv import (
    audio_track_ids,
    build_mkvmerge_args,
    estimate_remux_bytes,
    human_size,
    is_matroska,
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


def test_audio_track_ids_ignores_out_of_range():
    assert audio_track_ids(TRACKS, [5]) == []


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
