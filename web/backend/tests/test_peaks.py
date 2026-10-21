"""GET /api/items/peaks and /api/items/snap on ffmpeg-generated fixtures (M18)."""

from __future__ import annotations

import base64
import shutil
import subprocess
import time
import zipfile
from array import array
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from quipclipper import waveform
from quipclipper.clip import ClipRange, cut_clip
from quipclipper_web import epub_items, peaks
from quipclipper_web.app import create_app
from quipclipper_web.config import Settings

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")


def _ff(*args: str) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


@pytest.fixture(scope="module")
def media(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("media")
    # 1 kHz at 48 kHz, 3 s: a sign change every 0.5 ms.
    _ff("-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000:duration=3",
        str(root / "sine.wav"))
    # Tone 0-1 s, silence 1-1.5 s, tone 1.5-2.5 s.
    _ff("-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000:duration=1",
        "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono:d=0.5",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000:duration=1",
        "-filter_complex", "[0][1][2]concat=n=3:v=0:a=1", str(root / "gap.wav"))
    # 130 s, long enough that the whole file comes from the overview.
    _ff("-f", "lavfi", "-i", "sine=frequency=200:sample_rate=8000:duration=130",
        str(root / "long.wav"))
    # Video with a keyframe every second (25 fps, GOP 25) and a tone.
    _ff("-f", "lavfi", "-i", "testsrc=size=64x48:rate=25:duration=4",
        "-f", "lavfi", "-i", "sine=frequency=500:sample_rate=48000:duration=4",
        "-c:v", "mpeg4", "-g", "25", "-c:a", "aac", "-shortest", str(root / "video.mp4"))
    _ff("-f", "lavfi", "-i", "testsrc=size=64x48:rate=25:duration=1", "-an",
        "-c:v", "mpeg4", str(root / "silent-video.mp4"))
    return root


@pytest.fixture
def client(media, tmp_path):
    clips = tmp_path / "clips"
    clips.mkdir()
    app = create_app(Settings.from_env({
        "QC_MEDIA_ROOTS": str(media), "QC_CLIPS_DIR": str(clips),
        "QC_STATE_DIR": str(tmp_path / "state"),
    }))
    with TestClient(app) as c:
        yield c


def _ints(b64: str) -> array:
    a = array("h")
    a.frombytes(base64.b64decode(b64))
    return a


def test_peaks_short_file_is_a_detail_decode(client, media):
    r = client.get("/api/items/peaks", params={"path": str(media / "sine.wav"), "bins": 100})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "peaks" and body["source"] == "detail"
    assert body["bins"] == 100 and body["end"] == pytest.approx(3.0, abs=0.01)
    pairs = _ints(body["data"])
    assert len(pairs) == 200
    # ffmpeg's sine is 1/8 full scale.
    assert max(pairs[1::2]) == pytest.approx(4096, abs=30)
    assert min(pairs[0::2]) == pytest.approx(-4096, abs=30)
    assert body["peak"] == pytest.approx(4096, abs=30)
    assert body["rate"] == 48000


def test_peaks_zoomed_to_samples(client, media):
    r = client.get("/api/items/peaks", params={
        "path": str(media / "sine.wav"), "start": 1.0, "end": 1.001, "bins": 100})
    body = r.json()
    assert body["kind"] == "samples" and body["rate"] == 48000
    assert body["t0"] == 1.0 and body["count"] == 48
    s = _ints(body["data"])
    assert len(s) == 48
    # Two full cycles in 1 ms; a whole number of cycles in, so it starts at zero.
    assert abs(s[0]) < 50 and abs(s[24]) < 50


def test_detail_windows_start_on_the_sample_grid(client, media):
    """A window asked for between two samples starts on the nearer one, and says so, so
    the display, the snap and the cut agree on which sample is where."""
    r = client.get("/api/items/peaks", params={
        "path": str(media / "sine.wav"), "start": 1.0 + 0.3 / 48000, "end": 1.001, "bins": 100})
    assert r.json()["t0"] == 1.0
    full = waveform.decode(media / "sine.wav", start=1.0, duration=0.001)
    assert list(_ints(r.json()["data"])[:20]) == list(full[:20])


def test_peaks_long_file_builds_the_overview_then_serves_it(client, media):
    path = str(media / "long.wav")
    r = client.get("/api/items/peaks", params={"path": path, "bins": 500})
    assert r.status_code in (200, 202)
    deadline = time.monotonic() + 30
    while r.status_code == 202 and time.monotonic() < deadline:
        assert r.json()["kind"] == "pending"
        assert r.json()["overview"]["state"] in ("queued", "building", "ready")
        time.sleep(0.2)
        r = client.get("/api/items/peaks", params={"path": path, "bins": 500})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source"] == "overview" and body["overview"]["state"] == "ready"
    assert body["rate"] == peaks.OVERVIEW_RATE
    pairs = _ints(body["data"])
    assert len(pairs) == 1000
    assert max(pairs[1::2]) == pytest.approx(4096, abs=60)
    # Short windows still decode in detail once the overview exists.
    r = client.get("/api/items/peaks", params={"path": path, "start": 10, "end": 10.01,
                                               "bins": 400})
    assert r.json()["source"] == "detail" and r.json()["kind"] == "samples"


def test_peaks_cache_file_lands_in_the_state_dir(client, media, tmp_path):
    path = str(media / "long.wav")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if client.get("/api/items/peaks", params={"path": path}).status_code == 200:
            break
        time.sleep(0.2)
    files = list((tmp_path / "state" / "peaks").glob("*.qcpk"))
    assert len(files) == 1
    assert 0 < files[0].stat().st_size < 30_000  # 130 s at 20 ms buckets, 4 bytes each


def test_peaks_keyframes_for_video(client, media):
    r = client.get("/api/items/peaks", params={
        "path": str(media / "video.mp4"), "start": 0, "end": 3.5, "bins": 50,
        "keyframes": True})
    body = r.json()
    assert r.status_code == 200, r.text
    assert body["keyframes"][:4] == pytest.approx([0.0, 1.0, 2.0, 3.0], abs=0.05)


def test_peaks_errors(client, media, tmp_path):
    outside = tmp_path / "outside.wav"
    shutil.copy(media / "sine.wav", outside)
    assert client.get("/api/items/peaks", params={"path": str(outside)}).status_code == 403
    assert client.get("/api/items/peaks",
                      params={"path": str(media / "nope.wav")}).status_code == 404
    r = client.get("/api/items/peaks", params={"path": str(media / "silent-video.mp4")})
    assert r.status_code == 422
    r = client.get("/api/items/peaks", params={"path": str(media / "sine.wav"),
                                               "start": 2, "end": 1})
    assert r.status_code == 422
    r = client.get("/api/items/peaks", params={"path": str(media / "sine.wav"),
                                               "bins": 100000})
    assert r.status_code == 422
    assert client.get("/api/items/snap", params={"path": str(outside), "t": 1}).status_code == 403
    assert client.get("/api/items/snap", params={"path": str(media / "sine.wav"), "t": 1,
                                                 "mode": "loud"}).status_code == 422
    assert client.get("/api/items/peaks", params={"path": "yt:nosuchvid01"}).status_code == 404


def test_snap_zero_lands_on_a_crossing(client, media):
    r = client.get("/api/items/snap", params={"path": str(media / "sine.wav"), "t": 1.01023})
    body = r.json()
    assert r.status_code == 200 and body["found"]
    # Crossings every 0.5 ms (24 samples): the nearest to 1.01023 is 1.0100.
    assert body["snapped"] == pytest.approx(1.0100, abs=1.5 / 48000)
    assert abs(body["value"]) < 300
    assert body["delta"] == pytest.approx(body["snapped"] - 1.01023, abs=1e-6)


def test_snap_silence_finds_the_gap(client, media):
    r = client.get("/api/items/snap", params={
        "path": str(media / "gap.wav"), "t": 0.8, "mode": "silence", "radius": 1.0})
    body = r.json()
    assert body["found"] and 1.0 <= body["snapped"] <= 1.5
    assert body["level_db"] is None or body["level_db"] < -45
    r = client.get("/api/items/snap", params={
        "path": str(media / "gap.wav"), "t": 0.3, "mode": "silence", "radius": 0.2})
    assert r.json()["found"] is False  # all tone within 0.1-0.5 s


def test_exact_cut_starts_on_the_snapped_crossing(client, media, tmp_path):
    """The cut and the waveform use the same seek, so a clip started at a snapped
    zero crossing begins on that crossing, to the sample."""
    src = media / "video.mp4"
    snap = client.get("/api/items/snap", params={"path": str(src), "t": 1.23456}).json()
    assert snap["found"]
    t = snap["snapped"]
    out = cut_clip(src, ClipRange(t, t + 0.5), kind="audio", lossless=False,
                   audio_codec="wav", out=tmp_path / "c.wav")
    clip = waveform.decode(out)
    ref = waveform.decode(src, start=t, duration=0.5, rate=48000)
    assert abs(clip[0]) <= abs(snap["value"]) + 2
    assert list(clip[:64]) == pytest.approx(list(ref[:64]), abs=2)


def test_exact_cut_matches_the_snap_on_dts_in_matroska(client, media, tmp_path):
    """DTS in Matroska carries timestamps too coarse for -ss to land on the same sample
    twice; the anchored decode still makes the snap and the cut agree to the sample."""
    src = media / "dts.mkv"
    if not src.exists():
        r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                            "sine=frequency=330:sample_rate=48000:duration=14",
                            "-ac", "2", "-c:a", "dca", "-strict", "-2", str(src)])
        if r.returncode != 0:
            pytest.skip("this ffmpeg can't encode DTS")
    for t in (3.21234, 9.996, 12.71877):
        snap = client.get("/api/items/snap", params={"path": str(src), "t": t}).json()
        assert snap["found"], snap
        s = snap["snapped"]
        out = cut_clip(src, ClipRange(s, s + 0.2), kind="audio", lossless=False,
                       audio_codec="wav", out=tmp_path / f"c{t}.wav")
        clip = waveform.decode(out)
        assert abs(clip[0] - snap["value"]) <= 2, (t, list(clip[:4]), snap)


def test_epub_stored_member_reads_in_place(tmp_path, media):
    epub = tmp_path / "book.epub"
    with zipfile.ZipFile(epub, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.write(media / "sine.wav", "audio/part1.wav", compress_type=zipfile.ZIP_STORED)

    class Seg:
        audio = "audio/part1.wav"

    import quipclipper_web.epub_items as ei
    orig_segment, orig_book = ei._segment, ei.book_for
    ei._segment = lambda book, index: Seg()
    ei.book_for = lambda p: None
    try:
        inp, cleanup = epub_items.segment_audio_input(epub, 0)
    finally:
        ei._segment, ei.book_for = orig_segment, orig_book
    assert inp.startswith("subfile,") and cleanup is None
    s = waveform.decode(inp, start=1.0, duration=0.01)
    assert len(s) == 480 and max(s) == pytest.approx(4096, abs=30)
