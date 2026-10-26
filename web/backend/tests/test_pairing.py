"""Pairing (quipclipper.pair/1): an app asks for access, a person lets it in, and the app
gets a client token of its own; plus the server identity in /api/config and the server
named in Send to LeadStinger links without QC_PUBLIC_URL."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import socket
import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from quipclipper_web import lslink, pairing
from quipclipper_web.app import create_app
from quipclipper_web.config import Settings

CSRF = {"X-Quipclipper": "1"}
ASK = {"name": "LeadStinger", "host": "laptop"}


def _client(tmp_path: Path, peer: str = "127.0.0.1", **env: str) -> TestClient:
    """A client whose requests come from ``peer``: by default loopback, where the bundled
    nginx connects from, so its X-Real-IP and Tailscale-User-* headers are believed."""
    clips = tmp_path / "clips"
    clips.mkdir(parents=True, exist_ok=True)
    base = {"QC_STATE_DIR": str(tmp_path / "state"), "QC_CLIPS_DIR": str(clips)}
    return TestClient(create_app(Settings.from_env({**base, **env})), client=(peer, 50000))


def _start(c: TestClient, body: dict | None = None, **headers: str):
    return c.post("/api/pair", json=body or ASK, headers={**CSRF, **headers})


def _poll(c: TestClient, req: dict, secret: str | None = None):
    headers = {} if secret == "" else {"X-Pair-Secret": secret or req["poll_secret"]}
    return c.get(f"/api/pair/{req['id']}", headers=headers)


def _paired(c: TestClient) -> tuple[str, str]:
    """Pair one app; its (token, client_id)."""
    req = _start(c).json()
    assert c.post(f"/api/pair/{req['id']}/approve", headers=CSRF).status_code == 200
    got = _poll(c, req).json()
    return got["token"], got["client_id"]


def _basic(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


# --- the flow -------------------------------------------------------------------------


def test_pairing_happy_path(tmp_path):
    c = _client(tmp_path)
    r = _start(c)
    assert r.status_code == 200
    req = r.json()
    assert set(req) == {"id", "code", "poll_secret", "expires_in"}
    assert len(req["id"]) >= 22 and re.fullmatch(r"[A-Za-z0-9_-]+", req["id"])
    assert len(req["poll_secret"]) == 43
    assert re.fullmatch(f"[{pairing.CODE_ALPHABET}]{{4}}", req["code"])
    assert req["expires_in"] == 600
    assert _poll(c, req).json() == {"status": "pending"}

    listed = c.get("/api/pair").json()["requests"]
    assert len(listed) == 1
    row = listed[0]
    assert set(row) == {"id", "name", "host", "code", "address", "user", "user_name",
                        "created", "expires_at", "scopes"}
    assert row["scopes"] is None   # asked for none: full access
    assert (row["id"], row["name"], row["host"], row["code"]) == (
        req["id"], "LeadStinger", "laptop", req["code"])
    assert row["created"].endswith("Z") and row["expires_at"].endswith("Z")
    assert "poll_secret" not in json.dumps(listed)

    approved = c.post(f"/api/pair/{req['id']}/approve", headers=CSRF)
    assert approved.status_code == 200
    client_id = approved.json()["client_id"]
    assert approved.json() == {"status": "approved", "client_id": client_id}
    assert c.get("/api/pair").json() == {"requests": []}

    got = _poll(c, req).json()
    assert got["status"] == "approved" and got["client_id"] == client_id
    assert got["scopes"] is None
    assert re.fullmatch(r"qcc_[A-Za-z0-9_-]{43}", got["token"])
    clients = c.get("/api/clients").json()["clients"]
    assert [(x["id"], x["name"], x["host"]) for x in clients] == [
        (client_id, "LeadStinger", "laptop")]
    assert set(clients[0]) == {"id", "name", "host", "address", "user", "user_name",
                               "created", "last_used", "scopes"}
    assert clients[0]["scopes"] is None


def test_the_token_is_handed_out_once(tmp_path):
    c = _client(tmp_path)
    req = _start(c).json()
    c.post(f"/api/pair/{req['id']}/approve", headers=CSRF)
    assert "token" in _poll(c, req).json()
    assert _poll(c, req).status_code == 404   # the request is gone after that
    # Approving again is a 404 too: it isn't pending any more.
    assert c.post(f"/api/pair/{req['id']}/approve", headers=CSRF).status_code == 404


def test_deny(tmp_path):
    c = _client(tmp_path)
    req = _start(c).json()
    assert c.post(f"/api/pair/{req['id']}/deny", headers=CSRF).json() == {"status": "denied"}
    assert _poll(c, req).json() == {"status": "denied"}
    assert c.get("/api/pair").json() == {"requests": []}
    assert c.post(f"/api/pair/{req['id']}/approve", headers=CSRF).status_code == 404
    assert c.post(f"/api/pair/{req['id']}/deny", headers=CSRF).status_code == 404
    assert c.get("/api/clients").json() == {"clients": []}


def test_unknown_requests_are_404(tmp_path):
    c = _client(tmp_path)
    assert c.post("/api/pair/nope/approve", headers=CSRF).status_code == 404
    assert c.post("/api/pair/nope/deny", headers=CSRF).status_code == 404
    assert c.get("/api/pair/nope", headers={"X-Pair-Secret": "x"}).status_code == 404


def test_a_wrong_or_missing_poll_secret_is_a_404(tmp_path):
    c = _client(tmp_path)
    req = _start(c).json()
    assert _poll(c, req, "wrong").status_code == 404
    assert _poll(c, req, "").status_code == 404
    latin1 = c.get(f"/api/pair/{req['id']}", headers={"X-Pair-Secret": "é".encode("latin-1") * 43})
    assert latin1.status_code == 404   # not even ASCII: still just 404
    other = _start(c).json()
    assert _poll(c, req, other["poll_secret"]).status_code == 404
    # The approval goes only to the right secret, and isn't spent by the wrong ones.
    c.post(f"/api/pair/{req['id']}/approve", headers=CSRF)
    assert _poll(c, req, "wrong").status_code == 404
    assert _poll(c, req).json()["status"] == "approved"


def test_pairing_needs_the_csrf_header_but_no_token(tmp_path):
    c = _client(tmp_path, QC_API_TOKEN="server-token")
    assert c.post("/api/pair", json=ASK).status_code == 403
    assert _start(c).status_code == 200


@pytest.mark.parametrize("body", [
    {"name": "", "host": "x"},
    {"name": "   ", "host": "x"},
    {"name": "x" * 61, "host": "x"},
    {"name": "LeadStinger", "host": "h" * 101},
    {"name": "Lead\nStinger", "host": "x"},
    {"name": "LeadStinger", "host": "lap\x00top"},
    {"name": "LeadStinger", "host": "lap\x7ftop"},
    {"name": "LeadStinger‮", "host": "x"},          # a bidi override
    {"name": "Lead‍Stinger", "host": "x"},          # a zero-width joiner
    {"host": "x"},
    {"name": 5, "host": "x"},
])
def test_name_and_host_are_checked(tmp_path, body):
    assert _start(_client(tmp_path), body).status_code == 422


def test_host_may_be_empty_or_left_out(tmp_path):
    c = _client(tmp_path)
    assert _start(c, {"name": "LeadStinger", "host": ""}).status_code == 200
    assert _start(c, {"name": "LeadStinger"}).status_code == 200
    assert [r["host"] for r in c.get("/api/pair").json()["requests"]] == ["", ""]


def test_at_most_five_pending_requests(tmp_path):
    c = _client(tmp_path)
    reqs = [_start(c, **{"X-Real-IP": f"192.168.1.{i}"}).json() for i in range(5)]
    r = _start(c, **{"X-Real-IP": "192.168.1.99"})
    assert r.status_code == 429 and "5" in r.json()["detail"]
    # Answering one frees a place.
    c.post(f"/api/pair/{reqs[0]['id']}/deny", headers=CSRF)
    assert _start(c, **{"X-Real-IP": "192.168.1.99"}).status_code == 200


def test_at_most_ten_requests_a_minute_from_one_address(tmp_path):
    c = _client(tmp_path)
    for _ in range(10):
        req = _start(c, **{"X-Real-IP": "192.168.1.7"}).json()
        c.post(f"/api/pair/{req['id']}/deny", headers=CSRF)   # stay under the pending cap
    r = _start(c, **{"X-Real-IP": "192.168.1.7"})
    assert r.status_code == 429 and "minute" in r.json()["detail"]
    assert _start(c, **{"X-Real-IP": "192.168.1.8"}).status_code == 200


def test_the_request_shows_where_it_came_from(tmp_path):
    c = _client(tmp_path)
    _start(c, **{"X-Real-IP": "192.168.1.20"})
    _start(c, **{"X-Real-IP": "not an address"})
    assert [r["address"] for r in c.get("/api/pair").json()["requests"]] == [
        "192.168.1.20", "127.0.0.1"]


# --- who is asking (quipclipper_web.caller) ------------------------------------------------

TS = {"X-Real-IP": "100.116.110.76", "Tailscale-User-Login": "wiz@example.com",
      "Tailscale-User-Name": "Wiz Example"}


def _asked(c: TestClient) -> dict:
    (row,) = c.get("/api/pair").json()["requests"]
    return row


def test_behind_tailscale_serve_the_request_names_the_tailnet_user(tmp_path):
    c = _client(tmp_path)
    _start(c, **TS)
    row = _asked(c)
    assert (row["address"], row["user"], row["user_name"]) == (
        "100.116.110.76", "wiz@example.com", "Wiz Example")
    # Allowed, the app keeps who asked for it: Connected apps shows it.
    c.post(f"/api/pair/{row['id']}/approve", headers=CSRF)
    (app,) = c.get("/api/clients").json()["clients"]
    assert (app["address"], app["user"], app["user_name"]) == (
        "100.116.110.76", "wiz@example.com", "Wiz Example")


def test_a_direct_caller_cannot_say_who_it_is(tmp_path):
    # Not the bundled nginx (not in QC_FORWARDED_ALLOW_IPS): its headers count for nothing.
    c = _client(tmp_path, peer="192.168.1.50")
    _start(c, **TS)
    row = _asked(c)
    assert (row["address"], row["user"], row["user_name"]) == ("192.168.1.50", "", "")


def test_a_spoofed_address_does_not_dodge_the_rate_limit(tmp_path):
    c = _client(tmp_path, peer="192.168.1.50")
    for i in range(10):
        req = _start(c, **{"X-Real-IP": f"10.0.0.{i}"}).json()
        c.post(f"/api/pair/{req['id']}/deny", headers=CSRF)
    assert _start(c, **{"X-Real-IP": "10.0.0.99"}).status_code == 429


def test_trusted_proxies_are_configurable(tmp_path):
    c = _client(tmp_path, peer="172.18.0.5", QC_FORWARDED_ALLOW_IPS="172.18.0.0/16, ::1")
    _start(c, **TS)
    assert _asked(c)["user"] == "wiz@example.com"
    # Loopback is no longer trusted once the list leaves it out.
    c = _client(tmp_path / "b", QC_FORWARDED_ALLOW_IPS="172.18.0.0/16")
    _start(c, **TS)
    assert (_asked(c)["address"], _asked(c)["user"]) == ("127.0.0.1", "")
    with pytest.raises(ValueError, match="QC_FORWARDED_ALLOW_IPS"):
        Settings.from_env({"QC_FORWARDED_ALLOW_IPS": "nginx"})


def test_the_proxy_secret_vouches_for_any_peer(tmp_path):
    # With QC_PROXY_SECRET only nginx gets a request in at all, from any address.
    c = _client(tmp_path, peer="172.20.0.3", QC_PROXY_SECRET="s3cret")
    c.headers["X-Quip-Proxy-Secret"] = "s3cret"
    _start(c, **TS)
    row = _asked(c)
    assert (row["address"], row["user"]) == ("100.116.110.76", "wiz@example.com")


def test_forwarded_names_are_plain_text(tmp_path):
    c = _client(tmp_path)
    _start(c, **{"X-Real-IP": "100.64.0.9", "Tailscale-User-Login": "jo@example.com",
                 "Tailscale-User-Name": "=?utf-8?q?Jos=C3=A9?="})
    assert _asked(c)["user_name"] == "José"
    # A name without a login is no identity; and control characters never get through.
    from quipclipper_web import caller
    got = caller.caller({"tailscale-user-name": "Mallory"}, "127.0.0.1",
                        caller.parse_networks(caller.DEFAULT_TRUSTED))
    assert (got["user"], got["user_name"]) == ("", "")
    got = caller.caller({"tailscale-user-login": "a\x1b[31mb@example.com" + "x" * 300},
                        "::ffff:127.0.0.1", caller.parse_networks("127.0.0.1"))
    assert got["user"].startswith("a[31mb@example.com") and len(got["user"]) == 200


def test_unknown_peer_and_bad_real_ip(tmp_path):
    from quipclipper_web import caller
    nets = caller.parse_networks(caller.DEFAULT_TRUSTED)
    assert caller.caller({"x-real-ip": "1.2.3.4"}, None, nets)["address"] == "unknown"
    assert caller.caller({"x-real-ip": "1.2.3.4"}, "testclient", nets)["address"] == "testclient"
    assert caller.caller({"x-real-ip": "nope"}, "::1", nets)["address"] == "::1"


def test_an_empty_allow_list_believes_no_one(tmp_path):
    c = _client(tmp_path, QC_FORWARDED_ALLOW_IPS="")
    _start(c, **TS)
    row = _asked(c)
    assert (row["address"], row["user"], row["user_name"]) == ("127.0.0.1", "", "")


def test_ipv6_peers(tmp_path):
    from quipclipper_web import caller
    loop = caller.parse_networks(caller.DEFAULT_TRUSTED)
    v6 = {"x-real-ip": "fd7a:115c:a1e0::1", "tailscale-user-login": "jo@example.com"}
    # nginx over IPv6 loopback, naming an IPv6 caller.
    assert caller.caller(v6, "::1", loop) == {
        "address": "fd7a:115c:a1e0::1", "user": "jo@example.com", "user_name": ""}
    # An IPv4 peer written IPv4-mapped is judged as that IPv4 address: loopback is trusted,
    # a LAN address is not.
    assert caller.caller(v6, "::ffff:127.0.0.1", loop)["user"] == "jo@example.com"
    assert caller.caller(v6, "::ffff:192.168.1.50", loop) == {
        "address": "::ffff:192.168.1.50", "user": "", "user_name": ""}
    # IPv6 ranges in the list, and an IPv4 range never matching an IPv6 peer.
    nets = caller.parse_networks("fd00::/8 10.0.0.0/8")
    assert caller.caller(v6, "fd00::5", nets)["user"] == "jo@example.com"
    assert caller.caller(v6, "fe80::1", nets)["user"] == ""
    c = _client(tmp_path, peer="fd00::5", QC_FORWARDED_ALLOW_IPS="fd00::/8")
    _start(c, **TS)
    assert (_asked(c)["address"], _asked(c)["user"]) == ("100.116.110.76", "wiz@example.com")


def test_the_docker_app_image_trusts_only_private_ranges():
    from quipclipper_web import caller
    dockerfile = Path(__file__).resolve().parents[2] / "Dockerfile"
    if not dockerfile.is_file():
        pytest.skip("not a full checkout")
    value = re.search(r'QC_FORWARDED_ALLOW_IPS="([^"]*)"', dockerfile.read_text()).group(1)
    nets = caller.parse_networks(value)
    for peer in ("172.18.0.3", "10.1.2.3", "192.168.5.4", "fd12::3"):
        assert caller.trusted(peer, nets), peer
    for peer in ("100.116.110.76", "8.8.8.8", "2001:db8::1", "127.0.0.1"):
        assert not caller.trusted(peer, nets), peer


def test_forwarded_names_lose_format_characters(tmp_path):
    from quipclipper_web import caller
    got = caller.caller({"tailscale-user-login": "jo@example.com",
                         "tailscale-user-name": "Jo\u202e moc.elpmaxe\u200b"},
                        "127.0.0.1", caller.parse_networks("127.0.0.1"))
    assert got["user_name"] == "Jo moc.elpmaxe"


def test_clients_from_before_the_caller_fields_still_list(tmp_path):
    store = pairing.ClientStore(tmp_path)
    store.path.write_text(json.dumps({"clients": [{
        "id": "old", "name": "LeadStinger", "host": "mac", "token_sha256": "0" * 64,
        "created": "2026-10-24T00:00:00Z", "last_used": None}]}))
    (row,) = store.list()
    assert (row["address"], row["user"], row["user_name"]) == ("", "", "")


# --- expiry, with a clock ------------------------------------------------------------------


class Clock:
    def __init__(self, t: float = 1_800_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def test_a_request_expires_after_ten_minutes(tmp_path):
    clock = Clock()
    p = pairing.Pairing(pairing.ClientStore(tmp_path, clock), clock)
    req = p.start("LeadStinger", "laptop", "192.168.1.2")
    clock.t += 599
    assert p.poll(req["id"], req["poll_secret"]) == {"status": "pending"}
    clock.t += 1
    assert p.poll(req["id"], req["poll_secret"]) == {"status": "expired"}
    assert p.pending() == []
    assert p.approve(req["id"]) is None
    # It answers "expired" for a while, then it is gone.
    clock.t += pairing.LINGER
    assert p.poll(req["id"], req["poll_secret"]) is None


def test_an_expired_request_frees_its_pending_place(tmp_path):
    clock = Clock()
    p = pairing.Pairing(pairing.ClientStore(tmp_path, clock), clock)
    for i in range(5):
        p.start("LeadStinger", "h", f"10.0.0.{i}")
    with pytest.raises(pairing.PairError) as exc:
        p.start("LeadStinger", "h", "10.0.0.9")
    assert exc.value.status == 429
    clock.t += 600
    assert p.start("LeadStinger", "h", "10.0.0.9")["expires_in"] == 600


def test_the_rate_limit_is_per_minute(tmp_path):
    clock = Clock()
    p = pairing.Pairing(pairing.ClientStore(tmp_path, clock), clock)
    for _ in range(10):
        p.deny(p.start("LeadStinger", "h", "10.0.0.1")["id"])
    with pytest.raises(pairing.PairError):
        p.start("LeadStinger", "h", "10.0.0.1")
    clock.t += 60
    assert p.start("LeadStinger", "h", "10.0.0.1")


def test_an_approval_nobody_collects_takes_its_client_with_it(tmp_path):
    clock = Clock()
    store = pairing.ClientStore(tmp_path, clock)
    p = pairing.Pairing(store, clock)
    req = p.start("LeadStinger", "h", "10.0.0.1")
    clock.t += 590
    client_id = p.approve(req["id"])["client_id"]
    assert [c["id"] for c in store.list()] == [client_id]
    clock.t += pairing.LINGER - 1          # at least a minute to collect it, past expiry
    assert p.pending() == [] and store.list()
    clock.t += 1
    assert p.poll(req["id"], req["poll_secret"]) is None
    assert store.list() == []


def test_codes_avoid_lookalikes_and_differ_while_pending(tmp_path):
    p = pairing.Pairing(pairing.ClientStore(tmp_path))
    codes = [p.deny(r["id"]) and r["code"]
             for r in (p.start("A", "h", f"10.0.0.{i}") for i in range(5))]
    assert all(set(code) <= set(pairing.CODE_ALPHABET) for code in codes)
    assert not set("01OIL") & set(pairing.CODE_ALPHABET)
    p2 = pairing.Pairing(pairing.ClientStore(tmp_path / "b"))
    live = [p2.start("A", "h", f"10.0.0.{i}")["code"] for i in range(5)]
    assert len(set(live)) == 5


# --- client tokens -------------------------------------------------------------------------


def test_a_client_token_reads_and_skips_the_csrf_header_with_no_api_token_set(tmp_path):
    c = _client(tmp_path)  # no QC_API_TOKEN at all
    token, _ = _paired(c)
    for headers in ({"X-API-Key": token}, {"Authorization": f"Bearer {token}"},
                    {"Authorization": _basic("api", token)}):
        assert c.get("/api/clips", headers=headers).status_code == 200
        # A write without the CSRF header: a machine client, so it passes the gate.
        assert c.delete("/api/bookmarks", headers=headers).status_code == 200
    assert c.delete("/api/bookmarks").status_code == 403  # a browser still needs the header


def test_a_client_token_works_beside_the_server_token_and_hook_tokens(tmp_path):
    c = _client(tmp_path, QC_API_TOKEN="server-token", QC_HOOK_TOKEN="hook-token")
    token, _ = _paired(c)
    assert c.get("/api/clips", headers={"X-API-Key": token}).status_code == 200
    assert c.delete("/api/bookmarks", headers={"X-API-Key": token}).status_code == 200
    assert c.get("/api/clips", headers={"X-API-Key": "server-token"}).status_code == 200


@pytest.mark.parametrize("headers", [
    {"X-API-Key": "qcc_unknown"},
    {"Authorization": "Bearer qcc_unknown"},
    {"authorization": "bearer   qcc_unknown"},
    # a qcc_ value anywhere it is named outright must be a current one
    {"X-API-Key": "not-a-client-token", "Authorization": "Bearer qcc_unknown"},
    {"X-API-Key": "", "Authorization": "Bearer qcc_unknown"},
])
def test_an_unknown_client_token_is_401_even_with_no_api_token_set(tmp_path, headers):
    c = _client(tmp_path)
    r = c.get("/api/config", headers=headers)
    assert r.status_code == 401 and "Pair it again" in r.json()["detail"]


def test_a_revoked_client_token_is_401(tmp_path):
    c = _client(tmp_path)
    token, client_id = _paired(c)
    assert c.delete(f"/api/clients/{client_id}", headers=CSRF).json() == {"removed": True}
    assert c.get("/api/clips", headers={"X-API-Key": token}).status_code == 401
    assert c.get("/api/clips", headers={"Authorization": f"Bearer {token}"}).status_code == 401
    assert c.delete(f"/api/clients/{client_id}", headers=CSRF).json() == {"removed": False}


def test_two_different_client_tokens_at_once_are_refused(tmp_path):
    c = _client(tmp_path)
    a, _ = _paired(c)
    b, _ = _paired(c)
    r = c.get("/api/clips", headers={"X-API-Key": a, "Authorization": f"Bearer {b}"})
    assert r.status_code == 401


def test_an_unknown_qcc_basic_password_is_no_answer(tmp_path):
    """A Basic password that only looks like a client token may be a person's password: it
    neither authenticates nor gets a 401 of its own."""
    c = _client(tmp_path)
    assert c.get("/api/clips", headers={"Authorization": _basic("api", "qcc_x")}).status_code == 200
    assert c.delete("/api/bookmarks",
                    headers={"Authorization": _basic("api", "qcc_x")}).status_code == 403


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/pair"),
    ("POST", "/api/pair/{id}/approve"),
    ("POST", "/api/pair/{id}/deny"),
    ("GET", "/api/clients"),
    ("DELETE", "/api/clients/{client}"),
])
def test_a_client_token_cant_approve_list_or_revoke(tmp_path, method, path):
    c = _client(tmp_path, QC_API_TOKEN="server-token")
    token, client_id = _paired(c)
    req = _start(c).json()
    url = path.format(id=req["id"], client=client_id)
    r = c.request(method, url, headers={"X-API-Key": token})
    assert r.status_code == 403 and "paired app" in r.json()["detail"]
    # The request is still pending and the client still there.
    assert len(c.get("/api/pair", headers={"X-API-Key": "server-token"}).json()["requests"]) == 1
    assert c.get("/api/clients", headers={"X-API-Key": "server-token"}).json()["clients"]
    # The server's own token may.
    assert c.request(method, url, headers={"X-API-Key": "server-token"}).status_code == 200


def test_delete_clients_self_revokes_the_callers_token(tmp_path):
    c = _client(tmp_path, QC_API_TOKEN="server-token")
    token, client_id = _paired(c)
    other, other_id = _paired(c)
    assert c.delete("/api/clients/self").status_code == 403          # a browser: CSRF first
    assert c.delete("/api/clients/self", headers=CSRF).status_code == 401
    assert c.delete("/api/clients/self", headers={"X-API-Key": "server-token"}).status_code == 401
    assert c.delete("/api/clients/self", headers={"X-API-Key": token}).json() == {"removed": True}
    assert c.get("/api/clips", headers={"X-API-Key": token}).status_code == 401
    assert [x["id"] for x in c.get("/api/clients", headers=CSRF).json()["clients"]] == [other_id]


def test_the_gate_check_answers_for_client_tokens_only(tmp_path):
    c = _client(tmp_path, QC_API_TOKEN="server-token")
    token, client_id = _paired(c)
    assert c.get("/api/clients/check", headers={"X-API-Key": token}).json() == {
        "client_id": client_id}
    assert c.get("/api/clients/check", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    assert c.get("/api/clients/check").status_code == 401
    assert c.get("/api/clients/check", headers={"X-API-Key": "server-token"}).status_code == 401
    assert c.get("/api/clients/check", headers={"X-API-Key": "qcc_unknown"}).status_code == 401


def test_the_proxy_secret_still_comes_first(tmp_path):
    c = _client(tmp_path, QC_PROXY_SECRET="s3cret")
    ok = {"X-Quip-Proxy-Secret": "s3cret"}
    req = c.post("/api/pair", json=ASK, headers={**CSRF, **ok}).json()
    c.post(f"/api/pair/{req['id']}/approve", headers={**CSRF, **ok})
    token = c.get(f"/api/pair/{req['id']}", headers={**ok, "X-Pair-Secret": req["poll_secret"]}).json()["token"]
    assert c.get("/api/clips", headers={"X-API-Key": token}).status_code == 403
    assert c.post("/api/pair", json=ASK, headers=CSRF).status_code == 403
    assert c.get("/api/clips", headers={"X-API-Key": token, **ok}).status_code == 200


# --- the client store ----------------------------------------------------------------------


def test_the_store_keeps_only_a_hash_in_a_private_file(tmp_path):
    c = _client(tmp_path)
    token, client_id = _paired(c)
    path = tmp_path / "state" / "clients.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    text = path.read_text()
    assert token not in text
    row = json.loads(text)["clients"][0]
    assert set(row) == {"id", "name", "host", "address", "user", "user_name",
                        "token_sha256", "created", "last_used"}
    assert row["token_sha256"] == hashlib.sha256(token.encode()).hexdigest()
    assert not [p for p in path.parent.iterdir() if p.name.startswith(".clients.json")]


def test_last_used_is_written_at_most_once_a_minute(tmp_path):
    clock = Clock()
    store = pairing.ClientStore(tmp_path, clock)
    client, token = store.add("LeadStinger", "laptop")
    assert client["last_used"] is None
    assert store.authenticate(token)["last_used"] == pairing.iso(clock.t)
    first = (tmp_path / "clients.json").stat().st_mtime_ns
    clock.t += 59
    assert store.authenticate(token)["last_used"] == pairing.iso(clock.t - 59)
    assert (tmp_path / "clients.json").stat().st_mtime_ns == first
    clock.t += 1
    assert store.authenticate(token)["last_used"] == pairing.iso(clock.t)


def test_the_store_follows_its_file(tmp_path):
    a = pairing.ClientStore(tmp_path)
    b = pairing.ClientStore(tmp_path)  # another process with the same state dir
    _, token = a.add("LeadStinger", "h")
    assert b.authenticate(token) is not None
    (tmp_path / "clients.json").write_text("not json")
    assert a.authenticate(token) is None and a.list() == []   # unreadable: nobody paired


def test_authenticate_ignores_what_isnt_a_client_token(tmp_path):
    store = pairing.ClientStore(tmp_path)
    store.add("LeadStinger", "h")
    assert store.authenticate("") is None
    assert store.authenticate("server-token") is None
    assert store.authenticate("qcc_") is None


# --- QC_PAIRING=0 ---------------------------------------------------------------------------


def test_pairing_off_turns_every_pair_route_into_404(tmp_path):
    c = _client(tmp_path, QC_PAIRING="0")
    assert c.post("/api/pair", json=ASK, headers=CSRF).status_code == 404
    assert c.post("/api/pair", json={"name": ""}, headers=CSRF).status_code == 404  # not 422
    assert c.post("/api/pair", json=ASK).status_code == 404                          # not 403
    assert c.get("/api/pair").status_code == 404
    assert c.get("/api/pair/x", headers={"X-Pair-Secret": "y"}).status_code == 404
    assert c.post("/api/pair/x/approve", headers=CSRF).status_code == 404
    assert c.post("/api/pair/x/deny", headers=CSRF).status_code == 404
    assert c.get("/api/config").json()["pairing"] is False
    # Apps paired before keep working, and can still be listed and revoked.
    assert c.get("/api/clients").json() == {"clients": []}
    assert c.get("/api/pairing-not-a-pair-route").status_code == 404


def test_pairing_off_keeps_existing_clients(tmp_path):
    token, _ = _paired(_client(tmp_path))
    c = _client(tmp_path, QC_PAIRING="off")
    assert c.get("/api/clips", headers={"X-API-Key": token}).status_code == 200


# --- /api/config ------------------------------------------------------------------------------


def test_config_says_who_this_server_is(tmp_path, monkeypatch):
    monkeypatch.setattr(socket, "gethostname", lambda: "nas.local")
    cfg = _client(tmp_path).get("/api/config").json()
    assert cfg["pairing"] is True
    assert re.fullmatch(r"[0-9a-f]{32}", cfg["server_id"])
    assert cfg["server_name"] == "nas"
    # The id is kept in the state dir: another start of the server says the same.
    assert _client(tmp_path).get("/api/config").json()["server_id"] == cfg["server_id"]
    assert (tmp_path / "state" / "server-id").read_text().strip() == cfg["server_id"]


def test_server_name_comes_from_qc_server_name_and_is_capped(tmp_path):
    assert _client(tmp_path, QC_SERVER_NAME="Den").get("/api/config").json()["server_name"] == "Den"
    long = _client(tmp_path, QC_SERVER_NAME="x" * 80).get("/api/config").json()["server_name"]
    assert long == "x" * 63


def test_server_name_falls_back_to_the_advertisers_name(tmp_path, monkeypatch):
    """In Docker the app's host name is the container's; the advertiser (host network)
    knows the machine's."""
    monkeypatch.setattr(socket, "gethostname", lambda: "3f2a9c1b7d4e")
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "advertise.json").write_text(
        json.dumps({"url": "http://nas.local:8896", "name": "nas"}))
    assert _client(tmp_path).get("/api/config").json()["server_name"] == "nas"


