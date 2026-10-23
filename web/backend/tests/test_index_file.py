"""Re-index hooks (M20): POST /api/index/file re-indexes the files another app says
changed (Bazarr, Sonarr, Radarr), queued, deduplicated, one job at a time, at idle
priority; QC_PATH_MAP maps their paths; a hook token opens nothing else."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from quipclipper import subtitles
from quipclipper_web import file_index
from quipclipper_web import sub_cache as sub_cache_mod
from quipclipper_web.app import create_app
from quipclipper_web.config import Settings
from quipclipper_web.sub_cache import SubtitleCache

SRT = "1\n00:00:01,000 --> 00:00:03,000\nMake it so.\n"
NEW_SRT = "1\n00:00:01,000 --> 00:00:03,000\nTea, Earl Grey, hot.\n"
API, HOOK = "api-token-1", "hook-token-1"


def _media(tmp_path: Path) -> Path:
    media = tmp_path / "srv" / "shows"
    show = media / "Show (2001) [x]" / "Season 01"
    show.mkdir(parents=True)
    for ep in ("Show - S01E01", "Show - S01E02"):
        (show / f"{ep}.mkv").write_bytes(b"")
        (show / f"{ep}.en.srt").write_text(SRT)
    (media / "silent.mkv").write_bytes(b"")
    return media


def _client(tmp_path: Path, media: Path, monkeypatch, **env) -> TestClient:
    # No ffprobe here: a file with no sidecar has no tracks (so it's marked no_subs).
    monkeypatch.setattr(sub_cache_mod, "list_embedded_tracks", lambda v: [])
    monkeypatch.setattr("quipclipper.subtitles.list_embedded_tracks", lambda v: [])
    return TestClient(create_app(Settings.from_env({
        "QC_MEDIA_ROOTS": str(media), "QC_STATE_DIR": str(tmp_path / "state"),
        "QC_API_TOKEN": API, "QC_HOOK_TOKEN": HOOK, **env})))


def _hook(c: TestClient, *paths: str, **params):
    return c.post("/api/index/file", params={"path": list(paths), **params},
                  headers={"Authorization": f"Bearer {HOOK}"})


def _settle(c: TestClient, timeout: float = 10) -> dict:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        st = c.get("/api/index/file", headers={"X-API-Key": HOOK}).json()
        if st["job_id"] is None and not st["pending"]:
            return st
        time.sleep(0.05)
    raise AssertionError(f"still re-indexing: {st}")


def _search(c: TestClient, folder: Path, q: str) -> list[str]:
    r = c.get("/api/search/folder", params={"path": str(folder), "query": q},
              headers={"X-API-Key": API}).json()
    return [m["text"] for m in r["matches"]]


def test_a_changed_subtitle_is_reindexed_and_search_finds_the_new_line(tmp_path, monkeypatch):
    media = _media(tmp_path)
    c = _client(tmp_path, media, monkeypatch)
    ep = media / "Show (2001) [x]" / "Season 01" / "Show - S01E01.mkv"
    sub = ep.with_name("Show - S01E01.en.srt")
    r = _hook(c, str(ep))
    assert r.status_code == 202, r.text
    assert r.json()["files"] == [{"path": str(ep), "media": str(ep), "status": "queued"}]
    st = _settle(c)
    assert st["recent"][-1]["status"] == "indexed"
    assert "Make it so." in _search(c, media, "make it so")

    # Bazarr replaces the subtitle: the entry goes stale, search leaves it out...
    sub.write_text(NEW_SRT)
    t = time.time() + 5
    os.utime(sub, (t, t))
    assert _search(c, media, "earl grey hot") == []
    # ...until the hook names the subtitle file; its media file is found by name.
    r = _hook(c, str(sub))
    assert r.json()["files"][0]["media"] == str(ep)
    assert _settle(c)["recent"][-1] == {**_settle(c)["recent"][-1], "status": "indexed"}
    assert _search(c, media, "earl grey hot") == ["Tea, Earl Grey, hot."]
    # Asked again with nothing changed, it is left alone.
    _hook(c, str(sub))
    assert _settle(c)["recent"][-1]["status"] == "current"
    # Unless forced.
    _hook(c, str(sub), force="true")
    assert _settle(c)["recent"][-1]["status"] == "indexed"


def test_path_map_maps_container_paths(tmp_path, monkeypatch):
    media = _media(tmp_path)
    c = _client(tmp_path, media, monkeypatch, QC_PATH_MAP=f"/tv={media};/movies=/nowhere")
    r = _hook(c, "/tv/Show (2001) [x]/Season 01/Show - S01E02.en.srt")
    assert r.status_code == 202
    assert r.json()["files"][0]["media"] == str(
        media / "Show (2001) [x]" / "Season 01" / "Show - S01E02.mkv")
    # A prefix only matches whole path components; an unmapped path is used as it is.
    r = _hook(c, "/tvx/a.mkv", "/movies/a.mkv")
    assert r.status_code == 422
    assert [f["status"] for f in r.json()["files"]] == ["outside_roots", "outside_roots"]


@pytest.mark.parametrize(("raw", "env", "want"), [
    ("/tv/a/b.mkv", "/tv=/srv/shows", "/srv/shows/a/b.mkv"),
    ("/tv", "/tv=/srv/shows", "/srv/shows"),
    ("/tv/a.mkv", "/tv=/srv/shows:/tv/a.mkv=/x/y.mkv", "/x/y.mkv"),  # longest wins
    ("/movies/a.mkv", "/tv=/srv/shows", "/movies/a.mkv"),  # no match: as given
    ("/tv/a.mkv", "/tv/=/srv/shows/", "/srv/shows/a.mkv"),  # trailing slashes
])
def test_map_in(raw, env, want):
    pm = Settings.from_env({"QC_PATH_MAP": env}).path_map
    assert file_index.map_in(raw, pm) == want


def test_subtitle_to_media(tmp_path):
    d = tmp_path
    for n in ("Movie (2020).mkv", "Movie (2020) Extended.mkv", "Movie.mkv", "Song.flac",
              "Movie (2020).en.srt"):
        (d / n).write_bytes(b"")
    assert file_index.media_for(d / "Movie (2020).en.forced.srt") == d / "Movie (2020).mkv"
    assert file_index.media_for(d / "Movie (2020) Extended.EN.SRT") == \
        d / "Movie (2020) Extended.mkv"  # longest name wins, any case
    assert file_index.media_for(d / "Movie.srt") == d / "Movie.mkv"
    assert file_index.media_for(d / "Song.lrc") is None  # not a subtitle extension
    assert file_index.media_for(d / "Song.en.vtt") == d / "Song.flac"
    assert file_index.media_for(d / "Other.en.srt") is None
    assert file_index.media_for(d / "Movie.mkv") == d / "Movie.mkv"


def test_bad_paths_are_reported_per_path(tmp_path, monkeypatch):
    media = _media(tmp_path)
    c = _client(tmp_path, media, monkeypatch)
    (media / "orphan.en.srt").write_text(SRT)
    (media / "notes.txt").write_text("x")
    ok = str(media / "silent.mkv")
    r = _hook(c, ok, str(media / "orphan.en.srt"), str(media / "notes.txt"),
              str(media / "gone.mkv"), "/etc/passwd", str(media / ".." / ".." / "x.mkv"))
    assert r.status_code == 202
    assert [f["status"] for f in r.json()["files"]] == [
        "queued", "no_media", "not_media", "not_found", "outside_roots", "outside_roots"]
    assert _settle(c)["recent"][-1] == {**_settle(c)["recent"][-1], "path": ok,
                                         "status": "no_subs"}
    assert _hook(c).status_code == 400  # no path at all


def test_repeats_are_deduplicated_and_one_job_drains_them(tmp_path, monkeypatch):
    media = _media(tmp_path)
    c = _client(tmp_path, media, monkeypatch)
    gate, started = threading.Event(), threading.Event()
    real = SubtitleCache.resolve

    def slow(self, video, track=None):
        started.set()
        gate.wait(5)
        return real(self, video, track)

    monkeypatch.setattr(SubtitleCache, "resolve", slow)
    season = media / "Show (2001) [x]" / "Season 01"
    e1, e2 = season / "Show - S01E01.mkv", season / "Show - S01E02.mkv"
    first = _hook(c, str(e1)).json()
    assert started.wait(5)  # the job holds e1
    # Bazarr fires once per subtitle language: e2 twice, and its subtitle once more.
    a = _hook(c, str(e2)).json()
    b = _hook(c, str(e2), str(season / "Show - S01E02.en.srt")).json()
    assert a["job_id"] == b["job_id"] == first["job_id"]
    assert [f["status"] for f in b["files"]] == ["already_queued", "already_queued"]
    assert c.get("/api/index/file", headers={"X-API-Key": HOOK}).json()["pending"] == [str(e2)]
    gate.set()
    st = _settle(c)
    assert [(r["path"], r["status"]) for r in st["recent"]] == [
        (str(e1), "indexed"), (str(e2), "indexed")]
    jobs = [j for j in c.get("/api/jobs", headers={"X-API-Key": API}).json()["jobs"]]
    assert len(jobs) == 1 and "low priority" in jobs[0]["label"]
    # Once drained, the next request starts a new job.
    assert _hook(c, str(e1)).json()["job_id"] not in (None, first["job_id"])


def test_reindexing_runs_at_idle_priority(tmp_path, monkeypatch):
    media = _media(tmp_path)
    c = _client(tmp_path, media, monkeypatch)
    flags = []
    real = SubtitleCache.resolve

    def resolve(self, video, track=None):
        flags.append(getattr(subtitles._idle, "on", False))
        return real(self, video, track)

    monkeypatch.setattr(SubtitleCache, "resolve", resolve)
    _hook(c, str(media / "Show (2001) [x]" / "Season 01" / "Show - S01E01.mkv"))
    _settle(c)
    assert flags == [True]


def test_a_library_run_takes_hook_files_between_its_own(tmp_path, monkeypatch):
    media = _media(tmp_path)
    c = _client(tmp_path, media, monkeypatch)
    order, gate = [], threading.Event()
    real = SubtitleCache.resolve

    def resolve(self, video, track=None):
        order.append(Path(video).name)
        if len(order) == 1:
            gate.wait(5)
        return real(self, video, track)

    monkeypatch.setattr(SubtitleCache, "resolve", resolve)
    lib = c.post("/api/search/library/index", headers={"X-API-Key": API}).json()["job_id"]
    end = time.monotonic() + 5
    while not order and time.monotonic() < end:
        time.sleep(0.02)
    # The library run holds its first file; a hook names a file it hasn't reached.
    hooked = media / "Show (2001) [x]" / "Season 01" / "Show - S01E02.mkv"
    assert _hook(c, str(hooked)).status_code == 202
    gate.set()
    _settle(c)
    end = time.monotonic() + 10
    while c.get(f"/api/jobs/{lib}", headers={"X-API-Key": API}).json()["status"] == "running":
        assert time.monotonic() < end
        time.sleep(0.05)
    st = c.get("/api/index/file", headers={"X-API-Key": HOOK}).json()
    assert st["recent"][0]["path"] == str(hooked)
    # The library run did it in its second slot, before its own remaining files.
    assert order[:2] == ["silent.mkv", "Show - S01E02.mkv"]


def test_webhook_bodies(tmp_path, monkeypatch):
    media = _media(tmp_path)
    c = _client(tmp_path, media, monkeypatch, QC_PATH_MAP=f"/tv={media}")
    auth = {"Authorization": f"Bearer {HOOK}"}
    test = c.post("/api/index/file", json={"eventType": "Test"}, headers=auth)
    assert test.status_code == 200 and test.json()["event"] == "Test"
    sonarr = {"eventType": "Download", "isUpgrade": True,
              "episodeFile": {"path": "/tv/Show (2001) [x]/Season 01/Show - S01E01.mkv"}}
    r = c.post("/api/index/file", json=sonarr, headers=auth)
    assert r.status_code == 202 and r.json()["event"] == "Download"
    assert r.json()["files"][0]["status"] == "queued"
    renamed = {"eventType": "Rename", "renamedEpisodeFiles": [
        {"path": "/tv/Show (2001) [x]/Season 01/Show - S01E02.mkv", "previousPath": "/tv/x"}]}
    assert c.post("/api/index/file", json=renamed, headers=auth).status_code == 202
    plain = {"paths": [str(media / "silent.mkv")], "force": True}
    assert c.post("/api/index/file", json=plain, headers=auth).status_code == 202
    radarr = {"eventType": "Download", "movieFile": {"path": "/m/a.mkv"}}
    assert file_index.webhook_paths(radarr) == (["/m/a.mkv"], "Download")
    assert file_index.webhook_paths({"eventType": "Rename", "renamedMovieFiles": [
        {"path": "/m/b.mkv"}]}) == (["/m/b.mkv"], "Rename")
    assert file_index.webhook_paths([1]) == ([], None)


def test_tokens_and_csrf(tmp_path, monkeypatch):
    media = _media(tmp_path)
    c = _client(tmp_path, media, monkeypatch)
    ep = str(media / "silent.mkv")
    url = "/api/index/file"
    # A browser-style POST with no token and no CSRF header is refused...
    assert c.post(url, params={"path": ep}).status_code == 403
    # ...a wrong explicit token is 401...
    assert c.post(url, params={"path": ep},
                  headers={"Authorization": "Bearer nope"}).status_code == 401
    # ...the hook token works as Bearer, X-API-Key or the Basic password (curl -u hook:T),
    for h in ({"Authorization": f"Bearer {HOOK}"}, {"X-API-Key": HOOK}):
        assert c.post(url, params={"path": ep}, headers=h).status_code == 202
    assert c.post(url, params={"path": ep}, auth=("hook", HOOK)).status_code == 202
    # ...and so does the full API token.
    assert c.post(url, params={"path": ep}, headers={"X-API-Key": API}).status_code == 202
    # The hook token opens nothing else, read or write.
    for method, other in (("get", "/api/library/roots"), ("get", "/api/jobs"),
                          ("post", "/api/search/library/index")):
        r = getattr(c, method)(other, headers={"X-API-Key": HOOK})
        assert r.status_code == 403, other
        assert "re-index" in r.json()["detail"]
    assert c.get("/api/library/roots", headers={"X-API-Key": API}).status_code == 200
    _settle(c)


def test_hook_token_from_a_file_and_never_in_repr(tmp_path):
    f = tmp_path / "hook"
    f.write_text("from-file\n")
    s = Settings.from_env({"QC_HOOK_TOKEN_FILE": str(f)})
    assert s.hook_tokens == frozenset({"from-file"}) and "from-file" not in repr(s)
    assert Settings.from_env({"QC_HOOK_TOKEN": "a, b"}).hook_tokens == frozenset({"a", "b"})


def test_queue_retires_its_job_under_the_add_lock():
    q = file_index.FileQueue()
    submitted = []
    q.add([(Path("/a"), False)], lambda: submitted.append(1) or "j1")
    assert q.take() == (Path("/a"), False)
    # Not retired yet: an add now joins the running job.
    assert q.add([(Path("/b"), False)], lambda: "j2")[0] == "j1"
    assert q.take() == (Path("/b"), False)
    assert q.take() is None  # empty: retired...
    assert q.add([(Path("/c"), True)], lambda: "j3")[0] == "j3"  # ...so a new job starts
    # A force request upgrades the waiting one; retire() drops what waits.
    q.add([(Path("/c"), False)], lambda: "jx")
    assert q.retire() == [Path("/c")] and q.status()["job_id"] is None
