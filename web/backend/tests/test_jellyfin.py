"""Jellyfin: now playing (M15), titles, artwork and library scans (M18), Play on… (M22).

A fake Jellyfin (a real HTTP server on a loopback port) answers ``/Sessions``, ``/Items``
(one item, or the whole library in pages), images, ``/Library/VirtualFolders``, library
refreshes and remote-control commands, so the client's urllib requests, auth header and
error handling run for real.
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
        self.commands: list[tuple[str, str, dict]] = []  # (session, command, query)
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
                if url.path == "/Items" and query.get("ParentId"):
                    # A few of one library's files (the path map derivation, M22).
                    locs = next((f.get("Locations") or [] for f in fake.folders
                                 if f.get("ItemId") == query["ParentId"]), [])
                    found = [i for i in fake.library if any(
                        i["Path"].startswith(loc.rstrip("/") + "/") for loc in locs)]
                    start, limit = int(query.get("StartIndex", 0)), int(query["Limit"])
                    return self._send(200, {"Items": found[start:start + limit]})
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
                url, query = self._start()
                if url is None:
                    return
                parts = url.path.split("/")
                if len(parts) == 4 and parts[1] == "Items" and parts[3] == "Refresh":
                    fake.refreshed.append(parts[2])
                    self.send_response(204)
                    self.end_headers()
                    return
                if len(parts) in (4, 5) and parts[1] == "Sessions" and parts[3] == "Playing":
                    if not any(s.get("Id") == parts[2] for s in fake.sessions):
                        return self._send(404, {})
                    command = parts[4] if len(parts) == 5 else query.get("playCommand")
                    fake.commands.append((parts[2], command, query))
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
    index.lookup(ep.resolve())  # nothing built yet: the lookup starts a build in the background
    assert index._thread is not None  # (what it answered meanwhile is a race: not checked)
    index._thread.join(60)
    assert index.lookup(ep.resolve())["title"] == "The Best of Both Worlds"
    fake.library[0]["Name"] = "Renamed"
    now[0] += 60
    index.lookup(ep.resolve())  # a minute on: still fresh, no rebuild
    builds = len([r for r in fake.requests if r[1].get("Recursive")])
    now[0] += 3600
    index.lookup(ep.resolve())  # an hour on: rebuilt, the old entry answers meanwhile
    index._thread.join(60)
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


# --- Play on… (M22): list the clients that can be controlled, start a line on one --------

NOW = datetime.now(timezone.utc)


def _client_session(id, device, client, *, remote=True, media=("Audio", "Video"),
                    ago=30, **over) -> dict:
    """A session with nothing playing, as Jellyfin lists a connected client."""
    s = {"Id": id, "UserName": "wiz", "DeviceName": device, "Client": client,
         "SupportsRemoteControl": remote, "PlayableMediaTypes": list(media),
         "LastActivityDate": _iso(NOW - timedelta(seconds=ago))}
    s.update(over)
    return s


def _clients() -> list[dict]:
    return [
        _client_session("tv", "BRAVIA 4K VH2", "Android TV", ago=7200),  # parked for hours
        _client_session("web", "Firefox", "Jellyfin Web"),
        _client_session("infuse", "Apple TV", "Infuse-Library", remote=False, media=()),
        _client_session("old", "Apple TV", "Infuse-Library", remote=False, media=(),
                        ago=7200),  # gone quiet, and can't be controlled: left out
    ]


def _asked_within(fake) -> str:
    """How far back the last /Sessions request reached (a poster lookup may follow it)."""
    return [q for path, q, _ in fake.requests if path == "/Sessions"][-1]["activeWithinSeconds"]


def _playing(c: TestClient):
    """The app with the path index built, so the TNG episode has its Jellyfin id."""
    assert c.post("/api/jellyfin/index").status_code == 200
    return c


def test_sessions_all_lists_idle_clients_and_says_which_can_be_controlled(
        fake, tmp_path, known):
    root, _ = known
    fake.sessions = [_session(SupportsRemoteControl=True, PlayableMediaTypes=["Video"]),
                     *_clients()]
    c = _client(_env(fake, tmp_path, root))
    playing = c.get("/api/jellyfin/sessions").json()["sessions"]
    assert [s["id"] for s in playing] == ["sess1"]  # by default only what's playing
    assert playing[0]["can_play"] is True and playing[0]["idle"] is False
    assert _asked_within(fake) == str(jellyfin.ACTIVE_WITHIN)

    every = c.get("/api/jellyfin/sessions", params={"all": "1"}).json()["sessions"]
    assert _asked_within(fake) == str(jellyfin.CONTROLLABLE_WITHIN)
    # Playing first, then the idle ones by last activity; the quiet Infuse session is gone.
    assert [s["id"] for s in every] == ["sess1", "web", "infuse", "tv"]
    by_id = {s["id"]: s for s in every}
    assert by_id["tv"]["can_play"] is True and by_id["tv"]["idle"] is True
    assert by_id["infuse"]["can_play"] is False
    assert "remote control" in by_id["infuse"]["control_note"]
    assert by_id["web"]["media_types"] == ["Audio", "Video"]


def test_play_starts_the_file_at_the_time_on_the_client(fake, tmp_path, known):
    root, ep = known
    fake.sessions = _clients()
    c = _playing(_client(_env(fake, tmp_path, root)))
    r = c.post("/api/jellyfin/play", json={"session": "tv", "path": str(ep), "at": 1233.5})
    assert r.status_code == 200, r.text
    assert r.json() == {"session": "tv", "device": "BRAVIA 4K VH2", "client": "Android TV",
                        "item_id": EP_ID, "title": "The Best of Both Worlds",
                        "path": str(ep.resolve()), "at": 1233.5, "command": "play"}
    ((sid, command, query),) = fake.commands
    assert (sid, command) == ("tv", "PlayNow")
    assert query["itemIds"] == EP_ID and query["startPositionTicks"] == str(12335000000)


def test_a_client_already_on_the_file_seeks_and_unpauses(fake, tmp_path, known):
    root, ep = known
    fake.sessions = [_session(Id="tv", SupportsRemoteControl=True,
                              PlayableMediaTypes=["Video"],
                              NowPlayingItem={**_session()["NowPlayingItem"], "Id": EP_ID})]
    c = _playing(_client(_env(fake, tmp_path, root)))
    r = c.post("/api/jellyfin/play", json={"session": "tv", "path": str(ep), "at": 10})
    assert r.status_code == 200 and r.json()["command"] == "seek"
    assert [(cmd, q.get("seekPositionTicks")) for _, cmd, q in fake.commands] == [
        ("Seek", str(10 * jellyfin.TICKS)), ("Unpause", None)]


def test_play_refuses_a_start_too_big_to_be_a_time(fake, tmp_path, known):
    root, ep = known
    fake.sessions = _clients()
    c = _playing(_client(_env(fake, tmp_path, root)))
    # JSON's 1e400 reads as infinity: a 422, not a 500 from the tick conversion.
    r = c.post("/api/jellyfin/play", content=f'{{"session": "tv", "path": "{ep}", "at": 1e400}}',
               headers={"Content-Type": "application/json"})
    assert r.status_code == 422 and not fake.commands


def test_play_names_a_client_by_part_of_its_name(fake, tmp_path, known):
    root, ep = known
    fake.sessions = _clients()
    c = _playing(_client(_env(fake, tmp_path, root)))
    r = c.post("/api/jellyfin/play", json={"session": "firefox", "path": str(ep)})
    assert r.status_code == 200 and r.json()["session"] == "web"
    # "wiz" is every session's user: two controllable ones match, so it asks which.
    r = c.post("/api/jellyfin/play", json={"session": "wiz", "path": str(ep)})
    assert r.status_code == 400 and "tv = BRAVIA" in r.json()["detail"]


def test_play_refuses_what_cant_be_controlled_or_found(fake, tmp_path, known):
    root, ep = known
    fake.sessions = [*_clients(), _client_session("radio", "Kitchen", "Jellyfin Web",
                                                  media=("Audio",))]
    c = _playing(_client(_env(fake, tmp_path, root)))

    def play(session, path=ep):
        return c.post("/api/jellyfin/play", json={"session": session, "path": str(path)})

    r = play("infuse")
    assert r.status_code == 409 and "can't be controlled" in r.json()["detail"]
    r = play("apple tv")  # by name: Infuse is the only match, and it can't
    assert r.status_code == 409 and "Infuse" in r.json()["detail"]
    r = play("kitchen")  # it plays audio only, and this is an episode
    assert r.status_code == 409 and "can't play video" in r.json()["detail"]
    assert play("bedroom").status_code == 404
    other = ep.parent / "S03E25.mkv"  # in the roots, but Jellyfin doesn't know it
    other.write_bytes(b"")
    r = play("tv", other)
    assert r.status_code == 404 and "doesn't know this file" in r.json()["detail"]
    assert play("tv", ep.parent / "nope.mkv").status_code == 404
    assert c.post("/api/jellyfin/play", json={"session": "tv", "path": str(ep),
                                              "at": -1}).status_code == 422
    assert fake.commands == []


def test_play_when_the_client_closed_meanwhile_or_jellyfin_is_down(fake, tmp_path, known,
                                                                   monkeypatch):
    root, ep = known
    fake.sessions = _clients()
    c = _playing(_client(_env(fake, tmp_path, root)))
    real = jellyfin.JellyfinClient.sessions
    # Listed when asked, closed by the time the command arrives.
    monkeypatch.setattr(jellyfin.JellyfinClient, "sessions",
                        lambda self, active_within=0: [_client_session("gone", "TV", "x")])
    r = c.post("/api/jellyfin/play", json={"session": "gone", "path": str(ep)})
    assert r.status_code == 404 and "closed" in r.json()["detail"]
    monkeypatch.setattr(jellyfin.JellyfinClient, "sessions", real)
    fake.status = 500
    assert c.post("/api/jellyfin/play", json={"session": "tv",
                                              "path": str(ep)}).status_code == 502


def test_play_needs_jellyfin(tmp_path, media):
    root, ep = media
    c = _client({"QC_MEDIA_ROOTS": str(root), "QC_STATE_DIR": str(tmp_path / "state")})
    r = c.post("/api/jellyfin/play", json={"session": "tv", "path": str(ep)})
    assert r.status_code == 503


def test_cli_play_and_sessions_all(fake, known, cli_run, capsys):
    _, ep = known
    fake.sessions = _clients()
    code, out = cli_run("jellyfin.reindex")
    assert code == 0
    code, out = cli_run("jellyfin.play", str(ep), "--session", "bravia", "--at", "62")
    assert code == 0, out
    assert out["command"] == "play" and out["device"] == "BRAVIA 4K VH2"
    assert fake.commands[-1][2]["startPositionTicks"] == str(62 * jellyfin.TICKS)
    code, out = cli_run("nowplaying", "--all")
    assert code == 0 and {s["id"] for s in out["sessions"]} == {"tv", "web", "infuse"}


def test_cli_play_prints_what_it_did(fake, known, tmp_path, monkeypatch, capsys):
    from quipclipper import cli

    _fmt = cli._FORMATTERS["jellyfin.play"]
    _fmt({"command": "play", "title": "The Best of Both Worlds", "device": "BRAVIA",
          "client": "Android TV", "at": 62})
    _fmt({"command": "seek", "title": "X", "device": "Firefox", "client": "Jellyfin Web",
          "at": 3700})
    cli._FORMATTERS["jellyfin.sessions"]({"sessions": [
        {**_session(), "id": "infuse", "device": "Apple TV", "client": "Infuse-Library",
         "user": "wiz", "idle": True, "can_play": False,
         "control_note": "This app doesn't take remote control from Jellyfin."}]})
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "Playing The Best of Both Worlds on BRAVIA (Android TV) at 1:02"
    assert out[1] == "Seeked X on Firefox (Jellyfin Web) at 1:01:40"
    assert out[2].startswith("infuse  Apple TV (Infuse-Library, wiz)  idle, can't be controlled")


def test_wholphin_and_api_key_sessions(fake, tmp_path, known):
    """Wholphin says it takes remote control but ignores PlayNow (wizrock, 2026-10-26), so
    it is listed as one that can't; an API key's session (no user) isn't a client at all."""
    root, ep = known
    fake.sessions = [
        _client_session("wh", "SONY XR-65X90CK", "Wholphin", media=("Video",),
                        SupportedCommands=["DisplayMessage", "SendString"]),
        _client_session("hermes", "Jellyfin Server", "Hermes", remote=False, media=(),
                        UserName=None),
    ]
    c = _playing(_client(_env(fake, tmp_path, root)))
    (wh,) = c.get("/api/jellyfin/sessions", params={"all": "1"}).json()["sessions"]
    assert wh["id"] == "wh" and wh["can_play"] is False and "Wholphin" in wh["control_note"]
    r = c.post("/api/jellyfin/play", json={"session": "wh", "path": str(ep)})
    assert r.status_code == 409 and fake.commands == []


