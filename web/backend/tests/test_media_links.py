"""Signed media links (M27, D-133): a paired app gets a link VLC opens with no token.

Deterministic: the app's clock is injected, so expiry is checked without sleeping."""

from __future__ import annotations

import base64
import json
import logging
import os
import stat
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from quipclipper_web import media_links, pairing
from quipclipper_web.app import create_app
from quipclipper_web.config import Settings

CSRF = {"X-Quipclipper": "1"}
BODY = bytes(range(256)) * 40   # 10 240 bytes of "film"
T0 = 1_800_000_000.0


class Clock:
    def __init__(self, t: float = T0):
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def env(tmp_path):
    media = tmp_path / "media"
    (media / "Film (2001)").mkdir(parents=True)
    film = media / "Film (2001)" / "Film (2001).mkv"
    film.write_bytes(BODY)
    (media / "Film (2001)" / "Film (2001).en.srt").write_text(
        "1\n00:00:01,000 --> 00:00:02,500\nHello there.\n\n"
        "2\n00:00:03,000 --> 00:00:04,000\nGeneral Kenobi.\n", encoding="utf-8")
    (media / "Film (2001)" / "Film (2001).en.forced.srt").write_text(
        "1\n00:00:01,000 --> 00:00:02,000\nForced only.\n", encoding="utf-8")
    outside = tmp_path / "outside.mkv"
    outside.write_bytes(b"secret")
    clips = tmp_path / "clips"
    clips.mkdir()
    clock = Clock()
    settings = Settings.from_env({"QC_MEDIA_ROOTS": str(media), "QC_CLIPS_DIR": str(clips),
                                  "QC_STATE_DIR": str(tmp_path / "state")})
    c = TestClient(create_app(settings, clock=clock), client=("127.0.0.1", 50000))
    return {"c": c, "clock": clock, "film": film, "outside": outside,
            "state": tmp_path / "state", "media": media}


def _pair(c: TestClient, scopes: list[str] | None) -> tuple[str, str]:
    body = {"name": "LineFinder", "host": "tv"}
    if scopes is not None:
        body["scopes"] = scopes
    req = c.post("/api/pair", json=body, headers=CSRF).json()
    assert c.post(f"/api/pair/{req['id']}/approve", headers=CSRF).status_code == 200
    got = c.get(f"/api/pair/{req['id']}", headers={"X-Pair-Secret": req["poll_secret"]}).json()
    return got["token"], got["client_id"]


def _link(c: TestClient, path: Path, token: str | None = None, **q) -> dict:
    headers = {"X-API-Key": token} if token else {}
    r = c.get("/api/media/link", params={"path": str(path), **q}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _path(url: str) -> str:
    return urlsplit(url).path


def _payload(url: str) -> dict:
    tok = _path(url).split("/")[4]
    raw = tok.split(".")[0]
    return json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))


GONE = {"detail": "Not Found"}


def _gone(r) -> None:
    assert r.status_code == 404 and r.json() == GONE


# --- issuing ---------------------------------------------------------------------------


def test_a_media_link_scope_gets_a_link_and_the_link_needs_no_token(env):
    c = env["c"]
    token, cid = _pair(c, ["media.link"])
    got = _link(c, env["film"], token)
    assert set(got) == {"url", "subtitles_url", "expires_at", "starts_at"}
    assert got["subtitles_url"] is None and got["starts_at"] is None
    assert got["expires_at"] == pairing.iso(T0 + media_links.LIFETIME)
    assert got["url"].startswith("http://testserver/api/media/s/")
    assert got["url"].endswith("/Film%20%282001%29.mkv")
    body = _payload(got["url"])
    assert body["p"] == str(env["film"].resolve()) and body["c"] == cid and body["k"] == "file"
    r = c.get(_path(got["url"]))          # no X-API-Key: what VLC sends
    assert r.status_code == 200 and r.content == BODY
    assert r.headers["accept-ranges"] == "bytes"


def test_a_link_answers_range_and_head(env):
    c = env["c"]
    url = _path(_link(c, env["film"])["url"])
    r = c.get(url, headers={"Range": "bytes=100-199"})
    assert r.status_code == 206 and r.content == BODY[100:200]
    assert r.headers["content-range"] == f"bytes 100-199/{len(BODY)}"
    h = c.head(url)
    assert h.status_code == 200 and h.headers["content-length"] == str(len(BODY))
    assert h.content == b""


