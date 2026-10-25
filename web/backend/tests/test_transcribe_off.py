"""Machine transcription off (D-053): on by itself where whisper-cli and a model are
installed, off elsewhere, and QC_TRANSCRIBE=0 / =1 forces it. Off, nothing transcribes and
stored transcripts stay out of every answer, even on a server with whisper.cpp and a model."""

from __future__ import annotations

from pathlib import Path

from test_transcribe_api import _client, _tone, _wait, env, needs_ffmpeg, song  # noqa: F401

from quipclipper import transcribe as stt


def _off(env: dict) -> dict:
    return {**env, "QC_TRANSCRIBE": "0"}


def test_the_switch(env):  # noqa: F811
    installed = {k: env[k] for k in ("QC_WHISPER_BIN", "QC_WHISPER_MODEL")}
    assert stt.switch({}) is None and stt.switch({"QC_TRANSCRIBE": "maybe"}) is None
    assert stt.switch({"QC_TRANSCRIBE": " Off "}) is False
    assert stt.switch({"QC_TRANSCRIBE": " On "}) is True
    # Unset: on exactly where whisper-cli and a model file are.
    assert stt.enabled(installed) and stt.WhisperConfig.from_env(installed).available
    assert not stt.enabled({})
    assert not stt.enabled({**installed, "QC_WHISPER_MODEL": "/nowhere/ggml-small.en.bin"})
    assert not stt.enabled({**installed, "QC_WHISPER_BIN": "/nowhere/whisper-cli"})
    # 0 turns it off where it's installed; 1 forces it on where it isn't (not available).
    for off in ("0", "false", "no", "off"):
        assert not stt.enabled({**installed, "QC_TRANSCRIBE": off})
    forced = stt.WhisperConfig.from_env({"QC_TRANSCRIBE": "yes",
                                         "QC_WHISPER_BIN": "/nowhere/whisper-cli"})
    assert forced.enabled and not forced.available


def test_on_by_itself_where_whisper_is_installed(env):  # noqa: F811
    c = _client(env)
    assert c.get("/api/health").json()["transcribe"]["available"] is True
    assert c.get("/api/config").json()["transcribe"]["available"] is True
    nothing = {k: v for k, v in env.items() if not k.startswith("QC_WHISPER")}
    c = _client({**nothing, "QC_WHISPER_BIN": "/nowhere/whisper-cli"})
    assert "transcribe" not in c.get("/api/config").json()


def test_switched_off_where_whisper_is_installed(env):  # noqa: F811
    c = _client(_off(env))
    health = c.get("/api/health").json()
    assert "transcribe" not in health and "whisper-cli" not in health["tools"]
    assert "transcribe" not in c.get("/api/config").json()
    root = Path(env["QC_MEDIA_ROOTS"])
    calls = [
        c.post("/api/items/transcribe", json={"path": str(root / "a.mkv")}),
        c.get("/api/items/transcript", params={"path": str(root / "a.mkv")}),
        c.delete("/api/items/transcript", params={"path": str(root / "a.mkv")}),
        c.post("/api/items/transcript/library", params={"path": str(root / "a.mkv")}),
        c.delete("/api/items/transcript/library", params={"path": str(root / "a.mkv")}),
        c.post("/api/search/folder/transcribe", json={"path": str(root)}),
        c.get("/api/transcripts/orphans"),
        c.post("/api/transcripts/clean"),
    ]
    for r in calls:
        assert r.status_code == 404 and "QC_TRANSCRIBE" in r.json()["detail"], r.request.url


@needs_ffmpeg
def test_stored_transcripts_stay_out_while_off(env, song):  # noqa: F811
    root = Path(env["QC_MEDIA_ROOTS"])
    (root / "pod").mkdir()
    _tone(root / "pod" / "ep1.mp3")
    on = _client(env)
    for mode in ("nosubs", "gaps"):  # ep1.mp3 whole, then song.flac's gaps
        job = _wait(on, on.post("/api/search/folder/transcribe",
                                json={"path": str(root), "mode": mode}).json()["job_id"])
        assert job["status"] == "done", job
    hits = on.get("/api/search/folder", params={"path": str(root), "query": "way too fast"})
    assert {Path(h["path"]).name for h in hits.json()["matches"]} == {"ep1.mp3", "song.flac"}

    c = _client(_off(env))
    # Audio with nothing but a machine transcript isn't a source while it's off.
    rows = c.get("/api/library/browse", params={"path": str(root / "pod")}).json()["entries"]
    assert all(e["name"] != "ep1.mp3" for e in rows)
    status = c.get("/api/search/folder/index-status", params={"path": str(root)}).json()
    assert status["transcribed"] == 0 and status["audio_untranscribed"] == 0
    assert status["total"] == 1
    hits = c.get("/api/search/folder", params={"path": str(root), "query": "way too fast"})
    assert hits.json()["matches"] == []
    hits = c.get("/api/search", params={"path": str(song), "query": "way too fast"}).json()
    assert all(not m.get("machine") for m in hits["matches"])
    script = c.get("/api/items/subtitles", params={"path": str(song), "fmt": "json"}).json()
    assert script and all(x["source"] != "machine" for x in script)
    for url in ("/api/search/folder", "/api/search"):
        only = c.get(url, params={"path": str(root if url.endswith("folder") else song),
                                  "query": "way too fast", "machine": "only"})
        assert only.status_code == 404 and "QC_TRANSCRIBE" in only.json()["detail"]
    assert "transcript" not in c.get("/api/items", params={"path": str(song)}).json()