# --- the path map, derived when none is set (M22) ------------------------------------------

SHOWS_LIB, CLIPS_LIB, MUSIC_LIB = "5" * 32, "6" * 32, "4" * 32


def _derive_env(fake, tmp_path, root, **extra) -> dict[str, str]:
    env = _env(fake, tmp_path, root, **extra)
    if "QC_JELLYFIN_PATH_MAP" not in extra:
        del env["QC_JELLYFIN_PATH_MAP"]
    return env


def test_with_no_path_map_one_is_derived_from_the_libraries(fake, tmp_path, known):
    root, ep = known
    clips = tmp_path / "clips"
    (clips / "TNG").mkdir(parents=True)
    (clips / "TNG" / "line.mp4").write_bytes(b"")
    fake.folders = [{"Name": "Shows", "ItemId": SHOWS_LIB, "Locations": ["/shows"]},
                    {"Name": "Clips", "ItemId": CLIPS_LIB, "Locations": ["/clips"]},
                    {"Name": "Music", "ItemId": MUSIC_LIB, "Locations": ["/music"]}]
    fake.library.append({"Id": "2" * 32, "Type": "Video", "Path": "/clips/TNG/line.mp4"})
    c = _client(_derive_env(fake, tmp_path, root))
    index = c.post("/api/jellyfin/index").json()["index"]
    assert index["files"] == 1
    assert index["path_map"] == {
        "source": "derived",
        "pairs": [["/clips", str(clips.resolve())], ["/shows", str(root.resolve())]],
        "unmatched": ["/music"]}   # /music/x.flac is nowhere here
    body = c.get("/api/jellyfin/lookup", params={"path": str(ep)}).json()
    assert body["known"] is True and body["index"]["path_map"]["source"] == "derived"
    samples = [r[1] for r in fake.requests if r[1].get("ParentId")]
    assert {s["ParentId"] for s in samples} == {SHOWS_LIB, CLIPS_LIB, MUSIC_LIB}
    assert samples[0]["Fields"] == "Path" and "Video" in samples[0]["IncludeItemTypes"]
    # A restart keeps the derived map (and its index) without asking Jellyfin.
    fake.requests.clear()
    again = _client(_derive_env(fake, tmp_path, root)).get(
        "/api/jellyfin/lookup", params={"path": str(ep)}).json()
    assert again["known"] is True and again["index"]["path_map"] == index["path_map"]
    assert not fake.requests


