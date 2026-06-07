"""WebVTT rendering and the library/subtitle/search/clip routes (media-free).

The subtitle route is exercised through a sidecar .srt (parsed by pysubs2, no
ffmpeg needed); stream probing (/api/items) needs ffprobe and is covered by the
container smoke test rather than here.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from quipclipper.models import Cue
from quipclipper_web.app import create_app
from quipclipper_web.config import Settings
from quipclipper_web.media import cues_to_vtt

SRT = """1
00:00:01,000 --> 00:00:03,000
I'll be back.

2
00:00:04,000 --> 00:00:05,500
Hasta la vista.
"""


def test_cues_to_vtt() -> None:
    cues = [Cue(0, 1.0, 3.0, "I'll be back."), Cue(1, 4.0, 5.5, "Hasta la vista.")]
    vtt = cues_to_vtt(cues)
    assert vtt.startswith("WEBVTT")
    assert "00:00:01.000 --> 00:00:03.000" in vtt
    assert "I'll be back." in vtt


def _client(root: Path) -> TestClient:
    return TestClient(create_app(Settings.from_env({"QC_MEDIA_ROOTS": str(root)})))


def test_browse_route(tmp_path: Path) -> None:
    (tmp_path / "show.mkv").write_bytes(b"")
    client = _client(tmp_path)

    roots = client.get("/api/library/roots").json()
    assert roots["roots"] == [str(tmp_path)]

    listing = client.get("/api/library/browse", params={"path": str(tmp_path)}).json()
    assert [e["name"] for e in listing["entries"]] == ["show.mkv"]


def test_browse_route_forbids_outside(tmp_path: Path) -> None:
    root = tmp_path / "media"
    root.mkdir()
    client = _client(root)
    resp = client.get("/api/library/browse", params={"path": str(tmp_path)})
    assert resp.status_code == 403


def test_subtitles_route_from_sidecar(tmp_path: Path) -> None:
    video = tmp_path / "movie.mkv"
    video.write_bytes(b"")
    (tmp_path / "movie.srt").write_text(SRT, encoding="utf-8")

    resp = _client(tmp_path).get("/api/items/subtitles", params={"path": str(video)})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/vtt")
    assert "Hasta la vista." in resp.text


def test_subtitles_route_forbids_outside(tmp_path: Path) -> None:
    root = tmp_path / "media"
    root.mkdir()
    resp = _client(root).get("/api/items/subtitles", params={"path": "/etc/passwd"})
    assert resp.status_code == 403


# --- search route -----------------------------------------------------------


def test_search_returns_matches(tmp_path: Path) -> None:
    video = tmp_path / "movie.mkv"
    video.write_bytes(b"")
    (tmp_path / "movie.srt").write_text(SRT, encoding="utf-8")

    resp = _client(tmp_path).get(
        "/api/search", params={"path": str(video), "query": "be back"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["query"] == "be back"
    assert data["count"] >= 1
    m = data["matches"][0]
    assert m["score"] > 0
    assert "be back" in m["text"].lower()
    assert "start" in m and "end" in m
    assert "start_ts" in m and "end_ts" in m


def test_search_no_matches(tmp_path: Path) -> None:
    video = tmp_path / "movie.mkv"
    video.write_bytes(b"")
    (tmp_path / "movie.srt").write_text(SRT, encoding="utf-8")

    resp = _client(tmp_path).get(
        "/api/search", params={"path": str(video), "query": "xyzzy nothing"}
    )
    assert resp.status_code == 200
    assert resp.json()["count"] == 0


def test_search_forbids_outside(tmp_path: Path) -> None:
    root = tmp_path / "media"
    root.mkdir()
    resp = _client(root).get(
        "/api/search", params={"path": "/etc/passwd", "query": "test"}
    )
    assert resp.status_code == 403


# --- clip route (with mocked cut) -------------------------------------------


def test_clip_enqueues_job(tmp_path: Path) -> None:
    video = tmp_path / "movie.mkv"
    video.write_bytes(b"")
    (tmp_path / "movie.srt").write_text(SRT, encoding="utf-8")

    # Mock cut_clip so no ffmpeg is needed.
    fake_out = tmp_path / "clip.mkv"
    fake_out.write_bytes(b"fake clip data")

    client = _client(tmp_path)
    with patch("quipclipper_web.app.cut_clip", return_value=fake_out):
        resp = client.post(
            "/api/clip",
            json={
                "path": str(video),
                "query": "be back",
                "match_index": 0,
                "kind": "video",
                "lossless": True,
                "backend": "ffmpeg",
            },
        )
    assert resp.status_code == 200
    data = resp.json()
    assert "job_id" in data
    assert data["status"] in ("queued", "running", "done")

    # Poll until done (mocked cut is instant).
    job_id = data["job_id"]
    for _ in range(20):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "failed"):
            break
        time.sleep(0.1)
    assert job["status"] == "done"
    assert len(job["files"]) >= 1


def test_clip_mkvmerge_fallback_to_ffmpeg(tmp_path: Path) -> None:
    """When mkvmerge fails (e.g. unsplittable FLAC track), fall back to ffmpeg."""
    video = tmp_path / "movie.mkv"
    video.write_bytes(b"")
    (tmp_path / "movie.srt").write_text(SRT, encoding="utf-8")

    fake_out = tmp_path / "clip.mkv"
    fake_out.write_bytes(b"fake clip data")

    client = _client(tmp_path)
    with (
        patch("quipclipper_web.app.cut_with_mkvmerge", side_effect=RuntimeError("cannot split FLAC")),
        patch("quipclipper_web.app.mkvmerge_available", return_value=True),
        patch("quipclipper_web.app.cut_clip", return_value=fake_out) as mock_ffmpeg,
    ):
        resp = client.post(
            "/api/clip",
            json={
                "path": str(video),
                "query": "be back",
                "match_index": 0,
                "kind": "video",
                "lossless": True,
                "backend": "auto",
            },
        )
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]
    for _ in range(20):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "failed"):
            break
        time.sleep(0.1)
    assert job["status"] == "done"
    mock_ffmpeg.assert_called_once()


def test_clip_forbids_outside(tmp_path: Path) -> None:
    root = tmp_path / "media"
    root.mkdir()
    resp = _client(root).post(
        "/api/clip",
        json={"path": "/etc/passwd", "query": "test", "kind": "audio"},
    )
    assert resp.status_code == 403


def test_clip_requires_range_or_query(tmp_path: Path) -> None:
    video = tmp_path / "movie.mkv"
    video.write_bytes(b"")
    resp = _client(tmp_path).post(
        "/api/clip",
        json={"path": str(video), "kind": "audio"},
    )
    assert resp.status_code == 400


def test_jobs_list(tmp_path: Path) -> None:
    client = _client(tmp_path)
    resp = client.get("/api/jobs")
    assert resp.status_code == 200
    assert "jobs" in resp.json()
