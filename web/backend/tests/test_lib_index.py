"""The library index job (M20): user-started, idle priority, cancellable, resumable."""

from __future__ import annotations

import inspect
import json
import threading
import time
from pathlib import Path

from fastapi.testclient import TestClient

import quipclipper_web.app as web
from quipclipper import subtitles
from quipclipper_web import lib_index
from quipclipper_web import sub_cache as sub_cache_mod
from quipclipper_web.app import create_app
from quipclipper_web.config import Settings
from quipclipper_web.sub_cache import SubtitleCache

SRT = "1\n00:00:01,000 --> 00:00:03,000\nMake it so.\n"


def _library(tmp_path: Path, subtitled: int = 2, silent: int = 1) -> Path:
    media = tmp_path / "media"
    (media / "show").mkdir(parents=True)
    for i in range(subtitled):
        (media / "show" / f"ep{i:02d}.mkv").write_bytes(b"")
        (media / "show" / f"ep{i:02d}.srt").write_text(SRT)
    for i in range(silent):
        (media / f"silent{i}.mkv").write_bytes(b"")
    return media


def _client(tmp_path: Path, media: Path, monkeypatch) -> TestClient:
    # No ffprobe here: a file with no sidecar has no tracks (so it's marked no_subs).
    monkeypatch.setattr(sub_cache_mod, "list_embedded_tracks", lambda v: [])
    monkeypatch.setattr("quipclipper.subtitles.list_embedded_tracks", lambda v: [])
    client = TestClient(create_app(Settings.from_env(
        {"QC_MEDIA_ROOTS": str(media), "QC_STATE_DIR": str(tmp_path / "state")})))
    client.headers["X-Quipclipper"] = "1"
    return client


def _wait(client: TestClient, job_id: str, timeout: float = 10) -> dict:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] not in ("queued", "running"):
            return job
        time.sleep(0.05)
    raise AssertionError(f"job still running: {job}")


def test_indexes_the_whole_library_and_a_rerun_skips_what_is_done(tmp_path, monkeypatch):
    media = _library(tmp_path)
    c = _client(tmp_path, media, monkeypatch)
    r = c.post("/api/search/library/index").json()
    assert r["existing"] is False and r["paths"] == [str(media)]
    job = _wait(c, r["job_id"])
    assert job["status"] == "done"
    res = job["result"]
    assert (res["files"], res["todo"], res["indexed"], res["no_subs"], res["failed"]) == (3, 3, 2, 1, 0)
    assert "low priority" in job["label"] and res["whole_library"] is True
    # The folder's status sees it, and whole-library search now reads them.
    st = c.get("/api/search/folder/index-status", params={"path": str(media)}).json()
    assert (st["total"], st["indexed"], st["no_subs"]) == (3, 2, 1)

    # Resume: indexed files and files known to have no subtitles are skipped.
    again = _wait(c, c.post("/api/search/library/index").json()["job_id"])
    assert (again["result"]["already"], again["result"]["todo"]) == (3, 0)
    assert again["result"]["summary"].startswith("Nothing to index")


def test_a_folder_of_any_size_is_indexed_as_a_job(tmp_path, monkeypatch):
    media = _library(tmp_path, subtitled=4, silent=0)
    monkeypatch.setattr(web, "INDEX_CAP", 2)
    c = _client(tmp_path, media, monkeypatch)
    folder = str(media / "show")
    # The streaming Index refuses it; index-status counts it anyway.
    assert c.post("/api/search/folder/index", params={"path": folder}).status_code == 400
    st = c.get("/api/search/folder/index-status", params={"path": folder}).json()
    assert (st["total"], st["skipped"], st["cap"]) == (4, False, 2)
    job = _wait(c, c.post("/api/search/library/index", params={"path": folder}).json()["job_id"])
    assert job["result"]["indexed"] == 4 and job["result"]["paths"] == [folder]
    assert job["result"]["whole_library"] is False


def test_every_extraction_runs_at_idle_priority(tmp_path, monkeypatch):
    media = _library(tmp_path, subtitled=1, silent=1)
    c = _client(tmp_path, media, monkeypatch)
    flags = []
    real = SubtitleCache.resolve

    def resolve(self, video, track=None):
        flags.append(getattr(subtitles._idle, "on", False))
        return real(self, video, track)

    monkeypatch.setattr(SubtitleCache, "resolve", resolve)
    _wait(c, c.post("/api/search/library/index").json()["job_id"])
    assert flags and all(flags)
    # A search request on another thread doesn't inherit it.
    c.get("/api/search", params={"path": str(media / "show" / "ep00.mkv"), "query": "make"})
    assert getattr(subtitles._idle, "on", False) is False


