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


def _wait_done(client: TestClient, job_id: str) -> dict:
    for _ in range(40):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "failed"):
            return job
        time.sleep(0.05)
    return job


def test_clip_whole_file_when_end_omitted(tmp_path: Path) -> None:
    """start given with no end = whole file: the backend fills the end from the
    probed duration (used by batch export in the clips library)."""
    video = tmp_path / "movie.mkv"
    video.write_bytes(b"")
    fake_out = tmp_path / "clip.mka"
    fake_out.write_bytes(b"x")

    client = _client(tmp_path)
    with (
        patch("quipclipper_web.media.probe_duration", return_value=120.0),
        patch("quipclipper_web.app.cut_clip", return_value=fake_out) as mock_cut,
    ):
        resp = client.post("/api/clip", json={
            "path": str(video), "start": 0, "before": 0, "after": 0,
            "kind": "audio", "lossless": True, "backend": "ffmpeg",
        })
        assert resp.status_code == 200
        job = _wait_done(client, resp.json()["job_id"])
        assert job["status"] == "done"
        rng = mock_cut.call_args.args[1]
    assert rng.start == 0.0
    assert rng.end == 120.0


def test_clip_whole_file_fails_cleanly_without_duration(tmp_path: Path) -> None:
    video = tmp_path / "movie.mkv"
    video.write_bytes(b"")
    with patch("quipclipper_web.media.probe_duration", return_value=None):
        resp = _client(tmp_path).post("/api/clip", json={
            "path": str(video), "start": 0, "kind": "audio",
        })
    assert resp.status_code == 400
    assert "duration" in resp.json()["detail"].lower()


def test_clip_save_to_library_can_be_overridden_off(tmp_path: Path) -> None:
    """With the global default ON, an explicit save_to_library=false must win
    (the clip lands in the clips root, not a per-source subfolder) — R2."""
    media = tmp_path / "media"
    media.mkdir()
    video = media / "movie.mkv"
    video.write_bytes(b"")
    (media / "movie.srt").write_text(SRT, encoding="utf-8")
    clips = tmp_path / "clips"
    clips.mkdir()
    fake_out = clips / "out.mkv"
    fake_out.write_bytes(b"x")

    client = TestClient(create_app(Settings.from_env({
        "QC_MEDIA_ROOTS": str(media),
        "QC_CLIPS_DIR": str(clips),
        "QC_STATE_DIR": str(tmp_path / "state"),
        "QC_SAVE_TO_LIBRARY": "true",  # global default ON
    })))

    with patch("quipclipper_web.app.cut_clip", return_value=fake_out) as mock_cut:
        resp = client.post("/api/clip", json={
            "path": str(video), "query": "be back", "kind": "video",
            "lossless": True, "backend": "ffmpeg",
            "save_to_library": False,  # override the default OFF
        })
        assert resp.status_code == 200
        job = _wait_done(client, resp.json()["job_id"])
        assert job["status"] == "done"
        out_arg = Path(mock_cut.call_args.kwargs["out"])
    # Override respected: no per-source subfolder.
    assert out_arg.parent == clips


def test_clip_save_to_library_default_applies_when_unset(tmp_path: Path) -> None:
    """When the request omits save_to_library, the server default ON files the
    clip into a per-source subfolder — R2 (None falls back to the default)."""
    media = tmp_path / "media"
    media.mkdir()
    video = media / "movie.mkv"
    video.write_bytes(b"")
    (media / "movie.srt").write_text(SRT, encoding="utf-8")
    clips = tmp_path / "clips"
    clips.mkdir()
    fake_out = clips / "movie" / "out.mkv"
    fake_out.parent.mkdir(parents=True)
    fake_out.write_bytes(b"x")

    client = TestClient(create_app(Settings.from_env({
        "QC_MEDIA_ROOTS": str(media),
        "QC_CLIPS_DIR": str(clips),
        "QC_STATE_DIR": str(tmp_path / "state"),
        "QC_SAVE_TO_LIBRARY": "true",
    })))

    with patch("quipclipper_web.app.cut_clip", return_value=fake_out) as mock_cut:
        resp = client.post("/api/clip", json={
            "path": str(video), "query": "be back", "kind": "video",
            "lossless": True, "backend": "ffmpeg",
            # save_to_library omitted → None → use the server default (ON)
        })
        assert resp.status_code == 200
        job = _wait_done(client, resp.json()["job_id"])
        assert job["status"] == "done"
        out_arg = Path(mock_cut.call_args.kwargs["out"])
    assert out_arg.parent == clips / "movie"


