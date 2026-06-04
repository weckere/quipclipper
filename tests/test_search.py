from pathlib import Path

from clipper.search import search
from clipper.subtitles import load_subtitles

FIXTURE = Path(__file__).parent / "fixtures" / "sample.srt"


def cues():
    return load_subtitles(FIXTURE)


def test_exact_match_ranks_first():
    results = search("I'll be back", cues())
    assert results
    assert results[0].text == "I'll be back."
    assert results[0].score >= 90


def test_case_insensitive():
    results = search("HASTA LA VISTA", cues())
    assert results[0].text == "Hasta la vista, baby."


def test_fuzzy_tolerates_typo():
    results = search("hasta la vsta baby", cues())  # missing an 'i'
    assert results[0].text == "Hasta la vista, baby."
    assert results[0].score >= 80


def test_partial_phrase_matches():
    results = search("want to live", cues())
    assert results[0].text.endswith("want to live.")


def test_multiline_phrase_spanning_is_found():
    # The full line lives in a single caption here, but ensure the joined
    # search still surfaces it when querying the whole sentence.
    results = search("come with me if you want to live", cues())
    assert "want to live" in results[0].text.lower()


def test_no_match_below_threshold_is_dropped():
    results = search("completely unrelated dialogue xyzzy", cues(), min_score=80)
    assert results == []


def test_limit_respected():
    results = search("the", cues(), limit=2, min_score=0)
    assert len(results) <= 2


def test_match_timestamps_point_at_cue():
    results = search("not a tumor", cues())
    top = results[0]
    assert top.start == 20.0
    assert top.end == 22.0
