"""Subtitles changed since indexing (M20): told apart from never indexed, reported by the
views and an hourly timestamp-only pass, and re-indexed only when someone asks."""

from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import quipclipper.subtitles as subtitles_mod
from quipclipper_web import index_changes as ic
from quipclipper_web import sub_cache as sub_cache_mod
from quipclipper_web.app import create_app
from quipclipper_web.config import Settings
from quipclipper_web.sub_cache import SubtitleCache

SRT = "1\n00:00:01,000 --> 00:00:03,000\nMake it so.\n"
NEW_SRT = "1\n00:00:01,000 --> 00:00:03,000\nTea, Earl Grey, hot.\n"


def _bump(p: Path, by: float = 10) -> None:
    st = p.stat()
    os.utime(p, (st.st_atime, st.st_mtime + by))


@pytest.fixture
def lib(tmp_path, monkeypatch):
    monkeypatch.setattr(sub_cache_mod, "list_embedded_tracks", lambda v: [])
    monkeypatch.setattr("quipclipper.subtitles.list_embedded_tracks", lambda v: [])
    media = tmp_path / "media"
    (media / "show").mkdir(parents=True)
    for n in ("a", "b", "c"):
        (media / "show" / f"{n}.mkv").write_bytes(b"")
        (media / "show" / f"{n}.srt").write_text(SRT)
    (media / "show" / "silent.mkv").write_bytes(b"")
    return media


def _client(tmp_path, media, **env) -> TestClient:
    c = TestClient(create_app(Settings.from_env({
        "QC_MEDIA_ROOTS": str(media), "QC_STATE_DIR": str(tmp_path / "state"),
        "QC_INDEX_CHECK_INTERVAL": "0", **env})))
    c.headers["X-Quipclipper"] = "1"
    return c


def _settle(c: TestClient, timeout: float = 10) -> dict:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        st = c.get("/api/index/file").json()
        if st["job_id"] is None and not st["pending"]:
            return st
        time.sleep(0.05)
    raise AssertionError(st)


def test_changed_is_told_from_never_indexed(tmp_path, lib):
    cache = SubtitleCache(tmp_path / "state")
    show = lib / "show"
    for n in ("a", "b"):
        cache.resolve(show / f"{n}.mkv")
    assert cache.mark_no_subs(show / "silent.mkv")
    _bump(show / "a.srt")                        # Bazarr replaced a's subtitles
    (show / "silent.en.srt").write_text(SRT)     # and gave silent some
    states = {n: cache.index_state(show / f"{n}.mkv") for n in ("a", "b", "c", "silent")}
    assert states == {"a": "changed", "b": "indexed", "c": "not_indexed", "silent": "changed"}
    # The same with the names listing a folder scan passes.
    names = cache.entry_names()
    assert cache.index_state(show / "a.mkv", names=names) == "changed"
    # A file only ever opened with an explicit track, at its current mtime: not "changed".
    cache._write(cache._cache_path(show / "c.mkv", 3), show / "c.mkv", [])
    assert cache.index_state(show / "c.mkv") == "not_indexed"
    # A fresh cache (a restart) reads the owners from disk and still knows.
    assert SubtitleCache(tmp_path / "state").index_state(show / "a.mkv") == "changed"


def test_views_report_changed_files_and_search_leaves_them_out(tmp_path, lib):
    c = _client(tmp_path, lib)
    show = lib / "show"
    c.post("/api/search/library/index")
    _settle(c)
    for _ in range(100):
        if c.get("/api/search/library/index").json()["job"] is None:
            break
        time.sleep(0.05)
    assert "Make it so." in [m["text"] for m in c.get("/api/search/folder", params={
        "path": str(lib), "query": "make it so"}).json()["matches"]]
    (show / "a.srt").write_text(NEW_SRT)
    _bump(show / "a.srt")
    st = c.get("/api/search/folder/index-status", params={"path": str(show)}).json()
    assert (st["indexed"], st["changed"], st["changed_files"]) == (2, 1, [str(show / "a.mkv")])
    rows = {e["name"]: e["index_state"] for e in c.get(
        "/api/library/browse", params={"path": str(show)}).json()["entries"]}
    assert rows["a.mkv"] == "changed" and rows["b.mkv"] == "indexed"
    r = c.get("/api/search/folder", params={"path": str(lib), "query": "earl grey"}).json()
    assert r["matches"] == [] and r["changed"] == 1 and r["not_indexed"] == 0
    assert r["changed_files"] == [str(show / "a.mkv")] and r["capped"] is True
    # What the views saw is the badge's count too.
    assert c.get("/api/index/changes").json()["changed"] == 1
    # The banner's button: re-index exactly those files; then search has the new line.
    assert c.post("/api/index/file", json={"paths": r["changed_files"]}).status_code == 202
    _settle(c)
    assert [m["text"] for m in c.get("/api/search/folder", params={
        "path": str(lib), "query": "earl grey hot"}).json()["matches"]] == ["Tea, Earl Grey, hot."]
    assert c.get("/api/index/changes").json()["changed"] == 0


