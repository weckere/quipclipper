"""Tests for the YouTube source (youtube_items + the yt: endpoint branches).

Hermetic: every yt-dlp invocation is stubbed at the youtube_items._run_ytdlp
seam — no network, no yt-dlp binary needed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from quipclipper.models import Cue
from quipclipper_web import youtube_items
from quipclipper_web.app import create_app
from quipclipper_web.config import Settings
from quipclipper_web.youtube_items import (
    YouTubeStore,
    dedupe_auto_cues,
    extract_video_id,
    make_ref,
    parse_ref,
)

VID = "dQw4w9WgXcQ"
REF = f"yt:{VID}"
WATCH = f"https://www.youtube.com/watch?v={VID}"

VTT = """WEBVTT

00:00:01.000 --> 00:00:03.000
never gonna give you up

00:00:03.000 --> 00:00:05.000
never gonna let you down
"""

INFO = {
    "id": VID,
    "title": "Test Video",
    "channel": "Test Channel",
    "duration": 212,
    "upload_date": "20091025",
    "webpage_url": WATCH,
}


def _outdir(args: list[str]) -> Path:
    """The -o template's directory from a stubbed yt-dlp argv."""
    tpl = args[args.index("-o") + 1]
    return Path(tpl).parent


def fake_ytdlp(*, subs_on=("--write-subs",), vtt=VTT, info=None, urls=None):
    """A _run_ytdlp stand-in. Writes canned info/subs for fetches; returns
    canned -g output for stream-URL resolution. Records calls."""
    calls: list[list[str]] = []

    def run(args, timeout=120):
        calls.append(list(args))
        if "-g" in args:
            return "\n".join(urls or ["https://v.example/stream"]) + "\n"
        out = _outdir(args)
        if "--write-info-json" in args:
            (out / f"{VID}.info.json").write_text(
                json.dumps(info or INFO), encoding="utf-8")
        if any(flag in args for flag in subs_on):
            (out / f"{VID}.en.vtt").write_text(vtt, encoding="utf-8")
        return ""

    run.calls = calls
    return run


@pytest.fixture
def store(tmp_path):
    return YouTubeStore(tmp_path / "state")


def _client(tmp_path, monkeypatch, run=None) -> TestClient:
    monkeypatch.setattr(youtube_items, "_run_ytdlp", run or fake_ytdlp())
    youtube_items.clear_url_cache()
    media = tmp_path / "media"
    media.mkdir(exist_ok=True)
    c = TestClient(create_app(Settings.from_env({
        "QC_MEDIA_ROOTS": str(media),
        "QC_CLIPS_DIR": str(tmp_path / "clips"),
        "QC_STATE_DIR": str(tmp_path / "state"),
    })))
    c.headers["X-Quipclipper"] = "1"
    return c


# --- refs & URL parsing --------------------------------------------------------

def test_parse_and_make_ref_roundtrip():
    assert parse_ref(make_ref(VID)) == VID


def test_parse_ref_rejects_non_yt():
    assert parse_ref("/media/movies/x.mkv") is None
    assert parse_ref("yt:") is None                    # the folder, not an item
    assert parse_ref("yt:short") is None               # bad id length
    assert parse_ref("yt:../../../etc/passwd") is None


def test_extract_video_id_accepts_common_shapes():
    for url in (
        WATCH,
        f"https://youtube.com/watch?v={VID}",
        f"https://m.youtube.com/watch?v={VID}&t=42s",
        f"https://www.youtube.com/watch?list=PL123&v={VID}",
        f"https://youtu.be/{VID}",
        f"https://youtu.be/{VID}?t=10",
        f"https://www.youtube.com/shorts/{VID}",
        f"https://www.youtube.com/embed/{VID}",
        f"https://www.youtube.com/live/{VID}",
    ):
        assert extract_video_id(url) == VID, url


def test_extract_video_id_rejects_junk():
    for url in (
        f"http://www.youtube.com/watch?v={VID}",       # not https
        f"https://evil.com/watch?v={VID}",             # wrong host
        f"https://www.youtube.com.evil.com/watch?v={VID}",
        "https://www.youtube.com/watch?v=short",       # bad id
        f"-{WATCH}",                                   # argv-injection shape
        "notaurl",
    ):
        assert extract_video_id(url) is None, url


# --- dedupe_auto_cues ------------------------------------------------------------

