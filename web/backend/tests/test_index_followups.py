"""Small index bugs the second pre-release review deferred (D-046), each fixed: a library
walk that answers cancel and shows progress (JB-8), the change check for any track
number (JB-13), forgetting a file's changed mark wherever it is re-read, a cancelled
re-index job that says so, no_subs sweeping, a change re-index that returns at once
(JB-14), and a clip by match index counting in the search's own tuning."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import quipclipper_web.app as web
from quipclipper_web import index_changes as ic
from quipclipper_web import library
from quipclipper_web import sub_cache as sub_cache_mod
from quipclipper_web.app import create_app
from quipclipper_web.config import Settings
from quipclipper_web.sub_cache import SubtitleCache

SRT = "1\n00:00:01,000 --> 00:00:03,000\nMake it so.\n"


def _bump(p: Path, by: float = 10) -> None:
    st = p.stat()
    os.utime(p, (st.st_atime, st.st_mtime + by))


@pytest.fixture
def lib(tmp_path, monkeypatch):
    # No ffprobe here: a file with no sidecar has no tracks (so it's marked no_subs).
    monkeypatch.setattr(sub_cache_mod, "list_embedded_tracks", lambda v: [])
    monkeypatch.setattr("quipclipper.subtitles.list_embedded_tracks", lambda v: [])
    media = tmp_path / "media"
    (media / "show").mkdir(parents=True)
    for n in ("a", "b"):
        (media / "show" / f"{n}.mkv").write_bytes(b"")
        (media / "show" / f"{n}.srt").write_text(SRT)
    return media


def _client(tmp_path, media) -> TestClient:
    c = TestClient(create_app(Settings.from_env({
        "QC_MEDIA_ROOTS": str(media), "QC_STATE_DIR": str(tmp_path / "state"),
        "QC_INDEX_CHECK_INTERVAL": "0"})))
    c.headers["X-Quipclipper"] = "1"
    return c


def _wait(c: TestClient, job_id: str, timeout: float = 10) -> dict:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        job = c.get(f"/api/jobs/{job_id}").json()
        if job["status"] not in ("queued", "running"):
            return job
        time.sleep(0.02)
    raise AssertionError(f"job still running: {job}")


def _changed_a(tmp_path, lib) -> Path:
    """a.mkv indexed, then its subtitles changed, and the change list says so."""
    a = lib / "show" / "a.mkv"
    SubtitleCache(tmp_path / "state").resolve(a)
    _bump(lib / "show" / "a.srt")
    ic.ChangeLog(tmp_path / "state").note([(a, "changed")])
    return a


# --- JB-8: the walk answers cancel and shows progress ---------------------------------

def test_a_cancel_during_the_folder_walk_stops_the_run(tmp_path, lib, monkeypatch):
    real = web._walk_dirs

    def slow_walk(top):  # a cold pool: every listing takes a while
        yield from real(top)
        for _ in range(1000):
            time.sleep(0.01)
            yield top, []

    monkeypatch.setattr(web, "_walk_dirs", slow_walk)
    c = _client(tmp_path, lib)
    jid = c.post("/api/search/library/index").json()["job_id"]
    end = time.monotonic() + 5
    while "folders" not in (c.get(f"/api/jobs/{jid}").json().get("progress") or ""):
        assert time.monotonic() < end, "no progress during the walk"
        time.sleep(0.02)
    assert c.delete(f"/api/jobs/{jid}").json() == {"cancelling": jid}
    started = time.monotonic()
    job = _wait(c, jid, timeout=5)
    assert time.monotonic() - started < 3  # not after the whole walk (10 s)
    assert job["status"] == "cancelled"
    assert job["result"]["status"] == "cancelled"
    assert job["result"]["summary"].startswith("Stopped while looking for files")


# --- JB-13: an explicit track number above 15 -----------------------------------------

def test_a_file_indexed_only_by_a_high_track_is_not_changed(tmp_path, lib):
    cache = SubtitleCache(tmp_path / "state")
    c = lib / "show" / "b.mkv"
    cache._write(cache._cache_path(c, 23), c, [], 23)
    assert cache.index_state(c) == "not_indexed"
    # A restart reads the owners (and the track) back from disk.
    assert SubtitleCache(tmp_path / "state").index_state(c) == "not_indexed"
    _bump(lib / "show" / "b.srt")  # and once its subtitles change, it is
    assert SubtitleCache(tmp_path / "state").index_state(c) == "changed"


def test_entries_record_their_track(tmp_path, lib):
    cache = SubtitleCache(tmp_path / "state")
    a = lib / "show" / "a.mkv"
    cache.resolve(a)
    assert cache._stored_track(cache._cache_path(a)) == "auto"
    cache._write(cache._cache_path(a, 40), a, [], 40)
    assert cache._stored_track(cache._cache_path(a, 40)) == "40"
    # An entry from before the track was recorded has none (and is guessed at 0-15).
    old = cache._cache_path(a, 41)
    old.write_text('{"video": "%s", "cues": []}' % a)
    assert cache._stored_track(old) is None


# --- JB-14 ------------------------------------------------------------------------------

def test_folder_index_forgets_the_changed_mark(tmp_path, lib):
    _changed_a(tmp_path, lib)
    c = _client(tmp_path, lib)
    assert c.get("/api/index/changes").json()["changed"] == 1
    r = c.post("/api/search/folder/index", params={"path": str(lib / "show")})
    assert r.status_code == 200 and "2/2" in r.text
    assert c.get("/api/index/changes").json()["changed"] == 0
    # Written out, not only in memory.
    assert ic.ChangeLog(tmp_path / "state").files() == []


def test_opening_a_file_forgets_the_changed_mark(tmp_path, lib):
    a = _changed_a(tmp_path, lib)
    c = _client(tmp_path, lib)
    r = c.get("/api/search", params={"path": str(a), "query": "make it so"})
    assert r.status_code == 200 and r.json()["count"] == 1
    assert c.get("/api/index/changes").json()["changed"] == 0


def test_mark_no_subs_sweeps_the_old_entries(tmp_path, lib):
    cache = SubtitleCache(tmp_path / "state")
    a = lib / "show" / "a.mkv"
    cache.resolve(a)
    old = cache._cache_path(a)
    (lib / "show" / "a.srt").unlink()  # its subtitles went away
    assert cache.mark_no_subs(a)
    assert not old.exists()
    assert [p.suffix for _, p in cache._entries_of(a)] == [".nosubs"]


def test_a_cancelled_reindex_job_says_it_stopped(tmp_path, lib, monkeypatch):
    c = _client(tmp_path, lib)
    gate, started = threading.Event(), threading.Event()
    real = SubtitleCache.resolve

    def slow(self, video, track=None, fresh=False):
        started.set()
        gate.wait(5)
        raise web.Cancelled

    monkeypatch.setattr(SubtitleCache, "resolve", slow)
    jid = c.post("/api/index/file", json={"path": [str(lib / "show" / "a.mkv")],
                                          "force": True}).json()["job_id"]
    assert started.wait(5)
    c.delete(f"/api/jobs/{jid}")
    gate.set()
    job = _wait(c, jid)
    monkeypatch.setattr(SubtitleCache, "resolve", real)
    assert job["progress"].startswith("Stopped:")
    assert "nothing to do" not in job["progress"]


def test_reindexing_the_changes_queues_them_without_reading_the_disks(tmp_path, lib,
                                                                      monkeypatch):
    a = _changed_a(tmp_path, lib)
    outside = str(tmp_path / "elsewhere" / "x.mkv")
    sneaky = str(lib / "show" / ".." / ".." / "elsewhere" / "y.mkv")
    ic.ChangeLog(tmp_path / "state").note(
        [(a, "changed"), (lib / "show" / "gone.mkv", "changed"),
         (outside, "changed"), (sneaky, "changed")])
    c = _client(tmp_path, lib)
    calls = []
    real = library.resolve_within_roots
    monkeypatch.setattr(library, "resolve_within_roots",
                        lambda *a, **k: calls.append(a) or real(*a, **k))
    r = c.post("/api/index/changes/reindex")
    assert r.status_code == 202 and calls == []
    rows = {row["path"]: row["status"] for row in r.json()["files"]}
    assert rows == {str(a): "queued", str(lib / "show" / "gone.mkv"): "queued",
                    outside: "outside_roots", sneaky: "outside_roots"}
    assert r.json()["queued"] == 2
    end = time.monotonic() + 10
    while c.get("/api/index/file").json()["job_id"] is not None:
        assert time.monotonic() < end
        time.sleep(0.02)
    recent = {x["path"]: x["status"] for x in c.get("/api/index/file").json()["recent"]}
    assert recent == {str(a): "indexed", str(lib / "show" / "gone.mkv"): "missing"}
    assert c.get("/api/index/changes").json()["changed"] == 0


# --- clipping by match index ------------------------------------------------------------

def test_a_clip_by_match_index_counts_in_the_searchs_tuning(tmp_path, lib):
    (lib / "show" / "a.srt").write_text(
        "1\n00:00:01,000 --> 00:00:03,000\nMake it all so.\n\n"
        "2\n00:00:05,000 --> 00:00:07,000\nMake it so.\n")
    c = _client(tmp_path, lib)
    a = str(lib / "show" / "a.mkv")
    strict = c.get("/api/search", params={"path": a, "query": "make it so",
                                          "min_score": 100}).json()
    assert [m["text"] for m in strict["matches"]] == ["Make it so."]
    # Match 1 doesn't exist at min_score 100 (the default 60 would find "Make it all so.").
    r = c.post("/api/clip", json={"path": a, "query": "make it so", "match_index": 1,
                                  "min_score": 100})
    assert r.status_code == 404 and "out of range" in r.json()["detail"]
    r = c.post("/api/clip", json={"path": a, "query": "make it so", "match_index": 1,
                                  "max_span": 0})
    assert r.status_code == 422  # validated like the search's
