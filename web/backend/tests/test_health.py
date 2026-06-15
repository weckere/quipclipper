"""Smoke tests for the Phase 0 endpoints (media-free, no ffmpeg required)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from quipclipper_web.app import create_app
from quipclipper_web.config import Settings


def _client(env: dict[str, str] | None = None) -> TestClient:
    return TestClient(create_app(Settings.from_env(env or {})))


def test_health_ok() -> None:
    resp = _client().get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert set(body["tools"]) == {"ffmpeg", "ffprobe", "mkvmerge"}


def test_config_reflects_env() -> None:
    env = {
        "QC_MEDIA_ROOTS": "/srv/movies:/srv/tv",
        "QC_PASSWORD": "hunter2",
        "QC_JELLYFIN_URL": "http://localhost:8096",
    }
    body = _client(env).get("/api/config").json()
    assert body["media_roots"] == ["/srv/movies", "/srv/tv"]
    assert body["auth_required"] is True
    assert body["jellyfin_enabled"] is True


def test_config_defaults() -> None:
    body = _client().get("/api/config").json()
    assert body["media_roots"] == []
    assert body["auth_required"] is False
    assert body["jellyfin_enabled"] is False
