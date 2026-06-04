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
    collapse_overlapping: bool = True,
) -> list[Match]:
    """Return ranked, non-overlapping matches for `query` across the cues.

    The window scan produces many overlapping span-variants of the same line (the
    line alone, plus windows that join it with its neighbours). By default these
    are collapsed: results never overlap, keeping the best-scoring, tightest
    representative of each region — so a recurring phrase yields one candidate per
    place it occurs rather than several near-duplicates.

    Args:
        query: dialogue text to find.
        cues: subtitle cues to search.
        limit: maximum number of matches to return.
        min_score: drop matches scoring below this (0-100).
        max_span: how many consecutive cues a single match may join (>=1).
        collapse_overlapping: drop matches that overlap a higher-ranked one.
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

    # Rank by score; then, among equal-scoring span-variants, prefer the window
    # whose full text best matches the whole query (fuzz.ratio penalises both
    # missing and extra words). This keeps a single-cue hit for a one-line query
    # but the joined window for a sentence split across captions. Tighter span and
    # earlier position break any remaining ties.
    candidates.sort(
        key=lambda m: (-m.score, -fuzz.ratio(q, _normalize(m.text)), len(m.cues), m.start)
    )

    results: list[Match] = []
    accepted: list[tuple[int, int]] = []  # cue-index spans already taken
    for m in candidates:
        if m.score < min_score:
            break
        lo, hi = m.cues[0].index, m.cues[-1].index
        # Skip a match that overlaps an already-accepted (higher-ranked) region.
        if collapse_overlapping and any(lo <= b and a <= hi for a, b in accepted):
            continue
        elif not collapse_overlapping and any(lo == a for a, _ in accepted):
            # Without collapsing, still drop exact duplicate start cues.
            continue
        accepted.append((lo, hi))
        results.append(m)
        if len(results) >= limit:
            break
    return results
