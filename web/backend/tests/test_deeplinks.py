"""Deep links (M18): the route encoder, the ``link`` fields and ``GET /api/items/link``."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from quipclipper_web import deeplinks
from quipclipper_web.app import create_app
from quipclipper_web.config import Settings

SERVER = "https://quip.example"

SRT = """1
00:00:01,000 --> 00:00:03,500
I'll be back.

2
00:01:01,200 --> 00:01:04,800
Hasta la vista, baby.
"""


# --- the encoder ----------------------------------------------------------------------------


def test_times_are_written_like_javascript():
    assert deeplinks.fmt_time(12.5) == "12.5"
    assert deeplinks.fmt_time(90) == "90"
    assert deeplinks.fmt_time(0) == "0"
    assert deeplinks.fmt_time(3.0416666) == "3.042"
    assert deeplinks.fmt_time(0.0004) == "0"


def test_a_route_keeps_slashes_and_colons_readable():
    frag = deeplinks.item_route("/media/Movies/T2 (1991)/t2 & co.mkv", 61.2, (61.2, 64.8), 2)
    assert frag == ("#/item?path=/media/Movies/T2%20(1991)/t2%20%26%20co.mkv"
                    "&t=61.2&sel=61.2-64.8&track=2")
    assert deeplinks.route("search", {"q": "be back", "scope": "library"}) == \
        "#/search?q=be%20back&scope=library"
    assert deeplinks.route("bookmarks") == "#/bookmarks"
    assert deeplinks.item_route("yt:dQw4w9WgXcQ") == "#/item?path=yt:dQw4w9WgXcQ"


def test_the_seek_defaults_to_the_start_of_the_selection():
    assert deeplinks.item_route("/m/a.mkv", sel=(5, 7.25)) == "#/item?path=/m/a.mkv&t=5&sel=5-7.25"


def test_a_route_round_trips_through_a_query_parser():
    path = "/media/Shows/Ça va? #1/ep+1.mkv"
    link = deeplinks.link(SERVER + "/", deeplinks.item_route(path, 3, (3, 4)))
    assert link.startswith(SERVER + "/#/item?")
    q = parse_qs(urlsplit(link).fragment.split("?", 1)[1])
    assert q == {"path": [path], "t": ["3"], "sel": ["3-4"]}


@pytest.mark.parametrize("bad", ["5", "a-b", "7-5", "-1-2", "inf-inf", "nan-1"])
def test_bad_selections_are_refused(bad):
    with pytest.raises(ValueError):
        deeplinks.parse_sel(bad)


# --- the endpoints --------------------------------------------------------------------------


def _client(tmp_path: Path, public_url: str | None = SERVER):
    media = tmp_path / "media"
    media.mkdir()
    clips = tmp_path / "clips"
    clips.mkdir()
    env = {"QC_MEDIA_ROOTS": str(media), "QC_CLIPS_DIR": str(clips),
           "QC_STATE_DIR": str(tmp_path / "state")}
    if public_url is not None:
        env["QC_PUBLIC_URL"] = public_url
    client = TestClient(create_app(Settings.from_env(env)))
    client.headers["X-Quipclipper"] = "1"
    video = media / "movie.mkv"
    video.write_bytes(b"")
    (media / "movie.srt").write_text(SRT, encoding="utf-8")
    return client, media, clips, video


def _route(link: str) -> tuple[str, dict]:
    assert link.startswith(SERVER + "/#/")
    view, _, query = urlsplit(link).fragment.partition("?")
    return view, {k: v[0] for k, v in parse_qs(query).items()}


def test_item_link(tmp_path):
    client, _, _, video = _client(tmp_path)
    r = client.get("/api/items/link", params={"path": str(video), "at": 61.2, "sel": "61.2-64.8"})
    assert r.status_code == 200
    body = r.json()
    assert body["sel"] == [61.2, 64.8] and body["at"] == 61.2
    assert _route(body["link"]) == ("/item", {"path": str(video), "t": "61.2", "sel": "61.2-64.8"})
    # No --at: the link seeks to the start of the selection; a track rides along.
    r = client.get("/api/items/link", params={"path": str(video), "sel": "1-3.5", "track": 2})
    assert _route(r.json()["link"])[1] == {"path": str(video), "t": "1", "sel": "1-3.5",
                                          "track": "2"}


def test_item_link_checks_the_path_and_the_selection(tmp_path):
    client, media, _, video = _client(tmp_path)
    assert client.get("/api/items/link", params={"path": "/etc/passwd"}).status_code == 403
    assert client.get("/api/items/link", params={"path": str(media / "gone.mkv")}).status_code == 404
    assert client.get("/api/items/link", params={"path": str(media)}).status_code == 404
    assert client.get("/api/items/link", params={"path": "yt:aaaaaaaaaaa"}).status_code == 404
    r = client.get("/api/items/link", params={"path": str(video), "sel": "9-2"})
    assert r.status_code == 422 and "START <= END" in r.json()["detail"]
    assert client.get("/api/items/link", params={"path": str(video), "at": -1}).status_code == 422


def test_item_link_needs_a_public_url(tmp_path):
    client, _, _, video = _client(tmp_path, public_url=None)
    r = client.get("/api/items/link", params={"path": str(video), "at": 1})
    assert r.status_code == 503 and "QC_PUBLIC_URL" in r.json()["detail"]


def test_search_hits_carry_links(tmp_path):
    client, media, _, video = _client(tmp_path)
    hit = client.get("/api/search", params={"path": str(video), "query": "hasta la vista"}).json()["matches"][0]
    assert _route(hit["link"]) == ("/item", {"path": str(video), "t": "61.2", "sel": "61.2-64.8"})
    hits = client.get("/api/search/folder",
                      params={"path": str(media), "query": "be back"}).json()["matches"]
    assert hits and _route(hits[0]["link"]) == ("/item", {"path": str(video), "t": "1",
                                                          "sel": "1-3.5"})


def test_bookmarks_carry_links(tmp_path):
    client, _, _, video = _client(tmp_path)
    made = client.post("/api/bookmarks", json={"path": str(video), "start": 1, "end": 3.5,
                                               "sub_track": 0}).json()
    want = ("/item", {"path": str(video), "t": "1", "sel": "1-3.5", "track": "0"})
    assert _route(made["link"]) == want
    listed = client.get("/api/bookmarks").json()["bookmarks"][0]
    assert _route(listed["link"]) == want
    renamed = client.patch(f"/api/bookmarks/{made['id']}", json={"label": "x"}).json()
    assert _route(renamed["link"]) == want


def test_clips_carry_links(tmp_path):
    client, _, clips, _ = _client(tmp_path)
    (clips / "Movie").mkdir()
    (clips / "Movie" / "line.mp4").write_bytes(b"x")
    clip = client.get("/api/clips", params={"folder": "Movie"}).json()["clips"][0]
    assert _route(clip["link"]) == ("/item", {"path": str((clips / "Movie" / "line.mp4").resolve())})


def test_no_public_url_no_link_fields(tmp_path):
    client, media, clips, video = _client(tmp_path, public_url=None)
    (clips / "c.mp4").write_bytes(b"x")
    client.post("/api/bookmarks", json={"path": str(video), "start": 1, "end": 3.5})
    hits = client.get("/api/search", params={"path": str(video), "query": "be back"}).json()["matches"]
    fhits = client.get("/api/search/folder", params={"path": str(media), "query": "be back"}).json()["matches"]
    bms = client.get("/api/bookmarks").json()["bookmarks"]
    cl = client.get("/api/clips").json()["clips"]
    for row in hits + fhits + bms + cl:
        assert "link" not in row
    assert hits and fhits and bms and cl
