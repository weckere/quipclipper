from pathlib import Path

import pytest

from quipclipper.subtitles import (
    SubtitleTrack,
    best_track,
    find_sidecar,
    load_subtitles,
    resolve_subtitles,
)

FIXTURE = Path(__file__).parent / "fixtures" / "sample.srt"


def test_load_parses_all_cues():
    cues = load_subtitles(FIXTURE)
    assert len(cues) == 5
    assert cues[0].text == "I'll be back."
    assert cues[0].start == pytest.approx(1.0)
    assert cues[0].end == pytest.approx(3.0)


def test_text_is_cleaned_of_markup_and_newlines():
    cues = load_subtitles(FIXTURE)
    # multi-line caption is joined with a space
    assert cues[1].text == "Come with me if you want to live."
    # <i> tags stripped
    assert cues[2].text == "Hasta la vista, baby."
    # ASS position tag stripped
    assert cues[3].text == "Get to the chopper!"


def test_cues_are_sorted_and_indexed_contiguously():
    cues = load_subtitles(FIXTURE)
    assert [c.index for c in cues] == [0, 1, 2, 3, 4]
    assert all(cues[i].start <= cues[i + 1].start for i in range(len(cues) - 1))


def test_missing_file_raises():
    with pytest.raises(FileNotFoundError):
        load_subtitles(FIXTURE.parent / "does-not-exist.srt")


def test_find_sidecar(tmp_path):
    video = tmp_path / "movie.mkv"
    video.write_bytes(b"")
    srt = tmp_path / "movie.srt"
    srt.write_text("")
    assert find_sidecar(video) == srt


def test_find_sidecar_prefixed(tmp_path):
    video = tmp_path / "movie.mp4"
    video.write_bytes(b"")
    srt = tmp_path / "movie.en.srt"
    srt.write_text("")
    assert find_sidecar(video) == srt


def test_resolve_subtitles_missing_video_raises_clean_error(tmp_path):
    # A non-existent --video should be a clean FileNotFoundError, not a traceback
    # from ffprobe failing downstream.
    with pytest.raises(FileNotFoundError):
        resolve_subtitles(subs=None, video=tmp_path / "nope.mkv", track=None)


def test_resolve_subtitles_requires_a_source():
    with pytest.raises(ValueError):
        resolve_subtitles(subs=None, video=None, track=None)


# --- best_track: single source of truth for auto-selection -------------------

def _trk(index, language=None, title=None, forced=False, hearing_impaired=False, codec="subrip"):
    return SubtitleTrack(
        index=index, codec=codec, language=language, title=title,
        forced=forced, hearing_impaired=hearing_impaired,
    )


def test_best_track_empty_is_none():
    assert best_track([]) is None


def test_best_track_prefers_full_english_over_sdh_and_forced():
    tracks = [
        _trk(0, "eng", forced=True),          # forced -> lowest
        _trk(1, "eng", title="SDH", hearing_impaired=True),  # SDH -> middle
        _trk(2, "eng"),                        # full dialogue -> best
    ]
    assert best_track(tracks).index == 2


def test_best_track_prefers_sdh_over_forced_when_only_those_two():
    # The reported case: only eng SDH + eng forced -> SDH wins.
    tracks = [
        _trk(0, "eng", forced=True),
        _trk(1, "eng", title="SDH", hearing_impaired=True),
    ]
    assert best_track(tracks).index == 1


def test_best_track_detects_forced_and_sdh_by_title_without_dispositions():
    tracks = [
        _trk(0, "eng", title="Forced"),
        _trk(1, "eng", title="Full SDH"),
        _trk(2, "eng", title="English"),
    ]
    assert best_track(tracks).index == 2


def test_best_track_treats_untagged_language_as_english():
    # Single-language release with no language metadata should still
    # auto-select rather than being skipped as non-English.
    tracks = [_trk(0, None), _trk(1, "ger")]
    assert best_track(tracks).index == 0


def test_best_track_ties_keep_container_order():
    tracks = [_trk(0, "eng"), _trk(1, "eng")]
    assert best_track(tracks).index == 0


def test_best_track_prefers_text_over_image():
    # The NeverEnding Story case: a text English track (subrip) plus several
    # PGS image tracks. The text track must win even though both are English,
    # because image subtitles can't be extracted/displayed/searched.
    tracks = [
        _trk(0, "eng", title="English SDH", codec="subrip"),
        _trk(1, "eng", title="English SDH", codec="hdmv_pgs_subtitle"),
        _trk(2, "dan", codec="hdmv_pgs_subtitle"),
    ]
    assert best_track(tracks).index == 0


def test_best_track_text_wins_even_when_image_is_plain_dialogue():
    # A plain (non-SDH) English image track must still lose to a text track,
    # even an SDH/forced one — unusable beats slightly-worse-but-usable.
    tracks = [
        _trk(0, "eng", forced=True, codec="subrip"),         # text, forced
        _trk(1, "eng", codec="hdmv_pgs_subtitle"),           # image, full dialogue
    ]
    assert best_track(tracks).index == 0


def test_best_track_falls_back_to_image_when_only_option():
    tracks = [_trk(0, "eng", codec="hdmv_pgs_subtitle"), _trk(1, "spa", codec="dvd_subtitle")]
    # No text track exists; still returns the best-scored (English) image track
    # rather than None, so callers get a defined choice.
    assert best_track(tracks).index == 0