def test_hourly_pass_starts_no_extraction(tmp_path, lib, monkeypatch):
    cache = SubtitleCache(tmp_path / "state")
    show = lib / "show"
    for n in ("a", "b", "c"):
        cache.resolve(show / f"{n}.mkv")
    _bump(show / "b.srt")

    def forbidden(*a, **k):
        raise AssertionError("the hourly check must not extract or run anything")

    # No extraction, no probe, no subprocess, and no subtitle file read.
    monkeypatch.setattr(SubtitleCache, "resolve", forbidden)
    monkeypatch.setattr(subtitles_mod, "resolve_subtitles", forbidden)
    monkeypatch.setattr(sub_cache_mod, "list_embedded_tracks", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subtitles_mod, "load_subtitles", forbidden)
    seen = {}
    real_init = ic.HourlyCheck.__init__

    def capture(self, *a, **k):
        real_init(self, *a, **k)
        seen["check"] = self

    monkeypatch.setattr(ic.HourlyCheck, "__init__", capture)
    c = _client(tmp_path, lib)
    last = seen["check"].run_once()
    assert (last["files"], last["changed"]) == (4, 1)
    st = c.get("/api/index/changes").json()
    assert st["changed"] == 1 and st["files"] == [str(show / "b.mkv")]
    assert st["last_check"]["files"] == 4 and st["interval_s"] == 0
    # And no job ran.
    assert c.get("/api/jobs").json()["jobs"] == []


def test_the_badge_button_reindexes_what_the_check_found(tmp_path, lib):
    cache = SubtitleCache(tmp_path / "state")
    show = lib / "show"
    for n in ("a", "b"):
        cache.resolve(show / f"{n}.mkv")
    _bump(show / "a.srt")
    _bump(show / "b.srt")
    log = ic.ChangeLog(tmp_path / "state")
    log.note([(show / "a.mkv", "changed"), (show / "b.mkv", "changed"),
              (show / "gone.mkv", "changed")])
    c = _client(tmp_path, lib)
    assert c.get("/api/index/changes").json()["changed"] == 3  # kept across a restart
    r = c.post("/api/index/changes/reindex")
    assert r.status_code == 202 and r.json()["queued"] == 2
    _settle(c)
    assert c.get("/api/index/changes").json() == {
        **c.get("/api/index/changes").json(), "changed": 0, "files": []}
    assert c.post("/api/index/changes/reindex").json()["queued"] == 0


def test_hourly_timer_skips_overlap_keeps_last_on_a_missing_root_and_can_be_off(tmp_path):
    log = ic.ChangeLog(tmp_path)
    gate, started = threading.Event(), threading.Event()

    def slow_scan():
        started.set()
        gate.wait(5)
        yield Path("/m/a.mkv"), "changed"

    check = ic.HourlyCheck(log, slow_scan, interval=3600)
    t = threading.Thread(target=check.run_once)
    t.start()
    assert started.wait(5)
    assert check.running and check.run_once() is None  # one still running: skipped
    gate.set()
    t.join(5)
    assert log.summary()["changed"] == 1

    def gone():
        raise OSError("a media root is missing: /m")
        yield  # noqa: unreachable — a generator

    last = ic.HourlyCheck(log, gone, interval=3600).run_once()
    assert last["error"].startswith("a media root is missing")
    assert log.summary()["changed"] == 1  # an unmounted pool doesn't clear the badge

    off = ic.HourlyCheck(log, slow_scan, interval=0)
    off.start()
    assert off._thread is None
    assert Settings.from_env({"QC_INDEX_CHECK_INTERVAL": "0"}).index_check_interval == 0
    assert Settings.from_env({}).index_check_interval == 3600


def test_the_timer_thread_runs_at_idle_priority(tmp_path):
    log = ic.ChangeLog(tmp_path)
    ran = threading.Event()

    def scan():
        ran.set()
        return iter(())

    check = ic.HourlyCheck(log, scan, interval=3600, first_delay=0.01)
    main_nice = os.getpriority(os.PRIO_PROCESS, 0)
    check.start()
    try:
        assert ran.wait(5)
    finally:
        check.stop()
    assert check.priority  # something took effect (nice/SCHED_IDLE/io, or darwin bg)
    assert os.getpriority(os.PRIO_PROCESS, 0) == main_nice  # not the whole process


def test_a_reindex_during_a_pass_is_not_undone_by_it(tmp_path):
    log = ic.ChangeLog(tmp_path)
    log.begin_pass()
    log.forget("/m/a.mkv")
    log.replace(["/m/a.mkv", "/m/b.mkv"], {"at": 1})
    assert log.files() == ["/m/b.mkv"]
