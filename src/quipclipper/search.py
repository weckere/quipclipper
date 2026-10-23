"""Fuzzy, ranked dialogue search over subtitle cues.

Matching is case-insensitive and tolerant of typos. Because a spoken line is
often split across two or three consecutive captions, we search not only single
cues but also sliding windows of consecutive cues joined together, so a phrase
that spans captions still matches as one hit.

Scores (D-041), after both sides are normalised (case, punctuation, apostrophes,
whitespace and subtitle markup such as ``<i>``, ``{\\an8}``, ``♪`` and speaker
dashes dropped):

- **100** only when the query's words appear verbatim in the line: in order, as
  one contiguous run of whole words ("Make it so, Number One." for "make it so").
- **up to 99** for the query's words in order with small differences: a typo, a
  word missing or an extra word in between ("Make it all so.").
- **up to 75** for the query's words present in any order ("So I make it.").

The in-order score aligns the query's words with the line's, matching each word
exactly or within a typo, and rates the best aligned run with a recall-weighted
F-measure: a missing query word costs more than an extra word in the line.
"""

from __future__ import annotations

import re
from itertools import accumulate

from rapidfuzz import fuzz

from quipclipper.models import Cue, Match

VERBATIM = 100.0
PHRASE_MAX = 99.0  # the best a match that isn't verbatim can score
ANY_ORDER_MAX = 75.0  # every query word present, in no particular order
# Two words of 3+ letters this similar (fuzz.ratio) count as one word misspelled
# or misheard, credited by their similarity; shorter words must match exactly.
_TYPO_CUTOFF = 80.0
_BETA2 = 4.0  # F-beta with beta 2: recall (query words found) weighs 4x precision

_MARKUP = re.compile(r"<[^>]*>|\{[^}]*\}")
_APOSTROPHE = re.compile(r"['’‘ʼ`´]")
_NON_WORD = re.compile(r"[\W_]+")
# Scripts written without spaces between words: there a "word" can't be told apart,
# so these queries match on characters instead.
_UNSPACED = re.compile(r"[฀-๿぀-ヿ㐀-䶿一-鿿"
                       r"가-힯豈-﫿]")


def _normalize(text: str) -> str:
    """Lowercase words separated by single spaces: markup dropped, apostrophes
    removed ("I'll" -> "ill"), every other non-word character a separator."""
    text = _APOSTROPHE.sub("", _MARKUP.sub(" ", text).casefold())
    return _NON_WORD.sub(" ", text).strip()


def _f_beta(matched: float, query_weight: float, window_weight: float) -> float:
    recall = matched / query_weight
    precision = min(1.0, matched / window_weight)
    return (1 + _BETA2) * precision * recall / (_BETA2 * precision + recall)


