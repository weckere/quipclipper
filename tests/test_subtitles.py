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


def test_unparseable_file_raises_valueerror(tmp_path):
    """A file pysubs2 can't autodetect (empty or garbage) is normalized to a
    ValueError, not the raw pysubs2 exception — so callers that already handle
    ValueError (folder search skips it; /api/search → 409) don't see a 500."""
    bad = tmp_path / "garbage.srt"
    bad.write_text("\x00\x01 not a subtitle file at all")
    with pytest.raises(ValueError):
        load_subtitles(bad)

    empty = tmp_path / "empty.srt"
    empty.write_text("")
    with pytest.raises(ValueError):
        load_subtitles(empty)


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


# --- JSON transcripts (podcasts/audiobooks) ----------------------------------

def test_load_json_podcast_namespace(tmp_path):
    """Podcast Namespace transcript: startTime/endTime (seconds) + body."""
    j = tmp_path / "ep.json"
    j.write_text(
        '{"version":"1.0.0","segments":['
        '{"speaker":"Host","startTime":1.0,"endTime":4.0,"body":"Welcome back."},'
        '{"speaker":"Guest","startTime":5.0,"endTime":8.5,"body":"Resistance is futile."}'
        ']}'
    )
    cues = load_subtitles(j)
    assert [c.text for c in cues] == ["Welcome back.", "Resistance is futile."]
    assert cues[0].start == pytest.approx(1.0) and cues[0].end == pytest.approx(4.0)
    assert [c.index for c in cues] == [0, 1]


def test_load_json_whisper_segments(tmp_path):
    """Whisper output: start/end (seconds) + text, sorted by start."""
    j = tmp_path / "ep.json"
    j.write_text(
        '{"segments":['
        '{"start":5.0,"end":8.0,"text":"second"},'
        '{"start":1.0,"end":4.0,"text":" first "}'
        ']}'
    )
    cues = load_subtitles(j)
    assert [c.text for c in cues] == ["first", "second"]  # sorted + trimmed
    assert [c.index for c in cues] == [0, 1]


def test_load_json_whispercpp_offsets_milliseconds(tmp_path):
    """whisper.cpp: transcription[].offsets.from/to are milliseconds."""
    j = tmp_path / "ep.json"
    j.write_text(
        '{"transcription":[{"offsets":{"from":1000,"to":4000},"text":"hi there"}]}'
    )
    cues = load_subtitles(j)
    assert len(cues) == 1
    assert cues[0].start == pytest.approx(1.0) and cues[0].end == pytest.approx(4.0)


def test_load_json_timecode_strings_and_bare_list(tmp_path):
    """A bare list of segments with HH:MM:SS.mmm string times."""
    j = tmp_path / "ep.json"
    j.write_text('[{"start":"00:00:01.500","end":"00:00:03.000","text":"hello"}]')
    cues = load_subtitles(j)
    assert cues[0].start == pytest.approx(1.5) and cues[0].end == pytest.approx(3.0)


def test_load_json_unrecognized_raises_valueerror(tmp_path):
    j = tmp_path / "meta.json"
    j.write_text('{"title":"My Podcast","episodes":3}')  # not a transcript
    with pytest.raises(ValueError):
        load_subtitles(j)


def test_load_json_empty_segments_raises_valueerror(tmp_path):
    j = tmp_path / "ep.json"
    j.write_text('{"segments":[]}')
    with pytest.raises(ValueError):
        load_subtitles(j)


def test_find_sidecar_accepts_json(tmp_path):
    audio = tmp_path / "episode.mp3"
    audio.write_bytes(b"")
    j = tmp_path / "episode.json"
    j.write_text('{"segments":[]}')
    assert find_sidecar(audio) == j


def test_find_sidecar_skips_ytdlp_info_json(tmp_path):
    """yt-dlp's *.info.json metadata shares the stem but isn't a transcript."""
    audio = tmp_path / "episode.mp3"
    audio.write_bytes(b"")
    (tmp_path / "episode.info.json").write_text('{"title":"x"}')
    assert find_sidecar(audio) is None


def test_find_sidecar_prefers_srt_over_json(tmp_path):
    audio = tmp_path / "episode.mp3"
    audio.write_bytes(b"")
    srt = tmp_path / "episode.srt"
    srt.write_text("")
    (tmp_path / "episode.json").write_text('{"segments":[]}')
    assert find_sidecar(audio) == srt


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


def test_best_track_skips_commentary_for_the_main_track():
    # The Apollo 13 case: a "Commentary" eng SRT precedes the main eng SRT.
    # The plain dialogue track must win even though it comes later.
    tracks = [
        _trk(0, "eng", title="Commentary"),
        _trk(1, "eng", title="English"),
    ]
    assert best_track(tracks).index == 1


def test_best_track_commentary_loses_even_to_sdh_and_forced():
    tracks = [
        _trk(0, "eng", title="Director's Commentary"),
        _trk(1, "eng", title="Forced"),
        _trk(2, "eng", title="SDH", hearing_impaired=True),
    ]
    assert best_track(tracks).index == 2  # SDH > forced > commentary


def test_best_track_falls_back_to_commentary_when_only_option():
    # If commentary is the only English text track, still pick it over nothing.
    tracks = [_trk(0, "eng", title="Commentary")]
    assert best_track(tracks).index == 0


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


# --- B14: configurable language preference -----------------------------------

def test_best_track_default_prefers_english():
    # Default (no langs) preserves the historical English preference.
    tracks = [_trk(0, "spa"), _trk(1, "eng"), _trk(2, "ger")]
    assert best_track(tracks).index == 1


def test_best_track_honors_preferred_language():
    tracks = [_trk(0, "eng"), _trk(1, "spa"), _trk(2, "ger")]
    assert best_track(tracks, ["spa"]).index == 1
    assert best_track(tracks, "ger").index == 2  # comma-string form
    assert best_track(tracks, ["eng"]).index == 0


def test_best_track_ordered_language_fallback():
    # Prefer Spanish, then English. Spanish present -> Spanish; absent -> English.
    with_spa = [_trk(0, "eng"), _trk(1, "spa")]
    assert best_track(with_spa, ["spa", "eng"]).index == 1
    no_spa = [_trk(0, "eng"), _trk(1, "ger")]
    assert best_track(no_spa, ["spa", "eng"]).index == 0


def test_best_track_full_dialogue_beats_forced_within_preferred_language():
    tracks = [_trk(0, "spa", forced=True), _trk(1, "spa")]
    assert best_track(tracks, ["spa"]).index == 1


def test_best_track_two_letter_and_three_letter_codes_match():
    # "en" preference matches an "eng"-tagged track and vice-versa.
    assert best_track([_trk(0, "ger"), _trk(1, "eng")], ["en"]).index == 1
    assert best_track([_trk(0, "ger"), _trk(1, "en")], ["eng"]).index == 1
