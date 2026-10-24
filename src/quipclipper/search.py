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
- **up to 75** for the query's words present in any order ("So I make it."), less
  the more other words lie between them, so word soup spread over a window of
  captions falls below the default cut.

A word the query gives twice ("no no") needs to be in the line twice.

The in-order score aligns the query's words with the line's, matching each word
exactly or within a typo, and rates the best aligned run with a recall-weighted
F-measure: a missing query word costs more than an extra word in the line.
"""

from __future__ import annotations

import re
import unicodedata
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

# Tags only ("<i>", "</font>"): a bare "<" in "x < 3 … y > 2" isn't markup.
_MARKUP = re.compile(r"</?[A-Za-z][^<>]*>|\{[^}]*\}")
_APOSTROPHE = re.compile(r"['’‘ʼ`´]")
_NON_WORD = re.compile(r"[\W_]+")
_NON_WORD_ANY: list[re.Pattern[str]] = []  # the same, but combining marks are word parts
# Scripts written without spaces between words: there a "word" can't be told apart,
# so these queries match on characters instead.
_UNSPACED = re.compile(r"[฀-๿぀-ヿ㐀-䶿一-鿿"
                       r"가-힯豈-﫿]")


def _non_word_any() -> re.Pattern[str]:
    """``[\\W_]+`` that keeps combining marks (Unicode category M) inside words: ``\\w``
    leaves them out, which split Devanagari, Thai or pointed Arabic words at every vowel
    sign ("नमस्ते" became "नमस त", and "मैं" and "में" both "म"). Built on first need."""
    if not _NON_WORD_ANY:
        ranges: list[tuple[int, int]] = []
        for cp in (*range(0x30000), *range(0xE0000, 0xE0200)):
            if unicodedata.category(chr(cp))[0] == "M":
                if ranges and ranges[-1][1] == cp - 1:
                    ranges[-1] = (ranges[-1][0], cp)
                else:
                    ranges.append((cp, cp))
        marks = "".join(re.escape(chr(a)) + ("-" + re.escape(chr(b)) if b != a else "")
                        for a, b in ranges)
        _NON_WORD_ANY.append(re.compile(rf"(?:[^\w{marks}]|_)+"))
    return _NON_WORD_ANY[0]


def _normalize(text: str) -> str:
    """Lowercase words separated by single spaces: markup dropped, apostrophes
    removed ("I'll" -> "ill"), every other non-word character a separator. Text beyond
    ASCII is NFKC-normalised first, so a precomposed "é" and an "e" with a combining
    accent are one letter, and full-width "ｈｅｌｌｏ" is "hello"."""
    if "<" in text or "{" in text:
        text = _MARKUP.sub(" ", text)
    if text.isascii():
        return _NON_WORD.sub(" ", _APOSTROPHE.sub("", text.casefold())).strip()
    text = _APOSTROPHE.sub("", unicodedata.normalize("NFKC", text).casefold())
    if "\u0307" in text:  # casefolded Turkish "İ" is "i" + a dot above: just "i"
        text = text.replace("i\u0307", "i")
    return _non_word_any().sub(" ", text).strip()


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
        # Words the query gives more than once -> their positions in it ("no no no").
        at: dict[str, list[int]] = {}
        for j, w in enumerate(self.words):
            at.setdefault(w, []).append(j)
        self._repeated = {w: js for w, js in at.items() if len(js) > 1}

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
        if self._repeated:
            # A word the query repeats is covered once per time the line has it: one
            # "no" in the line covered all of "no no no", scoring 75 on "No.".
            got: dict[str, list[float]] = {}
            for _, hits in found:
                for j, s in hits:
                    js = self._repeated.get(self.words[j])
                    if js is not None and j == js[0]:
                        got.setdefault(self.words[j], []).append(s)
            for w, js in self._repeated.items():
                sims = sorted(got.get(w, ()), reverse=True)
                for k, j in enumerate(js):
                    best[j] = sims[k] if k < len(sims) else 0.0
        matched = sum(w * s for w, s in zip(self.weights, best))
        coverage = matched / self.total
        # The best any run could do: every word found matched, in order, no extras.
        exact = min(best) == 1.0  # every query word is in the window as written
        bound = VERBATIM if exact else PHRASE_MAX * _f_beta(coverage, 1.0, coverage)
        if bound < min_score:
            return None
        text = " ".join(n for n in norms if n)  # a cue of only "♪" leaves no gap
        if exact and f" {self.text} " in f" {text} ":
            return VERBATIM, text
        # In any order: the words found, over the words from the first to the last of
        # them, so a match spread through a window of other words scores less.
        span = sum(weights[found[0][0]:found[-1][0] + 1])
        any_order = ANY_ORDER_MAX * _f_beta(matched, self.total, span)
        return max(any_order, PHRASE_MAX * self.in_order(found, weights)), text

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
    memo, hits = q._memo, q.hits
    prepared = []
    for cue in cues:
        norm = _normalize(cue.text)
        words = norm.split()
        found = []
        for p, w in enumerate(words):
            h = memo.get(w)
            if h is None:
                h = hits(w)
            if h:
                found.append((p, h))
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