def test_a_derived_map_finds_a_library_folder_inside_a_root(fake, tmp_path):
    """Jellyfin's /tv is the "shows" folder inside the root /pool."""
    pool = tmp_path / "pool"
    ep = pool / "shows" / "TNG" / "Season 3" / "S03E26.mkv"
    ep.parent.mkdir(parents=True)
    ep.write_bytes(b"")
    (pool / "movies").mkdir()
    fake.folders = [{"Name": "Shows", "ItemId": SHOWS_LIB, "Locations": ["/tv"]}]
    fake.library = [_episode(Path="/tv/TNG/Season 3/S03E26.mkv")]
    index = _client(_derive_env(fake, tmp_path, pool)).post("/api/jellyfin/index").json()["index"]
    assert index["path_map"]["pairs"] == [["/tv", str((pool / "shows").resolve())]]
    assert index["files"] == 1


def test_a_derived_map_finds_a_root_inside_a_library_folder(fake, tmp_path, known):
    """Jellyfin's one library folder /data holds shows/…; here the root is that shows folder."""
    root, ep = known
    ep2 = ep.with_name("S03E25.mkv")
    ep2.write_bytes(b"")
    fake.folders = [{"Name": "All", "ItemId": SHOWS_LIB, "Locations": ["/data"]}]
    fake.library = [_episode(Path="/data/shows/Star Trek TNG/Season 3/S03E26.mkv")]
    c = _client(_derive_env(fake, tmp_path, root))
    # One file found this way is a guess (a generic name could match anywhere): no pair.
    index = c.post("/api/jellyfin/index").json()["index"]
    assert index["path_map"] == {"source": "derived", "pairs": [], "unmatched": ["/data/shows"]}
    # Two agreeing files make it.
    fake.library.append(_episode(Id="3" * 32, Path="/data/shows/Star Trek TNG/Season 3/S03E25.mkv"))
    index = c.post("/api/jellyfin/index").json()["index"]
    assert index["path_map"]["pairs"] == [["/data/shows", str(root.resolve())]]
    assert index["files"] == 2


