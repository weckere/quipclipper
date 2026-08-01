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

from quipclipper.clip import ClipRange
from quipclipper.models import Cue
from quipclipper_web.app import (
    DEFAULT_CLIP_TEMPLATE,
    _clean_title,
    _clip_template_context,
    _render_clip_template,
    _slugify,
    create_app,
)
from quipclipper.subtitles import StreamInfo
from quipclipper_web import epub_items as epub_items_mod
from quipclipper_web.config import Settings
from quipclipper_web.media import cues_to_vtt, probe_keyframe_before, stream_dict


def test_probe_keyframe_before_falls_back_on_timeout() -> None:
    """C5: ffprobe stalling must not 500 — the docstring promises a fallback to
    the requested target, so a TimeoutExpired returns target unchanged."""
    import subprocess as sp

    def boom(*a, **k):
        raise sp.TimeoutExpired(cmd="ffprobe", timeout=10)

    with patch("quipclipper_web.media.subprocess.run", side_effect=boom):
        assert probe_keyframe_before(Path("/fake.mkv"), 42.0) == 42.0


def test_clean_title_uses_parent_folder():
    assert _clean_title(Path("/movies/The Sandlot (1993)/file.mkv")) == "The Sandlot (1993)"


def test_clean_title_steps_past_season_folder():
    assert _clean_title(Path("/shows/Star Trek - TNG/Season 4/ep.mkv")) == "Star Trek - TNG"


def test_slugify_drops_punctuation_keeps_apostrophe():
    assert _slugify("The Sandlot (1993)") == "The_Sandlot_1993"
    assert _slugify("You're killing me, Smalls!") == "You're_killing_me_Smalls"


def _audio(channels, layout):
    return StreamInfo(kind="audio", type_index=0, codec="eac3", language="eng",
                      title=None, channels=channels, channel_layout=layout)


def test_stream_dict_groups_only_from_recognized_layout():
    """B18: channel groups come only from a known layout tag, never guessed from
    the channel count — so the Channels dropdown only offers real channels."""
    # Recognized layout -> exact groups.
    assert stream_dict(_audio(6, "5.1(side)"))["groups"] == ["front", "side", "center", "lfe"]
    assert stream_dict(_audio(2, "stereo"))["groups"] == ["front"]  # length 1 -> UI hides it
    # Missing/unknown layout -> no groups (no count guess), so the dropdown hides.
    assert stream_dict(_audio(6, None))["groups"] == []
    assert stream_dict(_audio(6, "weird(layout)"))["groups"] == []


def test_stream_dict_surfaces_attached_pic():
    """Cover-art video (mp3/m4a) is flagged so the frontend doesn't mistake it for
    real video (treats the file as audio-only, no video clip/re-encode)."""
    art = StreamInfo(kind="video", type_index=0, codec="mjpeg", language=None,
                     title=None, channels=None, channel_layout=None, attached_pic=True)
    assert stream_dict(art)["attached_pic"] is True
    assert stream_dict(_audio(2, "stereo"))["attached_pic"] is False


# --- B9: clip name template -------------------------------------------------

def _ctx(stem: str, start=27 * 60 + 58.0, end=28 * 60 + 8.0, cue=""):
    """A template context built from a synthetic source filename."""
    return _clip_template_context(Path(f"/shows/Star Trek - TNG/{stem}.mkv"),
                                  ClipRange(start=start, end=end), cue)


def test_default_template_reproduces_phase2_layout():
    """The default must match the old hardcoded {source}/{timestamp}_{cue}_{title}."""
    ctx = _ctx("S03E26", start=1678.0, end=1690.0, cue="You're killing me, Smalls!")
    assert (_render_clip_template(DEFAULT_CLIP_TEMPLATE, ctx)
            == "S03E26/00-27-58_You're_killing_me_Smalls_Star_Trek_-_TNG")


def test_template_drops_absent_cue_and_collapses_separators():
    ctx = _ctx("movie", start=125.0, cue="")  # no cue (range/bookmark clip)
    assert _render_clip_template("{timestamp}_{cue}_{title}", ctx) == "00-02-05_Star_Trek_-_TNG"


def test_template_slash_makes_subfolders():
    ctx = _ctx("movie", cue="hello there")
    assert _render_clip_template("{title}/{year}/{cue}", _ctx("Movie.2009.x264", cue="hi")) \
        == "Star_Trek_-_TNG/2009/hi"


def test_template_tokens_year_season_episode_duration():
    ctx = _ctx("Show.S03E26.1999.1080p", start=10.0, end=22.5)
    assert ctx["season"] == "03"
    assert ctx["episode"] == "26"
    assert ctx["year"] == "1999"
    assert ctx["duration"] == "12"  # round(12.5) -> 12 (banker's rounding)
    assert ctx["end"] == "00-00-22"


def test_template_missing_year_token_dropped():
    ctx = _ctx("no_year_here", cue="")
    # {year} empty -> segment becomes just the timestamp, no stray separators
    assert _render_clip_template("{year}_{timestamp}", ctx) == "00-27-58"


def test_template_strips_traversal_segments():
    ctx = _ctx("movie")
    assert _render_clip_template("../../etc/{timestamp}", ctx) == "etc/00-27-58"
    assert _render_clip_template("/{timestamp}", ctx) == "00-27-58"


def test_template_empty_render_is_not_blank():
    """A template that resolves to nothing still yields a usable base (timestamp)."""
    ctx = _ctx("movie")
    assert _render_clip_template("{cue}", _ctx("movie", cue="")) == ""  # caller falls back

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


def test_cues_to_vtt_emits_speaker_voice_tag() -> None:
    cues = [Cue(0, 1.0, 3.0, "Hello, friends.", speaker="Chris"),
            Cue(1, 4.0, 5.5, "No speaker here.")]
    vtt = cues_to_vtt(cues)
    assert "<v Chris>Hello, friends." in vtt   # speaker → WebVTT voice tag
    assert "\nNo speaker here." in vtt          # untagged line unchanged