def test_importing_the_app_writes_no_server_id(tmp_path):
    _client(tmp_path)
    assert not (tmp_path / "state" / "server-id").exists()


def test_server_id_is_made_once(tmp_path):
    first = pairing.server_id(tmp_path)
    assert pairing.server_id(tmp_path) == first
    (tmp_path / "server-id").write_text("damaged\n")
    fixed = pairing.server_id(tmp_path)
    assert re.fullmatch(r"[0-9a-f]{32}", fixed) and fixed != first
    assert pairing.server_id(tmp_path) == fixed


# --- the server Send to LeadStinger links name -----------------------------------------------


def _link_server(c: TestClient, **headers) -> str | None:
    return c.get("/api/config", headers=headers).json()["link_server"]


def test_links_name_qc_public_url_first(tmp_path):
    c = _client(tmp_path, QC_PUBLIC_URL="https://quip.example/")
    (tmp_path / "state").mkdir(exist_ok=True)
    (tmp_path / "state" / "advertise.json").write_text(json.dumps({"url": "http://nas.local:8896"}))
    assert _link_server(c, host="other:1") == "https://quip.example"


def test_links_fall_back_to_the_advertised_url(tmp_path):
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "advertise.json").write_text(json.dumps({"url": "http://nas.local:8896"}))
    assert _link_server(_client(tmp_path), host="192.168.1.20:8896") == "http://nas.local:8896"