def test_each_folder_of_a_library_gets_its_own_samples(fake, tmp_path, monkeypatch):
    """A library of two folders whose first dozen files are all in one of them: the other
    folder is still sampled (Jellyfin's order isn't by folder), a page at a time."""
    from quipclipper_web import jellyfin_index

    monkeypatch.setattr(jellyfin_index, "DERIVE_PAGE", 4)
    a, b = tmp_path / "a", tmp_path / "b"
    fake.folders = [{"Name": "Shows", "ItemId": SHOWS_LIB, "Locations": ["/shows", "/more"]}]
    fake.library = []
    for n in range(12):
        (a / "S").mkdir(parents=True, exist_ok=True)
        (a / "S" / f"e{n:02}.mkv").write_bytes(b"")
        fake.library.append(_episode(Id=f"{n:032}", Path=f"/shows/S/e{n:02}.mkv"))
    (b / "T").mkdir(parents=True)
    (b / "T" / "x.mkv").write_bytes(b"")
    fake.library.append(_episode(Id="f" * 32, Path="/more/T/x.mkv"))
    env = _derive_env(fake, tmp_path, a, QC_MEDIA_ROOTS=f"{a}:{b}")
    index = _client(env).post("/api/jellyfin/index").json()["index"]
    assert index["path_map"] == {"source": "derived", "unmatched": [], "pairs": [
        ["/more", str(b.resolve())], ["/shows", str(a.resolve())]]}
    pages = [r[1] for r in fake.requests if r[1].get("ParentId") == SHOWS_LIB]
    assert [(p["StartIndex"], p["Limit"]) for p in pages] == [
        ("0", "4"), ("4", "4"), ("8", "4"), ("12", "4")]


