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


def fake_ytdlp(*, subs_on=("--write-subs",), vtt=VTT, info=None, urls=None,
               video_bytes=b"fake-mp4-bytes"):
    """A _run_ytdlp stand-in. Writes canned info/subs for fetches, a canned
    video file for downloads, and returns canned -g output for stream-URL
    resolution. Records calls."""
    calls: list[list[str]] = []

    def run(args, timeout=120):
        calls.append(list(args))
        if "-g" in args:
            return "\n".join(urls or ["https://v.example/stream"]) + "\n"
        out = _outdir(args)
        # The id yt-dlp would name files by, derived from the request URL (last
        # arg after `--`), so backfilling files with different ids works.
        url = args[-1]
        vid = url.split("v=")[-1] if "v=" in url else VID
        if "--merge-output-format" in args:  # full-video download
            tpl = Path(args[args.index("-o") + 1])
            tpl.with_name(tpl.name.replace("%(ext)s", "mp4")).write_bytes(video_bytes)
            return ""
        if "--write-info-json" in args:
            meta = dict(info or INFO)
            if info is None:
                meta["id"] = vid
            (out / f"{vid}.info.json").write_text(json.dumps(meta), encoding="utf-8")
        if any(flag in args for flag in subs_on):
            (out / f"{vid}.en.vtt").write_text(vtt, encoding="utf-8")
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


def _add(client, url=WATCH):
    """Add a video and wait for the async add job to finish (idempotent re-adds
    return job_id null). Returns the endpoint's response body."""
    resp = client.post("/api/youtube", json={"url": url})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    if body.get("job_id"):
        assert _wait(client, body["job_id"])["status"] == "done"
    return body


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


# --- subtitle backfill: id detection + fetch (library files) --------------------

from quipclipper_web.youtube_items import fetch_subs_for_file, youtube_id_for_file

AAA = "AAAAAAAAAAA"  # a second valid 11-char id


def test_youtube_id_for_file_from_brackets(tmp_path):
    assert youtube_id_for_file(tmp_path / f"s01e02 the title [{VID}].mp4") == VID
    # The id is the LAST bracketed token before the extension.
    assert youtube_id_for_file(tmp_path / f"a [{AAA}] b [{VID}].mp4") == VID
    assert youtube_id_for_file(tmp_path / "no id at all.mp4") is None
    assert youtube_id_for_file(tmp_path / "short [abc].mp4") is None  # not 11 chars


def test_youtube_id_for_file_from_info_json(tmp_path):
    v = tmp_path / "clip.mp4"
    (tmp_path / "clip.info.json").write_text(json.dumps({"id": VID}), encoding="utf-8")
    assert youtube_id_for_file(v) == VID


def test_fetch_subs_for_file_writes_sidecar(tmp_path, monkeypatch):
    monkeypatch.setattr(youtube_items, "_run_ytdlp", fake_ytdlp())
    v = tmp_path / f"My Vid [{VID}].mp4"; v.write_bytes(b"")
    res = fetch_subs_for_file(v, ["en"])
    assert res["status"] == "ok" and res["id"] == VID
    sidecar = tmp_path / f"My Vid [{VID}].vtt"
    assert sidecar.is_file() and "never gonna" in sidecar.read_text()


def test_fetch_subs_for_file_skips_existing(tmp_path, monkeypatch):
    monkeypatch.setattr(youtube_items, "_run_ytdlp", fake_ytdlp())
    v = tmp_path / f"V [{VID}].mp4"; v.write_bytes(b"")
    (tmp_path / f"V [{VID}].vtt").write_text("WEBVTT\n", encoding="utf-8")
    assert fetch_subs_for_file(v, ["en"])["status"] == "skipped"


def test_fetch_subs_for_file_no_id_raises(tmp_path):
    with pytest.raises(ValueError):
        fetch_subs_for_file(tmp_path / "no-id.mp4", ["en"])


def test_fetch_subs_for_file_no_captions(tmp_path, monkeypatch):
    monkeypatch.setattr(youtube_items, "_run_ytdlp", fake_ytdlp(subs_on=()))
    v = tmp_path / f"V [{VID}].mp4"; v.write_bytes(b"")
    assert fetch_subs_for_file(v, ["en"])["status"] == "none"
    assert not (tmp_path / f"V [{VID}].vtt").exists()


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


