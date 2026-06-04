from pathlib import Path

from clipper.models import Cue
from clipper.search import search
from clipper.subtitles import load_subtitles

FIXTURE = Path(__file__).parent / "fixtures" / "sample.srt"


def cues():
    return load_subtitles(FIXTURE)


def _spans(matches):
    return [(m.cues[0].index, m.cues[-1].index) for m in matches]


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


# --- span-variant collapse -------------------------------------------------

def _repeat_cues():
    # The phrase appears at two separate places (cues 0 and 4); cues 1-3 fill the gap.
    texts = [
        "I'll be back.",
        "Get out of here.",
        "Stay put.",
        "Almost there.",
        "I'll be back!",
    ]
    return [Cue(index=i, start=float(i * 3), end=float(i * 3) + 2, text=t) for i, t in enumerate(texts)]


def test_collapse_removes_overlapping_span_variants():
    results = search("i'll be back", _repeat_cues(), min_score=70)
    spans = _spans(results)
    # Two distinct, non-overlapping regions (cue 0 and cue 4), not the joined windows.
    assert (0, 0) in spans
    assert (4, 4) in spans
    # No two results overlap in cue index.
    for i, (a, b) in enumerate(spans):
        for c, d in spans[i + 1 :]:
            assert b < c or d < a, f"overlap between {(a, b)} and {(c, d)}"


def test_collapse_prefers_tight_single_cue_over_padded_window():
    results = search("i'll be back", _repeat_cues(), min_score=70)
    # The best representative for the first region is the single cue, not a 2-3 cue window.
    first = next(m for m in results if m.cues[0].index == 0)
    assert len(first.cues) == 1


def test_collapse_can_be_disabled():
    collapsed = search("i'll be back", _repeat_cues(), min_score=70)
    expanded = search("i'll be back", _repeat_cues(), min_score=70, collapse_overlapping=False)
    assert len(expanded) > len(collapsed)


def test_collapse_keeps_legitimate_multicaption_phrase():
    # A sentence split across two captions: the joined window should win and survive.
    split = [
        Cue(index=0, start=0.0, end=2.0, text="Come with me if you"),
        Cue(index=1, start=2.0, end=4.0, text="want to live."),
    ]
    results = search("come with me if you want to live", split, min_score=70)
    assert _spans(results)[0] == (0, 1)
