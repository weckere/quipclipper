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


def _client(tmp_path: Path, **env: str) -> TestClient:
    clips = tmp_path / "clips"
    clips.mkdir(exist_ok=True)
    base = {"QC_STATE_DIR": str(tmp_path / "state"), "QC_CLIPS_DIR": str(clips)}
    return TestClient(create_app(Settings.from_env({**base, **env})))


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
    assert set(row) == {"id", "name", "host", "code", "address", "created", "expires_at"}
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
    assert re.fullmatch(r"qcc_[A-Za-z0-9_-]{43}", got["token"])
    clients = c.get("/api/clients").json()["clients"]
    assert [(x["id"], x["name"], x["host"]) for x in clients] == [
        (client_id, "LeadStinger", "laptop")]
    assert set(clients[0]) == {"id", "name", "host", "created", "last_used"}


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
        "192.168.1.20", "testclient"]


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
    assert set(row) == {"id", "name", "host", "token_sha256", "created", "last_used"}
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