def test_clips_browse_rejects_sibling_dir_traversal(tmp_path: Path) -> None:
    """A sibling like /clips-evil must not pass the clips-dir check — R3.
    (The old string-prefix test allowed it; is_relative_to does not.)"""
    media = tmp_path / "media"
    media.mkdir()
    clips = tmp_path / "clips"
    clips.mkdir()
    evil = tmp_path / "clips-evil"
    evil.mkdir()
    (evil / "secret.mkv").write_bytes(b"x")

    client = TestClient(create_app(Settings.from_env({
        "QC_MEDIA_ROOTS": str(media),
        "QC_CLIPS_DIR": str(clips),
        "QC_STATE_DIR": str(tmp_path / "state"),
    })))

    resp = client.get("/api/clips", params={"folder": "../clips-evil"})
    assert resp.status_code == 403


def test_jobs_list(tmp_path: Path) -> None:
    client = _client(tmp_path)
    resp = client.get("/api/jobs")
    assert resp.status_code == 200
    assert "jobs" in resp.json()


def test_jobs_prune_old_finished() -> None:
    """Finished jobs past MAX_FINISHED_AGE are dropped on submit — R16."""
    import time as time_mod

    from quipclipper_web.jobs import JobRegistry

    reg = JobRegistry(max_workers=1)
    old = reg.submit(lambda: [], label="old")
    # Wait for it to finish, then age it past the cutoff.
    for _ in range(50):
        if old.finished is not None:
            break
        time_mod.sleep(0.05)
    assert old.finished is not None
    old.finished -= JobRegistry.MAX_FINISHED_AGE + 1

    fresh = reg.submit(lambda: [], label="fresh")
    assert reg.get(old.id) is None
    assert reg.get(fresh.id) is not None
    reg.shutdown()


def test_negative_track_rejected(tmp_path: Path) -> None:
    """Negative track values become bad ffmpeg stream specifiers — R12."""
    video = tmp_path / "movie.mkv"
    video.write_bytes(b"")
    client = _client(tmp_path)
    resp = client.get(
        "/api/items/subtitles", params={"path": str(video), "track": -1},
    )
    assert resp.status_code == 422
    resp = client.get(
        "/api/search", params={"path": str(video), "query": "hi", "track": -1},
    )
    assert resp.status_code == 422


# --- bookmarks ---------------------------------------------------------------


def test_bookmark_crud(tmp_path: Path) -> None:
    video = tmp_path / "movie.mkv"
    video.write_bytes(b"")
    client = _client(tmp_path)

    # List empty
    resp = client.get("/api/bookmarks", params={"path": str(video)})
    assert resp.status_code == 200
    assert resp.json()["bookmarks"] == []

    # Create
    resp = client.post(
        "/api/bookmarks",
        json={"path": str(video), "label": "Test clip", "start": 10.5, "end": 15.0},
    )
    assert resp.status_code == 200
    bm = resp.json()
    assert bm["label"] == "Test clip"
    assert bm["start"] == 10.5
    assert bm["end"] == 15.0
    bm_id = bm["id"]

    # List with one entry
    resp = client.get("/api/bookmarks", params={"path": str(video)})
    assert len(resp.json()["bookmarks"]) == 1

    # Delete
    resp = client.delete(f"/api/bookmarks/{bm_id}")
    assert resp.status_code == 200

    # Gone
    resp = client.get("/api/bookmarks", params={"path": str(video)})
    assert resp.json()["bookmarks"] == []


def test_bookmark_forbids_outside(tmp_path: Path) -> None:
    root = tmp_path / "media"
    root.mkdir()
    client = _client(root)
    resp = client.post(
        "/api/bookmarks",
        json={"path": "/etc/passwd", "label": "bad", "start": 0, "end": 1},
    )
    assert resp.status_code == 403


def test_bookmark_delete_not_found(tmp_path: Path) -> None:
    client = _client(tmp_path)
    resp = client.delete("/api/bookmarks/nonexistent")
    assert resp.status_code == 404


# --- clips library -----------------------------------------------------------


def test_clips_list_empty(tmp_path: Path) -> None:
    client = _client(tmp_path)
    resp = client.get("/api/clips")
    assert resp.status_code == 200
    data = resp.json()
    assert data["clips"] == []


