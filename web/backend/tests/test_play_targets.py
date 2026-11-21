"""Play targets (M27): a paired app registers by polling, is listed, and plays what it is sent.

Deterministic: the clock is injected, polls use wait=0 or are woken by the send itself, and
the "not listening" case sets the registry's delivery wait to 0. Nothing sleeps."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from quipclipper_web import pairing, play_targets
from quipclipper_web.app import create_app
from quipclipper_web.config import Settings

CSRF = {"X-Quipclipper": "1"}
T0 = 1_800_000_000.0


class Clock:
    def __init__(self, t: float = T0):
        self.t = t

    def __call__(self) -> float:
        return self.t


# --- the registry on its own ---------------------------------------------------------------


TV = {"id": "tv1", "name": "LineFinder", "host": "Living room TV"}
PLAY = {"path": "/m/a.mkv", "at": 12.5, "subtitles": "eng", "track": 2, "from": "iPhone"}


def _alive(_id: str) -> bool:
    return True


def test_registry_round_trip_through_a_waiting_poll():
    clock = Clock()
    reg = play_targets.PlayTargets(clock)

    async def run():
        poll = asyncio.create_task(reg.receive(TV, "VLC", 25))
        await asyncio.sleep(0)            # the poll registers and waits
        assert [t["id"] for t in reg.targets(_alive)] == ["tv1"]
        send = asyncio.create_task(reg.send("tv1", PLAY, "phone", _alive))
        got = await poll                  # the send woke it
        assert got == {"id": got["id"], **PLAY}
        assert reg.report("tv1", got["id"], True, "VLC started")
        assert not reg.report("tv1", got["id"], True, "twice")
        return await send

    out = asyncio.run(run())
    assert out == {"target": {"id": "tv1", "name": "LineFinder", "host": "Living room TV",
                              "player": "VLC", "listening": True},
                   "delivered": True, "result": {"ok": True, "detail": "VLC started"}}


def test_registry_listing_ends_after_the_grace_and_unknown_is_404():
    clock = Clock()
    reg = play_targets.PlayTargets(clock)
    assert asyncio.run(reg.receive(TV, "VLC", 0)) is None
    clock.t += play_targets.LISTEN_GRACE - 1
    assert len(reg.targets(_alive)) == 1
    assert reg.targets(lambda _id: False) == []     # unpaired meanwhile: not listed
    clock.t += 1
    assert reg.targets(_alive) == []
    with pytest.raises(play_targets.PlayError) as exc:
        asyncio.run(reg.send("tv1", PLAY, "phone", _alive))
    assert exc.value.status == 404


def test_registry_drops_what_nobody_collects():
    clock = Clock()
    reg = play_targets.PlayTargets(clock, deliver_wait=0)

    async def run():
        await reg.receive(TV, "VLC", 0)
        with pytest.raises(play_targets.PlayError) as exc:
            await reg.send("tv1", PLAY, "phone", _alive)
        assert exc.value.status == 409 and "isn't listening" in exc.value.detail
        # dropped, never delivered late
        assert await reg.receive(TV, "VLC", 0) is None

    asyncio.run(run())


def test_registry_newer_send_replaces_the_pending_one():
    reg = play_targets.PlayTargets(Clock())

    async def run():
        await reg.receive(TV, "VLC", 0)
        first = asyncio.create_task(reg.send("tv1", {**PLAY, "at": 1.0}, "a", _alive))
        await asyncio.sleep(0)
        second = asyncio.create_task(reg.send("tv1", {**PLAY, "at": 2.0}, "b", _alive))
        await asyncio.sleep(0)
        with pytest.raises(play_targets.PlayError) as exc:
            await first
        assert exc.value.status == 409 and "newer" in exc.value.detail
        got = await reg.receive(TV, "VLC", 0)
        assert got["at"] == 2.0
        reg.report("tv1", got["id"], False, "VLC isn't installed")
        return await second

    assert asyncio.run(run())["result"] == {"ok": False, "detail": "VLC isn't installed"}


def test_registry_expired_request_is_not_handed_out():
    clock = Clock()
    reg = play_targets.PlayTargets(clock)

    async def run():
        await reg.receive(TV, "VLC", 0)
        send = asyncio.create_task(reg.send("tv1", PLAY, "a", _alive))
        await asyncio.sleep(0)
        clock.t += play_targets.REQUEST_TTL
        assert await reg.receive(TV, "VLC", 0) is None
        with pytest.raises(play_targets.PlayError) as exc:
            await send
        assert exc.value.status == 409

    asyncio.run(run())


def test_registry_newer_poll_ends_the_older_one_empty():
    reg = play_targets.PlayTargets(Clock())

    async def run():
        old = asyncio.create_task(reg.receive(TV, "VLC", 25))
        await asyncio.sleep(0)
        new = asyncio.create_task(reg.receive(TV, "VLC", 25))
        await asyncio.sleep(0)
        assert await old is None
        send = asyncio.create_task(reg.send("tv1", PLAY, "a", _alive))
        got = await new
        assert got["path"] == PLAY["path"]
        reg.report("tv1", got["id"], True, "")
        await send

    asyncio.run(run())


def test_registry_forget_ends_the_poll_and_the_target():
    reg = play_targets.PlayTargets(Clock())

    async def run():
        poll = asyncio.create_task(reg.receive(TV, "VLC", 25))
        await asyncio.sleep(0)
        reg.forget("tv1")
        with pytest.raises(play_targets.Revoked):
            await poll
        assert reg.targets(_alive) == []

    asyncio.run(run())


def test_registry_rate_limits_each_sender():
    clock = Clock()
    reg = play_targets.PlayTargets(clock, deliver_wait=0)

    async def run():
        await reg.receive(TV, "VLC", 0)
        for _ in range(play_targets.SEND_RATE):
            with pytest.raises(play_targets.PlayError) as exc:
                await reg.send("tv1", PLAY, "a", _alive)
            assert exc.value.status == 409
        with pytest.raises(play_targets.PlayError) as exc:
            await reg.send("tv1", PLAY, "a", _alive)
        assert exc.value.status == 429
        with pytest.raises(play_targets.PlayError) as exc:   # another sender may
            await reg.send("tv1", PLAY, "b", _alive)
        assert exc.value.status == 409
        clock.t += 60
        await reg.receive(TV, "VLC", 0)
        with pytest.raises(play_targets.PlayError) as exc:
            await reg.send("tv1", PLAY, "a", _alive)
        assert exc.value.status == 409

    asyncio.run(run())


# --- the routes --------------------------------------------------------------------------------


@pytest.fixture
def env(tmp_path):
    media = tmp_path / "media"
    (media / "Film (2001)").mkdir(parents=True)
    film = media / "Film (2001)" / "Film (2001).mkv"
    film.write_bytes(b"film")
    outside = tmp_path / "outside.mkv"
    outside.write_bytes(b"secret")
    clips = tmp_path / "clips"
    clips.mkdir()
    clock = Clock()
    settings = Settings.from_env({"QC_MEDIA_ROOTS": str(media), "QC_CLIPS_DIR": str(clips),
                                  "QC_STATE_DIR": str(tmp_path / "state")})
    app = create_app(settings, clock=clock)
    with TestClient(app, client=("127.0.0.1", 50000)) as c:
        yield {"c": c, "app": app, "clock": clock, "film": film, "outside": outside}


def _pair(c: TestClient, scopes, name="LineFinder", host="tv") -> tuple[str, str]:
    body = {"name": name, "host": host}
    if scopes is not None:
        body["scopes"] = scopes
    req = c.post("/api/pair", json=body, headers=CSRF).json()
    assert c.post(f"/api/pair/{req['id']}/approve", headers=CSRF).status_code == 200
    got = c.get(f"/api/pair/{req['id']}", headers={"X-Pair-Secret": req["poll_secret"]}).json()
    return got["token"], got["client_id"]


def _key(token: str) -> dict:
    return {"X-API-Key": token}


def _register(c, token, player="VLC"):
    r = c.get("/api/play/receive", params={"wait": 0, "player": player}, headers=_key(token))
    assert r.status_code == 200, r.text
    assert r.json() == {"request": None}


def test_scopes_open_exactly_their_routes():
    recv = ["play.receive"]
    send = ["play.send"]
    assert pairing.scope_allows(recv, "GET", "/api/play/receive")
    assert pairing.scope_allows(recv, "POST", "/api/play/receive/abc")
    assert not pairing.scope_allows(recv, "GET", "/api/play/targets")
    assert not pairing.scope_allows(recv, "POST", "/api/play/targets/abc")
    assert not pairing.scope_allows(recv, "GET", "/api/media/link")
    assert pairing.scope_allows(send, "GET", "/api/play/targets")
    assert pairing.scope_allows(send, "POST", "/api/play/targets/abc")
    assert not pairing.scope_allows(send, "GET", "/api/play/receive")
    assert not pairing.scope_allows(send, "POST", "/api/play/receive/abc")
    assert not pairing.scope_allows(send, "POST", "/api/play/targets/a/b")
    # jellyfin.play promises Jellyfin devices only
    assert not pairing.scope_allows(["jellyfin.play"], "GET", "/api/play/targets")


def test_a_poll_lists_the_app_as_a_target(env):
    c = env["c"]
    tv, tv_id = _pair(c, ["play.receive", "media.link"], host="Living room")
    assert c.get("/api/play/targets").json() == {"targets": []}
    _register(c, tv)
    assert c.get("/api/play/targets").json() == {"targets": [
        {"id": tv_id, "name": "LineFinder", "host": "Living room", "player": "VLC",
         "listening": True}]}
    env["clock"].t += play_targets.LISTEN_GRACE
    assert c.get("/api/play/targets").json() == {"targets": []}


def test_a_receive_only_token_cant_list_or_send(env):
    c = env["c"]
    tv, tv_id = _pair(c, ["play.receive"])
    _register(c, tv)
    assert c.get("/api/play/targets", headers=_key(tv)).status_code == 403
    r = c.post(f"/api/play/targets/{tv_id}", json={"path": str(env["film"])},
               headers=_key(tv))
    assert r.status_code == 403


def test_a_send_only_token_cant_receive(env):
    c = env["c"]
    phone, _ = _pair(c, ["play.send"], name="LineFinder", host="iPhone")
    assert c.get("/api/play/receive", params={"wait": 0},
                 headers=_key(phone)).status_code == 403
    assert c.post("/api/play/receive/x", json={"ok": True},
                  headers=_key(phone)).status_code == 403
    assert c.get("/api/play/targets", headers=_key(phone)).status_code == 200


def test_a_person_or_api_token_cant_be_a_target(env):
    r = env["c"].get("/api/play/receive", params={"wait": 0})
    assert r.status_code == 403 and "paired app" in r.json()["detail"]


def test_a_send_round_trip(env):
    c = env["c"]
    tv, tv_id = _pair(c, ["play.receive", "media.link"], host="Living room")
    phone, _ = _pair(c, ["play.send"], host="iPhone")
    _register(c, tv)
    with ThreadPoolExecutor(1) as pool:
        sent = pool.submit(c.post, f"/api/play/targets/{tv_id}",
                           json={"path": str(env["film"]), "at": 61.5, "subtitles": "ENG",
                                 "track": 3}, headers=_key(phone))
        # Whichever comes first, the poll collects it: queued already, or woken by it.
        got = c.get("/api/play/receive", params={"wait": 25, "player": "VLC"},
                    headers=_key(tv)).json()["request"]
        assert got == {"id": got["id"], "path": str(env["film"].resolve()), "at": 61.5,
                       "subtitles": "eng", "track": 3, "from": "LineFinder on iPhone"}
        assert set(got) == {"id", "path", "at", "subtitles", "track", "from"}  # no link
        r = c.post(f"/api/play/receive/{got['id']}", json={"ok": True, "detail": "Opened in VLC"},
                   headers=_key(tv))
        assert r.status_code == 200 and r.json() == {"reported": True}
        out = sent.result()
    assert out.status_code == 200, out.text
    assert out.json() == {"target": {"id": tv_id, "name": "LineFinder", "host": "Living room",
                                     "player": "VLC", "listening": True},
                          "delivered": True,
                          "result": {"ok": True, "detail": "Opened in VLC"}}
    # Reported once; another app can't report it either.
    assert c.post(f"/api/play/receive/{got['id']}", json={"ok": True},
                  headers=_key(tv)).status_code == 404


def test_the_web_ui_sends_as_a_person(env):
    c = env["c"]
    tv, tv_id = _pair(c, ["play.receive"])
    _register(c, tv)
    with ThreadPoolExecutor(1) as pool:
        sent = pool.submit(c.post, f"/api/play/targets/{tv_id}",
                           json={"path": str(env["film"])}, headers=CSRF)
        got = c.get("/api/play/receive", params={"wait": 25},
                    headers=_key(tv)).json()["request"]
        assert got["from"] == "QuipClipper" and got["at"] == 0.0
        c.post(f"/api/play/receive/{got['id']}", json={"ok": False, "detail": "No VLC"},
               headers=_key(tv))
        assert sent.result().json()["result"] == {"ok": False, "detail": "No VLC"}


def test_not_listening_is_409_and_nothing_is_delivered_late(env):
    c = env["c"]
    env["app"].state.play_targets.deliver_wait = 0
    tv, tv_id = _pair(c, ["play.receive"])
    _register(c, tv)
    r = c.post(f"/api/play/targets/{tv_id}", json={"path": str(env["film"])}, headers=CSRF)
    assert r.status_code == 409 and r.json()["detail"] == "LineFinder on tv isn't listening."
    _register(c, tv)   # the dropped request isn't handed out


def test_unknown_target_is_404(env):
    r = env["c"].post("/api/play/targets/nope", json={"path": str(env["film"])},
                      headers=CSRF)
    assert r.status_code == 404


def test_a_path_outside_the_roots_is_refused_before_queueing(env):
    c = env["c"]
    tv, tv_id = _pair(c, ["play.receive"])
    _register(c, tv)
    r = c.post(f"/api/play/targets/{tv_id}", json={"path": str(env["outside"])},
               headers=CSRF)
    assert r.status_code == 403
    r = c.post(f"/api/play/targets/{tv_id}",
               json={"path": str(env["film"].with_name("missing.mkv"))}, headers=CSRF)
    assert r.status_code == 404
    r = c.post(f"/api/play/targets/{tv_id}", json={"path": str(env["film"]), "at": -1},
               headers=CSRF)
    assert r.status_code == 422
    _register(c, tv)   # nothing was queued


def test_revoking_the_app_removes_the_target(env):
    c = env["c"]
    tv, tv_id = _pair(c, ["play.receive"])
    _register(c, tv)
    assert len(c.get("/api/play/targets").json()["targets"]) == 1
    assert c.delete(f"/api/clients/{tv_id}", headers=CSRF).json() == {"removed": True}
    assert c.get("/api/play/targets").json() == {"targets": []}
    assert c.get("/api/play/receive", params={"wait": 0}, headers=_key(tv)).status_code == 401
    r = c.post(f"/api/play/targets/{tv_id}", json={"path": str(env["film"])}, headers=CSRF)
    assert r.status_code == 404


def test_revoking_ends_an_open_poll_with_401(env):
    c = env["c"]
    tv, tv_id = _pair(c, ["play.receive"])
    _register(c, tv)
    reg = env["app"].state.play_targets
    with ThreadPoolExecutor(1) as pool:
        poll = pool.submit(c.get, "/api/play/receive", params={"wait": 25},
                           headers=_key(tv))
        # Revoking from the registry's side directly would race the poll's start; revoke
        # through the route once the poll is waiting (its waiter is set in the event loop).
        while not poll.done():
            t = reg._targets.get(tv_id)
            if t is not None and t.waiter is not None:
                break
        c.delete(f"/api/clients/{tv_id}", headers=CSRF)
        assert poll.result().status_code == 401


def test_full_access_token_may_send_and_receive(env):
    c = env["c"]
    full, full_id = _pair(c, None)
    _register(c, full)
    assert [t["id"] for t in c.get("/api/play/targets", headers=_key(full)).json()
            ["targets"]] == [full_id]


def test_player_label_is_checked(env):
    c = env["c"]
    tv, _ = _pair(c, ["play.receive"])
    r = c.get("/api/play/receive", params={"wait": 0, "player": "V‮LC"}, headers=_key(tv))
    assert r.status_code == 422
    r = c.get("/api/play/receive", params={"wait": 0, "player": "x" * 21}, headers=_key(tv))
    assert r.status_code == 422