def test_cues_to_vtt_sanitizes_injection() -> None:
    """S3: cue text with a blank line, a '-->' arrow, or '<'/'&' must not break
    WebVTT parsing — blank lines are dropped, the arrow is defused, and the
    metacharacters are escaped."""
    cues = [Cue(0, 1.0, 3.0, "line one\n\n--> not a cue\n<b> & stuff")]
    vtt = cues_to_vtt(cues)
    body = vtt.split("00:00:01.000 --> 00:00:03.000\n", 1)[1]
    # The only real cue-timing arrow is the header we split on; the payload's
    # literal '-->' must have been neutralised.
    assert "\n-->" not in body
    assert "&amp;" in body and "&lt;b>" in body   # & and < escaped (> is fine in VTT)
    assert "\n\n" not in body.rstrip("\n")           # no blank line inside the cue


def _csrf(client: TestClient) -> TestClient:
    """Attach the CSRF header the frontend sends, so state-changing requests pass
    the _security_gate middleware. Tests for the gate itself construct a raw
    TestClient without this."""
    client.headers["X-Quipclipper"] = "1"
    return client


def _client(root: Path) -> TestClient:
    return _csrf(TestClient(create_app(Settings.from_env({"QC_MEDIA_ROOTS": str(root)}))))


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


def test_clip_audio_source_coerces_video_request_to_audio(tmp_path: Path) -> None:
    """A video clip request against an audio-only source (.mp3 + transcript) is
    coerced to an audio clip — the lossless audio path (mkvmerge), never the
    video re-encode (cut_clip)."""
    audio = tmp_path / "episode.mp3"
    audio.write_bytes(b"")
    (tmp_path / "episode.srt").write_text(SRT, encoding="utf-8")

    fake_out = tmp_path / "clip.mka"
    fake_out.write_bytes(b"fake clip data")

    client = _client(tmp_path)
    with (
        patch("quipclipper_web.app.mkvmerge_available", return_value=True),
        patch("quipclipper_web.app.cut_with_mkvmerge", return_value=fake_out) as mock_mkv,
        patch("quipclipper_web.app.cut_clip") as mock_ffmpeg,
    ):
        resp = client.post(
            "/api/clip",
            json={"path": str(audio), "query": "be back", "match_index": 0,
                  "kind": "video", "lossless": True, "backend": "auto"},
        )
        assert resp.status_code == 200
        job_id = resp.json()["job_id"]
        for _ in range(20):
            job = client.get(f"/api/jobs/{job_id}").json()
            if job["status"] in ("done", "failed"):
                break
            time.sleep(0.1)
    assert job["status"] == "done", job
    mock_mkv.assert_called_once()      # routed through the lossless audio copy
    mock_ffmpeg.assert_not_called()    # never the video re-encode


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


def test_clip_negative_match_index_rejected(tmp_path: Path) -> None:
    """C6: match_index must be >= 0 — a negative value would bypass the bounds
    check and IndexError (500). Validation now rejects it with 422."""
    video = tmp_path / "movie.mkv"
    video.write_bytes(b"")
    resp = _client(tmp_path).post(
        "/api/clip",
        json={"path": str(video), "query": "hi", "match_index": -1, "kind": "audio"},
    )
    assert resp.status_code == 422


def test_clip_negative_audio_track_rejected(tmp_path: Path) -> None:
    """C10: a negative audio_tracks index selects the last stream (codecs[-1]) or
    yields a bad ffmpeg a:N specifier — rejected with 422."""
    video = tmp_path / "movie.mkv"
    video.write_bytes(b"")
    resp = _client(tmp_path).post(
        "/api/clip",
        json={"path": str(video), "start": 1, "end": 2, "kind": "audio",
              "audio_tracks": [-1]},
    )
    assert resp.status_code == 422


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


def test_clip_audio_format_wav_is_fullmix_reencode(tmp_path: Path) -> None:
    """audio_format=wav (audio, no split) routes to cut_clip with audio_codec
    set and a .wav output — a full-mix lossless re-encode (keeps 5.1)."""
    video = tmp_path / "movie.mkv"
    video.write_bytes(b"")
    fake_out = tmp_path / "clip.wav"
    fake_out.write_bytes(b"x")

    client = _client(tmp_path)
    with patch("quipclipper_web.app.cut_clip", return_value=fake_out) as mock_cut:
        resp = client.post("/api/clip", json={
            "path": str(video), "start": 10, "end": 12,
            "kind": "audio", "lossless": False, "audio_format": "wav",
            "backend": "auto",
        })
        assert resp.status_code == 200
        job = _wait_done(client, resp.json()["job_id"])
        assert job["status"] == "done"
        kwargs = mock_cut.call_args.kwargs
    assert kwargs["audio_codec"] == "wav"
    assert str(kwargs["out"]).endswith(".wav")


def test_clip_split_groups_reach_splitter(tmp_path: Path) -> None:
    """split_groups from the request is passed through to split_audio_channels
    as `categories` so only the chosen channel groups are exported."""
    video = tmp_path / "movie.mkv"
    video.write_bytes(b"")
    out = tmp_path / "movie_front.wav"
    out.write_bytes(b"x")

    client = _client(tmp_path)
    with patch("quipclipper_web.app.split_audio_channels", return_value=[out]) as mock_split:
        resp = client.post("/api/clip", json={
            "path": str(video), "start": 10, "end": 12,
            "kind": "audio", "split_channels": True, "split_format": "wav",
            "split_groups": ["center", "surround"],
        })
        assert resp.status_code == 200
        job = _wait_done(client, resp.json()["job_id"])
        assert job["status"] == "done"
        assert mock_split.call_args.kwargs["categories"] == ["center", "surround"]


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


def _clips_client(tmp_path: Path):
    """Client + (media, clips) dirs for clip-output tests."""
    media = tmp_path / "media"
    media.mkdir()
    clips = tmp_path / "clips"
    clips.mkdir()
    client = _csrf(TestClient(create_app(Settings.from_env({
        "QC_MEDIA_ROOTS": str(media),
        "QC_CLIPS_DIR": str(clips),
        "QC_STATE_DIR": str(tmp_path / "state"),
    }))))
    return client, media, clips