def test_dedupe_collapses_rolling_windows():
    cues = [
        Cue(index=0, start=0.0, end=2.0, text="hello world"),
        Cue(index=1, start=2.0, end=4.0, text="hello world"),          # exact repeat
        Cue(index=2, start=4.0, end=6.0, text="hello world this is a test"),
        Cue(index=3, start=6.0, end=8.0, text=""),                     # empty
    ]
    out = dedupe_auto_cues(cues)
    assert [c.text for c in out] == ["hello world", "this is a test"]
    assert out[0].end == 4.0  # exact repeat extended the first cue


# --- store -----------------------------------------------------------------------

def test_add_writes_state_and_is_idempotent(store, monkeypatch):
    run = fake_ytdlp()
    monkeypatch.setattr(youtube_items, "_run_ytdlp", run)
    meta = store.add(WATCH, ["en"])
    assert meta["id"] == VID and meta["title"] == "Test Video"
    assert meta["sub_source"] == "manual" and meta["sub_lang"] == "en"
    d = store._item_dir(VID)
    assert (d / "info.json").is_file() and (d / "subs.vtt").is_file()
    n_calls = len(run.calls)
    again = store.add(WATCH, ["en"])  # idempotent, no new yt-dlp runs
    assert again["added_at"] == meta["added_at"]
    assert len(run.calls) == n_calls


def test_add_falls_back_to_auto_subs(store, monkeypatch):
    run = fake_ytdlp(subs_on=("--write-auto-subs",))
    monkeypatch.setattr(youtube_items, "_run_ytdlp", run)
    meta = store.add(WATCH, ["en"])
    assert meta["sub_source"] == "auto"
    flags = [a for call in run.calls for a in call]
    assert "--write-subs" in flags and "--write-auto-subs" in flags


def test_add_rejects_bad_url(store):
    with pytest.raises(ValueError):
        store.add("https://evil.com/watch?v=" + VID, ["en"])


def test_add_cleans_up_on_ytdlp_failure(store, monkeypatch):
    def boom(args, timeout=120):
        raise RuntimeError("yt-dlp failed: video unavailable")
    monkeypatch.setattr(youtube_items, "_run_ytdlp", boom)
    with pytest.raises(RuntimeError):
        store.add(WATCH, ["en"])
    assert not store._item_dir(VID).exists()  # nothing half-added


def test_cues_and_meta(store, monkeypatch):
    monkeypatch.setattr(youtube_items, "_run_ytdlp", fake_ytdlp())
    store.add(WATCH, ["en"])
    cues = store.cues(VID)
    assert [c.text for c in cues] == ["never gonna give you up", "never gonna let you down"]
    with pytest.raises(KeyError):
        store.meta("aaaaaaaaaaa")


def test_stream_url_resolution_and_cache(monkeypatch):
    run = fake_ytdlp(urls=["https://v.example/video", "https://a.example/audio"])
    monkeypatch.setattr(youtube_items, "_run_ytdlp", run)
    youtube_items.clear_url_cache()
    v, a = youtube_items.resolve_stream_urls(WATCH)
    assert (v, a) == ("https://v.example/video", "https://a.example/audio")
    youtube_items.resolve_stream_urls(WATCH)  # served from cache
    assert len(run.calls) == 1

    youtube_items.clear_url_cache()
    monkeypatch.setattr(
        youtube_items, "_run_ytdlp", fake_ytdlp(urls=["https://v.example/progressive"]))
    v, a = youtube_items.resolve_stream_urls(WATCH)
    assert v == "https://v.example/progressive" and a is None


# --- endpoints --------------------------------------------------------------------

