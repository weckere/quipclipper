from pathlib import Path

from quipclipper.models import Cue
from quipclipper.search import search
from quipclipper.subtitles import load_subtitles

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


# --- ranking: 100 only for a verbatim phrase (D-041) ------------------------

def _lines(*texts, machine=()):
    return [Cue(index=i, start=float(i * 10), end=float(i * 10) + 2, text=t,
                source="machine" if i in machine else None)
            for i, t in enumerate(texts)]


def _score_of(query, text):
    hits = search(query, _lines(text), min_score=0)
    return hits[0].score if hits else 0.0


def test_verbatim_scores_100():
    assert _score_of("make it so", "Make it so.") == 100
    assert _score_of("make it so", "Make it so, Number One.") == 100
    assert _score_of("make it so", "- I think so, sir. - Then make it so.") == 100


def test_case_punctuation_and_markup_are_still_verbatim():
    for text in ("MAKE IT SO!", "<i>Make it so.</i>", "- Make it so.", "♪ make it so ♪",
                 "Make... it... so", "{\\an8}Make it so.", "make  it\nso"):
        assert _score_of("make it so", text) == 100, text
    # Straight and curly apostrophes, either side.
    assert _score_of("i'll be back", "I’ll be back.") == 100
    assert _score_of("I’ll be back", "i'll be back") == 100


def test_words_out_of_order_score_well_below_100():
    soup = _score_of("make it so", "So I make it.")
    assert 0 < soup <= 80


def test_extra_word_between_scores_below_100_but_above_word_soup():
    extra = _score_of("make it so", "Make it all so.")
    assert 80 < extra < 100
    assert extra > _score_of("make it so", "So I make it.")


def test_word_inside_a_longer_word_is_not_verbatim():
    # "make it so" is a substring of "make it sound", but "so" isn't a word there.
    assert _score_of("make it so", "You make it sound very complex.") < 100


def test_near_miss_typo_is_found_below_100():
    typo = _score_of("mak it so", "Make it so.")
    assert 85 <= typo < 100
    misheard = _score_of("make it so", "Make it sew.")  # 'so' is too short to fuzz
    assert misheard < typo


def test_a_short_line_inside_a_long_query_is_not_a_match():
    # partial_ratio used to score "HOT" 100 for "tea earl grey hot".
    assert search("tea earl grey hot", _lines("HOT")) == []
    assert _score_of("what is going on", "Go") < 60


def test_verbatim_ranks_first_then_in_order_then_any_order():
    cues = _lines(
        "So, I make it.",               # any order
        "You make it sound easy.",      # partial phrase
        "Make it so, Number One.",      # verbatim, longer line
        "Make it all so.",              # an extra word
        "Make it so.",                  # verbatim, the whole line
    )
    texts = [m.text for m in search("make it so", cues, min_score=0)]
    assert texts[:3] == ["Make it so.", "Make it so, Number One.", "Make it all so."]
    assert set(texts[3:]) == {"So, I make it.", "You make it sound easy."}


def test_only_verbatim_reaches_100_and_scores_never_round_up_to_it():
    cues = _lines("Make it so.", "Make it sew.", "Make it all so.", "So make it.",
                  "Make it soo.", "make it so")
    for m in search("make it so", cues, min_score=0, collapse_overlapping=False):
        verbatim = m.text.lower().replace(".", "").strip() == "make it so"
        assert (m.score == 100) == verbatim or len(m.cues) > 1
        assert m.score == 100 or round(m.score, 1) <= 99


def test_equal_scores_keep_one_order_and_real_lines_beat_machine_lines():
    cues = _lines("Make it so.", "Engage.", "Make it so.", machine=(0,))
    first = search("make it so", cues)
    assert [m.cues[0].index for m in first] == [2, 0]  # the subtitle line first
    assert first[1].machine and not first[0].machine
    assert [m.cues[0].index for m in search("make it so", cues)] == [2, 0]


def test_single_word_query():
    cues = _lines("Engage.", "Bridge, engage engines.", "They're engaged.", "Engagement")
    hits = search("engage", cues)
    assert [m.text for m in hits[:2]] == ["Engage.", "Bridge, engage engines."]
    assert hits[0].score == hits[1].score == 100
    engaged = next(m for m in hits if m.text == "They're engaged.")
    assert 80 < engaged.score < 100


def test_phrase_split_across_captions_is_verbatim():
    split = [
        Cue(index=0, start=0.0, end=2.0, text="Come with me if you"),
        Cue(index=1, start=2.0, end=4.0, text="want to live."),
    ]
    top = search("come with me if you want to live", split)[0]
    assert top.score == 100 and len(top.cues) == 2


def test_min_score_still_filters():
    cues = _lines("Make it so.", "So I make it.", "It is so.")
    assert [m.text for m in search("make it so", cues, min_score=100)] == ["Make it so."]
    assert "It is so." not in [m.text for m in search("make it so", cues)]


def test_unspaced_scripts_match_on_characters():
    cues = _lines("東京に行きます。", "大阪に行きます。")
    hits = search("東京に行", cues)
    assert hits[0].text == "東京に行きます。" and hits[0].score == 100
    assert all(m.score < 100 for m in hits[1:])


# --- release review 2 (t-m20-review-2) -----------------------------------------------

def _scores(query, *texts, **kw):
    return [(round(m.score, 1), m.text) for m in search(query, _lines(*texts), **kw)]


def test_a_repeated_query_word_needs_repeating_in_the_line():
    # SR-1: one "no" in the line covered all of "no no no" (75 on "No.").
    assert _scores("no no no", "No.") == []
    assert _scores("no no", "No, no.") == [(100.0, "No, no.")]
    assert _scores("make make make", "make") == []


def test_words_in_any_order_spread_over_other_words_score_less():
    # SR-1: three cues of 30 words holding "so", "make" and "it" scored 75.
    soup = ("So, what do you want to do today, Number One?",
            "I do not know, Captain, maybe make a cake or bake bread?",
            "It is very late already.")
    assert _scores("make it so", *soup) == []
    assert all(score < 60 for score, _ in _scores("make it so", *soup, min_score=0))
    assert 60 <= _scores("make it so", "It was so hard to make.")[0][0] <= 75


def test_unicode_text_is_normalised_and_marks_stay_in_words():
    # SR-4: NFD vs NFC never matched; combining marks split words, so "मैं" was "में".
    nfc, nfd = "café", "café"
    assert _scores(nfc, nfd)[0][0] == 100 and _scores(nfd, nfc)[0][0] == 100
    assert _scores("मैं", "में") == []
    assert _scores("नमस्ते दोस्त", "नमस्ते दोस्त!")[0][0] == 100
    assert _scores("hello", "ｈｅｌｌｏ")[0][0] == 100  # full width
    assert _scores("istanbul", "İstanbul")[0][0] == 100


def test_an_empty_cue_between_does_not_break_a_verbatim_run():
    # SR-7: "♪" or "{\an8}" alone left a double space, so 99 instead of 100.
    assert _scores("make it so", "Make it", "♪", "so.")[0][0] == 100
    assert _scores("make it so", "Make it", "{\\an8}", "so.")[0][0] == 100


def test_a_bare_less_than_is_not_markup():
    # SR-8: "<" … ">" ate the text between them.
    assert _scores("is less", "x < 3 is less and y > 2")[0][0] == 100
    assert _scores("make it so", "<i>Make it so.</i>")[0][0] == 100