def test_clip_search_filed_in_subfolder_with_template(tmp_path: Path) -> None:
    """A dialogue-search clip lands in clips/<source stem>/ named
    {timestamp}_{cue text}_{clean name}.ext (clean name = parent folder) — B5/B7."""
    client, media, clips = _clips_client(tmp_path)
    video = media / "movie.mkv"
    video.write_bytes(b"")
    (media / "movie.srt").write_text(SRT, encoding="utf-8")
    fake_out = clips / "movie" / "out.mkv"
    fake_out.parent.mkdir(parents=True)
    fake_out.write_bytes(b"x")

    with patch("quipclipper_web.app.cut_clip", return_value=fake_out) as mock_cut:
        resp = client.post("/api/clip", json={
            "path": str(video), "query": "be back", "kind": "video",
            "lossless": True, "backend": "ffmpeg", "before": 0, "after": 0,
        })
        assert resp.status_code == 200
        assert _wait_done(client, resp.json()["job_id"])["status"] == "done"
        out = Path(mock_cut.call_args.kwargs["out"])
    assert out.parent == clips / "movie"        # always a per-source subfolder
    assert out.name.startswith("00-00-01_")     # whole-second timestamp (cue at 1.0)
    assert out.name.endswith("_media.mkv")      # clean name (parent folder) last
    assert "back" in out.name.lower()           # cue text included


def test_clip_range_filename_drops_absent_cue_text(tmp_path: Path) -> None:
    """A range clip has no cue text, so the filename is just {timestamp}_{clean}."""
    client, media, clips = _clips_client(tmp_path)
    video = media / "movie.mkv"
    video.write_bytes(b"")
    fake_out = clips / "movie" / "out.mka"
    fake_out.parent.mkdir(parents=True)
    fake_out.write_bytes(b"x")

    with patch("quipclipper_web.app.cut_clip", return_value=fake_out) as mock_cut:
        resp = client.post("/api/clip", json={
            "path": str(video), "start": 65, "end": 70, "before": 0, "after": 0,
            "kind": "audio", "lossless": True, "backend": "ffmpeg",
        })
        assert resp.status_code == 200
        assert _wait_done(client, resp.json()["job_id"])["status"] == "done"
        out = Path(mock_cut.call_args.kwargs["out"])
    assert out.parent == clips / "movie"
    assert out.name == "00-01-05_media.mka"


def test_clip_range_with_cue_text_names_the_file(tmp_path: Path) -> None:
    """A start/end clip can carry the selected cue's dialogue (cue_text) so the
    {cue} token is filled — the web UI sends it for dialogue-search hits and
    manual Start/End selections, matching the query-based path's naming."""
    client, media, clips = _clips_client(tmp_path)
    video = media / "movie.mkv"
    video.write_bytes(b"")
    fake_out = clips / "movie" / "out.mka"
    fake_out.parent.mkdir(parents=True)
    fake_out.write_bytes(b"x")

    with patch("quipclipper_web.app.cut_clip", return_value=fake_out) as mock_cut:
        resp = client.post("/api/clip", json={
            "path": str(video), "start": 65, "end": 70, "before": 0, "after": 0,
            "cue_text": "Hasta la vista", "kind": "audio",
            "lossless": True, "backend": "ffmpeg",
        })
        assert resp.status_code == 200
        assert _wait_done(client, resp.json()["job_id"])["status"] == "done"
        out = Path(mock_cut.call_args.kwargs["out"])
    assert out.name == "00-01-05_Hasta_la_vista_media.mka"


def test_video_lossless_uses_ffmpeg_for_exact_end(tmp_path: Path) -> None:
    """A lossless video clip must cut with ffmpeg (exact end), not mkvmerge —
    mkvmerge can only end a kept range on a keyframe, bloating short clips on
    long-GOP sources (a 3s line became 22s). Audio passthrough stays mkvmerge."""
    client, media, clips = _clips_client(tmp_path)
    video = media / "movie.mkv"
    video.write_bytes(b"")
    out = clips / "movie" / "out.mkv"
    out.parent.mkdir(parents=True)
    out.write_bytes(b"x")
    with patch("quipclipper_web.app.mkvmerge_available", return_value=True), \
         patch("quipclipper_web.app.cut_with_mkvmerge") as mkv, \
         patch("quipclipper_web.app.cut_clip", return_value=out) as ff:
        resp = client.post("/api/clip", json={
            "path": str(video), "start": 65, "end": 70, "before": 0, "after": 0,
            "kind": "video", "lossless": True, "backend": "auto",
        })
        assert resp.status_code == 200
        assert _wait_done(client, resp.json()["job_id"])["status"] == "done"
    ff.assert_called_once()       # ffmpeg path
    mkv.assert_not_called()       # not mkvmerge


def test_video_reencode_uses_hardware_encoder_when_available(tmp_path: Path) -> None:
    """A re-encoded video clip (lossless=False) hardware-encodes on the iGPU when
    VAAPI is available — the bug was it always emitted libx264."""
    client, media, clips = _clips_client(tmp_path)
    video = media / "movie.mkv"
    video.write_bytes(b"")
    out = clips / "movie" / "out.mp4"
    out.parent.mkdir(parents=True)
    out.write_bytes(b"x")
    with patch("quipclipper_web.app._vaapi_h264_available", return_value=True), \
         patch("quipclipper_web.app.cut_clip", return_value=out) as cc:
        resp = client.post("/api/clip", json={
            "path": str(video), "start": 5, "end": 8, "before": 0, "after": 0,
            "kind": "video", "lossless": False, "backend": "auto",
        })
        assert resp.status_code == 200
        assert _wait_done(client, resp.json()["job_id"])["status"] == "done"
    kw = cc.call_args.kwargs
    assert kw["video_encoder"] == "h264_vaapi"
    assert kw["vaapi_device"]  # the configured render node