def test_a_one_folder_library_asks_for_its_samples_only(fake, tmp_path, known):
    root, _ = known
    fake.folders = [{"Name": "Shows", "ItemId": SHOWS_LIB, "Locations": ["/shows"]}]
    _client(_derive_env(fake, tmp_path, root)).post("/api/jellyfin/index")
    pages = [r[1] for r in fake.requests if r[1].get("ParentId") == SHOWS_LIB]
    assert [(p["StartIndex"], p["Limit"]) for p in pages] == [("0", "5")]


def test_samples_that_disagree_map_nothing(fake, tmp_path):
    """Two files of one library found under two different roots: no pair is a guess."""
    a, b = tmp_path / "a", tmp_path / "b"
    for root, name in ((a, "one.mkv"), (b, "two.mkv")):
        (root / "S").mkdir(parents=True)
        (root / "S" / name).write_bytes(b"")
    fake.folders = [{"Name": "Shows", "ItemId": SHOWS_LIB, "Locations": ["/shows"]}]
    fake.library = [_episode(Path="/shows/S/one.mkv"),
                    _episode(Id="3" * 32, Path="/shows/S/two.mkv")]
    env = _derive_env(fake, tmp_path, a, QC_MEDIA_ROOTS=f"{a}:{b}")
    index = _client(env).post("/api/jellyfin/index").json()["index"]
    assert index["path_map"] == {"source": "derived", "pairs": [], "unmatched": ["/shows"]}
    assert index["files"] == 0


def test_an_explicit_path_map_wins_and_nothing_is_derived(fake, tmp_path, known):
    root, _ = known
    fake.folders = [{"Name": "Shows", "ItemId": SHOWS_LIB, "Locations": ["/shows"]}]
    index = _client(_env(fake, tmp_path, root)).post("/api/jellyfin/index").json()["index"]
    assert index["path_map"]["source"] == "explicit"
    assert "unmatched" not in index["path_map"]
    assert not [r for r in fake.requests if r[1].get("ParentId")]


