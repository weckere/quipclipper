from pathlib import Path

import pytest

from clipper.subtitles import find_sidecar, load_subtitles

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