def test_video_reencode_uses_software_without_hardware(tmp_path: Path) -> None:
    client, media, clips = _clips_client(tmp_path)
    video = media / "movie.mkv"
    video.write_bytes(b"")
    out = clips / "movie" / "out.mp4"
    out.parent.mkdir(parents=True)
    out.write_bytes(b"x")
    with patch("quipclipper_web.app._vaapi_h264_available", return_value=False), \
         patch("quipclipper_web.app.cut_clip", return_value=out) as cc:
        resp = client.post("/api/clip", json={
            "path": str(video), "start": 5, "end": 8, "before": 0, "after": 0,
            "kind": "video", "lossless": False, "backend": "auto",
        })
        assert resp.status_code == 200
        assert _wait_done(client, resp.json()["job_id"])["status"] == "done"
    assert cc.call_args.kwargs["video_encoder"] == "libx264"


def test_audio_lossless_still_uses_mkvmerge(tmp_path: Path) -> None:
    client, media, clips = _clips_client(tmp_path)
    video = media / "movie.mkv"
    video.write_bytes(b"")
    out = clips / "movie" / "out.mka"
    out.parent.mkdir(parents=True)
    out.write_bytes(b"x")
    with patch("quipclipper_web.app.mkvmerge_available", return_value=True), \
         patch("quipclipper_web.app.cut_with_mkvmerge", return_value=out) as mkv, \
         patch("quipclipper_web.app.cut_clip") as ff:
        resp = client.post("/api/clip", json={
            "path": str(video), "start": 65, "end": 70, "before": 0, "after": 0,
            "kind": "audio", "lossless": True, "backend": "auto",
        })
        assert resp.status_code == 200
        assert _wait_done(client, resp.json()["job_id"])["status"] == "done"
    mkv.assert_called_once()
    ff.assert_not_called()


def test_clip_custom_template_controls_path(tmp_path: Path) -> None:
    """A custom template drives both the subfolder(s) and filename."""
    client, media, clips = _clips_client(tmp_path)
    video = media / "movie.mkv"
    video.write_bytes(b"")
    fake_out = clips / "x.mka"
    fake_out.write_bytes(b"x")
    with patch("quipclipper_web.app.cut_clip", return_value=fake_out) as mock_cut:
        resp = client.post("/api/clip", json={
            "path": str(video), "start": 65, "end": 70, "before": 0, "after": 0,
            "kind": "audio", "lossless": True, "backend": "ffmpeg",
            "template": "{title}/clips/{timestamp}-{end}",
        })
        assert resp.status_code == 200
        assert _wait_done(client, resp.json()["job_id"])["status"] == "done"
        out = Path(mock_cut.call_args.kwargs["out"])
    assert out == clips / "media" / "clips" / "00-01-05-00-01-10.mka"


def test_clip_template_traversal_rejected(tmp_path: Path) -> None:
    """A template that escapes the clips dir is refused (defense in depth)."""
    client, media, _clips = _clips_client(tmp_path)
    video = media / "movie.mkv"
    video.write_bytes(b"")
    # Token values are slugified (no '/'), so traversal can only come from
    # literal template text; '..' segments are stripped, but assert the job
    # still succeeds and stays contained rather than 500-ing.
    def _fake_cut(*a, **k):
        out = Path(k["out"])
        out.write_bytes(b"x")
        return out

    with patch("quipclipper_web.app.cut_clip", side_effect=_fake_cut) as mock_cut:
        resp = client.post("/api/clip", json={
            "path": str(video), "start": 1, "end": 2, "before": 0, "after": 0,
            "kind": "audio", "lossless": True, "backend": "ffmpeg",
            "template": "../../../{timestamp}",
        })
        assert resp.status_code == 200
        assert _wait_done(client, resp.json()["job_id"])["status"] == "done"
        out = Path(mock_cut.call_args.kwargs["out"]).resolve()
    assert out.is_relative_to(_clips.resolve())


def test_clip_uniquifies_existing_target(tmp_path: Path) -> None:
    """R6: a second export resolving to the same path must not clobber the first —
    the target gets a _2 suffix (engine cuts with ffmpeg -y)."""
    client, media, clips = _clips_client(tmp_path)
    video = media / "movie.mkv"
    video.write_bytes(b"")
    # A clip already sitting at the default target path.
    existing = clips / "movie" / "00-01-05_media.mka"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"old")

    def _fake_cut(*a, **k):
        out = Path(k["out"])
        out.write_bytes(b"new")
        return out

    with patch("quipclipper_web.app.cut_clip", side_effect=_fake_cut) as mock_cut:
        resp = client.post("/api/clip", json={
            "path": str(video), "start": 65, "end": 70, "before": 0, "after": 0,
            "kind": "audio", "lossless": True, "backend": "ffmpeg",
        })
        assert resp.status_code == 200
        assert _wait_done(client, resp.json()["job_id"])["status"] == "done"
        out = Path(mock_cut.call_args.kwargs["out"])
    assert out.name == "00-01-05_media_2.mka"   # suffixed, original untouched
    assert existing.read_bytes() == b"old"


def test_clip_split_passes_extensionless_base(tmp_path: Path) -> None:
    """For split-channels, the splitter gets the base WITHOUT extension so it can
    append '_<channel>.<ext>' (no doubled slug / mid-name extension)."""
    client, media, clips = _clips_client(tmp_path)
    video = media / "movie.mkv"
    video.write_bytes(b"")
    out = clips / "movie" / "00-01-05_media_front.wav"
    out.parent.mkdir(parents=True)
    out.write_bytes(b"x")
    with patch("quipclipper_web.app.split_audio_channels", return_value=[out]) as mock_split:
        resp = client.post("/api/clip", json={
            "path": str(video), "start": 65, "end": 70, "before": 0, "after": 0,
            "kind": "audio", "split_channels": True, "split_format": "wav",
        })
        assert resp.status_code == 200
        assert _wait_done(client, resp.json()["job_id"])["status"] == "done"
        base = Path(mock_split.call_args.kwargs["out"])
    assert base == clips / "movie" / "00-01-05_media"   # no extension, no channel yet


