"""The line index (M27): a library-wide line search scores only the cues that could match.

The index must give the same results as scoring every cue (the recall test compares the
endpoint with and without it on a corpus of files, queries typed right and misheard),
be built for a cache that existed before it (backfill, resumable), survive a restart,
and stay right when files are re-indexed or removed. A big folder set's listing is kept
between searches (folder_cache).
"""

from __future__ import annotations

import os
import random
import sqlite3
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from quipclipper.models import Cue
from quipclipper.search import Prefilter, search
from quipclipper_web import folder_cache
from quipclipper_web import sub_cache as sub_cache_mod
from quipclipper_web.app import create_app
from quipclipper_web.config import Settings
from quipclipper_web.line_index import FILE_NAME, LineIndex, _decode, _encode
from quipclipper_web.sub_cache import SubtitleCache

LINES = [
    "You're gonna need a bigger boat.", "May the Force be with you.", "I'll be back.",
    "Here's looking at you, kid.", "You can't handle the truth!", "Make it so, Number One.",
    "Houston, we have a problem.", "Winter is coming.", "No, no, no!", "I am your father.",
]
FILLER = ("the a you i to and it of that is what we me my no have your be so just know do "
          "all but like with oh yeah right get boat force back kid truth home father").split()
QUERIES = [
    "you're gonna need a bigger boat", "may the force be with you", "i'll be back",
    "you can't handle the truth", "make it so", "winter is coming", "no no",
    "you're gonna need a bigger goat", "may the horse be with you", "i'll be black",
    "houston we have problem", "here's looking at you kid all", "make it sew",
    "what do you want", "boat", "こんにちは",
]


def _srt(rnd: random.Random, n: int) -> str:
    out = []
    for i in range(n):
        if rnd.random() < 0.15:
            text = rnd.choice(LINES)
        else:
            text = " ".join(rnd.choice(FILLER) for _ in range(rnd.randint(1, 8)))
        s, e = i * 3, i * 3 + 2
        out.append(f"{i + 1}\n00:{s // 60:02d}:{s % 60:02d},000 --> "
                   f"00:{e // 60:02d}:{e % 60:02d},000\n{text}\n")
    return "\n".join(out)


@pytest.fixture
def lib(tmp_path, monkeypatch):
    monkeypatch.setattr(sub_cache_mod, "list_embedded_tracks", lambda v: [])
    monkeypatch.setattr("quipclipper.subtitles.list_embedded_tracks", lambda v: [])
    rnd = random.Random(5)
    media = tmp_path / "media"
    for show in ("alpha", "beta"):
        (media / show).mkdir(parents=True)
        for k in range(12):
            (media / show / f"{show}.e{k:02d}.mkv").write_bytes(b"")
            (media / show / f"{show}.e{k:02d}.srt").write_text(_srt(rnd, rnd.randint(5, 80)))
    return media


def _client(tmp_path, media) -> TestClient:
    c = TestClient(create_app(Settings.from_env({
        "QC_MEDIA_ROOTS": str(media), "QC_STATE_DIR": str(tmp_path / "state"),
        "QC_INDEX_CHECK_INTERVAL": "0", "QC_TRANSCRIBE": "off"})))
    c.headers["X-Quipclipper"] = "1"
    return c


def _index_all(c, media):
    for show in ("alpha", "beta"):
        r = c.post("/api/search/folder/index", params={"path": str(media / show)})
        assert r.status_code == 200


