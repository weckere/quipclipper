"""Jellyfin now playing (M15): settings, path mapping and /api/jellyfin/sessions.

A fake Jellyfin (a real HTTP server on a loopback port) answers ``/Sessions`` and
``/Items``, so the client's urllib requests, auth header and error handling run for real.
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
        self.requests: list[tuple[str, dict, str]] = []
        self.status = 200
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                url = urlparse(self.path)
                query = {k: v[0] for k, v in parse_qs(url.query).items()}
                fake.requests.append((url.path, query, self.headers.get("Authorization", "")))
                if self.headers.get("Authorization") != f'MediaBrowser Token="{KEY}"':
                    return self._send(401, {})
                if fake.status != 200:
                    return self._send(fake.status, {})
                if url.path == "/Sessions":
                    return self._send(200, fake.sessions)
                if url.path == "/Items":
                    ids = query.get("Ids", "")
                    return self._send(200, {"Items": [fake.items[ids]] if ids in fake.items
                                            else []})
                self._send(404, {})

            def _send(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
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