class _Query:
    """A normalised query and the per-word lookups the window scoring needs."""

    def __init__(self, query: str) -> None:
        self.text = _normalize(query)
        self.words = self.text.split()
        self.weights = [len(w) + 1 for w in self.words]
        self.total = float(sum(self.weights))
        self.unspaced = bool(_UNSPACED.search(self.text))
        self._memo: dict[str, tuple[tuple[int, float], ...]] = {}

    def hits(self, word: str) -> tuple[tuple[int, float], ...]:
        """``(query word index, similarity 0-1)`` for each query word ``word`` matches."""
        found = self._memo.get(word)
        if found is None:
            out = []
            for j, q in enumerate(self.words):
                if word == q:
                    out.append((j, 1.0))
                elif len(word) >= 3 and len(q) >= 3:
                    r = fuzz.ratio(word, q, score_cutoff=_TYPO_CUTOFF)
                    if r:
                        out.append((j, r / 100.0))
            found = self._memo[word] = tuple(out)
        return found

    def in_order(self, found: list, weights: list[int]) -> float:
        """Best F-beta over runs of the window's words that match query words in order.

        ``found`` lists ``(word position, hits)`` for the window's matching words, by
        position. A run's precision is the matched weight over the weight of the
        window's words from its first to its last match (so extra words in between
        cost); its recall is the matched weight over the whole query's."""
        cum = list(accumulate(weights, initial=0))
        n = len(self.words)
        best = 0.0
        for a, (pa, ha) in enumerate(found):
            for ja, sa in ha:
                dp = [0.0] * n  # best matched weight of a run from a, ending on query word j
                dp[ja] = top = sa * self.weights[ja]
                best = max(best, _f_beta(top, self.total, weights[pa]))
                for pb, hb in found[a + 1:]:
                    updates = []
                    for jb, sb in hb:
                        prev = max(dp[:jb], default=0.0)
                        if prev > 0.0 and prev + sb * self.weights[jb] > dp[jb]:
                            updates.append((jb, prev + sb * self.weights[jb]))
                    for jb, v in updates:
                        dp[jb] = v
                    if updates and max(dp) > top:
                        top = max(dp)
                        best = max(best, _f_beta(top, self.total, cum[pb + 1] - cum[pa]))
        return best

    def score(self, found: list, weights: list[int], norms: list[str],
              min_score: float) -> tuple[float, str] | None:
        """A window's score and its normalised text, or None when it can't reach
        ``min_score`` (checked on cheap upper bounds before any alignment)."""
        best = [0.0] * len(self.words)
        for _, hits in found:
            for j, s in hits:
                if s > best[j]:
                    best[j] = s
        coverage = sum(w * s for w, s in zip(self.weights, best)) / self.total
        # The best any run could do: every word found matched, in order, no extras.
        exact = min(best) == 1.0  # every query word is in the window as written
        bound = VERBATIM if exact else max(PHRASE_MAX * _f_beta(coverage, 1.0, coverage),
                                           ANY_ORDER_MAX * coverage)
        if bound < min_score:
            return None
        text = " ".join(norms)
        if exact and f" {self.text} " in f" {text} ":
            return VERBATIM, text
        return max(ANY_ORDER_MAX * coverage, PHRASE_MAX * self.in_order(found, weights)), text

    def score_unspaced(self, text: str, min_score: float) -> float:
        """Character matching for scripts without spaces between words."""
        q, c = self.text.replace(" ", ""), text.replace(" ", "")
        if q in c:
            return VERBATIM
        scorer = fuzz.partial_ratio if len(c) >= len(q) else fuzz.ratio
        return min(PHRASE_MAX, scorer(q, c, score_cutoff=min_score))


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
    q = _Query(query)
    if not q.words:
        return []
    max_span = max(1, max_span)

    # Normalise each cue once; a window reuses its cues' words and matches.
    prepared = []
    for cue in cues:
        norm = _normalize(cue.text)
        words = norm.split()
        found = [(p, h) for p, w in enumerate(words) if (h := q.hits(w))]
        prepared.append((norm, [len(w) + 1 for w in words], found))

    candidates: list[Match] = []
    for start in range(len(cues)):
        norms: list[str] = []
        weights: list[int] = []
        found: list = []
        for span in range(max_span):
            i = start + span
            if i >= len(cues):
                break
            norm, w, f = prepared[i]
            if f:
                found.extend((p + len(weights), h) for p, h in f)
            norms.append(norm)
            weights.extend(w)
            if q.unspaced:
                text = " ".join(norms)
                score = q.score_unspaced(text, min_score)
                if score < min_score:
                    continue
            else:
                if not found:
                    continue
                scored = q.score(found, weights, norms, min_score)
                if scored is None or scored[0] < min_score:
                    continue
                score, text = scored
            window = tuple(cues[start : i + 1])
            joined = " ".join(c.text for c in window)
            candidates.append(Match(score=score, cues=window, text=joined,
                                    fit=fuzz.ratio(q.text, text)))

    # Rank by score; at an equal score a subtitle line beats a machine transcript
    # line (M21: a tiebreak, never a penalty, so a real line is never pushed down).
    # Then, among equal-scoring span-variants, prefer the window whose full text
    # best matches the whole query (fit: fuzz.ratio penalises both missing and
    # extra words). This keeps a single-cue hit for a one-line query but the joined
    # window for a sentence split across captions, and puts "Make it so." above
    # "Make it so, Number One." Tighter span and earlier position break any
    # remaining ties.
    candidates.sort(key=lambda m: (-m.score, m.machine, -m.fit, len(m.cues), m.start))

    results: list[Match] = []
    accepted: list[tuple[int, int]] = []  # cue-index spans already taken
    for m in candidates:
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