def test_clips_list_with_files(tmp_path: Path) -> None:
    # Create a clips dir with some files
    clips = tmp_path / "clips"
    clips.mkdir()
    (clips / "test.mkv").write_bytes(b"x" * 100)
    sub = clips / "Movie Name"
    sub.mkdir()
    (sub / "clip1.mkv").write_bytes(b"y" * 200)

    client = TestClient(
        create_app(Settings.from_env({
            "QC_MEDIA_ROOTS": str(tmp_path),
            "QC_CLIPS_DIR": str(clips),
        }))
    )
    resp = client.get("/api/clips")
    data = resp.json()
    assert len(data["folders"]) == 1
    assert data["folders"][0] == "Movie Name"
    assert len(data["clips"]) == 1
    assert data["clips"][0]["name"] == "test.mkv"

    # Browse subfolder
    resp = client.get("/api/clips", params={"folder": "Movie Name"})
    data = resp.json()
    assert len(data["clips"]) == 1
    assert data["clips"][0]["name"] == "clip1.mkv"


def test_clips_download_url_prefix(tmp_path: Path) -> None:
    """QC_CLIPS_URL_PREFIX points download_url at the front proxy — R8."""
    clips = tmp_path / "clips"
    clips.mkdir()
    (clips / "test.mkv").write_bytes(b"x")
    sub = clips / "Movie Name"
    sub.mkdir()
    (sub / "clip1.mkv").write_bytes(b"y")

    client = TestClient(
        create_app(Settings.from_env({
            "QC_MEDIA_ROOTS": str(tmp_path),
            "QC_CLIPS_DIR": str(clips),
            "QC_CLIPS_URL_PREFIX": "/clips/",  # trailing slash is stripped
        }))
    )
    data = client.get("/api/clips").json()
    assert data["clips"][0]["download_url"] == "/clips/test.mkv"
    # stream_url still goes through the API (transcode/range handling)
    assert data["clips"][0]["stream_url"].startswith("/api/clips/stream/")

    data = client.get("/api/clips", params={"folder": "Movie Name"}).json()
    assert data["clips"][0]["download_url"] == "/clips/Movie%20Name/clip1.mkv"


def test_clips_download_url_default_is_api(tmp_path: Path) -> None:
    """Without QC_CLIPS_URL_PREFIX, downloads go through the backend API."""
    clips = tmp_path / "clips"
    clips.mkdir()
    (clips / "test.mkv").write_bytes(b"x")

    client = TestClient(
        create_app(Settings.from_env({
            "QC_MEDIA_ROOTS": str(tmp_path),
            "QC_CLIPS_DIR": str(clips),
        }))
    )
    data = client.get("/api/clips").json()
    assert data["clips"][0]["download_url"] == "/api/clips/download/test.mkv"


def test_clips_download(tmp_path: Path) -> None:
    clips = tmp_path / "clips"
    clips.mkdir()
    (clips / "test.mkv").write_bytes(b"clip data")

    client = TestClient(
        create_app(Settings.from_env({
            "QC_MEDIA_ROOTS": str(tmp_path),
            "QC_CLIPS_DIR": str(clips),
        }))
    )
    resp = client.get("/api/clips/download/test.mkv")
    assert resp.status_code == 200


def test_clips_download_forbids_traversal(tmp_path: Path) -> None:
    clips = tmp_path / "clips"
    clips.mkdir()

    client = TestClient(
        create_app(Settings.from_env({
            "QC_MEDIA_ROOTS": str(tmp_path),
            "QC_CLIPS_DIR": str(clips),
        }))
    )
    # FastAPI normalizes ../../ in the URL, so the path resolves inside clips_dir
    # and gets a 404 (file not found). The important thing is it never serves
    # files outside clips_dir.
    resp = client.get("/api/clips/download/../../../etc/passwd")
    assert resp.status_code in (403, 404)


# --- Jellyfin metadata -------------------------------------------------------


