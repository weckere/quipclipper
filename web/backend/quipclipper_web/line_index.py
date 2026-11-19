"""Where each word is in the subtitle cache: a fast library-wide line search (M27).

A folder or library dialogue search used to score every cue of every indexed file: on a
home library (~32k files, ~20M cues) that took minutes. This index keeps, for each
cache entry the search reads (a file's auto-selected subtitles) and each word in it, the
cues that hold the word. A search looks up the query's words, and the words within a
typo of them, and from where they are works out the few windows of cues that could
reach the minimum score (:func:`quipclipper.search.candidate_regions`); only those are
read and scored. The bound leaves out only windows that can't score enough, so the
results are the same as scoring every cue.

- **One SQLite file** beside the cache (``line_index.sqlite`` in the state dir). Python's
  own ``sqlite3``, no extension. ``posting`` is clustered by word, so a word's entries
  are one range read; a word's cues are a bit set where the word is common in the entry
  and a list of cue positions where it is rare.
- **Kept current by the cache:** :class:`~quipclipper_web.sub_cache.SubtitleCache` adds
  every entry it writes for the auto-selected subtitles and removes every entry it
  deletes (re-indexing a file writes a new entry and sweeps the old one).
- **Built for an existing cache without extracting anything:** :meth:`LineIndex.backfill`
  reads the entries already on disk, one at a time, on a thread of its own after the
  server starts, and resumes after a restart (an entry in the index is skipped). An
  entry it hasn't reached yet is searched the old way, so results never depend on it.
- **Never in the way:** any SQLite error leaves the index out (the search scores those
  files the old way); a file from another schema version is rebuilt.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import sys
import threading
import time
import zlib
from array import array
from itertools import accumulate
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz, process

from quipclipper.models import Cue
from quipclipper.search import Prefilter, candidate_regions, cue_words

log = logging.getLogger(__name__)

SCHEMA_VERSION = "1"
FILE_NAME = "line_index.sqlite"
# The engine's typo rule (search._TYPO_CUTOFF): words of 3+ letters this similar match.
_TYPO = 80.0
_TYPO_MIN_LEN = 3
# Entries written per transaction while backfilling.
_BATCH = 50
# The backfill stages this many entries or more (see LineIndex.backfill), in chunks.
BULK_MIN = 200
BULK_CHUNK = 2000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS entry (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,  -- the cache entry's file name
    cues INTEGER NOT NULL,      -- how many cues it holds
    stamp INTEGER,              -- the cache file's mtime (ns) when it was indexed
    words BLOB NOT NULL         -- its word ids (array of uint32), to remove it
);
CREATE TABLE IF NOT EXISTS word (id INTEGER PRIMARY KEY, text TEXT NOT NULL UNIQUE);
-- Each entry's cues, packed (see _pack): a search reads the few it scores from here, not
-- the whole entry's JSON (parsing those was most of a library search's time).
CREATE TABLE IF NOT EXISTS cues (entry INTEGER PRIMARY KEY, data BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS stage_cues (entry INTEGER PRIMARY KEY, data BLOB NOT NULL);
-- The backfill's chunk on its way in (see LineIndex.backfill): rows in the order written.
CREATE TABLE IF NOT EXISTS stage_entry (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, cues INTEGER NOT NULL, stamp INTEGER,
    words BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS stage (word INTEGER NOT NULL, entry INTEGER NOT NULL,
                                  cues BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS posting (
    word INTEGER NOT NULL,
    entry INTEGER NOT NULL,
    cues BLOB NOT NULL,         -- see _encode
    PRIMARY KEY (word, entry)
) WITHOUT ROWID;
"""


def _le(a: array) -> bytes:
    if sys.byteorder == "big":
        a = array(a.typecode, a)
        a.byteswap()
    return a.tobytes()


def _from_le(code: str, data: bytes) -> array:
    a = array(code)
    a.frombytes(data)
    if sys.byteorder == "big":
        a.byteswap()
    return a


