"""Machine transcription switched off (D-040): experimental, off unless QC_TRANSCRIBE turns
it on. Off, nothing transcribes and stored transcripts stay out of every answer, even on a
server with whisper.cpp and a model."""

from __future__ import annotations

from pathlib import Path

from test_transcribe_api import _client, _tone, _wait, env, needs_ffmpeg, song  # noqa: F401

from quipclipper import transcribe as stt


def _off(env: dict) -> dict:
    return {k: v for k, v in env.items() if k != "QC_TRANSCRIBE"}


def test_the_switch():
    assert not stt.enabled({})
    assert not stt.enabled({"QC_TRANSCRIBE": "0"})
    assert not stt.enabled({"QC_TRANSCRIBE": "false"})
    assert stt.enabled({"QC_TRANSCRIBE": "1"}) and stt.enabled({"QC_TRANSCRIBE": " On "})


def test_whisper_and_a_model_are_not_enough(env):  # noqa: F811
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
