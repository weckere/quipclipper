"""contrib/hooks/quipclipper-reindex (M20) against a real server: POSIX sh + curl,
the way Bazarr, Sonarr and Radarr run it."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from quipclipper_web import sub_cache as sub_cache_mod
from quipclipper_web.app import create_app
from quipclipper_web.config import Settings

SCRIPT = Path(__file__).resolve().parents[3] / "contrib" / "hooks" / "quipclipper-reindex"
SH = shutil.which("dash") or shutil.which("sh")
pytestmark = pytest.mark.skipif(
    not SCRIPT.exists() or not shutil.which("curl") or not SH,
    reason="needs contrib/hooks (not in the Nix build's source), curl and sh")
HOOK = "hook-secret-123"
SRT = "1\n00:00:01,000 --> 00:00:03,000\nMake it so.\n"


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setattr(sub_cache_mod, "list_embedded_tracks", lambda v: [])
    monkeypatch.setattr("quipclipper.subtitles.list_embedded_tracks", lambda v: [])
    media = tmp_path / "shows"
    (media / "Show").mkdir(parents=True)
    (media / "Show" / "Show S01E01.mkv").write_bytes(b"")
    (media / "Show" / "Show S01E01.en.srt").write_text(SRT)
    app = create_app(Settings.from_env({
        "QC_MEDIA_ROOTS": str(media), "QC_STATE_DIR": str(tmp_path / "state"),
        "QC_HOOK_TOKEN": HOOK, "QC_PATH_MAP": f"/tv={media}"}))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    end = time.monotonic() + 10
    while not srv.started:
        assert time.monotonic() < end
        time.sleep(0.02)
    token = tmp_path / "token"
    token.write_text(HOOK + "\n")
    yield f"http://127.0.0.1:{port}", token, media
    srv.should_exit = True
    t.join(5)


def _run(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([SH, str(SCRIPT), *args], capture_output=True, text=True,
                          timeout=60, env={"PATH": os.environ["PATH"], **(env or {})})


def test_bazarr_style_call(server):
    url, token, media = server
    # Bazarr: --url/--token-file, then "{{episode}}" "{{subtitles}}" in its own paths.
    r = _run("--url", url, "--token-file", str(token),
             "/tv/Show/Show S01E01.mkv", "/tv/Show/Show S01E01.en.srt")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "quipclipper-reindex: queued 2 file(s) for re-indexing"
    assert r.stderr == "" and HOOK not in r.stdout


def test_sonarr_and_radarr_custom_script_events(server):
    url, token, _ = server
    env = {"QUIPCLIPPER_URL": url, "QUIPCLIPPER_TOKEN_FILE": str(token)}
    ok = _run(env={**env, "sonarr_eventtype": "Test"})
    assert ok.returncode == 0 and "accepts the token" in ok.stdout
    r = _run(env={**env, "sonarr_eventtype": "Download",
                  "sonarr_episodefile_path": "/tv/Show/Show S01E01.mkv"})
    assert r.returncode == 0 and "queued 1" in r.stdout, r.stderr
    r = _run(env={**env, "radarr_eventtype": "Rename",
                  "radarr_moviefile_paths": "/tv/Show/Show S01E01.mkv|/tv/Show/Show S01E01.en.srt"})
    assert r.returncode == 0 and "queued 2" in r.stdout, r.stderr
    grab = _run(env={**env, "sonarr_eventtype": "Grab"})
    assert grab.returncode == 0 and "nothing to re-index" in grab.stdout


def test_failures_say_why_on_stderr(server):
    url, token, tmp = server
    bad = token.with_name("bad")
    bad.write_text("wrong")
    r = _run("--url", url, "--token-file", str(bad), "/tv/Show/Show S01E01.mkv")
    assert r.returncode == 1 and "HTTP 401" in r.stderr and "wrong" not in r.stderr
    t = _run("--url", url, "--token-file", str(bad), env={"sonarr_eventtype": "Test"})
    assert t.returncode == 1 and "HTTP 401" in t.stderr
    r = _run("--url", url, "--token-file", str(token), "/movies/Film.mkv")
    assert r.returncode == 1
    assert "HTTP 422" in r.stderr and "outside_roots: /movies/Film.mkv" in r.stderr
    assert _run("/tv/x.mkv").returncode == 2  # no URL
    assert _run("--url", url, "/tv/x.mkv").returncode == 2  # no token