def test_refresh_preserves_transcript_when_refetch_fails(store, monkeypatch):
    """M1: a failed reindex must NOT destroy the stored transcript — for a
    since-deleted video it would be unrecoverable."""
    monkeypatch.setattr(youtube_items, "_run_ytdlp", fake_ytdlp())
    store.add(WATCH, ["en"])
    assert len(store.cues(VID)) == 2

    def boom(args, timeout=120):
        raise RuntimeError("yt-dlp failed: video is private")
    monkeypatch.setattr(youtube_items, "_run_ytdlp", boom)
    with pytest.raises(RuntimeError):
        store.refresh_subs(VID, ["en"])
    # The old transcript survives.
    assert len(store.cues(VID)) == 2
    assert (store._item_dir(VID) / "subs.vtt").is_file()


def test_refresh_preserves_transcript_when_no_captions(store, monkeypatch):
    """A refetch that succeeds but finds no captions keeps the old transcript
    (and the old sub_lang/sub_source), rather than blanking it."""
    monkeypatch.setattr(youtube_items, "_run_ytdlp", fake_ytdlp())
    store.add(WATCH, ["en"])
    # Refetch produces nothing (subs disabled on the video now).
    monkeypatch.setattr(youtube_items, "_run_ytdlp", fake_ytdlp(subs_on=()))
    store.refresh_subs(VID, ["en"])
    assert len(store.cues(VID)) == 2
    assert store.meta(VID)["sub_source"] == "manual"


def test_download_ignores_stale_partial_fragments(store, monkeypatch):
    """M2: leftover video-dl.* fragments from a prior failed run must never be
    promoted to video.mp4 — only the merged output is installed."""
    monkeypatch.setattr(youtube_items, "_run_ytdlp", fake_ytdlp())
    store.add(WATCH, ["en"])
    item_dir = store._item_dir(VID)
    # A stale audio fragment that sorts BEFORE "video-dl.mp4" ('f' < 'm').
    (item_dir / "video-dl.f140.m4a.part").write_bytes(b"garbage-partial")
    vp = store.download(VID)
    assert vp.name == "video.mp4"
    assert vp.read_bytes() == b"fake-mp4-bytes"       # the real merged output
    assert not list(item_dir.glob("video-dl.*"))      # temps cleaned up


def test_download_cleans_temps_on_failure(store, monkeypatch):
    monkeypatch.setattr(youtube_items, "_run_ytdlp", fake_ytdlp())
    store.add(WATCH, ["en"])
    item_dir = store._item_dir(VID)

    def boom(args, timeout=120):
        (item_dir / "video-dl.f137.mp4.part").write_bytes(b"partial")  # simulate a fragment
        raise RuntimeError("yt-dlp failed mid-download")
    monkeypatch.setattr(youtube_items, "_run_ytdlp", boom)
    with pytest.raises(RuntimeError):
        store.download(VID)
    assert not list(item_dir.glob("video-dl.*"))  # cleaned even on failure
    assert store.video_path(VID) is None


def test_iso639_2_lang_codes_normalized_for_youtube(store, monkeypatch):
    """L1: QC_SUBTITLE_LANGS is documented with 3-letter codes ('eng'); YouTube
    uses 2-letter tags. The fetch must request 'en' so captions actually land."""
    run = fake_ytdlp()
    monkeypatch.setattr(youtube_items, "_run_ytdlp", run)
    store.add(WATCH, ["eng", "spa"])
    sub_calls = [c for c in run.calls if "--sub-langs" in c]
    assert sub_calls, "no subtitle fetch happened"
    spec = sub_calls[0][sub_calls[0].index("--sub-langs") + 1]
    tags = spec.split(",")
    assert "en" in tags and "es" in tags
    assert store.meta(VID)["sub_source"] == "manual"  # captions actually landed


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

