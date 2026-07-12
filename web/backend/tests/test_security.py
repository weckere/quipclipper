"""Tests for the _security_gate middleware: CSRF header, proxy shared secret,
and API-token auth for programmatic clients."""

from __future__ import annotations

import base64

from fastapi.testclient import TestClient

from quipclipper_web.app import create_app
from quipclipper_web.config import Settings


def _basic(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def _app(env: dict[str, str] | None = None) -> TestClient:
    # A raw client with NO default headers, so we control exactly what's sent.
    return TestClient(create_app(Settings.from_env(env or {})))


# --- CSRF: state-changing requests need the X-Quipclipper header --------------

def test_unsafe_request_without_csrf_header_rejected() -> None:
    client = _app()
    # DELETE is a state-changing method; without the header the middleware 403s
    # before routing (a cross-site page can't set a custom header).
    resp = client.delete("/api/bookmarks")
    assert resp.status_code == 403
    assert "csrf" in resp.json()["detail"].lower()


def test_unsafe_request_with_csrf_header_passes_gate() -> None:
    client = _app()
    resp = client.delete("/api/bookmarks", headers={"X-Quipclipper": "1"})
    # Passes the gate and reaches the handler (clearing bookmarks succeeds).
    assert resp.status_code != 403


def test_safe_request_needs_no_csrf_header() -> None:
    # GET is never gated, even without the header.
    assert _app().get("/api/health").status_code == 200


def test_csrf_header_is_case_insensitive() -> None:
    client = _app()
    resp = client.delete("/api/bookmarks", headers={"x-QuIpClIpPeR": "1"})
    assert resp.status_code != 403


# --- proxy shared secret (S1) -------------------------------------------------

_SECRET = {"QC_PROXY_SECRET": "s3cr3t-token"}


def test_proxy_secret_blocks_requests_without_the_header() -> None:
    client = _app(_SECRET)
    assert client.get("/api/config").status_code == 403


def test_proxy_secret_allows_requests_with_the_matching_header() -> None:
    client = _app(_SECRET)
    resp = client.get("/api/config", headers={"X-Quip-Proxy-Secret": "s3cr3t-token"})
    assert resp.status_code == 200


def test_proxy_secret_rejects_a_wrong_header() -> None:
    client = _app(_SECRET)
    resp = client.get("/api/config", headers={"X-Quip-Proxy-Secret": "wrong"})
    assert resp.status_code == 403


def test_proxy_secret_exempts_health_for_liveness_probes() -> None:
    # Container/orchestrator health checks hit the backend directly, without the
    # secret nginx would inject — /api/health must stay reachable.
    client = _app(_SECRET)
    assert client.get("/api/health").status_code == 200


def test_no_proxy_secret_means_no_enforcement() -> None:
    # The default (unset) leaves the backend open to direct requests — the
    # loopback bind is then the only boundary.
    assert _app().get("/api/config").status_code == 200


def test_proxy_secret_still_requires_csrf_on_unsafe_methods() -> None:
    # Both gates apply: a request carrying the proxy secret but no CSRF header is
    # still rejected on a state-changing method.
    client = _app(_SECRET)
    resp = client.delete("/api/bookmarks", headers={"X-Quip-Proxy-Secret": "s3cr3t-token"})
    assert resp.status_code == 403
    assert "csrf" in resp.json()["detail"].lower()


# --- API token auth for programmatic clients ----------------------------------

_TOKEN = {"QC_API_TOKEN": "tok-abc123"}


def test_api_token_exempts_csrf_via_x_api_key() -> None:
    client = _app(_TOKEN)
    # A state-changing request with a valid token needs no CSRF header.
    resp = client.delete("/api/bookmarks", headers={"X-API-Key": "tok-abc123"})
    assert resp.status_code == 200


def test_api_token_exempts_csrf_via_bearer() -> None:
    client = _app(_TOKEN)
    resp = client.delete("/api/bookmarks", headers={"Authorization": "Bearer tok-abc123"})
    assert resp.status_code == 200


def test_api_token_exempts_csrf_via_basic_password() -> None:
    client = _app(_TOKEN)
    # The token supplied as the HTTP Basic password (username ignored) — one
    # credential (`curl -u api:tok-abc123`) satisfies auth and skips CSRF.
    resp = client.delete("/api/bookmarks", headers={"Authorization": _basic("api", "tok-abc123")})
    assert resp.status_code == 200


def test_token_check_rejects_non_ascii_without_raising() -> None:
    # A latin-1-decoded header (e.g. from nginx) can contain non-ASCII, which
    # hmac.compare_digest rejects with TypeError. _token_ok must swallow it and
    # return False (a mismatch) rather than let it become a 500.
    from quipclipper_web.app import _token_ok
    assert _token_ok("t\xe9k", frozenset({"tok-abc123"})) is False


def test_invalid_x_api_key_rejected() -> None:
    client = _app(_TOKEN)
    resp = client.delete("/api/bookmarks", headers={"X-API-Key": "wrong"})
    assert resp.status_code == 401


def test_invalid_bearer_rejected() -> None:
    client = _app(_TOKEN)
    resp = client.get("/api/config", headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 401


def test_normal_basic_user_is_not_treated_as_a_token_attempt() -> None:
    # A real browser user's Basic password isn't the token, so it's NOT a "bad
    # token" (no 401) — it just falls through to the normal CSRF rules (403 here).
    client = _app(_TOKEN)
    resp = client.delete("/api/bookmarks", headers={"Authorization": _basic("quip", "hunter2")})
    assert resp.status_code == 403
    assert "csrf" in resp.json()["detail"].lower()


def test_api_token_valid_get_passes() -> None:
    client = _app(_TOKEN)
    assert client.get("/api/config", headers={"X-API-Key": "tok-abc123"}).status_code == 200


def test_multiple_tokens_are_all_accepted() -> None:
    client = _app({"QC_API_TOKEN": "old-token, new-token"})
    for tok in ("old-token", "new-token"):
        r = client.delete("/api/bookmarks", headers={"X-API-Key": tok})
        assert r.status_code == 200, tok


def test_no_token_configured_ignores_api_key_header() -> None:
    # With QC_API_TOKEN unset, the X-API-Key header means nothing: an unsafe
    # request still needs the CSRF header.
    client = _app()
    resp = client.delete("/api/bookmarks", headers={"X-API-Key": "anything"})
    assert resp.status_code == 403


def test_proxy_secret_applies_even_to_token_clients() -> None:
    # The proxy secret is a transport gate checked before token auth: a valid API
    # token doesn't exempt a request from it (nginx injects it for real clients).
    client = _app({**_SECRET, **_TOKEN})
    resp = client.get("/api/config", headers={"X-API-Key": "tok-abc123"})
    assert resp.status_code == 403  # missing X-Quip-Proxy-Secret
    ok = client.get(
        "/api/config",
        headers={"X-API-Key": "tok-abc123", "X-Quip-Proxy-Secret": "s3cr3t-token"},
    )
    assert ok.status_code == 200