def test_other_methods_are_refused(env):
    c = env["c"]
    url = _path(_link(c, env["film"])["url"])
    for method in ("POST", "PUT", "DELETE"):
        assert c.request(method, url, headers=CSRF).status_code == 405


def test_a_scope_without_media_link_is_refused(env):
    c = env["c"]
    token, _ = _pair(c, ["search"])
    r = c.get("/api/media/link", params={"path": str(env["film"])},
              headers={"X-API-Key": token})
    assert r.status_code == 403
    # ...and the media.link scope opens nothing else.
    token, _ = _pair(c, ["media.link"])
    assert c.get("/api/media", params={"path": str(env["film"])},
                 headers={"X-API-Key": token}).status_code == 403
    assert "media.link" in pairing.SCOPES


def test_a_full_token_gets_links_too(env):
    c = env["c"]
    token, cid = _pair(c, None)
    got = _link(c, env["film"], token)
    assert _payload(got["url"])["c"] == cid
    assert c.get(_path(got["url"])).status_code == 200


def test_only_files_inside_the_media_roots_get_links(env):
    c = env["c"]
    for path in (env["outside"], env["media"] / ".." / "outside.mkv"):
        r = c.get("/api/media/link", params={"path": str(path)})
        assert r.status_code == 403
    assert c.get("/api/media/link", params={"path": str(env["media"])}).status_code == 404
    assert c.get("/api/media/link", params={"path": "yt:dQw4w9WgXcQ"}).status_code == 400


# --- what the signed route refuses -----------------------------------------------------


def test_a_link_expires(env):
    c, clock = env["c"], env["clock"]
    url = _path(_link(c, env["film"])["url"])
    clock.t = T0 + media_links.LIFETIME - 1
    assert c.get(url).status_code == 200
    clock.t = T0 + media_links.LIFETIME
    _gone(c.get(url))


def test_a_tampered_or_made_up_link_is_the_same_404(env):
    c = env["c"]
    url = _path(_link(c, env["film"])["url"])
    prefix, tok, name = url.rsplit("/", 2)
    payload, sig = tok.split(".")
    # Point the payload at the outside file and keep the old signature.
    body = _payload(url)
    body["p"] = str(env["outside"])
    forged = base64.urlsafe_b64encode(json.dumps(body).encode()).rstrip(b"=").decode()
    flipped = sig[:-1] + ("A" if sig[-1] != "A" else "B")
    for bad in (f"{forged}.{sig}", f"{payload}.{flipped}", "x.y", "nodot", f"{payload}."):
        _gone(c.get(f"{prefix}/{bad}/{name}"))
    _gone(c.get("/api/media/s/abc"))   # an unknown route answers the very same


def test_a_link_dies_when_its_client_is_revoked(env):
    c = env["c"]
    token, cid = _pair(c, ["media.link"])
    url = _path(_link(c, env["film"], token)["url"])
    assert c.get(url).status_code == 200
    assert c.delete(f"/api/clients/{cid}", headers=CSRF).json() == {"removed": True}
    _gone(c.get(url))


def test_a_link_dies_when_the_key_is_deleted(env):
    c = env["c"]
    url = _path(_link(c, env["film"])["url"])
    key = env["state"] / media_links.KEY_FILE
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    key.unlink()
    _gone(c.get(url))


def test_a_file_moved_out_of_the_roots_is_gone(env):
    c = env["c"]
    url = _path(_link(c, env["film"])["url"])
    env["film"].unlink()
    os.symlink(env["outside"], env["film"])    # same name, now pointing outside
    _gone(c.get(url))


def test_the_link_serves_only_its_file(env):
    """The last segment is decoration: another name still serves the signed file."""
    c = env["c"]
    url = _path(_link(c, env["film"])["url"])
    other = url.rsplit("/", 1)[0] + "/outside.mkv"
    assert c.get(other).content == BODY


# --- subtitles -------------------------------------------------------------------------