def test_links_fall_back_to_the_address_the_browser_used(tmp_path):
    c = _client(tmp_path)
    assert _link_server(c, host="192.168.1.20:8896") == "http://192.168.1.20:8896"
    # A proxy in front (TLS, a path prefix) says so in X-Forwarded-*.
    assert _link_server(c, **{"host": "127.0.0.1:8000", "x-forwarded-proto": "https",
                             "x-forwarded-host": "quip.example", "x-forwarded-prefix": "/qc/"}
                        ) == "https://quip.example/qc"
    assert _link_server(c, **{"host": "nas:8896", "x-forwarded-proto": "https, http"}
                        ) == "https://nas:8896"
    # Nothing usable: no server, and Send to LeadStinger says how to set one.
    assert _link_server(c, host="a b") is None
    r = c.get("/api/leadstinger/link", params={"clip": "x.flac"}, headers={"host": "a b"})
    assert r.status_code == 503 and "QC_PUBLIC_URL" in r.json()["detail"]


def test_a_broken_advertise_file_is_skipped(tmp_path):
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "advertise.json").write_text(json.dumps({"url": "ftp://x"}))
    assert _link_server(_client(tmp_path), host="nas:8896") == "http://nas:8896"
    (tmp_path / "state" / "advertise.json").write_text("{")
    assert _link_server(_client(tmp_path), host="nas:8896") == "http://nas:8896"