def test_before_any_build_the_map_says_none(fake, tmp_path, known):
    from quipclipper_web import jellyfin_index

    root, _ = known
    index = jellyfin_index.JellyfinIndex(
        tmp_path / "state", lambda: jellyfin.JellyfinClient(fake.url, KEY), (), [root])
    assert index.map_status() == {"source": "none", "pairs": [], "unmatched": []}
    assert index.path_map == ()


def test_the_derived_map_maps_sessions_and_the_clips_scan(fake, tmp_path, known):
    from quipclipper_web import jellyfin_index

    root, ep = known
    clips = tmp_path / "clips"
    (clips / "x").mkdir(parents=True)
    (clips / "x" / "old.mp4").write_bytes(b"")
    fake.folders = [{"Name": "Shows", "ItemId": SHOWS_LIB, "Locations": ["/shows"]},
                    {"Name": "Clips", "ItemId": CLIPS_LIB, "Locations": ["/clips"]}]
    fake.library.append({"Id": "2" * 32, "Type": "Video", "Path": "/clips/x/old.mp4"})
    fake.sessions = [_session()]
    c = _client(_derive_env(fake, tmp_path, root))
    c.post("/api/jellyfin/index")
    s = c.get("/api/jellyfin/sessions").json()["sessions"][0]
    assert s["path"] == str(ep.resolve())
    assert c.app.state.settings.jellyfin_path_map == ()
    # The clips scan asks the index for its map when it runs (called here directly: no timer).
    index = jellyfin_index.JellyfinIndex(
        tmp_path / "state2", lambda: jellyfin.JellyfinClient(fake.url, KEY), (), [root],
        derive_from=[clips])
    refresher = jellyfin_index.LibraryRefresher(
        lambda: jellyfin.JellyfinClient(fake.url, KEY), clips, lambda: index.path_map)
    assert refresher.refresh()["error"].startswith("No Jellyfin library holds")  # none yet
    index.build()
    assert refresher.refresh() == {**refresher.last, "library": "Clips", "error": None}
    assert fake.refreshed == [CLIPS_LIB]


def test_a_derivation_that_finds_nothing_keeps_the_map_that_worked(fake, tmp_path, known):
    root, ep = known
    fake.folders = [{"Name": "Shows", "ItemId": SHOWS_LIB, "Locations": ["/shows"]}]
    c = _client(_derive_env(fake, tmp_path, root))
    assert c.post("/api/jellyfin/index").json()["index"]["path_map"]["pairs"] == [
        ["/shows", str(root.resolve())]]
    hidden = root.with_name("shows-away")
    root.rename(hidden)  # the mount is gone for a moment
    try:
        index = c.post("/api/jellyfin/index").json()["index"]
    finally:
        hidden.rename(root)
    assert index["path_map"]["pairs"] == [["/shows", str(root.resolve())]]
    assert index["path_map"]["unmatched"] == ["/shows"]


def test_the_plugins_reindex_waits_for_the_first_derived_map(fake, tmp_path, known, monkeypatch):
    """source=jellyfin before any map is derived: 503 (try again), not a 422 the plugin
    would take as final; once derived, the same request is mapped and queued."""
    from quipclipper_web import jellyfin_index

    root, ep = known
    (ep.parent / "S03E26.en.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nHi.\n")
    fake.folders = [{"Name": "Shows", "ItemId": SHOWS_LIB, "Locations": ["/shows"]}]
    monkeypatch.setattr(jellyfin_index.JellyfinIndex, "refresh_if_stale",
                        lambda self: None)  # no background build: the test builds it
    c = _client(_derive_env(fake, tmp_path, root))
    body = {"paths": ["/shows/Star Trek TNG/Season 3/S03E26.mkv"], "source": "jellyfin"}
    r = c.post("/api/index/file", json=body)
    assert r.status_code == 503 and "derived yet" in r.json()["detail"]
    # The transcript provider's read waits the same way (M22 Phase 3).
    r = c.get("/api/items/transcript", params={"path": body["paths"][0], "source": "jellyfin",
                                               "lines": "machine"})
    assert r.status_code == 503 and "derived yet" in r.json()["detail"]
    c.post("/api/jellyfin/index")
    r = c.post("/api/index/file", json=body)
    assert r.status_code == 202, r.text
    assert r.json()["files"][0]["media"] == str(ep.resolve())