def test_cancel_stops_it_and_a_rerun_resumes(tmp_path, monkeypatch):
    media = _library(tmp_path, subtitled=6, silent=0)
    c = _client(tmp_path, media, monkeypatch)
    real = SubtitleCache.resolve
    gate = threading.Event()

    def slow(self, video, track=None):
        gate.wait(5)
        return real(self, video, track)

    monkeypatch.setattr(SubtitleCache, "resolve", slow)
    jid = c.post("/api/search/library/index").json()["job_id"]
    # One at a time: asking again returns the running job.
    assert c.post("/api/search/library/index").json() == {
        "job_id": jid, "existing": True, "paths": [str(media)]}
    while (c.get(f"/api/jobs/{jid}").json().get("result") or {}).get("status") != "indexing":
        time.sleep(0.02)
    assert c.delete(f"/api/jobs/{jid}").json() == {"cancelling": jid}
    gate.set()
    job = _wait(c, jid)
    assert job["status"] == "cancelled"
    done = job["result"]["indexed"]
    assert done < 6 and job["result"]["status"] == "cancelled"
    assert c.get("/api/search/library/index").json()["last"]["status"] == "cancelled"

    again = _wait(c, c.post("/api/search/library/index").json()["job_id"])
    assert again["result"]["already"] == done
    assert again["result"]["indexed"] == 6 - done


def test_it_never_starts_on_its_own(tmp_path, monkeypatch):
    """No timer, no startup or scan hook: only the POST submits it (D-037's rule)."""
    media = _library(tmp_path)
    with _client(tmp_path, media, monkeypatch) as c:  # runs the lifespan (startup)
        c.get("/api/library/browse", params={"path": str(media)})
        c.get("/api/search/folder/index-status", params={"path": str(media)})
        c.get("/api/search/folder", params={"path": str(media), "query": "make it so"})
        time.sleep(0.2)
        assert c.get("/api/search/library/index").json() == {"job": None, "last": None}
        assert c.get("/api/jobs").json()["jobs"] == []
    src = inspect.getsource(web)
    assert src.count("lib_jobs.submit(") == 1
    assert "def library_index(" in src[src.index("lib_jobs.submit(") - 4000:src.index("lib_jobs.submit(")]


def test_a_run_cut_short_by_a_restart_reads_interrupted(tmp_path, monkeypatch):
    media = _library(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    (state / "library_index.json").write_text(json.dumps(
        {"paths": [str(media)], "status": "indexing", "done": 5, "todo": 9}))
    c = _client(tmp_path, media, monkeypatch)
    last = c.get("/api/search/library/index").json()["last"]
    assert last["status"] == "interrupted" and last["done"] == 5
    assert c.get("/api/jobs").json()["jobs"] == []  # and nothing restarted it


def test_a_file_it_cannot_tell_about_is_counted_and_left_for_the_next_run(tmp_path, monkeypatch):
    media = _library(tmp_path, subtitled=1, silent=0)
    (media / "broken.mkv").write_bytes(b"")
    c = _client(tmp_path, media, monkeypatch)

    def tracks(video):
        raise RuntimeError("ffprobe could not read it")

    monkeypatch.setattr(sub_cache_mod, "list_embedded_tracks", tracks)
    monkeypatch.setattr("quipclipper.subtitles.list_embedded_tracks", tracks)
    res = _wait(c, c.post("/api/search/library/index").json()["job_id"])["result"]
    assert (res["indexed"], res["failed"]) == (1, 1)
    assert res["errors"][0]["path"].endswith("broken.mkv")
    again = _wait(c, c.post("/api/search/library/index").json()["job_id"])["result"]
    assert (again["already"], again["todo"]) == (1, 1)  # tried again, not marked no_subs


def test_progress_has_an_eta(tmp_path):
    class Job:
        cancel_requested = False
        progress = result = None

    t = [0.0]

    def now():
        t[0] += 1.0
        return t[0]

    files = [(Path(f"/m/{i}.mkv"), "not_indexed") for i in range(10)]
    seen = []

    def index(f):
        seen.append((job.progress, dict(job.result)))
        return "indexed"

    job = Job()
    res = lib_index.run(job, paths=["/m"], scan=lambda: files, index=index,
                        log=lib_index.RunLog(tmp_path), now=now)
    assert res["indexed"] == 10 and res["eta_s"] is None and res["files_per_hour"]
    later = [r for _, r in seen if r["eta_s"] is not None]
    assert later and later[0]["eta_s"] > later[-1]["eta_s"]
    assert any("left" in p for p, _ in seen)
    assert json.loads((tmp_path / "library_index.json").read_text())["status"] == "done"


def test_sweeping_a_file_reads_only_its_own_entries(tmp_path, monkeypatch):
    """The stale-entry sweep after an extraction used to read every cache entry: a
    whole-library index was quadratic in the cache size."""
    cache = SubtitleCache(tmp_path)
    media = tmp_path / "m"
    media.mkdir()
    for i in range(20):
        (media / f"{i}.mkv").write_bytes(b"")
        (media / f"{i}.srt").write_text(SRT)
        cache.resolve(media / f"{i}.mkv")
    reads = []
    real = SubtitleCache._stored_video
    monkeypatch.setattr(SubtitleCache, "_stored_video",
                        lambda self, f: reads.append(f) or real(self, f))
    (media / "new.mkv").write_bytes(b"")
    (media / "new.srt").write_text(SRT)
    cache.resolve(media / "new.mkv")
    assert reads == []  # the owner map was built by the first sweep and kept current
    # Clearing still finds every entry, and a fresh cache rebuilds the map from disk.
    assert SubtitleCache(tmp_path).clear(media / "3.mkv") == 1
    assert cache.clear_under(media) == 20
    assert list((tmp_path / "sub_cache").glob("*.json")) == []