def test_a_bad_qc_public_url_is_still_a_503(tmp_path):
    c = _client(tmp_path, QC_PUBLIC_URL="ftp://nope")
    assert _link_server(c) is None
    r = c.get("/api/leadstinger/link", params={"clip": "x.flac"})
    assert r.status_code == 503 and r.json()["detail"].startswith("QC_PUBLIC_URL")


def test_send_to_leadstinger_links_work_without_qc_public_url(tmp_path):
    c = _client(tmp_path)
    clip = tmp_path / "clips" / "LeadStinger" / "line.flac"
    clip.parent.mkdir(parents=True)
    clip.write_bytes(b"flac")
    r = c.get("/api/leadstinger/link", params={"clip": "LeadStinger/line.flac"},
              headers={"host": "nas:8896"})
    assert r.status_code == 200
    assert r.json()["link"].startswith("leadstinger://quip?server=http%3A%2F%2Fnas%3A8896&clip=")


@pytest.mark.parametrize("headers,expected", [
    ({"host": "nas:8896"}, "http://nas:8896"),
    ({"host": "nas", "x-quip-host": "nas:8896"}, "http://nas:8896"),
    ({"host": "nas", "x-quip-host": "nas:8896", "x-forwarded-host": "quip.example"},
     "http://quip.example"),
    ({"host": "[::1]:8080"}, "http://[::1]:8080"),
    ({"host": "nas.example."}, "http://nas.example."),
    ({"host": "user@evil"}, None),
    ({"host": "nas/x"}, None),
    ({"host": "nas", "x-forwarded-proto": "gopher"}, None),
    ({"host": "nas", "x-forwarded-prefix": "/a?b"}, None),
    ({"host": ""}, None),
])
def test_request_base(headers, expected):
    assert lslink.request_base(headers, "http") == expected


