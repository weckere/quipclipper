"""The machine transcription endpoints (M21), against a fake whisper-cli."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from quipclipper_web.app import create_app
from quipclipper_web.config import Settings

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")

# Hears one sung line 2.5-4 s into whatever it's given (and a "[Music]" it should drop);
# FAKE_WHISPER_SLEEP makes it slow, to cancel it.
FAKE_WHISPER = textwrap.dedent('''\
    #!{python}
    import json, os, sys, time
    a = sys.argv[1:]
    time.sleep(float(os.environ.get("FAKE_WHISPER_SLEEP") or 0))
    segs = [{{"offsets": {{"from": 2500, "to": 4000}}, "text": " ♪ Driving way too fast ♪"}},
            {{"offsets": {{"from": 4000, "to": 5000}}, "text": " [Music]"}}]
    json.dump({{"transcription": segs}}, open(a[a.index("-of") + 1] + ".json", "w"))
''')

SRT = "1\n00:00:00,500 --> 00:00:01,500\nWhy won't she be mine?\n"


@pytest.fixture
def env(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    exe = bindir / "whisper-cli"
    exe.write_text(FAKE_WHISPER.format(python=sys.executable))
    exe.chmod(0o755)
    model = tmp_path / "ggml-small.en.bin"
    model.write_bytes(b"model")
    # The idle launcher runs as `python -m quipclipper.idle`: it must import the engine.
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(sys.path))
    root = tmp_path / "media"
    root.mkdir()
    return {
        "QC_MEDIA_ROOTS": str(root), "QC_STATE_DIR": str(tmp_path / "state"),
        "QC_CLIPS_DIR": str(tmp_path / "clips"),
        "QC_WHISPER_BIN": str(exe), "QC_WHISPER_MODEL": str(model),
    }


def _client(env: dict) -> TestClient:
    c = TestClient(create_app(Settings.from_env(env)))
    c.headers["X-Quipclipper"] = "1"
    return c


def _tone(path: Path, seconds: float = 12.0) -> Path:
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                    f"sine=frequency=440:duration={seconds}", "-ar", "48000", "-y", str(path)],
                   check=True)
    return path


def _wait(c: TestClient, job_id: str, timeout: float = 60) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = c.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "failed", "cancelled"):
            return job
        time.sleep(0.1)
    raise AssertionError(f"job {job_id} still {job['status']}")


@pytest.fixture
def song(env):
    root = Path(env["QC_MEDIA_ROOTS"])
    media = _tone(root / "song.flac")
    (root / "song.en.srt").write_text(SRT)
    return media


@needs_ffmpeg
def test_transcribe_gaps_then_script_search_and_captions(env, song):
    c = _client(env)
    assert c.get("/api/health").json()["transcribe"]["available"] is True
    assert c.get("/api/config").json()["transcribe"]["default_model"] == "small.en"
    r = c.post("/api/items/transcribe", json={"path": str(song)})
    assert r.status_code == 200, r.text
    job = _wait(c, r.json()["job_id"])
    assert job["status"] == "done", job
    assert job["result"]["mode"] == "gaps" and job["result"]["transcribed"] == [[1.5, 12.0]]
    assert job["result"]["new_lines"] >= 1
    assert "small.en" in job["label"]

    script = c.get("/api/items/subtitles", params={"path": str(song), "fmt": "json"}).json()
    assert script[0] == {"start": 0.5, "end": 1.5, "text": "Why won't she be mine?",
                         "speaker": None, "source": None}
    assert {"♪ Driving way too fast ♪"} == {x["text"] for x in script if x["source"] == "machine"}
    only = c.get("/api/items/subtitles", params={"path": str(song), "fmt": "json",
                                                 "machine": "only"}).json()
    assert only and all(x["source"] == "machine" for x in only)
    # Not in the player's captions until it's copied to the library (D-037).
    vtt = c.get("/api/items/subtitles", params={"path": str(song)}).text
    assert "way too fast" not in vtt and "Why won't she be mine" in vtt

    hits = c.get("/api/search", params={"path": str(song), "query": "way too fast"}).json()
    assert hits["matches"][0]["machine"] is True and hits["matches"][0]["score"] == 100
    none = c.get("/api/search", params={"path": str(song), "query": "way too fast",
                                        "machine": "exclude"}).json()
    assert all(not m["machine"] for m in none["matches"])
    folder = c.get("/api/search/folder", params={"path": str(song.parent),
                                                 "query": "way too fast"}).json()
    assert folder["matches"][0]["machine"] is True

    info = c.get("/api/items", params={"path": str(song)}).json()["transcript"]
    assert info["stored"]["models"] == ["small.en"] and info["library"] == []
    assert info["sidecar_name"] == "song.en.whisper.srt" and info["writable"] is True

    # A second request for the same gaps has nothing new to do.
    again = _wait(c, c.post("/api/items/transcribe", json={"path": str(song)}).json()["job_id"])
    assert again["result"]["note"] == "Nothing new to transcribe."


@needs_ffmpeg
def test_copy_to_library_puts_machine_lines_in_the_captions(env, song):
    c = _client(env)
    assert c.post("/api/items/transcript/library", params={"path": str(song)}).status_code == 404
    _wait(c, c.post("/api/items/transcribe", json={"path": str(song), "mode": "range",
                                                   "start": 1, "end": 6}).json()["job_id"])
    r = c.post("/api/items/transcript/library", params={"path": str(song)})
    assert r.status_code == 200, r.text
    assert r.json()["sidecar"] == "song.en.whisper.srt"
    side = song.with_name("song.en.whisper.srt")
    assert "Driving way too fast" in side.read_text()
    vtt = c.get("/api/items/subtitles", params={"path": str(song)}).text
    assert "<c.machine>≈ ♪ Driving way too fast ♪</c>" in vtt
    # The library copy is not the file's subtitles: the real sidecar still is.
    assert c.get("/api/items", params={"path": str(song)}).json()["transcript"]["library"] == [
        "song.en.whisper.srt"]
    srt = c.get("/api/items/transcript", params={"path": str(song), "fmt": "srt"})
    assert srt.headers["content-disposition"].endswith("song.en.whisper.srt")
    lines = c.get("/api/items/transcript", params={"path": str(song)}).json()["lines"]
    assert lines and all(x["source"] == "machine" for x in lines)

    assert c.delete("/api/items/transcript", params={"path": str(song)}).json()["deleted"] is True
    # The library copy still answers searches once the stored transcript is gone.
    hits = c.get("/api/search", params={"path": str(song), "query": "way too fast"}).json()
    assert hits["matches"][0]["machine"] is True
    gone = c.delete("/api/items/transcript/library", params={"path": str(song)}).json()
    assert gone["removed"] == ["song.en.whisper.srt"] and not side.exists()


@needs_ffmpeg
def test_a_file_with_no_subtitles_becomes_searchable(env):
    root = Path(env["QC_MEDIA_ROOTS"])
    media = _tone(root / "talk.flac")
    c = _client(env)
    assert c.get("/api/search", params={"path": str(media), "query": "x"}).status_code == 404
    job = _wait(c, c.post("/api/items/transcribe", json={"path": str(media), "mode": "file"}
                          ).json()["job_id"])
    assert job["result"]["transcribed"] == [[0.0, 12.0]]
    hits = c.get("/api/search", params={"path": str(media), "query": "way too fast"}).json()
    assert hits["matches"][0]["machine"] is True


@needs_ffmpeg
def test_copy_refuses_a_read_only_folder(env, song):
    c = _client(env)
    _wait(c, c.post("/api/items/transcribe", json={"path": str(song)}).json()["job_id"])
    song.parent.chmod(0o555)
    try:
        if os.access(song.parent, os.W_OK):
            pytest.skip("running as a user who can write anywhere")
        r = c.post("/api/items/transcript/library", params={"path": str(song)})
        assert r.status_code == 403 and "writableMediaRoots" in r.json()["detail"]
    finally:
        song.parent.chmod(0o755)


@needs_ffmpeg
def test_a_running_transcription_cancels_and_a_repeat_joins_it(env, song, monkeypatch):
    monkeypatch.setenv("FAKE_WHISPER_SLEEP", "30")
    c = _client(env)
    body = {"path": str(song), "mode": "file"}
    first = c.post("/api/items/transcribe", json=body).json()["job_id"]
    assert c.post("/api/items/transcribe", json=body).json() == {"job_id": first,
                                                                 "existing": True}
    deadline = time.monotonic() + 20
    while c.get(f"/api/jobs/{first}").json()["status"] != "running":
        assert time.monotonic() < deadline
        time.sleep(0.05)
    assert c.delete(f"/api/jobs/{first}").json() == {"cancelling": first}
    assert _wait(c, first, timeout=20)["status"] == "cancelled"
    assert any(j["id"] == first for j in c.get("/api/jobs").json()["jobs"])


def test_requests_that_cannot_run(env, tmp_path):
    root = Path(env["QC_MEDIA_ROOTS"])
    (root / "a.mkv").write_bytes(b"")
    c = _client(env)
    bad_range = c.post("/api/items/transcribe", json={"path": str(root / "a.mkv"),
                                                      "mode": "range", "start": 5, "end": 2})
    assert bad_range.status_code == 422
    no_model = c.post("/api/items/transcribe", json={"path": str(root / "a.mkv"),
                                                     "model": "medium.en"})
    assert no_model.status_code == 422 and "medium.en" in no_model.json()["detail"]
    assert c.post("/api/items/transcribe", json={"path": "yt:dQw4w9WgXcQ"}).status_code == 422
    assert c.post("/api/items/transcribe", json={"path": "/etc/passwd"}).status_code == 403
    off = _client({k: v for k, v in env.items() if not k.startswith("QC_WHISPER")})
    r = off.post("/api/items/transcribe", json={"path": str(root / "a.mkv")})
    assert r.status_code == 503 and "whisper.cpp" in r.json()["detail"]
    assert off.get("/api/health").json()["transcribe"]["available"] is False