def test_subtitles_in_a_language_skip_forced(env):
    c = env["c"]
    got = _link(c, env["film"], subtitles="eng")
    sub = got["subtitles_url"]
    assert sub.endswith("/Film%20%282001%29.eng.srt")
    assert _payload(sub)["x"].endswith("Film (2001).en.srt")
    r = c.get(_path(sub))
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/x-subrip")
    assert "Hello there." in r.text and "00:00:01,000 --> 00:00:02,500" in r.text
    assert "Forced only." not in r.text
    vtt = c.get(_path(_link(c, env["film"], subtitles="en",
                             subtitles_format="vtt")["subtitles_url"]))
    assert vtt.text.startswith("WEBVTT") and vtt.headers["content-type"].startswith("text/vtt")


def test_no_subtitles_in_that_language_is_null(env, monkeypatch):
    from quipclipper import subtitles as subs
    monkeypatch.setattr(subs, "list_embedded_tracks", lambda v: [
        subs.SubtitleTrack(0, "subrip", "ger", None, forced=True),
        subs.SubtitleTrack(1, "hdmv_pgs_subtitle", "ger", None)])
    assert _link(env["c"], env["film"], subtitles="ger")["subtitles_url"] is None


def test_an_embedded_track_in_the_language(env, monkeypatch):
    from quipclipper import subtitles as subs
    monkeypatch.setattr(subs, "list_embedded_tracks", lambda v: [
        subs.SubtitleTrack(0, "subrip", "fre", None, forced=True),
        subs.SubtitleTrack(1, "subrip", "fre", None, hearing_impaired=True),
        subs.SubtitleTrack(2, "subrip", "fre", None)])
    assert _payload(_link(env["c"], env["film"], subtitles="fr")["subtitles_url"])["t"] == 2


def test_a_named_track_wins(env):
    got = _link(env["c"], env["film"], subtitles="eng", track=3)
    assert _payload(got["subtitles_url"])["t"] == 3


def test_a_subtitle_link_dies_with_its_client(env):
    c = env["c"]
    token, cid = _pair(c, ["media.link"])
    sub = _path(_link(c, env["film"], token, subtitles="eng")["subtitles_url"])
    assert c.get(sub).status_code == 200
    c.delete(f"/api/clients/{cid}", headers=CSRF)
    _gone(c.get(sub))


# --- logs ------------------------------------------------------------------------------


def test_the_access_log_never_shows_a_token(env):
    url = _path(_link(env["c"], env["film"])["url"])
    tok = url.split("/")[4]
    rec = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1,
                            '%s - "%s %s HTTP/%s" %d',
                            ("1.2.3.4:5", "GET", url + "?x=1", "1.1", 200), None)
    for f in logging.getLogger("uvicorn.access").filters:
        f.filter(rec)
    line = rec.getMessage()
    assert tok not in line and tok.split(".")[1] not in line
    assert "/api/media/s/<redacted>/Film%20%282001%29.mkv?x=1" in line


# --- the key ---------------------------------------------------------------------------


def test_the_key_is_made_once_and_kept(tmp_path):
    a = media_links.load_key(tmp_path)
    assert a is not None and len(a) == 32
    assert media_links.load_key(tmp_path) == a
    assert media_links.load_key(tmp_path / "none", create=False) is None
    (tmp_path / media_links.KEY_FILE).write_text("damaged\n")
    assert media_links.load_key(tmp_path) is None    # fail closed: no links at all


def test_the_proxy_secret_still_guards_a_link(tmp_path):
    media = tmp_path / "media"
    media.mkdir()
    film = media / "a.mkv"
    film.write_bytes(BODY)
    (tmp_path / "clips").mkdir()
    secret = {"X-Quip-Proxy-Secret": "s3cret"}
    c = TestClient(create_app(Settings.from_env({
        "QC_MEDIA_ROOTS": str(media), "QC_CLIPS_DIR": str(tmp_path / "clips"),
        "QC_STATE_DIR": str(tmp_path / "state"), "QC_PROXY_SECRET": "s3cret"})))
    url = _path(c.get("/api/media/link", params={"path": str(film)}, headers=secret)
                .json()["url"])
    assert c.get(url).status_code == 403
    assert c.get(url, headers=secret).content == BODY