def test_pair_link_encodes_like_encode_uri_component():
    assert lslink.pair_link("http://nas.local:8896/") == \
        "leadstinger://pair?server=http%3A%2F%2Fnas.local%3A8896"
    with pytest.raises(lslink.LinkError):
        lslink.pair_link("http://user:pw@nas")


def test_check_label():
    assert pairing.check_label("LeadStinger", "name", 1, 60) == "LeadStinger"
    assert pairing.check_label("Läptöp 2", "host", 0, 100) == "Läptöp 2"
    for bad in ("", "a\tb", "a b", "a⁦b"):
        with pytest.raises(ValueError):
            pairing.check_label(bad, "name", 1, 60)


def test_atomic_writes_set_the_mode_from_the_start(tmp_path):
    p = tmp_path / "f.json"
    pairing.write_atomic(p, "{}", mode=0o600)
    assert stat.S_IMODE(p.stat().st_mode) == 0o600 and p.read_text() == "{}"
    os.chmod(p, 0o644)
    pairing.write_atomic(p, "{ }", mode=0o600)
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_ids_never_start_with_a_dash(tmp_path, monkeypatch):
    """A leading "-" made the CLI read a request or client id as an option."""
    from quipclipper_web import pairing

    seq = iter(["-dashfirst", "-again", "okid"])
    monkeypatch.setattr(pairing.secrets, "token_urlsafe", lambda n=None: next(seq))
    assert pairing._new_id(9) == "okid"


