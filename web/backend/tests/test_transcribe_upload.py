"""POST /api/transcribe (M21): audio an app sends, heard with word timings, nothing kept.

whisper.cpp is a fake that writes the -ojf JSON it would; the audio is a tiny WAV made
here sample by sample. Needs no web/frontend."""

from __future__ import annotations

import io
import json
import math
import os
import shutil
import struct
import sys
import textwrap
import time
import wave
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from quipclipper import cli
from quipclipper.client import Response
from quipclipper_web import app as web
from quipclipper_web import pairing
from quipclipper_web.app import create_app
from quipclipper_web.config import Settings

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")

# Hears "Hailing frequencies open." 0.5-1.5 s into whatever it is given, with tokens, and a
# "[Music]" it should drop. FAKE_WHISPER_SLEEP makes it slow, to cancel it.
FAKE_WHISPER = textwrap.dedent('''\
    #!{python}
    import json, os, sys, time
    a = sys.argv[1:]
    time.sleep(float(os.environ.get("FAKE_WHISPER_SLEEP") or 0))
    t = lambda s, f, e: {{"text": s, "offsets": {{"from": f, "to": e}}}}
    segs = [{{"offsets": {{"from": 500, "to": 1500}}, "text": " Hailing frequencies open.",
              "tokens": [t(" Hail", 500, 700), t("ing", 700, 900),
                         t(" frequencies", 900, 1300), t(" open", 1300, 1450),
                         t(".", 1450, 1500)]}},
            {{"offsets": {{"from": 1500, "to": 2000}}, "text": " [Music]", "tokens": []}}]
    json.dump({{"result": {{"language": "en"}}, "transcription": segs}},
              open(a[a.index("-of") + 1] + ".json", "w"))
''')

WORDS = [{"start": 0.5, "end": 0.9, "text": "Hailing"},
         {"start": 0.9, "end": 1.3, "text": "frequencies"},
         {"start": 1.3, "end": 1.5, "text": "open."}]