def _search(c, media, q, **kw):
    params = {"path": [str(media / "alpha"), str(media / "beta")], "query": q, **kw}
    r = c.get("/api/search/folder", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def test_recall_same_as_scoring_every_cue(tmp_path, lib, monkeypatch):
    c = _client(tmp_path, lib)
    _index_all(c, lib)
    assert c.get("/api/search/line-index").json()["entries"] == 24
    tunings = [{}, {"min_score": 40, "max_span": 2}, {"min_score": 90, "limit": 20},
               {"min_score": 0, "max_span": 1}]
    narrowed = []
    real = LineIndex.candidates

    def spy(self, *a, **k):
        got = real(self, *a, **k)
        narrowed.append(got)
        return got

    monkeypatch.setattr(LineIndex, "candidates", spy)
    with_index = {(q, i): _search(c, lib, q, **t) for q in QUERIES for i, t in enumerate(tunings)}
    assert any(r["count"] for r in with_index.values())
    # The index answered (all but the unspaced query) and left files out.
    assert sum(g is not None for g in narrowed) == (len(QUERIES) - 1) * len(tunings)
    assert any(g is not None and 0 < len(g) < 24 for g in narrowed)
    # The old path: the index can't answer, so every cue is scored.
    monkeypatch.setattr(LineIndex, "candidates", lambda *a, **k: None)
    for (q, i), got in with_index.items():
        assert got == _search(c, lib, q, **tunings[i]), q


def test_files_not_yet_in_the_index_are_still_searched(tmp_path, lib):
    state = tmp_path / "state"
    # Indexed before the line index existed: the cache has them, the index doesn't.
    plain = SubtitleCache(state)
    for v in sorted(lib.rglob("*.mkv")):
        plain.resolve(v)
    c = _client(tmp_path, lib)
    assert c.get("/api/search/line-index").json()["entries"] == 0
    before = _search(c, lib, "make it so")
    assert before["count"] > 0
    # The backfill fills it in from the cache (no extraction), and the results stay.
    c.app.state  # noqa: B018 - the app is built
    with c:  # runs the lifespan: the backfill thread starts with the server
        for _ in range(500):
            st = c.get("/api/search/line-index").json()
            if st["backfill"]["state"] in ("ready", "failed"):
                break
            threading.Event().wait(0.01)
        assert st["backfill"]["state"] == "ready", st
        assert st["entries"] == 24 and st["backfill"]["added"] == 24
        assert _search(c, lib, "make it so") == before


def test_reindex_and_removal_keep_the_index_right(tmp_path, lib):
    c = _client(tmp_path, lib)
    _index_all(c, lib)
    v = lib / "alpha" / "alpha.e00.mkv"
    srt = v.with_suffix(".srt")
    srt.write_text("1\n00:00:01,000 --> 00:00:03,000\nThe quokka sings at midnight.\n")
    st = srt.stat()
    os.utime(srt, (st.st_atime, st.st_mtime + 10))
    assert _search(c, lib, "quokka sings at midnight")["count"] == 0  # changed, not indexed
    r = c.post("/api/index/file", json={"paths": [str(v)]})
    assert r.status_code in (200, 202), r.text
    for _ in range(500):
        if _search(c, lib, "quokka sings at midnight")["count"]:
            break
        threading.Event().wait(0.01)
    hit = _search(c, lib, "quokka sings at midnight")
    assert [m["path"] for m in hit["matches"]] == [str(v)]
    assert c.get("/api/search/line-index").json()["entries"] == 24  # the old entry went
    # Re-index the folder from scratch: every entry is replaced, none left behind.
    r = c.post("/api/search/folder/index", params={"path": str(lib / "alpha"), "force": True})
    assert r.status_code == 200
    idx = LineIndex(tmp_path / "state" / FILE_NAME)
    names = {p.name for p in (tmp_path / "state" / "sub_cache").glob("*.json")}
    assert idx.names() <= names and len(idx) == 24


def test_index_survives_a_restart_and_rebuilds_a_bad_file(tmp_path):
    path = tmp_path / FILE_NAME
    cues = [Cue(index=1, start=0, end=1, text="Make it so."),
            Cue(index=2, start=1, end=2, text="Engage!")]
    idx = LineIndex(path)
    assert idx.add("a.json", cues, stamp=1)
    pf = Prefilter("make it so", 60.0)
    assert idx.candidates(pf, ["a.json"], 3) == {"a.json": [(0, 1)]}
    again = LineIndex(path)
    assert again.names() == {"a.json"}
    assert again.candidates(pf, ["a.json"], 3) == {"a.json": [(0, 1)]}
    assert again.remove("a.json") and not again.remove("a.json")
    assert LineIndex(path).candidates(pf, ["a.json"], 3) == {}
    # Another schema version: started over.
    again.add("b.json", cues)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE meta SET value='0' WHERE key='version'")
    assert len(LineIndex(path)) == 0
    # Not a database at all: started over too.
    for suffix in ("", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)
    path.write_bytes(b"not sqlite" * 100)
    fresh = LineIndex(path)
    assert fresh.usable and len(fresh) == 0


def test_backfill_is_resumable_and_follows_rewrites(tmp_path):
    files = {}
    for k in range(5):
        f = tmp_path / f"{k}.json"
        f.write_text("x")
        files[f] = [Cue(index=1, start=0, end=1, text=f"word{k} make it so")]
    idx = LineIndex(tmp_path / FILE_NAME)
    stop = threading.Event()
    seen = []

    def read(f):
        seen.append(f.name)
        if len(seen) == 2:
            stop.set()
        return files[f]

    st = idx.backfill(lambda: sorted(files), read, stop=stop)
    assert st["state"] == "stopped" and len(idx) == 2
    st = idx.backfill(lambda: sorted(files), read)
    assert st["state"] == "ready" and len(idx) == 5 and st["added"] == 3
    # An entry rewritten since (a new mtime) is indexed again; one gone is dropped.
    f0 = sorted(files)[0]
    files[f0] = [Cue(index=1, start=0, end=1, text="quokka")]
    os.utime(f0, ns=(1, 10**18))
    f1 = sorted(files)[1]
    f1.unlink()
    del files[f1]
    st = idx.backfill(lambda: sorted(files), read)
    assert st["added"] == 1 and st["removed"] == 1 and len(idx) == 4
    assert idx.candidates(Prefilter("quokka", 60), [f0.name], 3) == {f0.name: [(0, 0)]}


def test_positions_round_trip():
    for n, ps in ((10, [0, 9]), (1000, [3, 999]), (1000, list(range(0, 1000, 3))),
                  (70000, [0, 69999])):
        assert _decode(_encode(ps, n)) == sum(1 << p for p in ps)


def test_typo_words_come_from_the_whole_vocabulary(tmp_path):
    idx = LineIndex(tmp_path / FILE_NAME)
    idx.add("a.json", [Cue(index=1, start=0, end=1, text="You're gonna need a bigger boat")])
    idx.add("b.json", [Cue(index=1, start=0, end=1, text="Unrelated words entirely here")])
    for q in ("you're gonna need a biger boat", "youre gona need a bigger boats"):
        pf = Prefilter(q, 60.0)
        assert idx.candidates(pf, ["a.json", "b.json"], 3) == {"a.json": [(0, 0)]}
        cues = [Cue(index=1, start=0, end=1, text="You're gonna need a bigger boat")]
        assert search(q, cues)  # and the engine agrees it matches


def test_a_big_folder_listing_is_kept(tmp_path, lib, monkeypatch):
    monkeypatch.setattr(folder_cache, "MIN_FILES", 10)
    monkeypatch.setattr(folder_cache, "REFRESH_AFTER", 3600.0)
    c = _client(tmp_path, lib)
    _index_all(c, lib)
    first = _search(c, lib, "make it so")
    # A file added outside QuipClipper isn't listed until the listing is refreshed...
    new = lib / "alpha" / "alpha.e99.mkv"
    new.write_bytes(b"")
    new.with_suffix(".srt").write_text(
        "1\n00:00:01,000 --> 00:00:03,000\nThe quokka sings at midnight.\n")
    SubtitleCache(tmp_path / "state").resolve(new)
    assert _search(c, lib, "quokka sings")["count"] == 0
    assert _search(c, lib, "make it so") == first
    # ...but a file re-indexed through QuipClipper is searched at once.
    v = lib / "beta" / "beta.e03.mkv"
    srt = v.with_suffix(".srt")
    srt.write_text("1\n00:00:01,000 --> 00:00:03,000\nA platypus plays the oboe.\n")
    st = srt.stat()
    os.utime(srt, (st.st_atime, st.st_mtime + 10))
    c.post("/api/items/subtitles/reindex", params={"path": str(v)})
    hit = _search(c, lib, "platypus plays the oboe")
    assert [m["path"] for m in hit["matches"]] == [str(v)]


def test_listing_cache_refreshes_in_the_background():
    clock = [0.0]
    jobs = []
    lc = folder_cache.ListingCache(min_files=2, refresh_after=60, now=lambda: clock[0],
                                   background=jobs.append)
    builds = []

    def build(restat):
        builds.append(restat)
        return list(range(len(builds) + 1))

    got, kept = lc.get("k", build, len)
    assert (got, kept, builds) == ([0, 1], False, [False])
    clock[0] = 59
    assert lc.get("k", build, len) == ([0, 1], True) and not jobs
    clock[0] = 61
    assert lc.get("k", build, len) == ([0, 1], True) and len(jobs) == 1
    assert lc.get("k", build, len) == ([0, 1], True) and len(jobs) == 1  # one at a time
    jobs.pop()()
    assert builds == [False, True]
    assert lc.get("k", build, len) == ([0, 1, 2], True)
    # Too small to keep: listed afresh each time.
    small = folder_cache.ListingCache(min_files=100)
    assert small.get("s", lambda r: [1], len) == ([1], False)
    assert small.get("s", lambda r: [2], len) == ([2], False)


def test_bulk_backfill_stages_chunks_and_leaves_newer_entries(tmp_path, monkeypatch):
    from quipclipper_web import line_index as li

    monkeypatch.setattr(li, "BULK_MIN", 3)
    monkeypatch.setattr(li, "BULK_CHUNK", 4)
    files = {}
    for k in range(10):
        f = tmp_path / f"{k:02d}.json"
        f.write_text("x")
        files[f] = [Cue(index=1, start=0, end=1, text=f"word{k} make it so"),
                    Cue(index=2, start=1, end=2, text="engage")]
    order = sorted(files)
    idx = LineIndex(tmp_path / FILE_NAME)

    def read(f):
        if f == order[5]:  # meanwhile, a re-index writes entry 6 anew and deletes entry 7
            idx.add(order[6].name, [Cue(index=1, start=0, end=1, text="quokka")], stamp=7)
            order[7].unlink()
        return files[f]

    st = idx.backfill(lambda: list(order), read)
    assert st["state"] == "ready", st
    assert idx.names() == {f.name for f in order} - {order[7].name}
    pf = Prefilter("make it so", 60.0)
    got = idx.candidates(pf, idx.names(), 3)
    assert set(got) == idx.names() - {order[6].name}  # 6 holds the newer lines
    assert idx.candidates(Prefilter("quokka", 60.0), [order[6].name], 3) == {order[6].name: [(0, 0)]}
    again = LineIndex(tmp_path / FILE_NAME)  # and it is all on disk
    assert again.names() == idx.names()
    assert again.candidates(pf, again.names(), 3) == got


def test_packed_cues_round_trip(tmp_path):
    from quipclipper_web.line_index import _pack, _unpack

    cues = [Cue(index=7, start=0.1, end=1.25, text="Make it so.", speaker="Picard"),
            Cue(index=8, start=1.3333333333333333, end=2.0, text="Ünïcødé ♪ \ud800 <i>x</i>"),
            Cue(index=12, start=3.0, end=4.0, text="")]
    assert _unpack(_pack(cues)) == cues
    assert _unpack(_pack(cues), [(1, 1)]) == [None, cues[1], None]
    plain = [Cue(index=1, start=0, end=1, text="a")]
    assert _unpack(_pack(plain)) == plain
    idx = LineIndex(tmp_path / FILE_NAME)
    idx.add("a.json", cues)
    assert idx.cues("a.json", [(0, 2)]) == cues
    assert idx.cues("b.json", [(0, 0)]) is None
    idx.remove("a.json")
    assert idx.cues("a.json", [(0, 0)]) is None