# --- scoped client tokens (M22) ----------------------------------------------------------


def _paired_scoped(c: TestClient, scopes: list[str]) -> tuple[str, str, dict]:
    """Pair one app that asks for ``scopes``; its (token, client_id, what its poll said)."""
    req = _start(c, {"name": "Jellyfin", "host": "wizfin", "scopes": scopes}).json()
    assert c.post(f"/api/pair/{req['id']}/approve", headers=CSRF).status_code == 200
    got = _poll(c, req).json()
    return got["token"], got["client_id"], got


def test_a_request_names_its_scopes_and_the_token_keeps_them(tmp_path):
    c = _client(tmp_path)
    req = _start(c, {"name": "Jellyfin", "host": "wizfin",
                     "scopes": ["transcripts.read", "index.file", "index.file"]}).json()
    listed = c.get("/api/pair").json()["requests"]
    assert listed[0]["scopes"] == ["index.file", "transcripts.read"]  # sorted, no repeats
    c.post(f"/api/pair/{req['id']}/approve", headers=CSRF)
    got = _poll(c, req).json()
    assert got["scopes"] == ["index.file", "transcripts.read"]
    assert c.get("/api/clients").json()["clients"][0]["scopes"] == [
        "index.file", "transcripts.read"]
    saved = json.loads((tmp_path / "state" / "clients.json").read_text())["clients"][0]
    assert saved["scopes"] == ["index.file", "transcripts.read"]