def test_clips_library_hides_empty_subfolders(tmp_path: Path) -> None:
    """Subfolders with no clips (e.g. left after deleting their clips) are
    hidden from the library — B8."""
    client, media, clips = _clips_client(tmp_path)
    (clips / "empty").mkdir()                     # no clips -> hidden
    full = clips / "full"
    full.mkdir()
    (full / "clip.mka").write_bytes(b"x")         # has a clip -> shown
    data = client.get("/api/clips").json()
    assert data["folders"] == ["full"]


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
    old = reg.submit(lambda _job: [], label="old")
    # Wait for it to finish, then age it past the cutoff.
    for _ in range(50):
        if old.finished is not None:
            break
        time_mod.sleep(0.05)
    assert old.finished is not None
    old.finished -= JobRegistry.MAX_FINISHED_AGE + 1

    fresh = reg.submit(lambda _job: [], label="fresh")
    assert reg.get(old.id) is None
    assert reg.get(fresh.id) is not None
    reg.shutdown()


def test_job_cancel_queued_and_prune_finished() -> None:
    """R2: a queued job can be cancelled; a finished one can be pruned."""
    import threading
    import time as time_mod

    from quipclipper_web.jobs import JobRegistry, Status

    reg = JobRegistry(max_workers=1)
    # Occupy the single worker so the next job stays queued.
    gate = threading.Event()
    busy = reg.submit(lambda _job: (gate.wait(5), [])[1], label="busy")
    for _ in range(50):
        if busy.status == Status.running:
            break
        time_mod.sleep(0.02)

    queued = reg.submit(lambda _job: [], label="queued")
    assert queued.status == Status.queued
    assert reg.cancel(queued.id) is True
    assert reg.get(queued.id).status == Status.cancelled

    # A running job can't be cancelled from here.
    assert reg.cancel(busy.id) is False
    gate.set()
    for _ in range(50):
        if busy.finished is not None:
            break
        time_mod.sleep(0.02)
    # Finished job is pruned by cancel.
    assert reg.cancel(busy.id) is True
    assert reg.get(busy.id) is None
    reg.shutdown()


def test_cancel_job_endpoint(tmp_path: Path) -> None:
    """R2: DELETE /api/jobs/{id} cancels/prunes; 404 for unknown."""
    client = _client(tmp_path)
    assert client.delete("/api/jobs/nope").status_code == 404

    # A mocked instant job finishes, then DELETE prunes it (200).
    video = tmp_path / "movie.mkv"
    video.write_bytes(b"")
    (tmp_path / "movie.srt").write_text(SRT, encoding="utf-8")
    fake_out = tmp_path / "clip.mkv"
    fake_out.write_bytes(b"x")
    with patch("quipclipper_web.app.cut_clip", return_value=fake_out):
        r = client.post("/api/clip", json={
            "path": str(video), "query": "be back", "kind": "video",
            "lossless": True, "backend": "ffmpeg",
        })
    job_id = r.json()["job_id"]
    _wait_done(client, job_id)
    assert client.delete(f"/api/jobs/{job_id}").status_code == 200
    assert client.get(f"/api/jobs/{job_id}").status_code == 404


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


def test_bookmark_stores_cue_for_export_naming(tmp_path: Path) -> None:
    """A bookmark keeps its matched dialogue so exporting it later fills the
    {cue} naming token (it has no cue selected in the script at export time)."""
    video = tmp_path / "movie.mkv"
    video.write_bytes(b"")
    client = _client(tmp_path)
    resp = client.post("/api/bookmarks", json={
        "path": str(video), "label": "L", "start": 10.0, "end": 13.0,
        "cue": "Hasta la vista",
    })
    assert resp.status_code == 200
    assert resp.json()["cue"] == "Hasta la vista"
    got = client.get("/api/bookmarks", params={"path": str(video)}).json()["bookmarks"]
    assert got[0]["cue"] == "Hasta la vista"


def test_bookmark_tolerates_unknown_stored_key(tmp_path: Path) -> None:
    """C7: a stored record with an extra key (newer version / hand-edit) must not
    crash the list/get — unknown keys are dropped, not passed to the constructor."""
    import json as _json

    from quipclipper_web.bookmarks import BookmarkStore

    store = BookmarkStore(tmp_path)
    # Write a record carrying a field the dataclass doesn't know about.
    record = {
        "id": "abc123", "path": "/m/movie.mkv", "label": "L",
        "start": 1.0, "end": 2.0, "created": "2026-01-01T00:00:00+00:00",
        "future_field": "surprise",
    }
    (tmp_path / "bookmarks.json").write_text(_json.dumps([record]), encoding="utf-8")

    got = store.list_all()
    assert len(got) == 1 and got[0].label == "L"      # loaded, not TypeError
    assert store.get("abc123").id == "abc123"
    assert store.list_for_path("/m/movie.mkv")[0].id == "abc123"


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


def test_bookmark_stores_and_patches_buffer(tmp_path: Path) -> None:
    """A bookmark persists its before/after buffer (B16), defaulting to 0, and
    PATCH can adjust it."""
    video = tmp_path / "movie.mkv"
    video.write_bytes(b"")
    client = _client(tmp_path)
    client.delete("/api/bookmarks")  # clean slate (state dir is shared in tests)

    # No buffer given -> defaults to 0 (back-compat with old payloads).
    bm = client.post("/api/bookmarks", json={
        "path": str(video), "label": "a", "start": 10, "end": 12,
    }).json()
    assert bm["before"] == 0 and bm["after"] == 0

    # With a buffer.
    bm = client.post("/api/bookmarks", json={
        "path": str(video), "label": "b", "start": 30, "end": 32,
        "before": 1.5, "after": 2.0,
    }).json()
    assert bm["before"] == 1.5 and bm["after"] == 2.0

    # PATCH adjusts only the given fields.
    patched = client.patch(f"/api/bookmarks/{bm['id']}", json={"after": 3.5}).json()
    assert patched["before"] == 1.5 and patched["after"] == 3.5

    assert client.patch("/api/bookmarks/nope", json={"after": 1}).status_code == 404