def wav_bytes(seconds: float, rate: int = 22050) -> bytes:
    """A stereo 440 Hz tone as a WAV file, the same bytes every time."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(
            struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / rate))) * 2
            for i in range(int(seconds * rate))))
    return buf.getvalue()


@pytest.fixture
def env(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    exe = bindir / "whisper-cli"
    exe.write_text(FAKE_WHISPER.format(python=sys.executable))
    exe.chmod(0o755)
    model = tmp_path / "ggml-small.en.bin"
    model.write_bytes(b"model")
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(sys.path))  # the idle launcher
    for d in ("media", "clips"):
        (tmp_path / d).mkdir()
    return {
        "QC_MEDIA_ROOTS": str(tmp_path / "media"), "QC_STATE_DIR": str(tmp_path / "state"),
        "QC_CLIPS_DIR": str(tmp_path / "clips"),
        "QC_WHISPER_BIN": str(exe), "QC_WHISPER_MODEL": str(model),
    }


def _client(env: dict) -> TestClient:
    c = TestClient(create_app(Settings.from_env(env)))
    c.headers["X-Quipclipper"] = "1"
    return c


def _uploads(env: dict) -> list[str]:
    d = Path(env["QC_STATE_DIR"]) / "transcribe-uploads"
    return sorted(p.name for p in d.iterdir()) if d.exists() else []


def _wait(c: TestClient, job_id: str, *, until=("done", "failed", "cancelled"),
          timeout: float = 60) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        job = c.get(f"/api/jobs/{job_id}").json()
        if job["status"] in until:
            return job
        assert time.monotonic() < deadline, f"job {job_id} still {job['status']}"
        time.sleep(0.05)


def _wait_gone(env: dict, timeout: float = 10) -> None:
    """The job's cleanup runs on its worker just after the job says it's over."""
    deadline = time.monotonic() + timeout
    while _uploads(env):
        assert time.monotonic() < deadline, _uploads(env)
        time.sleep(0.05)


# --- the happy path ------------------------------------------------------------------


@needs_ffmpeg
def test_upload_is_heard_with_word_timings_and_nothing_is_kept(env):
    c = _client(env)
    r = c.post("/api/transcribe", content=wav_bytes(3.0),
               headers={"Content-Type": "audio/wav"})
    assert r.status_code == 200, r.text
    assert r.json()["duration"] == 3.0
    job = _wait(c, r.json()["job_id"])
    assert job["status"] == "done", job
    assert job["result"] == {
        "cues": [{"start": 0.5, "end": 1.5, "text": "Hailing frequencies open.",
                  "machine": True, "words": WORDS}],
        "cues_source": {"kind": "machine", "engine": "whisper.cpp", "model": "small.en",
                        "lang": "en"},
        "duration": 3.0}
    assert "small.en" in job["label"] and "files" not in job
    _wait_gone(env)
    # Nothing landed in the library or the clips, and no transcript was stored.
    assert not any(Path(env["QC_MEDIA_ROOTS"]).iterdir())
    assert not any(Path(env["QC_CLIPS_DIR"]).iterdir())
    assert not (Path(env["QC_STATE_DIR"]) / "transcripts").exists() or \
        not any((Path(env["QC_STATE_DIR"]) / "transcripts").iterdir())


@needs_ffmpeg
def test_streamed_upload_without_a_length_and_query_params(env):
    c = _client(env)
    data = wav_bytes(1.0)
    chunks = (data[i:i + 4096] for i in range(0, len(data), 4096))
    r = c.post("/api/transcribe", content=chunks, params={"lang": "en", "model": "small.en"})
    assert r.status_code == 200, r.text
    assert _wait(c, r.json()["job_id"])["result"]["cues_source"]["lang"] == "en"


# --- health and config -----------------------------------------------------------------


def test_health_and_config_say_what_an_upload_may_be(env):
    c = _client(env)
    caps = {"max_bytes": 64 * 1024 * 1024, "max_seconds": 300.0}
    assert c.get("/api/health").json()["transcribe"]["upload"] == caps
    assert c.get("/api/config").json()["transcribe"]["upload"] == caps
    small = _client({**env, "QC_TRANSCRIBE_UPLOAD_MAX_MB": "2",
                     "QC_TRANSCRIBE_UPLOAD_MAX_SECONDS": "60"})
    assert small.get("/api/health").json()["transcribe"]["upload"] == {
        "max_bytes": 2 * 1024 * 1024, "max_seconds": 60.0}
    off = _client({**env, "QC_TRANSCRIBE_UPLOAD_MAX_MB": "0"})
    assert off.get("/api/health").json()["transcribe"]["upload"] is False
    assert off.get("/api/config").json()["transcribe"]["upload"] is False
    # Forced on without whisper: nothing to hear it with.
    broken = _client({**env, "QC_TRANSCRIBE": "1", "QC_WHISPER_BIN": "/nowhere/whisper-cli"})
    assert broken.get("/api/health").json()["transcribe"]["upload"] is False
    # Off: no transcribe block at all (D-053).
    assert "transcribe" not in _client({**env, "QC_TRANSCRIBE": "0"}).get("/api/health").json()


# --- refusals ---------------------------------------------------------------------------


def test_off_or_not_set_up_is_503(env):
    for extra, words in (({"QC_TRANSCRIBE": "0"}, "off"),
                         ({"QC_TRANSCRIBE": "1", "QC_WHISPER_BIN": "/nowhere/whisper-cli"},
                          "whisper.cpp"),
                         ({"QC_TRANSCRIBE_UPLOAD_MAX_SECONDS": "0"}, "uploaded audio")):
        r = _client({**env, **extra}).post("/api/transcribe", content=b"RIFF")
        assert r.status_code == 503 and words in r.json()["detail"], (extra, r.text)
    assert _uploads(env) == []


def test_bad_model_or_language_is_422(env):
    c = _client(env)
    r = c.post("/api/transcribe", content=b"x", params={"model": "large-v3"})
    assert r.status_code == 422 and "large-v3" in r.json()["detail"]
    r = c.post("/api/transcribe", content=b"x", params={"lang": "de"})
    assert r.status_code == 422 and "English only" in r.json()["detail"]
    assert c.post("/api/transcribe", content=b"x",
                  params={"lang": "x" * 20}).status_code == 422


@needs_ffmpeg
def test_unreadable_or_empty_body_is_400(env):
    c = _client(env)
    r = c.post("/api/transcribe", content=b"this is not audio " * 100)
    assert r.status_code == 400 and "ffmpeg" in r.json()["detail"]
    r = c.post("/api/transcribe", content=b"")
    assert r.status_code == 400 and "no audio" in r.json()["detail"]
    assert _uploads(env) == []


def test_too_many_bytes_is_413_whatever_the_length_header_says(env):
    c = _client({**env, "QC_TRANSCRIBE_UPLOAD_MAX_MB": str(64 / 1024)})  # 64 KB
    big = b"\0" * (100 * 1024)
    # Said up front: refused before reading.
    r = c.post("/api/transcribe", content=big)
    assert r.status_code == 413 and "MB" in r.json()["detail"]
    # Streamed with no length: cut off while reading.
    r = c.post("/api/transcribe", content=(big[i:i + 8192] for i in range(0, len(big), 8192)))
    assert r.status_code == 413
    assert _uploads(env) == []


@needs_ffmpeg
def test_too_long_is_413(env):
    c = _client({**env, "QC_TRANSCRIBE_UPLOAD_MAX_SECONDS": "1"})
    r = c.post("/api/transcribe", content=wav_bytes(2.0))
    assert r.status_code == 413 and "longer than 1 s" in r.json()["detail"]
    assert _uploads(env) == []


# --- cancel, queueing, cleanup ------------------------------------------------------------


@needs_ffmpeg
def test_cancel_a_running_upload_removes_its_files(env, monkeypatch):
    monkeypatch.setenv("FAKE_WHISPER_SLEEP", "30")
    c = _client(env)
    job_id = c.post("/api/transcribe", content=wav_bytes(1.0)).json()["job_id"]
    _wait(c, job_id, until=("running",))
    assert len(_uploads(env)) == 1
    assert c.delete(f"/api/jobs/{job_id}").json() == {"cancelling": job_id}
    assert _wait(c, job_id, timeout=20)["status"] == "cancelled"
    _wait_gone(env)


@needs_ffmpeg
def test_queued_upload_cancelled_before_it_starts_and_the_cap_on_waiting_ones(env, monkeypatch):
    monkeypatch.setattr(web, "UPLOADS_MAX", 2)
    monkeypatch.setenv("FAKE_WHISPER_SLEEP", "30")
    c = _client(env)
    first = c.post("/api/transcribe", content=wav_bytes(1.0)).json()["job_id"]
    _wait(c, first, until=("running",))
    second = c.post("/api/transcribe", content=wav_bytes(1.0)).json()["job_id"]
    assert c.get(f"/api/jobs/{second}").json()["status"] == "queued"  # one at a time
    r = c.post("/api/transcribe", content=wav_bytes(1.0))
    assert r.status_code == 429 and "2 uploads" in r.json()["detail"]
    assert len(_uploads(env)) == 2
    assert c.delete(f"/api/jobs/{second}").json() == {"cancelled": second}
    assert len(_uploads(env)) == 1  # removed at once: it never ran
    c.delete(f"/api/jobs/{first}")
    _wait(c, first, timeout=20)
    _wait_gone(env)


def test_leftovers_are_swept_at_start(env):
    left = Path(env["QC_STATE_DIR"]) / "transcribe-uploads" / "up-crashed"
    left.mkdir(parents=True)
    (left / "upload").write_bytes(b"audio")
    with _client(env):
        assert _uploads(env) == []


# --- a paired app's token -------------------------------------------------------------------


@needs_ffmpeg
def test_a_transcribe_scoped_app_sees_only_its_own_jobs(env):
    store = pairing.ClientStore(Path(env["QC_STATE_DIR"]))
    Path(env["QC_STATE_DIR"]).mkdir(parents=True, exist_ok=True)
    _, token = store.add("LeadStinger", "laptop", scopes=("transcribe",))
    _, other = store.add("Plugin", "jf", scopes=("index.file",))
    c = TestClient(create_app(Settings.from_env(env)))
    app = {"X-API-Key": token}
    r = c.post("/api/transcribe", content=wav_bytes(2.0), headers=app)
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    job = _wait_as(c, job_id, app)
    assert job["status"] == "done" and "from LeadStinger" in job["label"]
    assert job["result"]["cues"][0]["words"] == WORDS
    # Someone else's job is not there for it; the job list and other routes are closed.
    person = c.post("/api/transcribe", content=wav_bytes(1.0),
                    headers={"X-Quipclipper": "1"}).json()["job_id"]
    assert c.get(f"/api/jobs/{person}", headers=app).status_code == 404
    assert c.delete(f"/api/jobs/{person}", headers=app).status_code == 404
    assert c.get("/api/jobs", headers=app).status_code == 403
    assert c.get("/api/library/roots", headers=app).status_code == 403
    # A token without the scope can't upload.
    r = c.post("/api/transcribe", content=wav_bytes(1.0), headers={"X-API-Key": other})
    assert r.status_code == 403 and "scopes" in r.json()["detail"]
    _wait(c, person)


def _wait_as(c, job_id, headers, timeout=60):
    deadline = time.monotonic() + timeout
    while True:
        job = c.get(f"/api/jobs/{job_id}", headers=headers).json()
        if job["status"] in ("done", "failed", "cancelled"):
            return job
        assert time.monotonic() < deadline
        time.sleep(0.05)


# --- the CLI: transcribe.file, against the app and run here --------------------------------


class AppTransport:
    def __init__(self, client: TestClient):
        self.client = client
        self.bodies: list = []

    def request(self, method, path, query, body, stream=False):
        self.bodies.append(body)
        kw = {"content": body} if isinstance(body, bytes) else {"json": body}
        r = self.client.request(method, path, params=query, headers={"X-Quipclipper": "1"},
                                **kw)
        return Response(r.status_code, {k.lower(): v for k, v in r.headers.items()}, r.content)


@needs_ffmpeg
def test_cli_sends_the_file_and_waits(env, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    sample = tmp_path / "sample.wav"
    sample.write_bytes(wav_bytes(2.0))
    t = AppTransport(_client(env))
    code = cli.main(["--json", "transcribe.file", str(sample), "--lang", "en"], transport=t)
    assert code == 0
    job = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert job["status"] == "done" and job["result"]["cues"][0]["words"] == WORDS
    assert t.bodies[0] == sample.read_bytes()


@needs_ffmpeg
def test_cli_runs_it_here_without_a_server(env, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    for k in ("QC_URL", "QC_API_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("QC_WHISPER_BIN", env["QC_WHISPER_BIN"])
    monkeypatch.setenv("QC_WHISPER_MODEL", env["QC_WHISPER_MODEL"])
    sample = tmp_path / "sample.wav"
    sample.write_bytes(wav_bytes(2.0))
    assert cli.main(["--json", "transcribe.file", str(sample)]) == 0
    got = json.loads(capsys.readouterr().out)
    assert got["cues"][0]["words"] == WORDS and got["duration"] == 2.0
    assert cli.main(["transcribe.file", str(sample)]) == 0
    out = capsys.readouterr().out
    assert "Hailing frequencies open." in out and "Hailing@0.50" in out
    monkeypatch.setenv("QC_TRANSCRIBE_UPLOAD_MAX_SECONDS", "1")
    assert cli.main(["transcribe.file", str(sample)]) != 0
    assert "longer than 1 s" in capsys.readouterr().err
