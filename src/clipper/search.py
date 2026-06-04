"""Fuzzy, ranked dialogue search over subtitle cues.

Matching is case-insensitive and tolerant of typos. Because a spoken line is
often split across two or three consecutive captions, we search not only single
cues but also sliding windows of consecutive cues joined together, so a phrase
that spans captions still matches as one hit.
"""

from __future__ import annotations

from rapidfuzz import fuzz

from clipper.models import Cue, Match


def _normalize(text: str) -> str:
    return " ".join(text.lower().split())


def _score(query: str, candidate: str) -> float:
    """Score how well `query` appears within `candidate`.

    `partial_ratio` finds the best-matching substring, so a short query still
    scores high against a longer caption that contains it. `token_set_ratio`
    rewards matches where word order differs or extra words are present. We take
    the max so either kind of match surfaces.
    """
    return max(
        fuzz.partial_ratio(query, candidate),
        fuzz.token_set_ratio(query, candidate),
    )


def search(
    query: str,
    cues: list[Cue],
    *,
    limit: int = 10,
    min_score: float = 60.0,
    max_span: int = 3,
) -> list[Match]:
    """Return ranked matches for `query` across the cues.

    Args:
        query: dialogue text to find.
        cues: subtitle cues to search.
        limit: maximum number of matches to return.
        min_score: drop matches scoring below this (0-100).
        max_span: how many consecutive cues a single match may join (>=1).
    """
    q = _normalize(query)
    if not q:
        return []
    max_span = max(1, max_span)

    candidates: list[Match] = []
    for start in range(len(cues)):
        joined = ""
        for span in range(max_span):
            i = start + span
            if i >= len(cues):
                break
            joined = cues[i].text if span == 0 else f"{joined} {cues[i].text}"
            score = _score(q, _normalize(joined))
            window = tuple(cues[start : i + 1])
            candidates.append(Match(score=score, cues=window, text=joined))

    # Rank by score, then prefer the tighter span and earlier position so a
    # single-cue exact hit beats a sprawling multi-cue near-match of equal score.
    candidates.sort(key=lambda m: (-m.score, len(m.cues), m.start))

    results: list[Match] = []
    seen_starts: set[int] = set()
    for m in candidates:
        if m.score < min_score:
            break
        # Collapse overlapping windows that begin at the same cue.
        if m.index in seen_starts:
            continue
        seen_starts.add(m.index)
        results.append(m)
        if len(results) >= limit:
            break
    return results