def test_a_scoped_request_shows_its_scopes_and_who_asked(tmp_path):
    c = _client(tmp_path)
    req = _start(c, {"name": "Jellyfin", "host": "tv", "scopes": ["index.file"]}, **TS).json()
    row = _asked(c)
    assert (row["scopes"], row["user"], row["address"]) == (
        ["index.file"], "wiz@example.com", "100.116.110.76")
    c.post(f"/api/pair/{req['id']}/approve", headers=CSRF)
    (app,) = c.get("/api/clients").json()["clients"]
    assert (app["scopes"], app["user"], app["user_name"], app["address"]) == (
        ["index.file"], "wiz@example.com", "Wiz Example", "100.116.110.76")


@pytest.mark.parametrize("scopes", [[], ["everything"], ["index.file", "clips.delete"], "index.file",
                                    [1]])
def test_unknown_or_empty_scopes_are_refused(tmp_path, scopes):
    c = _client(tmp_path)
    r = _start(c, {"name": "Jellyfin", "scopes": scopes})
    assert r.status_code == 422
    assert c.get("/api/pair").json() == {"requests": []}


def test_a_scoped_token_opens_only_its_scopes_routes(tmp_path):
    c = _client(tmp_path, QC_API_TOKEN="server-token")
    token, client_id, _ = _paired_scoped(c, ["index.file"])
    key = {"X-API-Key": token}
    # Its scope: the re-index queue, read and write (a POST with no path is a 400, not 403).
    assert c.get("/api/index/file", headers=key).status_code == 200
    assert c.post("/api/index/file", json={"paths": []}, headers=key).status_code == 400
    # What every client may do: check itself, see the server is up.
    assert c.get("/api/clients/check", headers=key).json() == {"client_id": client_id}
    assert c.get("/api/health", headers=key).status_code == 200
    # Anything else is 403, naming what it may do.
    for method, path in [("GET", "/api/clips"), ("GET", "/api/config"),
                         ("GET", "/api/items/transcript"), ("DELETE", "/api/bookmarks"),
                         ("GET", "/api/clients"), ("GET", "/api/pair"),
                         ("DELETE", f"/api/clients/{client_id}")]:
        r = c.request(method, path, headers=key)
        assert r.status_code == 403, (method, path, r.text)
        assert "may only queue subtitle re-indexes" in r.json()["detail"]
    # Other tokens are untouched: the server's own, and a full client token.
    full, _ = _paired(c)
    assert c.get("/api/clips", headers={"X-API-Key": full}).status_code == 200
    assert c.get("/api/clips", headers={"X-API-Key": "server-token"}).status_code == 200
    # It disconnects itself like any client.
    assert c.delete("/api/clients/self", headers=key).json() == {"removed": True}
    assert c.get("/api/index/file", headers=key).status_code == 401


