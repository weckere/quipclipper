"""The web package reports the version in its pyproject.toml (kept equal to the
root one by scripts/release.sh and the engine's tests/test_version.py)."""

from __future__ import annotations

import importlib.metadata as md
import re
from pathlib import Path

from fastapi.testclient import TestClient

import quipclipper_web
from quipclipper_web.app import create_app
from quipclipper_web.config import Settings

BACKEND = Path(__file__).resolve().parents[1]
VERSION = re.search(r'^version = "([^"]+)"', (BACKEND / "pyproject.toml").read_text(), re.M).group(1)


def test_web_version_matches_pyproject() -> None:
    assert quipclipper_web.__version__ == VERSION, (
        f"quipclipper_web.__version__ is {quipclipper_web.__version__}, pyproject.toml "
        f"says {VERSION}; reinstall (pip install -e web/backend)"
    )


def test_web_version_fallback_reads_the_checkout(monkeypatch) -> None:
    def missing(name: str) -> str:
        raise md.PackageNotFoundError(name)

    monkeypatch.setattr(md, "version", missing)
    assert quipclipper_web._version() == VERSION


def test_health_reports_the_version() -> None:
    c = TestClient(create_app(Settings.from_env({})))
    c.headers["X-Quipclipper"] = "1"
    assert c.get("/api/health").json()["version"] == VERSION
