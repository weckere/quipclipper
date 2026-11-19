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

import math
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
    if not text.isascii() and not unicodedata.is_normalized("NFKC", text):
        text = unicodedata.normalize("NFKC", text)
    text = _APOSTROPHE.sub("", text.casefold())
    if text.isascii():  # most lines once a curly apostrophe is gone ("♪" isn't a mark)
        return _NON_WORD.sub(" ", text).strip()
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


# --- the candidate prefilter (an index of where each word is) -----------------------
# A window scores at most PHRASE_MAX * f_beta(coverage) unless every query word is in it
# as written (_Query.score's first bound), and coverage (the query's word weight found,
# each word credited by its similarity) is at most the weight of the query words that
# have any match in the window. So from where each word is, cue by cue, one can tell the
# windows that can't reach min_score without reading their text: an index of the cues'
# words narrows a library to the few windows worth scoring, and the scores, and so the
# results, stay exactly what searching every cue gives.


def coverage_needed(min_score: float) -> float:
    """The least share of the query's word weight a window needs (each word found
    counted whole) for its score's upper bound to reach ``min_score``."""
    if min_score <= 0:
        return 0.0
    if min_score > PHRASE_MAX:
        return 1.0  # only a verbatim line scores more: every word must be there
    # PHRASE_MAX * (1 + B2) c / (B2 + c) >= m  <=>  c >= B2 m / ((1 + B2) PHRASE_MAX - m)
    return _BETA2 * min_score / ((1 + _BETA2) * PHRASE_MAX - min_score)


class Prefilter:
    """What an index of the cues' words needs to find a query's candidate windows.

    ``words`` and ``weights`` are the normalised query's words and their weights;
    :meth:`hits` says which query words a cue word matches (exactly as the scoring
    does); ``need`` is the least total weight of matched query words a window must
    hold to be worth scoring. ``usable`` is False for a query with no words, or in a
    script written without spaces (matched on characters, not words): search every
    cue for those."""

    def __init__(self, query: str, min_score: float) -> None:
        self._q = _Query(query)
        self.words = self._q.words
        self.weights = self._q.weights
        self.usable = bool(self.words) and not self._q.unspaced
        total = sum(self.weights)
        if min_score > PHRASE_MAX:
            need = total
        else:  # rounded down a hair: a float a little under the bound must not cut a window
            need = math.ceil(coverage_needed(min_score) * total * (1 - 1e-6))
        self.need = max(1, need)  # a window with no word matched is never scored

    def hits(self, word: str) -> tuple[tuple[int, float], ...]:
        """``(query word index, similarity)`` for each query word a normalised cue word
        matches: the same word, or (both 3+ letters) one within a typo."""
        return self._q.hits(word)


def cue_words(text: str) -> list[str]:
    """A cue's words as the scoring sees them (what an index of the cues stores)."""
    return _normalize(text).split()


def candidate_regions(masks: list[int], weights: list[int], need: int, max_span: int,
                      n: int) -> list[tuple[int, int]]:
    """The ranges of cue positions (inclusive) whose windows may reach the score.

    ``masks[j]`` has bit ``p`` set when cue ``p`` holds a word matching query word ``j``;
    ``n`` is the number of cues. A window of up to ``max_span`` cues is worth scoring
    only if the weights of the query words it holds add up to ``need``; every such window
    lies inside a window of exactly ``max_span`` cues that also does, so those are the
    ones checked, all at once, as bit sets. Pass the result to :func:`search`."""
    if not n or sum(w for w, m in zip(weights, masks) if m) < need:
        return []
    every = (1 << n) - 1
    # Bit p of reach[j]: the window starting at cue p holds a match for word j.
    reach = []
    for m in masks:
        r = m
        for k in range(1, max_span):
            r |= m >> k
        reach.append(r & every)
    # Add up the weights of the words each window holds, bit-sliced (planes[b] is bit b
    # of every window's sum), then keep the windows whose sum is at least ``need``.
    planes: list[int] = []
    for w, r in zip(weights, reach):
        if not r:
            continue
        level = 0
        while w:
            if w & 1:
                carry, lv = r, level
                while carry:
                    while lv >= len(planes):
                        planes.append(0)
                    planes[lv], carry = planes[lv] ^ carry, planes[lv] & carry
                    lv += 1
            w >>= 1
            level += 1
    greater, equal = 0, every
    for b in range(max(len(planes), need.bit_length()) - 1, -1, -1):
        plane = planes[b] if b < len(planes) else 0
        if need >> b & 1:
            equal &= plane
        else:
            greater |= equal & plane
            equal &= every ^ plane
    starts = greater | equal
    cover = starts
    for k in range(1, max_span):
        cover |= starts << k
    cover &= every
    out = []
    while cover:
        lo = (cover & -cover).bit_length() - 1
        run = cover >> lo
        length = (~run & (run + 1)).bit_length() - 1
        out.append((lo, lo + length - 1))
        cover &= ~(((1 << length) - 1) << lo)
    return out


def search(
    query: str | Prefilter,
    cues: list[Cue],
    *,
    limit: int = 10,
    min_score: float = 60.0,
    max_span: int = 3,
    collapse_overlapping: bool = True,
    regions: list[tuple[int, int]] | None = None,
) -> list[Match]:
    """Return ranked, non-overlapping matches for `query` across the cues.

    The window scan produces many overlapping span-variants of the same line (the
    line alone, plus windows that join it with its neighbours). By default these
    are collapsed: results never overlap, keeping the best-scoring, tightest
    representative of each region — so a recurring phrase yields one candidate per
    place it occurs rather than several near-duplicates.

    Args:
        query: dialogue text to find, or a :class:`Prefilter` made of it (a search over
            many files then works out each word's matches once, not once a file).
        cues: subtitle cues to search.
        limit: maximum number of matches to return.
        min_score: drop matches scoring below this (0-100).
        max_span: how many consecutive cues a single match may join (>=1).
        collapse_overlapping: drop matches that overlap a higher-ranked one.
        regions: only windows lying wholly inside one of these ``(first, last)`` ranges
            of positions in ``cues`` (inclusive, disjoint, in order) are scored. From
            :func:`candidate_regions`, which leaves out only windows that can't reach
            ``min_score``, the result is the same as searching every cue.
    """
    q = query._q if isinstance(query, Prefilter) else _Query(query)
    if not q.words:
        return []
    max_span = max(1, max_span)

    # Normalise each cue once; a window reuses its cues' words and matches. Only the
    # cues in a region are read (the others stay None: no window reaches them).
    memo, hits = q._memo, q.hits
    if regions is None:
        regions = [(0, len(cues) - 1)]
    prepared: list = [None] * len(cues)
    for k in (k for lo, hi in regions for k in range(max(lo, 0), min(hi, len(cues) - 1) + 1)):
        cue = cues[k]
        norm = _normalize(cue.text)
        words = norm.split()
        found = []
        for p, w in enumerate(words):
            h = memo.get(w)
            if h is None:
                h = hits(w)
            if h:
                found.append((p, h))
        prepared[k] = (norm, [len(w) + 1 for w in words], found)

    candidates: list[Match] = []
    starts = ((start, min(hi, len(cues) - 1)) for lo, hi in regions
              for start in range(max(lo, 0), min(hi, len(cues) - 1) + 1))
    for start, last in starts:
        norms: list[str] = []
        weights: list[int] = []
        found: list = []
        for span in range(max_span):
            i = start + span
            if i > last:
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