def test_add_endpoint_is_async_and_browse(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    # The add returns a job immediately (no synchronous yt-dlp wait → no 504).
    resp = client.post("/api/youtube", json={"url": WATCH})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["job_id"] and body["status"] in ("queued", "running", "done")
    assert _wait(client, body["job_id"])["status"] == "done"

    root = client.get("/api/library/browse").json()
    folder = [e for e in root["entries"] if e.get("is_youtube")]
    assert folder and folder[0]["path"] == "yt:" and folder[0]["is_dir"] is True

    listing = client.get("/api/library/browse", params={"path": "yt:"}).json()
    assert [e["path"] for e in listing["entries"]] == [REF]
    assert listing["entries"][0]["name"] == "Test Video"

    # Re-adding is idempotent and returns the entry immediately (job_id null).
    again = client.post("/api/youtube", json={"url": WATCH}).json()
    assert again["job_id"] is None and again["entry"]["path"] == REF


def test_add_endpoint_rejects_bad_url_synchronously(tmp_path, monkeypatch):
    # URL validation happens before the job is queued, so a bad URL is a fast 400.
    client = _client(tmp_path, monkeypatch)
    assert client.post("/api/youtube", json={"url": "https://evil.com/x"}).status_code == 400


def test_add_job_fails_on_ytdlp_error(tmp_path, monkeypatch):
    def boom(args, timeout=120):
        raise RuntimeError("yt-dlp failed: 403")
    client = _client(tmp_path, monkeypatch, run=boom)
    body = client.post("/api/youtube", json={"url": WATCH}).json()
    assert body["job_id"]  # accepted; the failure surfaces on the job
    job = _wait(client, body["job_id"])
    assert job["status"] == "failed"
    assert "403" in job["error"]


def test_add_dedupes_in_flight(tmp_path, monkeypatch):
    import threading
    gate = threading.Event()
    real = youtube_items.YouTubeStore.add

    def blocking_add(self, url, langs):
        gate.wait(5)
        return real(self, url, langs)

    monkeypatch.setattr(youtube_items.YouTubeStore, "add", blocking_add)
    client = _client(tmp_path, monkeypatch)
    j1 = client.post("/api/youtube", json={"url": WATCH}).json()
    j2 = client.post("/api/youtube", json={"url": WATCH}).json()
    assert j1["job_id"] and j1["job_id"] == j2["job_id"]  # same in-flight job
    gate.set()
    assert _wait(client, j1["job_id"])["status"] == "done"


def test_delete_endpoint(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _add(client)
    assert client.delete(f"/api/youtube/{VID}").status_code == 200
    assert client.get("/api/library/browse", params={"path": "yt:"}).json()["entries"] == []
    assert client.delete(f"/api/youtube/{VID}").status_code == 404


def test_item_info_synthesized(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _add(client)
    info = client.get("/api/items", params={"path": REF}).json()
    assert info["is_youtube"] is True
    assert info["duration"] == 212.0
    kinds = {s["kind"] for s in info["streams"]}
    assert kinds == {"video", "audio"}
    assert info["has_sidecar"] is True
    assert client.get("/api/items", params={"path": "yt:aaaaaaaaaaa"}).status_code == 404


def test_subtitles_json_and_vtt(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _add(client)
    cues = client.get("/api/items/subtitles", params={"path": REF, "fmt": "json"}).json()
    assert cues[0]["text"] == "never gonna give you up"
    vtt = client.get("/api/items/subtitles", params={"path": REF})
    assert vtt.headers["content-type"].startswith("text/vtt")
    assert "never gonna let you down" in vtt.text


def test_dialogue_search_on_item_and_folder(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _add(client)
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
    _add(client)
    resp = client.post("/api/items/subtitles/reindex", params={"path": REF})
    assert resp.status_code == 200
    assert resp.json()["cues"] == 2


# --- subtitle backfill endpoints ------------------------------------------------

def test_fetch_subs_file_endpoint_then_searchable(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    v = tmp_path / "media" / f"My Vid [{VID}].mp4"; v.write_bytes(b"")
    job = client.post("/api/media/fetch-subs", params={"path": str(v)}).json()
    assert job["job_id"]
    assert _wait(client, job["job_id"])["status"] == "done"
    assert (tmp_path / "media" / f"My Vid [{VID}].vtt").is_file()
    # Now dialogue search on the LIBRARY file works via the fresh sidecar.
    hits = client.get("/api/search", params={"path": str(v), "query": "give you up"}).json()
    assert hits["count"] >= 1


def test_items_flags_backfillable_youtube_file(tmp_path, monkeypatch):
    """/api/items surfaces youtube_id + backfill_writable for a library video
    with a YT id and no subtitles, so the UI can offer 'Fetch subtitles'."""
    import quipclipper_web.media as media_mod
    monkeypatch.setattr(media_mod, "item_info", lambda p, langs=None: {
        "name": p.name, "path": str(p), "size": 0, "duration": 5.0,
        "streams": [{"kind": "video", "index": 0}], "subtitle_tracks": [],
        "best_track": None, "has_sidecar": False,
    })
    client = _client(tmp_path, monkeypatch)
    v = tmp_path / "media" / f"Vid [{VID}].mp4"; v.write_bytes(b"")
    info = client.get("/api/items", params={"path": str(v)}).json()
    assert info["youtube_id"] == VID and info["backfill_writable"] is True


def test_fetch_subs_file_no_id_400(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    v = tmp_path / "media" / "no-id.mp4"; v.write_bytes(b"")
    assert client.post("/api/media/fetch-subs", params={"path": str(v)}).status_code == 400


def test_fetch_subs_folder_endpoint(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    d = tmp_path / "media" / "chan"; d.mkdir()
    (d / f"a [{VID}].mp4").write_bytes(b"")
    (d / f"b [{AAA}].mp4").write_bytes(b"")   # a different id
    (d / "no-id.mp4").write_bytes(b"")        # not a candidate
    (d / f"has-subs [{'B' * 11}].mp4").write_bytes(b"")
    (d / f"has-subs [{'B' * 11}].vtt").write_text("WEBVTT\n")  # already has subs → skipped

    resp = client.post("/api/search/folder/fetch-subs", params={"path": str(d)}).json()
    assert resp["candidates"] == 2  # only the two id'd, subtitle-less files
    assert _wait(client, resp["job_id"])["status"] == "done"
    assert (d / f"a [{VID}].vtt").is_file()
    assert (d / f"b [{AAA}].vtt").is_file()


def test_fetch_subs_folder_no_candidates(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    d = tmp_path / "media" / "empty"; d.mkdir()
    (d / "plain.mp4").write_bytes(b"")  # no YouTube id
    resp = client.post("/api/search/folder/fetch-subs", params={"path": str(d)}).json()
    assert resp["candidates"] == 0 and resp["job_id"] is None


def test_media_endpoints_for_yt(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _add(client)
    # Raw media: defensive 400 (the frontend uses the transcode path).
    assert client.get("/api/media", params={"path": REF}).status_code == 400
    # Keyframe probe: echo (no local file to probe).
    kf = client.get("/api/media/keyframe", params={"path": REF, "time": 12.5}).json()
    assert kf == {"requested": 12.5, "actual": 12.5}


def test_bookmarks_accept_yt_refs(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _add(client)
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


def test_bookmark_listing_has_youtube_title_as_source_name(tmp_path, monkeypatch):
    """The Bookmarks browser groups by path; a yt: ref must carry the real video
    title as source_name (not the raw 'yt:<id>' the frontend would otherwise show)."""
    client = _client(tmp_path, monkeypatch)
    _add(client)
    client.post("/api/bookmarks", json={"path": REF, "label": "a", "start": 1, "end": 2})
    # A plain file bookmark's source_name is its file name.
    (tmp_path / "media" / "movie.mkv").write_bytes(b"")
    fpath = str(tmp_path / "media" / "movie.mkv")
    client.post("/api/bookmarks", json={"path": fpath, "label": "b", "start": 1, "end": 2})

    by_path = {b["path"]: b for b in client.get("/api/bookmarks").json()["bookmarks"]}
    assert by_path[REF]["source_name"] == "Test Video"
    assert by_path[fpath]["source_name"] == "movie.mkv"


# --- full-video download ------------------------------------------------------------

def _download(client):
    """POST the download and wait for its job (the pool runs it async)."""
    resp = client.post(f"/api/youtube/{VID}/download")
    assert resp.status_code == 200, resp.text
    job_id = resp.json()["job_id"]
    if job_id is None:
        return  # already downloaded
    assert _wait(client, job_id)["status"] == "done"


def test_download_creates_local_file_and_flags(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _add(client)
    _download(client)
    vp = tmp_path / "state" / "youtube" / VID / "video.mp4"
    assert vp.read_bytes() == b"fake-mp4-bytes"
    info = client.get("/api/items", params={"path": REF}).json()
    assert info["downloaded"] is True
    entry = client.get("/api/library/browse", params={"path": "yt:"}).json()["entries"][0]
    assert entry["downloaded"] is True
    # Idempotent: a second POST short-circuits without a job.
    again = client.post(f"/api/youtube/{VID}/download").json()
    assert again["job_id"] is None and again["status"] == "done"


def test_downloaded_media_served_raw_and_keyframe_probed(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _add(client)
    # Before download: raw media is a 400.
    assert client.get("/api/media", params={"path": REF}).status_code == 400
    _download(client)
    resp = client.get("/api/media", params={"path": REF})
    assert resp.status_code == 200
    assert resp.content == b"fake-mp4-bytes"
    assert resp.headers["content-type"] == "video/mp4"
    # Keyframe: no longer the echo — the local file is probed (fake bytes make
    # ffprobe fail, and probe_keyframe_before falls back to the target).
    kf = client.get("/api/media/keyframe", params={"path": REF, "time": 3.0}).json()
    assert kf["requested"] == 3.0


def test_downloaded_clip_uses_local_file_not_urls(tmp_path, monkeypatch):
    client, cap = _clip_setup(tmp_path, monkeypatch, ["https://v.example/video", "https://a.example/audio"])
    _download(client)
    resp = client.post("/api/clip", json={
        "path": REF, "start": 1.0, "end": 2.0, "kind": "video", "lossless": False,
        "backend": "ffmpeg",
    })
    assert resp.status_code == 200, resp.text
    assert _wait(client, resp.json()["job_id"])["status"] == "done"
    assert str(cap["source"]).endswith("video.mp4")  # the local file
    assert cap["aux_audio"] is None
    # And split_channels is no longer rejected (local file = full pipeline).
    resp2 = client.post("/api/clip", json={
        "path": REF, "start": 1.0, "end": 2.0, "kind": "audio", "split_channels": True,
    })
    assert resp2.status_code == 200, resp2.text


def test_remove_download_keeps_transcript(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _add(client)
    _download(client)
    assert client.delete(f"/api/youtube/{VID}/download").status_code == 200
    assert not (tmp_path / "state" / "youtube" / VID / "video.mp4").exists()
    assert (tmp_path / "state" / "youtube" / VID / "subs.vtt").exists()
    info = client.get("/api/items", params={"path": REF}).json()
    assert info["downloaded"] is False
    # Nothing to remove now.
    assert client.delete(f"/api/youtube/{VID}/download").status_code == 404


def test_download_unknown_video_404(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    assert client.post("/api/youtube/aaaaaaaaaaa/download").status_code == 404


# --- downloads to a user-defined folder (youtubeDir) --------------------------

def test_download_to_external_dir_names_and_sidecar(tmp_path, monkeypatch):
    """With a download dir, the video lands as '<Title> [<id>].mp4' with a .vtt
    sidecar, and its path is recorded so the yt: item still finds it."""
    monkeypatch.setattr(youtube_items, "_run_ytdlp", fake_ytdlp())
    dl = tmp_path / "media" / "one-offs"
    store = YouTubeStore(tmp_path / "state", download_dir=dl)
    store.add(WATCH, ["en"])
    vp = store.download(VID)
    assert vp == dl / "Test Video [dQw4w9WgXcQ].mp4"
    assert vp.read_bytes() == b"fake-mp4-bytes"
    assert vp.with_suffix(".vtt").is_file()          # searchable sidecar
    assert "never gonna" in vp.with_suffix(".vtt").read_text()
    # No hidden temp left behind.
    assert not list(dl.glob(".qc-dl-*"))
    # The state dir keeps only the transcript/metadata, not the video.
    assert not (tmp_path / "state" / "youtube" / VID / "video.mp4").exists()
    # meta/video_path resolve the external file.
    assert store.meta(VID)["downloaded"] is True
    assert store.video_path(VID) == vp


def test_remove_external_download_clears_path_and_sidecar(tmp_path, monkeypatch):
    monkeypatch.setattr(youtube_items, "_run_ytdlp", fake_ytdlp())
    dl = tmp_path / "media" / "one-offs"
    store = YouTubeStore(tmp_path / "state", download_dir=dl)
    store.add(WATCH, ["en"])
    vp = store.download(VID)
    assert store.remove_download(VID) is True
    assert not vp.exists() and not vp.with_suffix(".vtt").exists()
    assert store.video_path(VID) is None
    assert "download_path" not in store._raw(VID)
    assert store.meta(VID)["downloaded"] is False


def test_external_download_illegal_title_chars_sanitized(tmp_path, monkeypatch):
    info = {**INFO, "title": 'a/b: "c" | d? <e>'}
    monkeypatch.setattr(youtube_items, "_run_ytdlp", fake_ytdlp(info=info))
    dl = tmp_path / "dl"
    store = YouTubeStore(tmp_path / "state", download_dir=dl)
    store.add(WATCH, ["en"])
    vp = store.download(VID)
    assert vp.name == "ab c d e [dQw4w9WgXcQ].mp4"  # illegal chars dropped, spaces collapsed
    assert vp.is_file()


def test_remove_video_deletes_external_download(tmp_path, monkeypatch):
    """Removing a whole video must also delete its external download + sidecar,
    not orphan them in the media folder."""
    monkeypatch.setattr(youtube_items, "_run_ytdlp", fake_ytdlp())
    dl = tmp_path / "media" / "one-offs"
    store = YouTubeStore(tmp_path / "state", download_dir=dl)
    store.add(WATCH, ["en"])
    vp = store.download(VID)
    assert vp.is_file() and vp.with_suffix(".vtt").is_file()
    assert store.remove(VID) is True
    assert not vp.exists() and not vp.with_suffix(".vtt").exists()
    assert not (tmp_path / "state" / "youtube" / VID).exists()


def test_download_endpoint_uses_configured_youtube_dir(tmp_path, monkeypatch):
    """End-to-end: QC_YOUTUBE_DIR routes the download endpoint's output there."""
    monkeypatch.setattr(youtube_items, "_run_ytdlp", fake_ytdlp())
    youtube_items.clear_url_cache()
    media = tmp_path / "media"; media.mkdir()
    dl = media / "one-offs"
    client = TestClient(create_app(Settings.from_env({
        "QC_MEDIA_ROOTS": str(media),
        "QC_CLIPS_DIR": str(tmp_path / "clips"),
        "QC_STATE_DIR": str(tmp_path / "state"),
        "QC_YOUTUBE_DIR": str(dl),
    })))
    client.headers["X-Quipclipper"] = "1"
    _add(client)
    _download(client)
    assert (dl / "Test Video [dQw4w9WgXcQ].mp4").is_file()
    assert (dl / "Test Video [dQw4w9WgXcQ].vtt").is_file()


def test_concurrent_download_posts_return_the_same_job(tmp_path, monkeypatch):
    """M3: a repeat download POST while one is in flight returns the SAME job,
    not a duplicate — the store lock + the endpoint's in-flight map."""
    import threading

    gate = threading.Event()
    orig = youtube_items.YouTubeStore.download

    def blocking_download(self, video_id):
        gate.wait(5)  # hold the job "running" until the test releases it
        return orig(self, video_id)

    monkeypatch.setattr(youtube_items.YouTubeStore, "download", blocking_download)
    client = _client(tmp_path, monkeypatch)
    _add(client)

    j1 = client.post(f"/api/youtube/{VID}/download").json()
    j2 = client.post(f"/api/youtube/{VID}/download").json()
    assert j1["job_id"] and j1["job_id"] == j2["job_id"]  # deduped
    gate.set()
    assert _wait(client, j1["job_id"])["status"] == "done"


def test_download_job_appears_in_jobs_list(tmp_path, monkeypatch):
    """Download jobs run in their own pool but surface through /api/jobs."""
    client = _client(tmp_path, monkeypatch)
    _add(client)
    job_id = client.post(f"/api/youtube/{VID}/download").json()["job_id"]
    _wait(client, job_id)
    ids = [j["id"] for j in client.get("/api/jobs").json()["jobs"]]
    assert job_id in ids


def test_corrupt_info_json_is_clean_500_not_crash(tmp_path, monkeypatch):
    """L4: a corrupt info.json yields a 500 with a message, not an unhandled
    traceback, on /api/items and bookmark creation."""
    client = _client(tmp_path, monkeypatch)
    _add(client)
    (tmp_path / "state" / "youtube" / VID / "info.json").write_text("{ not json", encoding="utf-8")
    assert client.get("/api/items", params={"path": REF}).status_code == 500
    assert client.post("/api/bookmarks", json={
        "path": REF, "label": "x", "start": 0, "end": 1,
    }).status_code == 500


# --- clip branch --------------------------------------------------------------------

def _clip_setup(tmp_path, monkeypatch, urls):
    client = _client(tmp_path, monkeypatch, run=fake_ytdlp(urls=urls))
    _add(client)
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
    _add(client)

    def boom(url):
        raise RuntimeError("yt-dlp failed: gated")
    monkeypatch.setattr(youtube_items, "resolve_stream_urls", boom)
    import quipclipper_web.app as app_mod
    monkeypatch.setattr(app_mod.youtube_items, "resolve_stream_urls", boom)
    assert client.post("/api/clip", json={
        "path": REF, "start": 0, "end": 1,
    }).status_code == 502
