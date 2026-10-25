"""Jellyfin: now playing (M15), and titles, artwork and library scans (M18).

A fake Jellyfin (a real HTTP server on a loopback port) answers ``/Sessions``, ``/Items``
(one item, or the whole library in pages), images, ``/Library/VirtualFolders`` and library
refreshes, so the client's urllib requests, auth header and error handling run for real.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from quipclipper_web import jellyfin
from quipclipper_web.app import create_app
from quipclipper_web.config import Settings

KEY = "jf-test-key"


def _iso(dt: datetime) -> str:
    # Jellyfin's own format: 7 fractional digits and a Z.
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "0Z"


class FakeJellyfin:
    def __init__(self):
        self.sessions: list[dict] = []
        self.items: dict[str, dict] = {}
        self.library: list[dict] = []  # what GET /Items?Recursive=true lists
        self.folders: list[dict] = []  # /Library/VirtualFolders
        self.images: dict[tuple[str, str], tuple[bytes, str]] = {}
        self.refreshed: list[str] = []
        self.requests: list[tuple[str, dict, str]] = []
        self.status = 200
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _start(self):
                url = urlparse(self.path)
                query = {k: v[0] for k, v in parse_qs(url.query).items()}
                fake.requests.append((url.path, query, self.headers.get("Authorization", "")))
                if self.headers.get("Authorization") != f'MediaBrowser Token="{KEY}"':
                    self._send(401, {})
                    return None, None
                if fake.status != 200:
                    self._send(fake.status, {})
                    return None, None
                return url, query

            def do_GET(self):
                url, query = self._start()
                if url is None:
                    return
                if url.path == "/Sessions":
                    return self._send(200, fake.sessions)
                if url.path == "/Items" and query.get("Recursive") == "true":
                    start, limit = int(query["StartIndex"]), int(query["Limit"])
                    return self._send(200, {"Items": fake.library[start:start + limit]})
                if url.path == "/Items":
                    ids = query.get("Ids", "")
                    return self._send(200, {"Items": [fake.items[ids]] if ids in fake.items
                                            else []})
                if url.path == "/Library/VirtualFolders":
                    return self._send(200, fake.folders)
                parts = url.path.split("/")
                if len(parts) == 5 and parts[1] == "Items" and parts[3] == "Images":
                    if (parts[2], parts[4]) in fake.images:
                        body, ctype = fake.images[(parts[2], parts[4])]
                        return self._send(200, body, ctype)
                self._send(404, {})

            def do_POST(self):
                url, _ = self._start()
                if url is None:
                    return
                parts = url.path.split("/")
                if len(parts) == 4 and parts[1] == "Items" and parts[3] == "Refresh":
                    fake.refreshed.append(parts[2])
                    self.send_response(204)
                    self.end_headers()
                    return
                self._send(404, {})

            def _send(self, code, body, ctype="application/json"):
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def fake():
    f = FakeJellyfin()
    yield f
    f.close()


@pytest.fixture()
def media(tmp_path):
    root = tmp_path / "shows"
    ep = root / "Star Trek TNG" / "Season 3" / "S03E26.mkv"
    ep.parent.mkdir(parents=True)
    ep.write_bytes(b"")
    (tmp_path / "elsewhere").mkdir()
    return root, ep


def _client(env: dict[str, str]) -> TestClient:
    c = TestClient(create_app(Settings.from_env(env)))
    c.headers["X-Quipclipper"] = "1"
    return c


def _env(fake, tmp_path, root, **extra) -> dict[str, str]:
    return {
        "QC_MEDIA_ROOTS": str(root),
        "QC_STATE_DIR": str(tmp_path / "state"),
        "QC_CLIPS_DIR": str(tmp_path / "clips"),
        "QC_JELLYFIN_URL": fake.url + "/",
        "QC_JELLYFIN_API_KEY": KEY,
        "QC_JELLYFIN_PATH_MAP": f"/shows={root}:/elsewhere={tmp_path / 'elsewhere'}",
        **extra,
    }


def _session(**over) -> dict:
    s = {
        "Id": "sess1", "UserName": "wiz", "DeviceName": "Living room TV",
        "Client": "Jellyfin Android TV",
        "NowPlayingItem": {
            "Id": "item1", "Name": "The Best of Both Worlds", "SeriesName": "Star Trek TNG",
            "ParentIndexNumber": 3, "IndexNumber": 26, "Type": "Episode",
            "RunTimeTicks": 2700 * jellyfin.TICKS,
            "Path": "/shows/Star Trek TNG/Season 3/S03E26.mkv",
        },
        "PlayState": {"PositionTicks": 1234 * jellyfin.TICKS, "IsPaused": True},
    }
    s.update(over)
    return s


# --- settings --------------------------------------------------------------------------


def test_settings_read_url_key_file_and_path_map(tmp_path):
    key_file = tmp_path / "key"
    key_file.write_text("from-file\n")
    s = Settings.from_env({"QC_JELLYFIN_URL": "http://jf:8096/",
                           "QC_JELLYFIN_API_KEY_FILE": str(key_file),
                           "QC_JELLYFIN_PATH_MAP": "/movies=/srv/movies/:/shows/=/srv/tv:bad"})
    assert s.jellyfin_url == "http://jf:8096"
    assert s.jellyfin_api_key == "from-file"
    assert s.jellyfin_path_map == (("/movies", "/srv/movies"), ("/shows", "/srv/tv"))
    assert s.jellyfin_enabled
    assert "from-file" not in repr(s)


def test_settings_off_without_key_or_with_an_unreadable_key_file(tmp_path):
    assert not Settings.from_env({"QC_JELLYFIN_URL": "http://jf"}).jellyfin_enabled
    assert not Settings.from_env({"QC_JELLYFIN_URL": "http://jf", "QC_JELLYFIN_API_KEY_FILE":
                                  str(tmp_path / "missing")}).jellyfin_enabled
    assert not Settings.from_env({"QC_JELLYFIN_API_KEY": "k"}).jellyfin_enabled


def test_health_and_config_say_whether_jellyfin_is_on(fake, tmp_path, media):
    root, _ = media
    off = _client({"QC_MEDIA_ROOTS": str(root)})
    assert off.get("/api/health").json()["jellyfin"] is False
    assert off.get("/api/config").json()["jellyfin"] is False
    on = _client(_env(fake, tmp_path, root))
    assert on.get("/api/health").json()["jellyfin"] is True
    assert on.get("/api/config").json()["jellyfin"] is True


# --- path mapping ------------------------------------------------------------------------


def test_map_path_longest_prefix_on_whole_components():
    m = (("/video", "/zfs/video"), ("/video/kids", "/srv/kids"), ("/", "/root"))
    assert jellyfin.map_path("/video/a.mkv", m) == "/zfs/video/a.mkv"
    assert jellyfin.map_path("/video/kids/b.mkv", m) == "/srv/kids/b.mkv"
    assert jellyfin.map_path("/videos/c.mkv", m) == "/root/videos/c.mkv"
    assert jellyfin.map_path("/videos/c.mkv", m[:2]) is None
    assert jellyfin.map_path("/video", m) == "/zfs/video"


# --- /api/jellyfin/sessions --------------------------------------------------------------


def test_sessions_maps_the_path_and_reports_the_playhead(fake, tmp_path, media):
    root, ep = media
    fake.sessions = [_session(), {"Id": "idle", "UserName": "wiz", "DeviceName": "Phone"}]
    resp = _client(_env(fake, tmp_path, root)).get("/api/jellyfin/sessions")
    assert resp.status_code == 200, resp.text
    (s,) = resp.json()["sessions"]  # the idle session (nothing playing) is left out
    assert s["path"] == str(ep.resolve())
    assert s["position"] == 1234.0 and s["paused"] is True
    assert s["series"] == "Star Trek TNG" and (s["season"], s["episode"]) == (3, 26)
    assert s["duration"] == 2700.0 and s["note"] is None
    path, query, auth = fake.requests[0]
    assert path == "/Sessions" and query["activeWithinSeconds"] == "600"
    assert auth == f'MediaBrowser Token="{KEY}"'


def test_a_playing_session_extrapolates_from_its_last_report(fake, tmp_path, media):
    root, _ = media
    now = datetime.now(timezone.utc)
    fake.sessions = [
        _session(Id="a", PlayState={"PositionTicks": 100 * jellyfin.TICKS, "IsPaused": False},
                 LastPlaybackCheckIn=_iso(now - timedelta(seconds=6))),
        # A client silent for minutes is not assumed to have played on for minutes.
        _session(Id="b", PlayState={"PositionTicks": 100 * jellyfin.TICKS, "IsPaused": False},
                 LastPlaybackCheckIn=_iso(now - timedelta(minutes=5))),
    ]
    rows = {s["id"]: s for s in
            _client(_env(fake, tmp_path, root)).get("/api/jellyfin/sessions").json()["sessions"]}
    assert 105.5 < rows["a"]["position"] < 108
    assert rows["a"]["reported_position"] == 100.0
    assert rows["b"]["position"] == 100.0 + jellyfin.MAX_EXTRAPOLATE


def test_missing_path_is_looked_up_on_the_item(fake, tmp_path, media):
    root, ep = media
    s = _session()
    del s["NowPlayingItem"]["Path"]
    fake.sessions = [s]
    fake.items["item1"] = {"Id": "item1", "Path": "/shows/Star Trek TNG/Season 3/S03E26.mkv"}
    (row,) = _client(_env(fake, tmp_path, root)).get("/api/jellyfin/sessions").json()["sessions"]
    assert row["path"] == str(ep.resolve())
    assert ("/Items", {"Ids": "item1", "Fields": "Path"}) in [r[:2] for r in fake.requests]


def test_unmapped_or_outside_the_roots_has_no_path_and_says_why(fake, tmp_path, media):
    root, _ = media
    s1 = _session(Id="unmapped")
    s1["NowPlayingItem"] = {**s1["NowPlayingItem"], "Path": "/music/song.flac"}
    s2 = _session(Id="outside")  # mapped, but /elsewhere is not a media root
    s2["NowPlayingItem"] = {**s2["NowPlayingItem"], "Path": "/elsewhere/x.mkv"}
    fake.sessions = [s1, s2]
    rows = {r["id"]: r for r in
            _client(_env(fake, tmp_path, root)).get("/api/jellyfin/sessions").json()["sessions"]}
    assert rows["unmapped"]["path"] is None and "no path mapping" in rows["unmapped"]["note"]
    assert rows["outside"]["path"] is None and "media roots" in rows["outside"]["note"]
    assert rows["unmapped"]["jellyfin_path"] == "/music/song.flac"


def test_not_configured_is_503(tmp_path, media):
    root, _ = media
    resp = _client({"QC_MEDIA_ROOTS": str(root)}).get("/api/jellyfin/sessions")
    assert resp.status_code == 503 and "isn't configured" in resp.json()["detail"]


def test_bad_key_and_unreachable_are_502(fake, tmp_path, media):
    root, _ = media
    resp = _client(_env(fake, tmp_path, root, QC_JELLYFIN_API_KEY="wrong")).get(
        "/api/jellyfin/sessions")
    assert resp.status_code == 502 and "API key" in resp.json()["detail"]
    url = fake.url
    fake.close()
    resp = _client(_env(fake, tmp_path, root, QC_JELLYFIN_URL=url)).get("/api/jellyfin/sessions")
    assert resp.status_code == 502 and "unreachable" in resp.json()["detail"]
    fake.close = lambda: None  # already closed


# --- the CLI: jellyfin.sessions (nowplaying) and clip.nowplaying, through the app ------

SRT = ("1\n00:00:01,000 --> 00:00:03,000\nHello there.\n\n"
       "2\n00:00:04,000 --> 00:00:06,500\nMake it so.\n\n"
       "3\n00:00:09,000 --> 00:00:11,000\nEngage.\n")


class _Transport:
    def __init__(self, client: TestClient):
        self.client = client
        self.bodies: list[dict] = []

    def request(self, method, path, query, body, stream=False):
        from quipclipper.client import Response

        if path == "/api/clip":
            self.bodies.append(body)
        r = self.client.request(method, path, params=query, json=body)
        return Response(r.status_code, {k.lower(): v for k, v in r.headers.items()}, r.content)


@pytest.fixture()
def cli_run(fake, tmp_path, media, monkeypatch, capsys):
    from unittest.mock import patch

    from quipclipper import cli

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    for var in ("QC_URL", "QC_API_TOKEN", "QC_API_TOKEN_FILE"):
        monkeypatch.delenv(var, raising=False)
    root, ep = media
    ep.with_suffix(".srt").write_text(SRT, encoding="utf-8")
    transport = _Transport(_client(_env(fake, tmp_path, root)))

    def fake_cut(src, rng, **kw):
        out = Path(kw["out"])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"clip")
        return out

    def run(*argv):
        with patch("quipclipper_web.app.cut_clip", side_effect=fake_cut):
            code = cli.main(["--json", *argv], transport=transport)
        out = capsys.readouterr().out.strip().splitlines()
        return code, json.loads(out[-1]) if out else None

    run.transport = transport
    return run


def test_cli_nowplaying_lists_the_sessions(fake, cli_run):
    fake.sessions = [_session()]
    code, out = cli_run("nowplaying")
    assert code == 0 and out["sessions"][0]["device"] == "Living room TV"


def test_cli_clip_nowplaying_cuts_the_line_before_the_playhead(fake, cli_run):
    fake.sessions = [_session(PlayState={"PositionTicks": 8 * jellyfin.TICKS, "IsPaused": True})]
    code, out = cli_run("clip.nowplaying", "--kind", "audio", "--backend", "ffmpeg")
    assert code == 0, out
    assert out["status"] == "done" and out["line"]["text"] == "Make it so."
    # The job names the clip as the clips library has it, so `clips.download` can fetch it.
    (f,) = out["files"]
    assert f["clip"].endswith("/" + f["name"]) and not f["clip"].startswith("/")
    assert out["device"] == "Living room TV"
    (body,) = cli_run.transport.bodies
    assert (body["start"], body["end"], body["kind"]) == (4.0, 6.5, "audio")
    assert body["cue_text"] == "Make it so." and body["before"] == 2.0


def test_cli_clip_nowplaying_back_and_lines(fake, cli_run):
    fake.sessions = [_session(PlayState={"PositionTicks": 10 * jellyfin.TICKS, "IsPaused": True})]
    code, out = cli_run("clip.nowplaying", "--back", "5", "--lines", "2", "--no-wait")
    assert code == 0, out
    (body,) = cli_run.transport.bodies
    assert (body["start"], body["end"]) == (1.0, 6.5)  # at 5 s: the two lines started by then
    assert out["line"]["text"] == "Hello there. Make it so."


def test_cli_clip_nowplaying_several_sessions_asks_which(fake, cli_run):
    fake.sessions = [_session(), _session(Id="sess2", DeviceName="Bedroom")]
    code, out = cli_run("clip.nowplaying")
    assert code == 2 and out["error"]["code"] == "ambiguous"
    code, out = cli_run("clip.nowplaying", "--session", "bedroom", "--no-wait")
    assert code == 0 and out["session"] == "sess2"


# --- titles and artwork (M18): the path index ------------------------------------------

EP_ID, SERIES_ID, SEASON_ID, MOVIE_ID = "a" * 32, "b" * 32, "d" * 32, "9" * 32
TAG, SERIES_TAG, SEASON_TAG = "f" * 32, "c" * 32, "e" * 32


def _episode(**over) -> dict:
    item = {
        "Id": EP_ID, "Type": "Episode", "Name": "The Best of Both Worlds",
        "SeriesName": "Star Trek: The Next Generation", "SeriesId": SERIES_ID,
        "SeriesPrimaryImageTag": SERIES_TAG, "SeasonId": SEASON_ID, "SeasonName": "Season 3",
        "ParentPrimaryImageItemId": SEASON_ID, "ParentPrimaryImageTag": SEASON_TAG,
        "ParentIndexNumber": 3, "IndexNumber": 26, "ProductionYear": 1990,
        "ImageTags": {"Primary": TAG}, "ProviderIds": {"Tvdb": "123", "Imdb": "tt0708700"},
        "Path": "/shows/Star Trek TNG/Season 3/S03E26.mkv",
    }
    item.update(over)
    return item


@pytest.fixture()
def known(fake, media):
    """A Jellyfin that knows the TNG episode (and a show outside the roots, unmapped)."""
    fake.library = [_episode(), _episode(Id="1" * 32, Path="/music/x.flac")]
    return media


def _wait_job(c: TestClient, job_id: str) -> dict:
    import time

    for _ in range(200):
        job = c.get(f"/api/jobs/{job_id}").json()
        if job["status"] not in ("queued", "running"):
            return job
        time.sleep(0.02)
    raise AssertionError("job never finished")


def test_reindex_builds_the_index_and_keeps_it_on_disk(fake, tmp_path, known):
    root, ep = known
    c = _client(_env(fake, tmp_path, root))
    resp = c.post("/api/jellyfin/index")
    assert resp.status_code == 200, resp.text
    index = resp.json()["index"]
    # The episode, its season folder and its show folder; the unmapped track is left out.
    assert (index["files"], index["folders"], index["error"]) == (1, 2, None)
    assert index["built"].endswith("Z")
    assert (tmp_path / "state" / "jellyfin-index.json").exists()
    # A restart reads the disk copy: a lookup answers without asking Jellyfin again.
    fake.requests.clear()
    item = _client(_env(fake, tmp_path, root)).get(
        "/api/jellyfin/lookup", params={"path": str(ep)}).json()["item"]
    assert item["title"] == "The Best of Both Worlds"
    assert not [r for r in fake.requests if r[1].get("Recursive")]


def test_the_index_is_read_in_pages(fake, tmp_path, media, monkeypatch):
    from quipclipper_web import jellyfin_index

    root, _ = media
    monkeypatch.setattr(jellyfin_index, "PAGE_SIZE", 2)
    fake.library = [_episode(Id=f"{i:032x}", Path=f"/shows/S/Season 1/e{i}.mkv")
                    for i in range(5)]
    index = _client(_env(fake, tmp_path, root)).post("/api/jellyfin/index").json()["index"]
    assert index["files"] == 5
    pages = [r[1] for r in fake.requests if r[1].get("Recursive")]
    assert [p["StartIndex"] for p in pages] == ["0", "2", "4"]
    assert pages[0]["IncludeItemTypes"] == "Movie,Episode,Audio,AudioBook"
    # Movies in a collection are listed themselves, not as the collection.
    assert pages[0]["CollapseBoxSetItems"] == "false"
    assert set(pages[0]["Fields"].split(",")) >= {
        "Path", "ProviderIds", "ParentIndexNumber", "IndexNumber", "SeriesName",
        "ProductionYear", "ImageTags"}


def test_a_failed_build_is_reported_and_lookups_fall_back(fake, tmp_path, known):
    root, ep = known
    c = _client(_env(fake, tmp_path, root))
    fake.status = 500
    resp = c.post("/api/jellyfin/index")
    assert resp.status_code == 502
    fake.status = 200
    body = c.get("/api/jellyfin/lookup", params={"path": str(ep)}).json()
    assert body["known"] is False and body["index"]["error"] == "Jellyfin answered 500"
    assert body["naming"]["title"] == "Star_Trek_TNG"  # parsed: the show folder
    assert (body["naming"]["season"], body["naming"]["episode"]) == ("03", "26")


def test_lookup_reports_the_item_and_its_clip_names(fake, tmp_path, known):
    root, ep = known
    c = _client(_env(fake, tmp_path, root))
    c.post("/api/jellyfin/index")
    body = c.get("/api/jellyfin/lookup", params={"path": str(ep)}).json()
    assert body["known"] is True and body["path"] == str(ep.resolve())
    item = body["item"]
    assert (item["id"], item["type"], item["show"]) == (EP_ID, "Episode",
                                                       "Star Trek: The Next Generation")
    assert (item["season"], item["episode"], item["year"]) == (3, 26, 1990)
    assert item["provider_ids"] == {"Tvdb": "123", "Imdb": "tt0708700"}
    assert item["art"] == {"id": EP_ID, "type": "Primary", "tag": TAG, "wide": True}
    assert body["naming"] == {"title": "The_Best_of_Both_Worlds",
                              "show": "Star_Trek_The_Next_Generation", "season": "03",
                              "episode": "26", "year": "1990", "folder": "Star_Trek_TNG"}
    show = c.get("/api/jellyfin/lookup", params={"path": str(root / "Star Trek TNG")}).json()
    assert show["item"]["type"] == "Series" and show["item"]["art"]["tag"] == SERIES_TAG
    assert show["naming"] is None


def test_lookup_checks_the_path_and_needs_jellyfin(fake, tmp_path, known):
    root, _ = known
    c = _client(_env(fake, tmp_path, root))
    assert c.get("/api/jellyfin/lookup", params={"path": str(tmp_path / "elsewhere")}
                 ).status_code == 403
    assert c.get("/api/jellyfin/lookup", params={"path": str(root / "nope.mkv")}
                 ).status_code == 404
    off = _client({"QC_MEDIA_ROOTS": str(root)})
    assert off.get("/api/jellyfin/lookup", params={"path": str(root)}).status_code == 503
    assert off.post("/api/jellyfin/index").status_code == 503
    assert off.get(f"/api/jellyfin/image/{EP_ID}").status_code == 503


def test_library_rows_and_the_item_carry_the_poster(fake, tmp_path, known):
    root, ep = known
    c = _client(_env(fake, tmp_path, root))
    c.post("/api/jellyfin/index")
    (show,) = c.get("/api/library/browse", params={"path": str(root)}).json()["entries"]
    assert show["jellyfin"]["art"] == {"id": SERIES_ID, "type": "Primary", "tag": SERIES_TAG}
    assert show["jellyfin"]["title"] == "Star Trek: The Next Generation"
    (season,) = c.get("/api/library/browse",
                      params={"path": str(root / "Star Trek TNG")}).json()["entries"]
    assert season["jellyfin"]["art"]["id"] == SEASON_ID
    assert season["jellyfin"]["title"] == "Star Trek: The Next Generation · Season 3"
    (row,) = c.get("/api/library/browse", params={"path": str(ep.parent)}).json()["entries"]
    assert row["jellyfin"]["art"]["wide"] is True and row["jellyfin"]["episode"] == 26
    found = c.get("/api/library/search", params={"query": "trek"}).json()["entries"]
    assert found[0]["jellyfin"]["id"] == SERIES_ID
    with patch_item_info():
        info = c.get("/api/items", params={"path": str(ep)}).json()
    assert info["jellyfin"]["title"] == "The Best of Both Worlds"


def patch_item_info():
    from unittest.mock import patch

    return patch("quipclipper_web.app.media.item_info",
                 return_value={"duration": 2700.0, "streams": [], "size": 0})


def test_rows_jellyfin_doesnt_know_have_no_poster(fake, tmp_path, media):
    root, ep = media
    c = _client(_env(fake, tmp_path, root))
    c.post("/api/jellyfin/index")  # an empty library
    (row,) = c.get("/api/library/browse", params={"path": str(ep.parent)}).json()["entries"]
    assert "jellyfin" not in row
    off = _client({"QC_MEDIA_ROOTS": str(root)})
    (row,) = off.get("/api/library/browse", params={"path": str(ep.parent)}).json()["entries"]
    assert "jellyfin" not in row


def test_a_stale_index_rebuilds_in_the_background(fake, tmp_path, known):
    from quipclipper_web import jellyfin_index

    root, ep = known
    now = [1000.0]
    index = jellyfin_index.JellyfinIndex(
        tmp_path / "state", lambda: jellyfin.JellyfinClient(fake.url, KEY),
        (("/shows", str(root)),), [root], refresh_every=3600, clock=lambda: now[0])
    assert index.lookup(ep.resolve()) is None  # nothing yet: it starts building
    index._thread.join(5)
    assert index.lookup(ep.resolve())["title"] == "The Best of Both Worlds"
    fake.library[0]["Name"] = "Renamed"
    now[0] += 60
    index.lookup(ep.resolve())  # a minute on: still fresh, no rebuild
    builds = len([r for r in fake.requests if r[1].get("Recursive")])
    now[0] += 3600
    index.lookup(ep.resolve())  # an hour on: rebuilt, the old entry answers meanwhile
    index._thread.join(5)
    assert len([r for r in fake.requests if r[1].get("Recursive")]) == builds + 1
    assert index.lookup(ep.resolve())["title"] == "Renamed"


def test_an_index_mapped_with_another_path_map_is_not_reused(fake, tmp_path, known):
    from quipclipper_web import jellyfin_index

    root, ep = known
    mk = lambda pm: jellyfin_index.JellyfinIndex(  # noqa: E731
        tmp_path / "state", lambda: jellyfin.JellyfinClient(fake.url, KEY), pm, [root])
    mk((("/shows", str(root)),)).build()
    assert mk((("/shows", str(root)),)).built is not None
    assert mk((("/tv", str(root)),)).built is None


def test_two_movies_in_one_folder_give_the_folder_no_poster(tmp_path):
    from quipclipper_web import jellyfin_index

    movie = {"Type": "Movie", "Name": "A", "Id": MOVIE_ID, "ImageTags": {"Primary": TAG},
             "Path": "/movies/Both/a.mkv"}
    files, folders = jellyfin_index.build_index(
        [movie, {**movie, "Id": "8" * 32, "Name": "B", "Path": "/movies/Both/b.mkv"},
         {**movie, "Id": "7" * 32, "Path": "/movies/Solo (2001)/s.mkv", "ProductionYear": 2001}],
        (("/movies", "/m"),), [Path("/m")])
    assert set(files) == {"/m/Both/a.mkv", "/m/Both/b.mkv", "/m/Solo (2001)/s.mkv"}
    assert set(folders) == {"/m/Solo (2001)"}
    assert folders["/m/Solo (2001)"]["year"] == 2001
    assert files["/m/Both/a.mkv"]["show"] == "A"  # {show} for a movie: its title


# --- titles in clip names ----------------------------------------------------------------


def _clip(c: TestClient, path: Path, template: str | None) -> Path:
    from unittest.mock import patch

    out: list[Path] = []

    def fake_cut(src, rng, **kw):
        p = Path(kw["out"])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"clip")
        out.append(p)
        return p

    body = {"path": str(path), "start": 10.0, "end": 12.0, "kind": "video",
            "backend": "ffmpeg", "cue_text": "Mr. Worf, fire!", "embed_subs": False}
    if template is not None:
        body["template"] = template
    with patch("quipclipper_web.app.cut_clip", side_effect=fake_cut):
        resp = c.post("/api/clip", json=body)
        assert resp.status_code == 200, resp.text
        assert _wait_job(c, resp.json()["job_id"])["status"] == "done"
    return out[0]


def test_clip_names_use_jellyfin_titles(fake, tmp_path, known):
    root, ep = known
    c = _client(_env(fake, tmp_path, root))
    c.post("/api/jellyfin/index")
    clips = tmp_path / "clips"
    out = _clip(c, ep, "{show}/S{season}E{episode}_{title}_{year}")
    assert out.relative_to(clips).as_posix() == (
        "Star_Trek_The_Next_Generation/S03E26_The_Best_of_Both_Worlds_1990.mkv")
    # The clip's metadata sidecar keeps what Jellyfin knows (M18, the clips API).
    from quipclipper_web import clipmeta
    jf = clipmeta.read(out)["jellyfin"]
    assert {k: jf[k] for k in ("show", "season", "episode", "title", "year")} == {
        "show": "Star Trek: The Next Generation", "season": 3, "episode": 26,
        "title": "The Best of Both Worlds", "year": 1990}
    assert jf["type"] == "Episode" and jf["id"]
    # And the sidecar is hidden from the scan of the clips library.
    assert clipmeta.sidecar_path(out).name.startswith(".")


def test_the_default_name_doesnt_move(fake, tmp_path, known):
    root, ep = known
    c = _client(_env(fake, tmp_path, root))
    c.post("/api/jellyfin/index")
    clips = tmp_path / "clips"
    want = "S03E26/00-00-08_Mr_Worf_fire_Star_Trek_TNG"
    assert _clip(c, ep, None).relative_to(clips).as_posix() == want + ".mkv"
    # The earlier default, as a saved setting or an old tab sends it, names it the same.
    legacy = _clip(c, ep, "{source}/{timestamp}_{cue}_{title}")
    assert legacy.relative_to(clips).as_posix() == want + "_2.mkv"


def test_a_file_jellyfin_doesnt_know_keeps_the_parsed_names(fake, tmp_path, media):
    root, ep = media
    c = _client(_env(fake, tmp_path, root))
    c.post("/api/jellyfin/index")
    out = _clip(c, ep, "{show}/S{season}E{episode}_{title}")
    assert out.relative_to(tmp_path / "clips").as_posix() == "Star_Trek_TNG/S03E26_Star_Trek_TNG.mkv"


# --- the image proxy ---------------------------------------------------------------------


def test_image_proxy_keeps_the_key_and_caps_the_width(fake, tmp_path, media):
    root, _ = media
    fake.images[(SERIES_ID, "Primary")] = (b"\xff\xd8jpeg", "image/jpeg")
    c = _client(_env(fake, tmp_path, root))
    resp = c.get(f"/api/jellyfin/image/{SERIES_ID}", params={"w": 5000, "tag": SERIES_TAG})
    assert resp.status_code == 200 and resp.content == b"\xff\xd8jpeg"
    assert resp.headers["content-type"] == "image/jpeg"
    assert "immutable" in resp.headers["cache-control"]
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert KEY not in resp.headers.values()
    path, query, auth = fake.requests[-1]
    assert path == f"/Items/{SERIES_ID}/Images/Primary" and auth.endswith(f'"{KEY}"')
    assert (query["maxWidth"], query["tag"]) == ("800", SERIES_TAG)
    untagged = c.get(f"/api/jellyfin/image/{SERIES_ID}")
    assert untagged.headers["cache-control"] == "private, max-age=86400"
    assert fake.requests[-1][1]["maxWidth"] == "300"


def test_image_proxy_validates_and_refuses_what_isnt_a_poster(fake, tmp_path, media):
    root, _ = media
    c = _client(_env(fake, tmp_path, root))
    for bad in ("../../System/Info", "abc", "A" * 32):
        assert c.get(f"/api/jellyfin/image/{bad}").status_code in (404, 422), bad
    assert c.get(f"/api/jellyfin/image/{EP_ID}", params={"type": "Logo"}).status_code == 422
    assert c.get(f"/api/jellyfin/image/{EP_ID}", params={"tag": "x/y"}).status_code == 422
    assert c.get(f"/api/jellyfin/image/{EP_ID}", params={"w": 1}).status_code == 422
    assert c.get(f"/api/jellyfin/image/{EP_ID}").status_code == 404  # Jellyfin has none
    fake.images[(EP_ID, "Primary")] = (b"<svg onload=alert(1)>", "image/svg+xml")
    assert c.get(f"/api/jellyfin/image/{EP_ID}").status_code == 502
    assert not [r for r in fake.requests if "System" in r[0]]


def test_now_playing_carries_the_series_poster(fake, tmp_path, known):
    root, _ = known
    s = _session()
    s["NowPlayingItem"].update(Id=EP_ID, SeriesId=SERIES_ID, SeriesPrimaryImageTag=SERIES_TAG,
                               ImageTags={"Primary": TAG})
    movie = _session(Id="m")
    movie["NowPlayingItem"] = {"Id": MOVIE_ID, "Type": "Movie", "Name": "Film",
                               "ImageTags": {"Primary": TAG}, "Path": "/music/film.mkv"}
    fake.sessions = [s, movie]
    rows = {r["id"]: r for r in
            _client(_env(fake, tmp_path, root)).get("/api/jellyfin/sessions").json()["sessions"]}
    assert rows["sess1"]["art"] == {"id": SERIES_ID, "type": "Primary", "tag": SERIES_TAG}
    assert rows["m"]["art"] == {"id": MOVIE_ID, "type": "Primary", "tag": TAG}


def test_now_playing_falls_back_to_the_index_for_artwork(fake, tmp_path, known):
    root, _ = known
    c = _client(_env(fake, tmp_path, root))
    c.post("/api/jellyfin/index")
    fake.sessions = [_session()]  # its NowPlayingItem has no image tags
    (row,) = c.get("/api/jellyfin/sessions").json()["sessions"]
    assert row["art"]["id"] == EP_ID


# --- the clips library scan --------------------------------------------------------------


def _clips_env(fake, tmp_path, root):
    return _env(fake, tmp_path, root, QC_JELLYFIN_PATH_MAP=(
        f"/shows={root}:/clips={tmp_path / 'clips'}"))


@pytest.fixture()
def quick_scan(monkeypatch):
    from quipclipper_web import jellyfin_index

    monkeypatch.setattr(jellyfin_index, "REFRESH_DELAY", 0.2)


def _settle(fake, n=1):
    import time

    for _ in range(100):
        if len(fake.refreshed) >= n:
            break
        time.sleep(0.02)
    time.sleep(0.3)  # and nothing more follows


def test_clips_landing_scan_their_library_once(fake, tmp_path, media, monkeypatch):
    import time

    from quipclipper_web import jellyfin_index

    # Long enough that the second cut lands inside the delay even on a loaded build host
    # (a 0.2 s delay failed on wizrock at load 40: two scans).
    monkeypatch.setattr(jellyfin_index, "REFRESH_DELAY", 3.0)
    root, ep = media
    fake.folders = [
        {"Name": "Shows", "ItemId": "5" * 32, "Locations": ["/shows"]},
        {"Name": "Clips", "ItemId": "6" * 32, "Locations": ["/clips"]},
    ]
    c = _client(_clips_env(fake, tmp_path, root))
    _clip(c, ep, "{timestamp}")
    _clip(c, ep, "{timestamp}")  # a second clip inside the delay: still one scan
    for _ in range(300):
        if fake.refreshed:
            break
        time.sleep(0.05)
    time.sleep(0.5)  # and nothing more follows
    assert fake.refreshed == ["6" * 32]
    (_, query, _) = [r for r in fake.requests if r[0].endswith("/Refresh")][0]
    assert query["Recursive"] == "true"


def test_no_scan_without_a_clips_library_or_outside_the_clips_folder(fake, tmp_path, media,
                                                                    quick_scan):
    from quipclipper_web import jellyfin_index

    root, ep = media
    fake.folders = [{"Name": "Shows", "ItemId": "5" * 32, "Locations": ["/shows"]}]
    c = _client(_clips_env(fake, tmp_path, root))
    _clip(c, ep, "{timestamp}")
    _settle(fake, 0)
    assert fake.refreshed == []
    r = jellyfin_index.LibraryRefresher(lambda: jellyfin.JellyfinClient(fake.url, KEY),
                                        tmp_path / "clips", (("/shows", str(root)),))
    assert "No Jellyfin library" in r.refresh()["error"]


def test_library_for_picks_the_closest_folder(tmp_path):
    from quipclipper_web import jellyfin_index

    (tmp_path / "media" / "clips").mkdir(parents=True)
    libs = [{"Name": "All", "ItemId": "1", "Locations": ["/media"]},
            {"Name": "Clips", "ItemId": "2", "Locations": ["/media/clips", 7]},
            {"Name": "Other", "ItemId": "3", "Locations": ["/mediax"]}]
    pm = (("/media", str(tmp_path / "media")), ("/mediax", str(tmp_path / "x")))
    assert jellyfin_index.library_for(libs, tmp_path / "media" / "clips", pm)["Name"] == "Clips"
    assert jellyfin_index.library_for(libs, tmp_path / "media" / "a", pm)["Name"] == "All"
    assert jellyfin_index.library_for(libs, tmp_path / "x2", pm) is None


# --- the CLI: jellyfin.lookup and jellyfin.reindex ---------------------------------------


def test_cli_lookup_and_reindex(fake, known, cli_run):
    _, ep = known
    code, out = cli_run("jellyfin.reindex")
    assert code == 0 and out["index"]["files"] == 1
    code, out = cli_run("jellyfin.lookup", str(ep))
    assert code == 0 and out["item"]["title"] == "The Best of Both Worlds"
    assert out["naming"]["show"] == "Star_Trek_The_Next_Generation"


def test_cli_lookup_prints_what_jellyfin_knows(fake, known, cli_run, capsys):
    from quipclipper import cli

    _, ep = known
    cli_run("jellyfin.reindex")
    assert cli.main(["jellyfin.lookup", str(ep)], transport=cli_run.transport) == 0
    out = capsys.readouterr().out
    assert "Episode " + EP_ID in out
    assert "Star Trek: The Next Generation S03E26 The Best of Both Worlds (1990)" in out
    assert "{title}=The_Best_of_Both_Worlds" in out
