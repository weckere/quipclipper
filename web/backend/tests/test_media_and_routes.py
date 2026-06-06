"""WebVTT rendering and the library/subtitle routes (media-free).

The subtitle route is exercised through a sidecar .srt (parsed by pysubs2, no
ffmpeg needed); stream probing (/api/items) needs ffprobe and is covered by the
container smoke test rather than here.
"""

from __future__ import annotations

from pathlib import Path

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