def test_a_transcripts_read_token_reads_transcripts_only(tmp_path):
    c = _client(tmp_path)
    token, _, _ = _paired_scoped(c, ["transcripts.read"])
    key = {"Authorization": f"Bearer {token}"}
    # Past the gate (a missing path is FastAPI's 422), not refused by it.
    assert c.get("/api/items/transcript", headers=key).status_code == 422
    assert c.delete("/api/items/transcript", headers=key).status_code == 403
    assert c.get("/api/index/file", headers=key).status_code == 403


def test_a_scoped_basic_password_is_held_to_its_scopes_too(tmp_path):
    c = _client(tmp_path)
    token, _, _ = _paired_scoped(c, ["index.file"])
    auth = {"Authorization": _basic("jellyfin", token)}
    assert c.get("/api/index/file", headers=auth).status_code == 200
    assert c.get("/api/clips", headers=auth).status_code == 403


def test_a_scoped_token_may_poll_and_pair_again(tmp_path):
    c = _client(tmp_path)
    token, _, _ = _paired_scoped(c, ["index.file"])
    key = {"X-API-Key": token}
    again = c.post("/api/pair", json={"name": "Jellyfin", "scopes": ["index.file"]}, headers=key)
    assert again.status_code == 200
    req = again.json()
    assert c.get(f"/api/pair/{req['id']}", headers={**key, "X-Pair-Secret": req["poll_secret"]}
                 ).json() == {"status": "pending"}


@pytest.mark.parametrize("stored", ["index.file", {"x": 1}, ["clips.delete"]])
def test_a_damaged_scopes_entry_opens_nothing(tmp_path, stored):
    """A hand-edited clients.json whose scopes aren't a list of known names fails closed."""
    c = _client(tmp_path)
    token, _, _ = _paired_scoped(c, ["index.file"])
    path = tmp_path / "state" / "clients.json"
    data = json.loads(path.read_text())
    data["clients"][0]["scopes"] = stored
    path.write_text(json.dumps(data))
    key = {"X-API-Key": token}
    assert c.get("/api/index/file", headers=key).status_code == 403
    assert c.get("/api/clips", headers=key).status_code == 403
    assert c.get("/api/clients/check", headers=key).status_code == 200


def test_scope_allows_matches_whole_routes_only():
    allows = pairing.scope_allows
    assert allows(None, "GET", "/api/clips")
    assert allows(("index.file",), "post", "/api/index/file")
    assert not allows(("index.file",), "POST", "/api/index/file/x")
    assert not allows(("index.file",), "DELETE", "/api/index/file")
    assert not allows(("index.file",), "GET", "/api/index/changes")
    assert allows(("index.file",), "GET", "/api/pair/abc")
    assert not allows(("index.file",), "GET", "/api/pair/abc/approve")
    assert not allows(("index.file",), "POST", "/api/pair/abc/approve")
    assert not allows((), "GET", "/api/index/file")