def test_bookmark_clear_all(tmp_path: Path) -> None:
    video = tmp_path / "movie.mkv"
    video.write_bytes(b"")
    client = _client(tmp_path)
    client.delete("/api/bookmarks")  # clean slate (state dir is shared in tests)
    for i in range(3):
        client.post("/api/bookmarks", json={
            "path": str(video), "label": f"b{i}", "start": i, "end": i + 1,
        })
    resp = client.delete("/api/bookmarks")
    assert resp.status_code == 200 and resp.json()["deleted"] == 3
    assert client.get("/api/bookmarks").json()["bookmarks"] == []


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


def test_audio_clip_served_with_audio_mime(tmp_path: Path) -> None:
    """C9: audio files must stream with a real audio Content-Type (not
    application/octet-stream, which iOS Safari refuses)."""
    clips = tmp_path / "clips"
    clips.mkdir()
    (clips / "clip.mp3").write_bytes(b"x")
    (clips / "clip.flac").write_bytes(b"y")
    client = TestClient(create_app(Settings.from_env({
        "QC_MEDIA_ROOTS": str(tmp_path), "QC_CLIPS_DIR": str(clips),
    })))
    assert client.get("/api/clips/stream/clip.mp3").headers["content-type"] == "audio/mpeg"
    assert client.get("/api/clips/stream/clip.flac").headers["content-type"] == "audio/flac"


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


# --- HLS ---
#
# Desktop plays through this path too (via hls.js), so the stream-selector
# options the transcode endpoint accepts have to work here as well. ffmpeg is
# never launched: the fake below records the command and writes the playlist +
# first segment the route waits for.


class _FakeProc:
    """Stands in for the ffmpeg subprocess — never exits, so the route treats
    the transcode as still running."""

    returncode = None

    def kill(self) -> None:  # pragma: no cover - registry cleanup only
        pass


def _fake_ffmpeg(recorded: list[list[str]]):
    async def _spawn(*cmd: str, **_kw) -> _FakeProc:
        recorded.append(list(cmd))
        seg = Path(cmd[cmd.index("-hls_segment_filename") + 1])
        d = seg.parent
        d.mkdir(parents=True, exist_ok=True)
        (d / "seg00000.m4s").write_bytes(b"")
        (d / "index.m3u8").write_text(
            "#EXTM3U\n#EXT-X-MAP:URI=\"init.mp4\"\n"
            "#EXTINF:6.0,\nseg00000.m4s\n#EXT-X-ENDLIST\n"
        )
        return _FakeProc()

    return _spawn


def test_hls_forbids_outside(tmp_path: Path) -> None:
    client = _client(tmp_path)
    resp = client.get("/api/media/hls", params={"path": "/etc/passwd"})
    assert resp.status_code == 403


def test_hls_not_found(tmp_path: Path) -> None:
    client = _client(tmp_path)
    resp = client.get("/api/media/hls", params={"path": str(tmp_path / "nope.mkv")})
    assert resp.status_code == 404


def test_hls_maps_selected_audio_stream(tmp_path: Path) -> None:
    """`audio=N` segments that stream instead of ffmpeg's default pick (B17)."""
    video = tmp_path / "movie.mkv"
    video.write_bytes(b"")
    client = _client(tmp_path)
    cmds: list[list[str]] = []

    with patch("asyncio.create_subprocess_exec", _fake_ffmpeg(cmds)):
        resp = client.get("/api/media/hls", params={"path": str(video), "audio": 1})

    assert resp.status_code == 200
    assert "-map" in cmds[0] and "0:a:1" in cmds[0]


def test_hls_channel_subset_applies_pan(tmp_path: Path) -> None:
    """`chan` downmixes one channel group, and leaves the layout to the pan
    filter rather than forcing -ac 2 on top of it."""
    video = tmp_path / "movie.mkv"
    video.write_bytes(b"")
    client = _client(tmp_path)
    cmds: list[list[str]] = []

    with (
        patch("asyncio.create_subprocess_exec", _fake_ffmpeg(cmds)),
        patch("quipclipper_web.app._live_pan_filter", return_value="pan=mono|c0=c2"),
    ):
        resp = client.get(
            "/api/media/hls", params={"path": str(video), "chan": "center"}
        )

    assert resp.status_code == 200
    assert cmds[0][cmds[0].index("-filter:a") + 1] == "pan=mono|c0=c2"
    assert "-ac" not in cmds[0]


def test_hls_times_out_with_a_clean_504(tmp_path: Path) -> None:
    """ffmpeg that never produces a segment gives up with a 504 naming the
    limit, rather than hanging until a proxy cuts the connection."""
    video = tmp_path / "movie.mkv"
    video.write_bytes(b"")
    client = _client(tmp_path)

    async def _never_starts(*_cmd: str, **_kw) -> _FakeProc:
        return _FakeProc()  # writes no playlist, no segments

    with (
        patch("asyncio.create_subprocess_exec", _never_starts),
        patch("quipclipper_web.app._HLS_START_TIMEOUT", 0.3),
    ):
        resp = client.get("/api/media/hls", params={"path": str(video)})

    assert resp.status_code == 504
    assert "did not start within" in resp.json()["detail"]


def test_hls_token_separates_audio_selections(tmp_path: Path) -> None:
    """Two selections must segment into different dirs — otherwise one would be
    served the other's segments, or overwrite them mid-stream."""
    video = tmp_path / "movie.mkv"
    video.write_bytes(b"")
    client = _client(tmp_path)
    cmds: list[list[str]] = []

    with patch("asyncio.create_subprocess_exec", _fake_ffmpeg(cmds)):
        first = client.get("/api/media/hls", params={"path": str(video), "audio": 0})
        second = client.get("/api/media/hls", params={"path": str(video), "audio": 1})

    assert first.status_code == second.status_code == 200
    assert len(cmds) == 2  # the second didn't reuse the first's transcode
    assert first.text != second.text  # different token in the rewritten URIs


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