def test_jellyfin_meta_disabled(tmp_path: Path) -> None:
    client = _client(tmp_path)
    resp = client.get("/api/jellyfin/meta", params={"name": "test"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["meta"] is None
    assert data["enabled"] is False


def test_bookmark_auto_label(tmp_path: Path) -> None:
    video = tmp_path / "movie.mkv"
    video.write_bytes(b"")
    client = _client(tmp_path)
    resp = client.post(
        "/api/bookmarks",
        json={"path": str(video), "start": 60.0, "end": 90.0},
    )
    assert resp.status_code == 200
    # Auto-generated label should contain timestamps
    assert "00:01:00" in resp.json()["label"]


# --- library search ---


def test_library_search_global(tmp_path: Path) -> None:
    """Search across all roots returns matching dirs and videos."""
    (tmp_path / "Alien.mkv").write_bytes(b"")
    (tmp_path / "Aliens").mkdir()
    (tmp_path / "Batman.mkv").write_bytes(b"")
    client = _client(tmp_path)
    resp = client.get("/api/library/search", params={"query": "alien"})
    assert resp.status_code == 200
    names = [e["name"] for e in resp.json()["entries"]]
    assert "Alien.mkv" in names
    assert "Aliens" in names
    assert "Batman.mkv" not in names


def test_library_search_within_path(tmp_path: Path) -> None:
    """Search within a specific directory."""
    sub = tmp_path / "movies"
    sub.mkdir()
    (sub / "Alien.mkv").write_bytes(b"")
    (sub / "Batman.mkv").write_bytes(b"")
    (tmp_path / "Alien-top.mkv").write_bytes(b"")
    client = _client(tmp_path)
    resp = client.get(
        "/api/library/search",
        params={"query": "alien", "path": str(sub)},
    )
    assert resp.status_code == 200
    names = [e["name"] for e in resp.json()["entries"]]
    assert names == ["Alien.mkv"]


def test_library_search_empty_query(tmp_path: Path) -> None:
    """Empty query is rejected by validation (min_length=1)."""
    (tmp_path / "Alien.mkv").write_bytes(b"")
    client = _client(tmp_path)
    resp = client.get("/api/library/search", params={"query": ""})
    assert resp.status_code == 422


def test_library_search_no_matches(tmp_path: Path) -> None:
    (tmp_path / "Alien.mkv").write_bytes(b"")
    client = _client(tmp_path)
    resp = client.get("/api/library/search", params={"query": "zzznotfound"})
    assert resp.status_code == 200
    assert resp.json()["entries"] == []


def test_library_search_forbids_outside(tmp_path: Path) -> None:
    """path param outside roots is rejected."""
    client = _client(tmp_path)
    resp = client.get(
        "/api/library/search",
        params={"query": "test", "path": "/etc"},
    )
    assert resp.status_code == 403


# --- folder dialogue search ---


SRT2 = """1
00:00:01,000 --> 00:00:03,000
May the force be with you.

2
00:00:04,000 --> 00:00:06,000
I find your lack of faith disturbing.
"""


# --- transcode ---


def test_transcode_forbids_outside(tmp_path: Path) -> None:
    client = _client(tmp_path)
    resp = client.get("/api/media/transcode", params={"path": "/etc/passwd"})
    assert resp.status_code == 403


def test_transcode_not_found(tmp_path: Path) -> None:
    client = _client(tmp_path)
    resp = client.get(
        "/api/media/transcode",
        params={"path": str(tmp_path / "nope.mkv")},
    )
    assert resp.status_code == 404


# --- folder dialogue search ---


def test_folder_dialogue_search(tmp_path: Path) -> None:
    """Searching dialogue across files in a folder returns matches."""
    video = tmp_path / "episode1.mkv"
    video.write_bytes(b"")
    srt = tmp_path / "episode1.srt"
    srt.write_text(SRT)

    video2 = tmp_path / "episode2.mkv"
    video2.write_bytes(b"")
    srt2 = tmp_path / "episode2.srt"
    srt2.write_text(SRT2)

    client = _client(tmp_path)
    resp = client.get(
        "/api/search/folder",
        params={"path": str(tmp_path), "query": "force"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["files_scanned"] == 2
    assert data["count"] >= 1
    assert any("force" in m["text"].lower() for m in data["matches"])
    # All matches should reference episode2 for "force"
    assert all(m["file"] == "episode2.mkv" for m in data["matches"])


def test_folder_dialogue_search_no_subs(tmp_path: Path) -> None:
    """Files without subtitles are skipped gracefully."""
    (tmp_path / "nosubs.mkv").write_bytes(b"")
    client = _client(tmp_path)
    resp = client.get(
        "/api/search/folder",
        params={"path": str(tmp_path), "query": "hello"},
    )
    assert resp.status_code == 200
    assert resp.json()["count"] == 0
    assert resp.json()["files_scanned"] == 1


def test_folder_dialogue_search_forbids_outside(tmp_path: Path) -> None:
    client = _client(tmp_path)
    resp = client.get(
        "/api/search/folder",
        params={"path": "/etc", "query": "test"},
    )
    assert resp.status_code == 403


def test_folder_dialogue_search_not_a_dir(tmp_path: Path) -> None:
    f = tmp_path / "file.mkv"
    f.write_bytes(b"")
    client = _client(tmp_path)
    resp = client.get(
        "/api/search/folder",
        params={"path": str(f), "query": "test"},
    )
    assert resp.status_code == 400