def test_add_endpoint_and_browse(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    resp = client.post("/api/youtube", json={"url": WATCH})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["entry"]["path"] == REF and body["entry"]["is_youtube"] is True

    root = client.get("/api/library/browse").json()
    folder = [e for e in root["entries"] if e.get("is_youtube")]
    assert folder and folder[0]["path"] == "yt:" and folder[0]["is_dir"] is True

    listing = client.get("/api/library/browse", params={"path": "yt:"}).json()
    assert [e["path"] for e in listing["entries"]] == [REF]
    assert listing["entries"][0]["name"] == "Test Video"


def test_add_endpoint_rejects_bad_url(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    assert client.post("/api/youtube", json={"url": "https://evil.com/x"}).status_code == 400


def test_add_endpoint_maps_ytdlp_failure_to_502(tmp_path, monkeypatch):
    def boom(args, timeout=120):
        raise RuntimeError("yt-dlp failed: 403")
    client = _client(tmp_path, monkeypatch, run=boom)
    assert client.post("/api/youtube", json={"url": WATCH}).status_code == 502


def test_delete_endpoint(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    client.post("/api/youtube", json={"url": WATCH})
    assert client.delete(f"/api/youtube/{VID}").status_code == 200
    assert client.get("/api/library/browse", params={"path": "yt:"}).json()["entries"] == []
    assert client.delete(f"/api/youtube/{VID}").status_code == 404


def test_item_info_synthesized(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    client.post("/api/youtube", json={"url": WATCH})
    info = client.get("/api/items", params={"path": REF}).json()
    assert info["is_youtube"] is True
    assert info["duration"] == 212.0
    kinds = {s["kind"] for s in info["streams"]}
    assert kinds == {"video", "audio"}
    assert info["has_sidecar"] is True
    assert client.get("/api/items", params={"path": "yt:aaaaaaaaaaa"}).status_code == 404


def test_subtitles_json_and_vtt(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    client.post("/api/youtube", json={"url": WATCH})
    cues = client.get("/api/items/subtitles", params={"path": REF, "fmt": "json"}).json()
    assert cues[0]["text"] == "never gonna give you up"
    vtt = client.get("/api/items/subtitles", params={"path": REF})
    assert vtt.headers["content-type"].startswith("text/vtt")
    assert "never gonna let you down" in vtt.text


def test_dialogue_search_on_item_and_folder(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    client.post("/api/youtube", json={"url": WATCH})
    hits = client.get("/api/search", params={"path": REF, "query": "let you down"}).json()
    assert hits["count"] >= 1
    assert hits["matches"][0]["text"] == "never gonna let you down"

    folder = client.get(
        "/api/search/folder", params={"path": "yt:", "query": "give you up"}).json()
    assert folder["count"] >= 1
    assert folder["matches"][0]["path"] == REF
    assert folder["matches"][0]["file"] == "Test Video"


def test_reindex_refetches_transcript(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    client.post("/api/youtube", json={"url": WATCH})
    resp = client.post("/api/items/subtitles/reindex", params={"path": REF})
    assert resp.status_code == 200
    assert resp.json()["cues"] == 2


def test_media_endpoints_for_yt(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    client.post("/api/youtube", json={"url": WATCH})
    # Raw media: defensive 400 (the frontend uses the transcode path).
    assert client.get("/api/media", params={"path": REF}).status_code == 400
    # Keyframe probe: echo (no local file to probe).
    kf = client.get("/api/media/keyframe", params={"path": REF, "time": 12.5}).json()
    assert kf == {"requested": 12.5, "actual": 12.5}


def test_bookmarks_accept_yt_refs(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    client.post("/api/youtube", json={"url": WATCH})
    resp = client.post("/api/bookmarks", json={
        "path": REF, "label": "chorus", "start": 43.0, "end": 61.0,
    })
    assert resp.status_code == 200, resp.text
    got = client.get("/api/bookmarks", params={"path": REF}).json()["bookmarks"]
    assert len(got) == 1 and got[0]["path"] == REF
    # Unknown id -> 404, and real paths still work through _resolve_any.
    assert client.post("/api/bookmarks", json={
        "path": "yt:aaaaaaaaaaa", "label": "x", "start": 0, "end": 1,
    }).status_code == 404


# --- clip branch --------------------------------------------------------------------

def _clip_setup(tmp_path, monkeypatch, urls):
    client = _client(tmp_path, monkeypatch, run=fake_ytdlp(urls=urls))
    client.post("/api/youtube", json={"url": WATCH})
    captured = {}

    def fake_cut(source, rng, **kw):
        captured.update(source=source, rng=rng, **kw)
        out = Path(kw["out"])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"clip")
        return out

    import quipclipper_web.app as app_mod
    monkeypatch.setattr(app_mod, "cut_clip", fake_cut)
    return client, captured


def _wait(client, job_id):
    import time as _t
    for _ in range(100):
        j = client.get(f"/api/jobs/{job_id}").json()
        if j["status"] in ("done", "failed"):
            return j
        _t.sleep(0.05)
    raise AssertionError("job never finished")


def test_clip_video_exact_uses_url_pair(tmp_path, monkeypatch):
    client, cap = _clip_setup(
        tmp_path, monkeypatch, ["https://v.example/video", "https://a.example/audio"])
    resp = client.post("/api/clip", json={
        "path": REF, "start": 10.0, "end": 14.0, "kind": "video",
        "lossless": False, "cue_text": "give you up",
    })
    assert resp.status_code == 200, resp.text
    job = _wait(client, resp.json()["job_id"])
    assert job["status"] == "done", job
    assert cap["source"] == "https://v.example/video"
    assert cap["aux_audio"] == "https://a.example/audio"
    assert cap["lossless"] is False
    assert cap["kind"] == "video"


def test_clip_video_lossless_flag_passthrough(tmp_path, monkeypatch):
    client, cap = _clip_setup(
        tmp_path, monkeypatch, ["https://v.example/video", "https://a.example/audio"])
    resp = client.post("/api/clip", json={
        "path": REF, "start": 10.0, "end": 14.0, "kind": "video", "lossless": True,
    })
    assert resp.status_code == 200, resp.text
    assert _wait(client, resp.json()["job_id"])["status"] == "done"
    assert cap["lossless"] is True
    assert cap["aux_audio"] == "https://a.example/audio"


def test_clip_audio_uses_audio_url_alone(tmp_path, monkeypatch):
    client, cap = _clip_setup(
        tmp_path, monkeypatch, ["https://v.example/video", "https://a.example/audio"])
    resp = client.post("/api/clip", json={
        "path": REF, "start": 10.0, "end": 14.0, "kind": "audio",
    })
    assert resp.status_code == 200, resp.text
    assert _wait(client, resp.json()["job_id"])["status"] == "done"
    assert cap["source"] == "https://a.example/audio"
    assert cap["aux_audio"] is None
    # AAC audio, lossless default -> .m4a extension via the known-codec shortcut
    assert str(cap["out"]).endswith(".m4a")


def test_clip_progressive_single_url(tmp_path, monkeypatch):
    client, cap = _clip_setup(tmp_path, monkeypatch, ["https://v.example/progressive"])
    resp = client.post("/api/clip", json={
        "path": REF, "start": 1.0, "end": 2.0, "kind": "video", "lossless": False,
    })
    assert resp.status_code == 200, resp.text
    assert _wait(client, resp.json()["job_id"])["status"] == "done"
    assert cap["source"] == "https://v.example/progressive"
    assert cap["aux_audio"] is None


def test_clip_whole_file_end_from_metadata(tmp_path, monkeypatch):
    client, cap = _clip_setup(tmp_path, monkeypatch, ["https://v.example/progressive"])
    resp = client.post("/api/clip", json={"path": REF, "start": 0.0, "kind": "video",
                                          "lossless": False})
    assert resp.status_code == 200, resp.text
    assert _wait(client, resp.json()["job_id"])["status"] == "done"
    assert cap["rng"].end == pytest.approx(212 + 2.0)  # metadata duration + after-buffer


def test_clip_naming_lands_under_title_folder(tmp_path, monkeypatch):
    client, cap = _clip_setup(tmp_path, monkeypatch, ["https://v.example/progressive"])
    resp = client.post("/api/clip", json={
        "path": REF, "start": 10.0, "end": 14.0, "kind": "video", "lossless": False,
        "cue_text": "give you up",
    })
    assert resp.status_code == 200, resp.text
    assert _wait(client, resp.json()["job_id"])["status"] == "done"
    out = Path(cap["out"])
    clips_root = tmp_path / "clips"
    assert out.is_relative_to(clips_root)
    # Default template {source}/... -> per-video folder named by the title.
    assert out.parent.name == "Test Video"
    assert "Test_Channel" in out.name  # {title} token = channel


def test_clip_rejects_mkvmerge_and_split(tmp_path, monkeypatch):
    client, _ = _clip_setup(tmp_path, monkeypatch, ["https://v.example/progressive"])
    assert client.post("/api/clip", json={
        "path": REF, "start": 0, "end": 1, "backend": "mkvmerge",
    }).status_code == 400
    assert client.post("/api/clip", json={
        "path": REF, "start": 0, "end": 1, "kind": "audio", "split_channels": True,
    }).status_code == 400


def test_clip_unknown_video_404(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    assert client.post("/api/clip", json={
        "path": "yt:aaaaaaaaaaa", "start": 0, "end": 1,
    }).status_code == 404


def test_clip_stream_resolution_failure_502(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    client.post("/api/youtube", json={"url": WATCH})

    def boom(url):
        raise RuntimeError("yt-dlp failed: gated")
    monkeypatch.setattr(youtube_items, "resolve_stream_urls", boom)
    import quipclipper_web.app as app_mod
    monkeypatch.setattr(app_mod.youtube_items, "resolve_stream_urls", boom)
    assert client.post("/api/clip", json={
        "path": REF, "start": 0, "end": 1,
    }).status_code == 502