def test_folder_dialogue_search_skips_unparseable_subs(tmp_path: Path) -> None:
    """One file whose subtitles can't be parsed must not fail the whole search —
    it's skipped and matches from the other files still come back (HTTP 200).

    Regression: an unparseable sidecar/embedded track raised pysubs2's
    FormatAutodetectionError, which escaped _search_one's except tuple and 500'd
    the entire /api/search/folder request.
    """
    good = tmp_path / "good.mkv"
    good.write_bytes(b"")
    (tmp_path / "good.srt").write_text(SRT)

    # A sidecar that exists but is empty/garbage → pysubs2 can't autodetect it.
    bad = tmp_path / "bad.mkv"
    bad.write_bytes(b"")
    (tmp_path / "bad.srt").write_text("\x00\x01 not a subtitle file at all")

    client = _client(tmp_path)
    resp = client.get(
        "/api/search/folder",
        params={"path": str(tmp_path), "query": "back"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["files_scanned"] == 2          # both scanned
    assert data["count"] >= 1                   # the good file still matched
    assert all(m["file"] == "good.mkv" for m in data["matches"])


def test_folder_dialogue_search_capped(tmp_path: Path) -> None:
    """R3: an oversized folder scan stops at INDEX_CAP and reports capped=True so
    the UI can tell the user results are partial."""
    for i in range(5):
        (tmp_path / f"ep{i}.mkv").write_bytes(b"")
        (tmp_path / f"ep{i}.srt").write_text(SRT)
    client = _client(tmp_path)
    with patch("quipclipper_web.app.INDEX_CAP", 2):
        resp = client.get("/api/search/folder", params={"path": str(tmp_path), "query": "back"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["capped"] is True
    assert data["files_scanned"] <= 2


def test_folder_dialogue_search_not_capped_when_small(tmp_path: Path) -> None:
    (tmp_path / "ep.mkv").write_bytes(b"")
    (tmp_path / "ep.srt").write_text(SRT)
    resp = _client(tmp_path).get(
        "/api/search/folder", params={"path": str(tmp_path), "query": "back"}
    )
    assert resp.json()["capped"] is False


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


def test_recursive_scan_skips_appledouble_files(tmp_path: Path) -> None:
    """macOS ._* sidecars (and other dotfiles) must not be counted as videos —
    otherwise they inflate the index count and can never be indexed."""
    season = tmp_path / "Season 1"
    season.mkdir()
    (season / "episode1.mkv").write_bytes(b"")
    (season / "episode1.srt").write_text(SRT)
    # AppleDouble junk: same name/extension, not a real video.
    (season / "._episode1.mkv").write_bytes(b"\x00\x05\x16\x07")
    (tmp_path / ".hidden.mkv").write_bytes(b"")

    client = _client(tmp_path)
    # Folder dialogue search scans only the one real video.
    resp = client.get("/api/search/folder", params={"path": str(tmp_path), "query": "force"})
    assert resp.status_code == 200
    assert resp.json()["files_scanned"] == 1
    # index-status counts only the real video (total = 1, fully indexable).
    status = client.get("/api/search/folder/index-status", params={"path": str(tmp_path)})
    assert status.status_code == 200
    assert status.json()["total"] == 1


# --- EPUB3 media-overlay audiobooks (web) ------------------------------------

import zipfile  # noqa: E402

_EPUB_AUDIO = b"FAKE-AAC-PAYLOAD-" + b"\x00\x01\x02\x03" * 64

_EPUB_FILES = {
    "mimetype": "application/epub+zip",
    "META-INF/container.xml": (
        '<?xml version="1.0"?><container version="1.0"'
        ' xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
        '<rootfile full-path="OEBPS/content.opf"'
        ' media-type="application/oebps-package+xml"/></rootfiles></container>'
    ),
    "OEBPS/content.opf": (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        '<dc:title>The Test Book</dc:title><dc:creator>Ada</dc:creator></metadata>'
        '<manifest>'
        '<item id="c1" href="c1.xhtml" media-type="application/xhtml+xml" media-overlay="m1"/>'
        '<item id="m1" href="c1.smil" media-type="application/smil+xml"/>'
        '<item id="a1" href="audio/a1.mp4" media-type="audio/mp4"/>'
        '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
        '</manifest><spine><itemref idref="c1"/></spine></package>'
    ),
    "OEBPS/toc.ncx": (
        '<?xml version="1.0"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">'
        '<navMap><navPoint id="n1"><navLabel><text>The Assimilation</text></navLabel>'
        '<content src="c1.xhtml"/></navPoint></navMap></ncx>'
    ),
    "OEBPS/c1.xhtml": (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<html xmlns="http://www.w3.org/1999/xhtml"><body><h1 id="h">Chapter One</h1>'
        '<p><span id="s0">You will become one with the Borg.</span>'
        '<span id="s1">Resistance is futile.</span></p></body></html>'
    ),
    "OEBPS/c1.smil": (
        '<?xml version="1.0"?><smil xmlns="http://www.w3.org/ns/SMIL"'
        ' xmlns:epub="http://www.idpf.org/2007/ops" version="3.0"><body>'
        '<seq id="s" epub:textref="c1.xhtml" epub:type="chapter">'
        '<par id="p0"><text src="c1.xhtml#s0"/>'
        '<audio src="audio/a1.mp4" clipBegin="0.000s" clipEnd="3.500s"/></par>'
        '<par id="p1"><text src="c1.xhtml#s1"/>'
        '<audio src="audio/a1.mp4" clipBegin="3.500s" clipEnd="5.000s"/></par>'
        '</seq></body></smil>'
    ),
}


def _build_mo_epub(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        for name, data in _EPUB_FILES.items():
            z.writestr(name, data)
        z.writestr("OEBPS/audio/a1.mp4", _EPUB_AUDIO)
    return path


def test_browse_lists_epub_as_book(tmp_path: Path) -> None:
    _build_mo_epub(tmp_path / "book.epub")
    entries = _client(tmp_path).get("/api/library/browse", params={"path": str(tmp_path)}).json()["entries"]
    books = [e for e in entries if e.get("is_book")]
    assert len(books) == 1 and books[0]["name"] == "book.epub"


def test_browse_book_lists_audio_segments(tmp_path: Path) -> None:
    epub = _build_mo_epub(tmp_path / "book.epub")
    segs = _client(tmp_path).get("/api/library/browse", params={"path": str(epub)}).json()["entries"]
    assert len(segs) == 1  # one audio member -> one segment (both chapters fold in)
    assert segs[0]["is_audio"] and segs[0]["name"] == "The Assimilation"
    assert segs[0]["path"].endswith("#seg=0")


def test_segment_subtitles_and_search(tmp_path: Path) -> None:
    epub = _build_mo_epub(tmp_path / "book.epub")
    client = _client(tmp_path)
    ref = str(epub) + "#seg=0"
    cues = client.get("/api/items/subtitles", params={"path": ref, "fmt": "json"}).json()
    assert [c["text"] for c in cues] == ["You will become one with the Borg.", "Resistance is futile."]
    hits = client.get("/api/search", params={"path": ref, "query": "resistance is futile"}).json()
    assert hits["count"] >= 1


def test_folder_search_finds_books_in_a_folder(tmp_path: Path) -> None:
    """Folder dialogue search sweeps EPUB books too, with hits pointing at the
    chapter (segment ref) so opening one lands on the line."""
    _build_mo_epub(tmp_path / "book.epub")
    r = _client(tmp_path).get(
        "/api/search/folder", params={"path": str(tmp_path), "query": "resistance is futile"},
    ).json()
    assert r["files_scanned"] == 1 and r["count"] >= 1
    hit = next(h for h in r["matches"] if "Resistance is futile." in h["text"])
    assert hit["path"].endswith("#seg=0")  # points at the chapter
    assert "The Test Book" in hit["file"]


def test_folder_search_targets_a_book_directly(tmp_path: Path) -> None:
    """`/api/search/folder` accepts an EPUB book as its target (not just a dir)."""
    epub = _build_mo_epub(tmp_path / "book.epub")
    r = _client(tmp_path).get(
        "/api/search/folder", params={"path": str(epub), "query": "become one with the borg"},
    ).json()
    assert r["files_scanned"] == 1
    assert any(h["path"].endswith("#seg=0") for h in r["matches"])


def test_segment_media_streams_from_zip(tmp_path: Path) -> None:
    epub = _build_mo_epub(tmp_path / "book.epub")
    r = _client(tmp_path).get("/api/media", params={"path": str(epub) + "#seg=0"})
    assert r.status_code == 200 and r.content == _EPUB_AUDIO
    assert r.headers["accept-ranges"] == "bytes"
    # No copy was written to the state dir (streamed straight from the zip).
    assert not (tmp_path / "state" / "epub_audio").exists()


def test_segment_media_supports_range(tmp_path: Path) -> None:
    epub = _build_mo_epub(tmp_path / "book.epub")
    r = _client(tmp_path).get(
        "/api/media", params={"path": str(epub) + "#seg=0"}, headers={"Range": "bytes=10-19"},
    )
    assert r.status_code == 206
    assert r.content == _EPUB_AUDIO[10:20]
    assert r.headers["content-range"] == f"bytes 10-19/{len(_EPUB_AUDIO)}"


def test_segment_item_info_is_synthesized(tmp_path: Path) -> None:
    epub = _build_mo_epub(tmp_path / "book.epub")
    ref = str(epub) + "#seg=0"
    # No ffprobe / extraction — item_info is built from the manifest + cue timings.
    info = _client(tmp_path).get("/api/items", params={"path": ref}).json()
    assert info["name"] == "The Assimilation"
    assert info["book_title"] == "The Test Book"
    assert info["path"].endswith("#seg=0")
    assert [s["kind"] for s in info["streams"]] == ["audio"]
    assert info["streams"][0]["codec"] == "aac"
    assert info["duration"] == 5.0  # last cue end
    assert not (tmp_path / "state" / "epub_audio").exists()


def test_clip_epub_temp_cleaned_on_presubmit_error(tmp_path: Path) -> None:
    """R1: a pre-submit 4xx (here: neither start nor query) must not leak the
    extracted EPUB temp — do_cut's finally only runs once the job is queued."""
    epub = _build_mo_epub(tmp_path / "book.epub")
    client = _client(tmp_path)
    ref = str(epub) + "#seg=0"

    created: list[Path] = []
    real_extract = epub_items_mod.extract_segment_temp

    def _tracking_extract(ep, seg):
        p = real_extract(ep, seg)
        created.append(Path(p))
        return p

    with patch("quipclipper_web.app.epub_items.extract_segment_temp",
               side_effect=_tracking_extract):
        # No start and no query -> 400 after the temp is already extracted.
        resp = client.post("/api/clip", json={"path": ref, "kind": "audio"})
    assert resp.status_code == 400
    assert created and not created[0].exists()   # temp was cleaned up


def test_clip_from_segment_names_by_book(tmp_path: Path) -> None:
    epub = _build_mo_epub(tmp_path / "book.epub")
    client = _client(tmp_path)
    ref = str(epub) + "#seg=0"
    captured = {}

    def fake_mkv(source, rng, **kw):
        captured["source"] = source
        out = kw["out"]
        out = out.with_suffix(".mka") if out.suffix == "" else out
        captured["out"] = out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"clip")
        return out

    with (
        patch("quipclipper_web.app.mkvmerge_available", return_value=True),
        patch("quipclipper_web.app.cut_with_mkvmerge", side_effect=fake_mkv),
        patch("quipclipper_web.app.cut_clip") as mock_ffmpeg,
    ):
        resp = client.post("/api/clip", json={
            "path": ref, "start": 0.0, "end": 3.5, "kind": "video", "lossless": True,
            "backend": "auto", "cue_text": "You will become one with the Borg.",
        })
        assert resp.status_code == 200
        job_id = resp.json()["job_id"]
        for _ in range(20):
            job = client.get(f"/api/jobs/{job_id}").json()
            if job["status"] in ("done", "failed"):
                break
            time.sleep(0.1)
    assert job["status"] == "done", job
    mock_ffmpeg.assert_not_called()  # forced audio -> lossless mkvmerge copy
    # Cut from a transient extracted temp (deleted after), named by book + chapter.
    assert "qc-epub-" in captured["source"].name
    assert not captured["source"].exists()                  # temp cleaned up post-cut
    assert "The Test Book" in str(captured["out"].parent)   # {source} = book title
    assert "The_Assimilation" in captured["out"].name        # {title} = chapter
