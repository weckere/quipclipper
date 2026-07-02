"""Tests for the _security_gate middleware: CSRF header + proxy shared secret."""

from __future__ import annotations

from fastapi.testclient import TestClient

from quipclipper_web.app import create_app
from quipclipper_web.config import Settings


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