def _encode(positions: list[int], n: int) -> bytes:
    """A word's cue positions in one entry: a bit set (``0``) when it is in many of the
    cues, else the positions themselves (``1``: 16-bit, ``2``: 32-bit)."""
    size = (n + 7) // 8
    if size <= 4 * len(positions):
        bits = bytearray(size)
        for p in positions:
            bits[p >> 3] |= 1 << (p & 7)
        return b"\0" + bytes(bits)
    if n <= 0x10000:
        return b"\1" + _le(array("H", positions))
    return b"\2" + _le(array("I", positions))


def _decode(data: bytes) -> int:
    """:func:`_encode`'s positions as a bit set (bit ``p``: cue ``p`` holds the word)."""
    kind = data[0]
    if kind == 0:
        return int.from_bytes(data[1:], "little")
    mask = 0
    for p in _from_le("H" if kind == 1 else "I", data[1:]):
        mask |= 1 << p
    return mask


def _pack(cues: list[Cue]) -> bytes:
    """An entry's cues, compact: their numbers, times, text and (when any has one)
    speakers, zlib-compressed. :func:`_unpack` gives back equal cues."""
    texts = [c.text.encode("utf-8", "surrogatepass") for c in cues]
    speakers = [c.speaker for c in cues]
    extra = json.dumps(speakers).encode() if any(x is not None for x in speakers) else b""
    parts = [_le(array("q", [c.index for c in cues])), _le(array("d", [c.start for c in cues])),
             _le(array("d", [c.end for c in cues])), _le(array("I", map(len, texts))),
             b"".join(texts), extra]
    head = _le(array("I", [len(cues)] + [len(x) for x in parts]))
    return zlib.compress(head + b"".join(parts), 1)


def _unpack(data: bytes, wanted: list[tuple[int, int]] | None = None) -> list[Cue | None]:
    """:func:`_pack`'s cues; with ``wanted`` (position ranges), only those are built and
    the others are None (a search reads only the cues in its regions)."""
    raw = zlib.decompress(data)
    n = _from_le("I", raw[:4])[0]
    sizes = _from_le("I", raw[4:28])
    at = list(accumulate(sizes, initial=28))
    index = _from_le("q", raw[at[0]:at[1]])
    start = _from_le("d", raw[at[1]:at[2]])
    end = _from_le("d", raw[at[2]:at[3]])
    offs = list(accumulate(_from_le("I", raw[at[3]:at[4]]), initial=at[4]))
    speakers = json.loads(raw[at[5]:at[6]]) if sizes[5] else None
    out: list[Cue | None] = [None] * n
    for lo, hi in wanted if wanted is not None else [(0, n - 1)]:
        for p in range(max(lo, 0), min(hi, n - 1) + 1):
            out[p] = Cue(index=index[p], start=start[p], end=end[p],
                         text=raw[offs[p]:offs[p + 1]].decode("utf-8", "surrogatepass"),
                         speaker=speakers[p] if speakers else None)
    return out


def _positions(cues: list[Cue]) -> dict[str, list[int]]:
    """word -> the positions of the cues that hold it, in order."""
    at: dict[str, list[int]] = {}
    for p, cue in enumerate(cues):
        for w in cue_words(cue.text):
            ps = at.setdefault(w, [])
            if not ps or ps[-1] != p:
                ps.append(p)
    return at


