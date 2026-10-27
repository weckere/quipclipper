"""Check for updates over the API (M24): /api/update, its check and the daily setting.

GitHub is never asked: ``quipclipper.updates.fetch_latest`` is replaced in every test.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from quipclipper import updates as upd
from quipclipper_web import __version__
from quipclipper_web.app import create_app
from quipclipper_web.config import Settings

SHA = "2da1479c0ffee0123456789abcdef0123456789a"


def _release(version):
    return {"version": version, "tag": f"v{version}",
            "url": f"https://github.com/weckere/quipclipper/releases/tag/v{version}",
            "name": None, "published_at": None}


@pytest.fixture
def github(monkeypatch):
    """What GitHub answers: a version string, or a CheckError to raise."""
    answer = {"latest": "99.0.0", "calls": 0}

    def fake(version, **kw):
        answer["calls"] += 1
        got = answer["latest"]
        if isinstance(got, Exception):
            raise got
        return _release(got)

    monkeypatch.setattr(upd, "fetch_latest", fake)
    # A dev build's base tag: the commit a test names, else GitHub has no such tag.
    monkeypatch.setattr(upd, "fetch_tag_commit", lambda version, tag, **kw: answer.get("tag"))
    return answer


def _client(tmp_path, **env) -> TestClient:
    settings = Settings.from_env({"QC_STATE_DIR": str(tmp_path / "state"), **env})
    c = TestClient(create_app(settings))
    c.headers["X-Quipclipper"] = "1"
    return c


def test_status_before_any_check_asks_nobody(tmp_path, github):
    s = _client(tmp_path).get("/api/update").json()
    assert s["state"] == "unknown" and s["latest"] is None
    assert s["current"] == {"version": __version__, "commit": None, "short": None,
                            "dev": False, "install": "pip", "label": __version__}
    assert s["daily"] is True and s["daily_source"] == "default"
    assert s["how_to_update"]["install"] == "pip"
    assert len(s["how_to_update"]["commands"]) == 2  # the engine and the web backend
    assert github["calls"] == 0


def test_check_finds_a_newer_release(tmp_path, github):
    c = _client(tmp_path, QC_INSTALL="docker")
    s = c.post("/api/update/check").json()
    assert s["update_available"] is True and s["latest"]["version"] == "99.0.0"
    assert s["how_to_update"]["commands"] == ["docker compose pull", "docker compose up -d"]
    assert c.get("/api/update").json()["latest"]["version"] == "99.0.0"  # remembered
    assert github["calls"] == 1


def test_a_dev_build_on_its_base_release_is_up_to_date(tmp_path, github):
    github["latest"] = __version__
    c = _client(tmp_path, QC_BUILD_COMMIT=SHA, QC_INSTALL="nixos")
    s = c.post("/api/update/check").json()
    assert s["state"] == "up_to_date"
    assert s["current"]["label"] == f"dev build 2da1479, ahead of {__version__}"
    h = c.get("/api/health").json()
    assert (h["commit"], h["dev"], h["build"], h["install"]) == (
        SHA, True, s["current"]["label"], "nixos")


def test_a_nix_build_on_its_release_commit_reads_as_the_release(tmp_path, github):
    github.update(latest=__version__, tag=SHA)
    c = _client(tmp_path, QC_BUILD_COMMIT=SHA, QC_INSTALL="nixos")
    assert c.get("/api/health").json()["dev"] is True  # not checked yet
    c.post("/api/update/check")
    h = c.get("/api/health").json()
    assert (h["commit"], h["dev"], h["build"]) == (SHA, False, __version__)


def test_a_release_build_in_health(tmp_path):
    h = _client(tmp_path).get("/api/health").json()
    assert (h["commit"], h["dev"], h["build"]) == (None, False, __version__)


@pytest.mark.parametrize("error,kind", [
    (upd.CheckError("couldn't reach GitHub (timed out)"), "unreachable"),
    (upd.CheckError("rate limit", kind="rate_limited", retry_at=4_000_000_000.0),
     "rate_limited"),
])
def test_github_down_or_rate_limited_is_an_answer_not_a_failure(tmp_path, github, error, kind):
    c = _client(tmp_path)
    c.post("/api/update/check")
    github["latest"] = error
    resp = c.post("/api/update/check")
    assert resp.status_code == 200
    s = resp.json()
    assert s["error"]["kind"] == kind
    assert s["latest"]["version"] == "99.0.0" and s["checked_at"]  # the last good answer


def test_the_daily_setting_wins_over_qc_update_check(tmp_path, github):
    c = _client(tmp_path, QC_UPDATE_CHECK="0")
    s = c.get("/api/update").json()
    assert (s["daily"], s["daily_source"], s["next_check"]) == (False, "default", None)
    s = c.put("/api/update/settings", json={"daily": True}).json()
    assert (s["daily"], s["daily_source"]) == (True, "setting") and s["next_check"]
    # A restart keeps it, whatever QC_UPDATE_CHECK says.
    assert _client(tmp_path, QC_UPDATE_CHECK="0").get("/api/update").json()["daily"] is True
    assert c.put("/api/update/settings", json={}).status_code == 422
    assert github["calls"] == 0  # turning it on doesn't check by itself


def test_check_needs_the_csrf_header(tmp_path, github):
    c = _client(tmp_path)
    del c.headers["X-Quipclipper"]
    assert c.post("/api/update/check").status_code == 403
    assert c.put("/api/update/settings", json={"daily": False}).status_code == 403
    assert github["calls"] == 0


def test_the_daily_thread_starts_and_stops_with_the_app(tmp_path, github):
    with _client(tmp_path) as c:  # the lifespan runs: the thread waits FIRST_DELAY first
        assert c.get("/api/update").json()["next_check"]
    assert github["calls"] == 0


def test_round_trip_through_the_cli(tmp_path, github, capsys):
    """update.status, update.check and update.settings drive the server, not this machine."""
    import json

    from quipclipper import cli
    from test_cli_parity import TOKEN, AppTransport

    transport = AppTransport(_client(tmp_path, QC_API_TOKEN=TOKEN))

    def run(*argv):
        code = cli.main(["--json", *argv], transport=transport)
        return code, json.loads(capsys.readouterr().out.strip().splitlines()[-1])

    code, s = run("update.check")
    assert code == 0 and s["update_available"] and "where" not in s
    code, s = run("update.settings", "--no-daily")
    assert code == 0 and s["daily"] is False
    code, s = run("update.status")
    assert code == 0 and s["daily"] is False and s["latest"]["version"] == "99.0.0"
    github["latest"] = upd.CheckError("couldn't reach GitHub (offline)")
    code, s = run("update.check")
    assert code == 1 and s["error"]["kind"] == "unreachable"
    assert ("PUT", "/api/update/settings") in transport.calls
