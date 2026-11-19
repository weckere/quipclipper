"""The candidate prefilter (M27): narrowing the cues to score never changes a result.

A whole-library line search scores only the windows of cues an index of the cues' words
says could reach ``min_score`` (``Prefilter``, ``candidate_regions``, ``search(regions=)``).
These tests hold it to the promise that matters: on a corpus of lines, for queries typed
right, misheard (one wrong word, a word missing, a word extra, a typo) and in pieces, the
results are the same lines in the same order as scoring every cue. Seeded, so the corpus
is the same on every run.
"""

from __future__ import annotations

import random

import pytest

from quipclipper.models import Cue
from quipclipper.search import (
    PHRASE_MAX, Prefilter, _f_beta, candidate_regions, coverage_needed, cue_words, search,
)

LINES = [
    "You're gonna need a bigger boat.",
    "May the Force be with you.",
    "I'll be back.",
    "Here's looking at you, kid.",
    "You can't handle the truth!",
    "Houston, we have a problem.",
    "You talking to me?",
    "Make it so, Number One.",
    "Winter is coming.",
    "No, no, no, no!",
    "Elementary, my dear Watson.",
    "I am your father.",
    "Frankly, my dear, I don't give a damn.",
    "There's no place like home.",
]
FILLER = ("the a you i to and it of that is in what we me my on no have your be so just "
          "know do not all was but like with dont oh yeah right get boat force back kid "
          "truth problem home dear father coming need bigger handle looking talking").split()

QUERIES = [
    # typed right
    "you're gonna need a bigger boat", "may the force be with you", "i'll be back",
    "here's looking at you kid", "you can't handle the truth", "houston we have a problem",
    "make it so", "winter is coming", "no no no", "i am your father",
    # one word misheard
    "you're gonna need a bigger goat", "may the horse be with you", "i'll be black",
    "here's cooking at you kid", "you can't candle the truth", "austin we have a problem",
    # a word missing, a word extra, a typo
    "you're gonna need bigger boat", "may the force be with you all", "elementary my deer watson",
    "frankly my dear i dont give a damn", "theres no place like home", "you talkin to me",
    # short, common, and a single word
    "what do you want", "i love you", "no", "boat", "dear",
]


def _corpus(seed: int, n: int = 400) -> list[Cue]:
    rnd = random.Random(seed)
    cues, t = [], 0.0
    for i in range(n):
        if rnd.random() < 0.12:
            line = rnd.choice(LINES)
            if rnd.random() < 0.3:  # a line split over two captions
                words = line.split()
                cut = rnd.randint(1, max(1, len(words) - 1))
                for part in (" ".join(words[:cut]), " ".join(words[cut:])):
                    if part:
                        cues.append(Cue(index=len(cues) + 1, start=t, end=t + 1.5, text=part))
                        t += 2
                continue
            text = line
        else:
            text = " ".join(rnd.choice(FILLER) for _ in range(rnd.randint(1, 9)))
            if rnd.random() < 0.2:
                text = f"<i>{text.capitalize()}</i>"
        cues.append(Cue(index=len(cues) + 1, start=t, end=t + 1.5, text=text))
        t += 2
    return cues


def _regions(query: str, cues: list[Cue], min_score: float, max_span: int):
    pf = Prefilter(query, min_score)
    masks = [0] * len(pf.words)
    for p, cue in enumerate(cues):
        for w in cue_words(cue.text):
            for j, _ in pf.hits(w):
                masks[j] |= 1 << p
    return candidate_regions(masks, pf.weights, pf.need, max_span, len(cues))


def _key(matches):
    return [(m.score, m.fit, m.start, m.end, m.text, m.machine) for m in matches]


@pytest.mark.parametrize("seed", [1, 2, 3])
@pytest.mark.parametrize("min_score,max_span", [(60.0, 3), (0.0, 3), (45.0, 1), (80.0, 5),
                                                (99.5, 3)])
def test_regions_keep_every_result(seed, min_score, max_span):
    cues = _corpus(seed)
    for q in QUERIES:
        full = search(q, cues, limit=1000, min_score=min_score, max_span=max_span)
        regions = _regions(q, cues, min_score, max_span)
        part = (search(q, cues, limit=1000, min_score=min_score, max_span=max_span,
                       regions=regions) if regions else [])
        assert _key(part) == _key(full), q
        # The per-file cut (limit) keeps the same top lines too.
        assert _key(search(q, cues, limit=5, min_score=min_score, max_span=max_span,
                           regions=regions or [(0, -1)])) == _key(full[:5]), q


def test_the_prefilter_narrows():
    cues = _corpus(7, n=2000)
    regions = _regions("you're gonna need a bigger boat", cues, 60.0, 3)
    scored = sum(b - a + 1 for a, b in regions)
    assert 0 < scored < len(cues) // 4


def test_coverage_needed_is_the_bound():
    for m in (1.0, 30.0, 60.0, 75.0, 90.0, 98.9):
        c = coverage_needed(m)
        assert PHRASE_MAX * _f_beta(c, 1.0, c) == pytest.approx(m)
        assert PHRASE_MAX * _f_beta(c * 0.999, 1.0, c * 0.999) < m
    assert coverage_needed(0) == 0.0
    assert coverage_needed(99.5) == 1.0


def test_prefilter_need():
    pf = Prefilter("make it so", 60.0)
    assert pf.words == ["make", "it", "so"] and pf.weights == [5, 3, 3]
    assert pf.need == 7  # 0.5517 of 11, rounded up
    assert Prefilter("make it so", 100.0).need == 11
    assert Prefilter("make it so", 0.0).need == 1
    assert not Prefilter("!!!", 60.0).usable
    assert not Prefilter("こんにちは", 60.0).usable  # matched on characters: no prefilter


def test_candidate_regions_against_brute_force():
    rnd = random.Random(11)
    for _ in range(300):
        n = rnd.randint(1, 40)
        words = rnd.randint(1, 6)
        weights = [rnd.randint(2, 9) for _ in range(words)]
        masks = [sum(1 << p for p in range(n) if rnd.random() < 0.15) for _ in range(words)]
        span = rnd.randint(1, 4)
        need = rnd.randint(1, sum(weights))
        want = [False] * n
        for a in range(n):
            for b in range(a, min(n, a + span)):
                got = sum(w for w, m in zip(weights, masks) if m >> a << a & ((1 << (b + 1)) - 1))
                if got >= need:
                    for p in range(a, b + 1):
                        want[p] = True
        have = [False] * n
        for a, b in candidate_regions(masks, weights, need, span, n):
            for p in range(a, b + 1):
                assert not have[p]
                have[p] = True
        # Every window that holds enough is covered; anything more is a whole window of
        # `span` cues that holds enough (so still only windows that may score).
        assert all(h or not w for h, w in zip(have, want))
        for p in range(n):
            if have[p] and not want[p]:
                assert any(
                    sum(w for w, m in zip(weights, masks)
                        if m >> a << a & ((1 << (a + span)) - 1)) >= need
                    for a in range(max(0, p - span + 1), p + 1))


def test_regions_bound_the_windows():
    cues = [Cue(index=i + 1, start=i * 2.0, end=i * 2.0 + 1, text=t)
            for i, t in enumerate(["Make it", "so.", "Elsewhere.", "Make it so."])]
    # Only the last cue's region: the split line in cues 0-1 is out of it.
    got = search("make it so", cues, regions=[(3, 3)])
    assert [m.text for m in got] == ["Make it so."]
    got = search("make it so", cues, regions=[(0, 1), (3, 3)])
    assert sorted(m.start for m in got) == [0.0, 6.0]  # the split line, and the last cue