class LineIndex:
    """The word index of the subtitle cache's entries (see the module doc)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._local = threading.local()
        self._lock = threading.RLock()  # one writer; the in-memory maps change under it
        self._broken = False
        # name -> (entry id, cue count, stamp) for every entry in the index.
        self._entries: dict[str, tuple[int, int, int | None]] = {}
        # The vocabulary: word -> id, and the words by length for typo matching.
        self._words: dict[str, int] = {}
        self._by_len: dict[int, list[str]] = {}
        self._next_id = 1
        self.status: dict[str, Any] = {"state": "idle", "done": 0, "total": 0,
                                       "added": 0, "removed": 0, "started": None,
                                       "finished": None, "error": None}
        try:
            self._open()
        except sqlite3.Error as exc:
            self._fail("open", exc)

    # --- the database --------------------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30.0)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA cache_size=-16384")  # 16 MiB a connection (one a thread)
            self._local.conn = conn
        return conn

    def _open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            conn = self._conn()
            conn.executescript(_SCHEMA)
            row = conn.execute("SELECT value FROM meta WHERE key='version'").fetchone()
        except sqlite3.DatabaseError:
            row = ("unreadable",)
        if row is None:
            with self._conn() as conn:
                conn.execute("INSERT INTO meta VALUES ('version', ?)", (SCHEMA_VERSION,))
        elif row[0] != SCHEMA_VERSION:  # another layout, or a damaged file: start over
            self._close()
            for suffix in ("", "-wal", "-shm"):
                with contextlib.suppress(FileNotFoundError):
                    Path(str(self.path) + suffix).unlink()
            conn = self._conn()
            conn.executescript(_SCHEMA)
            with conn:
                conn.execute("INSERT INTO meta VALUES ('version', ?)", (SCHEMA_VERSION,))
        conn = self._conn()
        self._entries = {name: (eid, n, stamp) for eid, name, n, stamp
                         in conn.execute("SELECT id, name, cues, stamp FROM entry")}
        for wid, text in conn.execute("SELECT id, text FROM word"):
            self._words[text] = wid
            self._by_len.setdefault(len(text), []).append(text)
        with conn:  # a chunk a stop or a crash left half staged: the backfill redoes it
            conn.execute("DELETE FROM stage")
            conn.execute("DELETE FROM stage_entry")
            conn.execute("DELETE FROM stage_cues")
        self._next_id = 1 + max((e[0] for e in self._entries.values()), default=0)

    def _close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def _fail(self, what: str, exc: Exception) -> None:
        log.warning("line index: %s failed (%s); search reads every cue meanwhile", what, exc)
        self._broken = True

    @property
    def usable(self) -> bool:
        return not self._broken

    def names(self) -> set[str]:
        """The entries in the index."""
        return set(self._entries)

    def __contains__(self, name: str) -> bool:
        return name in self._entries

    def __len__(self) -> int:
        return len(self._entries)

    # --- keeping it current --------------------------------------------------------

    def _alloc(self) -> int:
        """A new entry id (with the lock held): the backfill's staged entries take ids
        before they are in ``entry``, so ids come from here, never from SQLite."""
        eid = self._next_id
        self._next_id += 1
        return eid

    def _word_ids(self, conn: sqlite3.Connection, words) -> tuple[list[int], list[str]]:
        """The ids of ``words``, adding the new ones (with the lock held)."""
        ids, fresh = [], []
        for w in words:
            wid = self._words.get(w)
            if wid is None:
                wid = conn.execute("INSERT INTO word (text) VALUES (?)", (w,)).lastrowid
                self._words[w] = wid
                fresh.append(w)
            ids.append(wid)
        return ids, fresh

    def _learn(self, fresh: list[str]) -> None:
        for w in fresh:
            self._by_len.setdefault(len(w), []).append(w)

    def add(self, name: str, cues: list[Cue], stamp: int | None = None,
            expect: Any = ...) -> bool:
        """Index entry ``name`` (a cache file name) holding ``cues``, replacing what the
        index had for it. ``stamp``: the cache file's mtime in ns (a later
        :meth:`backfill` indexes the entry again if the file differs). ``expect``: add it
        only if the index still has this stamp for it (None: still lacks it), so the
        backfill, which may have read an entry that has been rewritten and indexed since,
        leaves the newer one."""
        if self._broken:
            return False
        at = _positions(cues)
        packed = _pack(cues)
        try:
            with self._lock:
                if expect is not ... and self._stamp(name) != expect:
                    return False
                conn = self._conn()
                with conn:
                    self._delete(conn, name)
                    ids, fresh = self._word_ids(conn, at)
                    eid = self._alloc()
                    conn.execute(
                        "INSERT INTO entry (id, name, cues, stamp, words) VALUES (?, ?, ?, ?, ?)",
                        (eid, name, len(cues), stamp, _le(array("I", ids))))
                    conn.executemany(
                        "INSERT INTO posting (word, entry, cues) VALUES (?, ?, ?)",
                        ((wid, eid, _encode(ps, len(cues)))
                         for wid, ps in zip(ids, at.values())))
                    conn.execute("INSERT INTO cues VALUES (?, ?)", (eid, packed))
                self._entries[name] = (eid, len(cues), stamp)
                self._learn(fresh)
        except sqlite3.Error as exc:
            # The words this transaction added were rolled back: drop them here too.
            self._reload_words()
            self._fail("adding an entry", exc)
            return False
        return True

    def _stamp(self, name: str) -> int | None:
        e = self._entries.get(name)
        return None if e is None else e[2]

    def _reload_words(self) -> None:
        with contextlib.suppress(sqlite3.Error):
            words = dict(self._conn().execute("SELECT text, id FROM word"))
            by_len: dict[int, list[str]] = {}
            for w in words:
                by_len.setdefault(len(w), []).append(w)
            self._words, self._by_len = words, by_len

    def _delete(self, conn: sqlite3.Connection, name: str) -> bool:
        row = conn.execute("SELECT id, words FROM entry WHERE name=?", (name,)).fetchone()
        if row is None:
            return False
        eid, words = row
        conn.executemany("DELETE FROM posting WHERE word=? AND entry=?",
                         ((wid, eid) for wid in _from_le("I", words)))
        conn.execute("DELETE FROM cues WHERE entry=?", (eid,))
        conn.execute("DELETE FROM entry WHERE id=?", (eid,))
        return True

    def remove(self, name: str) -> bool:
        """Take entry ``name`` out of the index (its cache file was deleted)."""
        if self._broken or name not in self._entries:
            return False
        try:
            with self._lock:
                conn = self._conn()
                with conn:
                    gone = self._delete(conn, name)
                self._entries.pop(name, None)
        except sqlite3.Error as exc:
            self._fail("removing an entry", exc)
            return False
        return gone

    def backfill(self, entries: Callable[[], list[Path]],
                 read: Callable[[Path], list[Cue] | None],
                 stop: threading.Event | None = None,
                 pause: Callable[[], None] = lambda: time.sleep(0)) -> dict[str, Any]:
        """Bring the index level with the cache: index every entry ``entries()`` lists
        that it lacks or holds at another stamp (``read`` gives an entry's cues, or None
        when it is gone or unreadable) and drop what it holds that the cache no longer
        has. Resumable: what it finished stays. ``pause()`` runs between entries, so
        request threads get the interpreter lock. Progress is in :attr:`status`.

        Many entries at once (the first run on a big cache) go in by chunks of
        ``BULK_CHUNK``: staged in a table written in order, then moved into ``posting``
        sorted by word. Adding them one by one rewrites a page of every word's postings
        for every entry, ~900 pages an entry, which made the first run disk-bound."""
        st = self.status
        st.update(state="building", done=0, total=0, added=0, removed=0,
                  started=time.time(), finished=None, error=None)
        stopped = (lambda: False) if stop is None else stop.is_set
        try:
            files = entries()
            on_disk = {f.name for f in files}
            todo = []
            for f in files:
                try:
                    stamp = f.stat().st_mtime_ns
                except OSError:
                    continue
                if f.name not in self._entries or self._stamp(f.name) != stamp:
                    todo.append((f, stamp, self._stamp(f.name)))
            st["total"] = len(todo)
            for name in self.names() - on_disk:
                if stopped():
                    break
                if self.remove(name):
                    st["removed"] += 1
            if len(todo) >= BULK_MIN:
                for k in range(0, len(todo), BULK_CHUNK):
                    if stopped():
                        break
                    self._bulk(todo[k:k + BULK_CHUNK], read, stopped, pause)
            else:
                for f, stamp, had in todo:
                    if stopped():
                        break
                    if self._broken:
                        raise RuntimeError("the index can't be written")
                    cues = read(f)
                    if cues is not None and self.add(f.name, cues, stamp, expect=had):
                        st["added"] += 1
                        if not f.exists():  # deleted while it was read
                            self.remove(f.name)
                    st["done"] += 1
                    pause()
            if self._broken:
                raise RuntimeError("the index can't be written")
            st["state"] = "stopped" if stopped() else "ready"
        except Exception as exc:  # noqa: BLE001 - the server goes on without it
            st["state"] = "failed"
            st["error"] = str(exc)
            log.warning("line index backfill failed: %s", exc)
        finally:
            st["finished"] = time.time()
        return st

    def _bulk(self, chunk: list[tuple[Path, int, int | None]],
              read: Callable[[Path], list[Cue] | None],
              stopped: Callable[[], bool], pause: Callable[[], None]) -> None:
        """Index ``chunk`` (``(file, stamp, stamp the index had)``) through the stage."""
        st = self.status
        conn = self._conn()
        staged: list[tuple[Path, int, int | None, int, int]] = []
        try:
            with self._lock, conn:
                conn.execute("DELETE FROM stage")
                conn.execute("DELETE FROM stage_entry")
                conn.execute("DELETE FROM stage_cues")
            pending = 0
            for f, stamp, had in chunk:
                if stopped():
                    break
                cues = read(f)
                st["done"] += 1
                if cues is None:
                    continue
                at = _positions(cues)
                packed = _pack(cues)
                with self._lock:
                    ids, fresh = self._word_ids(conn, at)
                    eid = self._alloc()
                    conn.execute("INSERT INTO stage_entry VALUES (?, ?, ?, ?, ?)",
                                 (eid, f.name, len(cues), stamp, _le(array("I", ids))))
                    conn.executemany("INSERT INTO stage VALUES (?, ?, ?)",
                                     ((wid, eid, _encode(ps, len(cues)))
                                      for wid, ps in zip(ids, at.values())))
                    conn.execute("INSERT INTO stage_cues VALUES (?, ?)", (eid, packed))
                    pending += 1
                    if pending >= _BATCH:
                        conn.commit()
                        pending = 0
                    self._learn(fresh)
                staged.append((f, eid, had, len(cues), stamp))
                pause()
            with self._lock:
                conn.commit()
                # Leave out what changed since it was read: indexed anew, or deleted.
                keep, drop = [], []
                for item in staged:
                    f, eid, had = item[:3]
                    (keep if self._stamp(f.name) == had and f.exists() else drop).append(item)
                with conn:
                    for f, eid, had, _, _ in keep:
                        if had is not None:
                            self._delete(conn, f.name)  # the version it replaces
                    for k in range(0, len(drop), 500):
                        ids = [d[1] for d in drop[k:k + 500]]
                        marks = ",".join("?" * len(ids))
                        conn.execute(f"DELETE FROM stage WHERE entry IN ({marks})", ids)
                        conn.execute(f"DELETE FROM stage_entry WHERE id IN ({marks})", ids)
                        conn.execute(f"DELETE FROM stage_cues WHERE entry IN ({marks})", ids)
                    conn.execute("INSERT INTO entry (id, name, cues, stamp, words) "
                                 "SELECT id, name, cues, stamp, words FROM stage_entry")
                    conn.execute("INSERT INTO posting (word, entry, cues) SELECT word, entry, "
                                 "cues FROM stage ORDER BY word, entry")
                    conn.execute("INSERT INTO cues SELECT entry, data FROM stage_cues")
                    conn.execute("DELETE FROM stage")
                    conn.execute("DELETE FROM stage_entry")
                    conn.execute("DELETE FROM stage_cues")
                for f, eid, _, n, stamp in keep:
                    self._entries[f.name] = (eid, n, stamp)
                st["added"] += len(keep)
            with contextlib.suppress(sqlite3.Error):
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error as exc:
            with contextlib.suppress(sqlite3.Error):
                conn.rollback()
            self._reload_words()
            self._fail("backfilling", exc)

    # --- searching -----------------------------------------------------------------

    def cues(self, name: str, regions: list[tuple[int, int]]) -> list[Cue | None] | None:
        """Entry ``name``'s cues as the index holds them, only those in ``regions`` built
        (the rest None: :func:`quipclipper.search.search` with these ``regions`` reads no
        other). None when the index doesn't have it."""
        e = self._entries.get(name)
        if e is None or self._broken:
            return None
        try:
            row = self._conn().execute("SELECT data FROM cues WHERE entry=?",
                                       (e[0],)).fetchone()
            return None if row is None else _unpack(row[0], regions)
        except (sqlite3.Error, zlib.error, ValueError, IndexError) as exc:
            log.warning("line index: reading %s failed (%s)", name, exc)
            return None

    def _vocab_hits(self, pf: Prefilter) -> dict[int, int]:
        """word id -> the query words it matches (a bit set over query positions), for
        every indexed word that matches a query word as the scoring would."""
        cand: set[str] = set()
        for q in pf.words:
            if q in self._words:
                cand.add(q)
            if len(q) < _TYPO_MIN_LEN:
                continue
            # fuzz.ratio = 2 * common / (len a + len b) >= 0.8 needs lengths within 2:3.
            lo, hi = max(_TYPO_MIN_LEN, -(-2 * len(q) // 3)), (3 * len(q)) // 2
            for n in range(lo, hi + 1):
                words = self._by_len.get(n)
                if words:
                    for w, _, _ in process.extract(q, list(words), scorer=fuzz.ratio,
                                                   processor=None, score_cutoff=_TYPO,
                                                   limit=None):
                        cand.add(w)
        out: dict[int, int] = {}
        for w in cand:
            bits = 0
            for j, _ in pf.hits(w):  # the scoring's own test decides
                bits |= 1 << j
            if bits:
                out[self._words[w]] = bits
        return out

    def candidates(self, pf: Prefilter, names: Iterable[str],
                   max_span: int) -> dict[str, list[tuple[int, int]]] | None:
        """For each of ``names`` (indexed entries) whose cues may hold a match: the
        ranges of cue positions to score (see :func:`quipclipper.search.search`'s
        ``regions``). An entry left out has no window that can reach the score. None
        when the index can't answer (search every cue then)."""
        if self._broken or not pf.usable:
            return None
        wanted: dict[int, tuple[str, int]] = {}
        for name in names:
            e = self._entries.get(name)
            if e is not None:
                wanted[e[0]] = (name, e[1])
        try:
            conn = self._conn()
            present: dict[int, int] = {}
            rows: dict[int, list[tuple[int, bytes]]] = {}
            for wid, bits in self._vocab_hits(pf).items():
                for eid, data in conn.execute(
                        "SELECT entry, cues FROM posting WHERE word=?", (wid,)):
                    if eid in wanted:
                        present[eid] = present.get(eid, 0) | bits
                        rows.setdefault(eid, []).append((bits, data))
        except sqlite3.Error as exc:
            self._fail("a search", exc)
            return None
        weights, need, nwords = pf.weights, pf.need, len(pf.words)
        reach: dict[int, int] = {}  # a set of query words -> their weight

        def weight(bits: int) -> int:
            w = reach.get(bits)
            if w is None:
                w = reach[bits] = sum(weights[j] for j in range(nwords) if bits >> j & 1)
            return w

        out: dict[str, list[tuple[int, int]]] = {}
        for eid, bits in present.items():
            if weight(bits) < need:
                continue  # even every word it has, side by side, falls short
            masks = [0] * nwords
            for b, data in rows[eid]:
                m = _decode(data)
                for j in range(nwords):
                    if b >> j & 1:
                        masks[j] |= m
            name, n = wanted[eid]
            regions = candidate_regions(masks, weights, need, max_span, n)
            if regions:
                out[name] = regions
        return out
