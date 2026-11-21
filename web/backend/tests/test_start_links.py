"""Media links that start at a line (M27, D-137): an HLS playlist of the whole file whose
EXT-X-START is the keyframe at or before the line, its segments cut on demand.

Deterministic: ffprobe and ffmpeg are fakes (the clock is injected) except in the one
end-to-end test, which makes its own small film with ffmpeg's built-in encoders and
skips when there is no ffmpeg."""

from __future__ import annotations

import asyncio
import base64
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect

from quipclipper_web import app as app_mod
from quipclipper_web import media_links, start_links
from quipclipper_web.app import create_app
from quipclipper_web.config import Settings

CSRF = {"X-Quipclipper": "1"}
T0 = 1_800_000_000.0
GONE = {"detail": "Not Found"}
KEYFRAMES = [i * 4.0 for i in range(151)]      # a keyframe every 4 s of a 600 s film
SRC = start_links.Source(duration=600.0, offset=0.0, maps=(0, 2, 1), key=0)


class Clock:
    def __init__(self, t: float = T0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def _ffprobe_streams(*streams: dict, duration="600.000", start_time="0.000") -> str:
    return json.dumps({"format": {"duration": duration, "start_time": start_time},
                       "streams": list(streams)})


def _stream(i: int, kind: str, codec: str, default=0, pic=0) -> dict:
    return {"index": i, "codec_type": kind, "codec_name": codec,
            "disposition": {"default": default, "attached_pic": pic}}


FILM_STREAMS = _ffprobe_streams(_stream(0, "video", "h264", 1), _stream(1, "audio", "aac"),
                                _stream(2, "audio", "ac3", 1), _stream(3, "audio", "flac"),
                                _stream(4, "subtitle", "subrip"))


class FakeProbe:
    """subprocess.run for ffprobe: the stream listing, or the keyframes in an interval."""

    def __init__(self, listing: str = FILM_STREAMS, keyframes=KEYFRAMES):
        self.listing, self.keyframes, self.calls = listing, keyframes, []

    def __call__(self, cmd, **_kw):
        self.calls.append(cmd)
        if "-read_intervals" in cmd:
            lo, hi = (float(x) for x in cmd[cmd.index("-read_intervals") + 1].split("%"))
            rows = []
            for k in self.keyframes:
                for t in (k, k + 0.04):          # a keyframe and the frame after it
                    if lo <= t <= hi:
                        rows.append(f"{t:.6f},{'K__' if t == k else '___'},")
            return SimpleNamespace(returncode=0, stdout="\n".join(rows) + "\n", stderr="")
        return SimpleNamespace(returncode=0, stdout=self.listing, stderr="")


# --- the grid and the playlist ---------------------------------------------------------


def test_the_grid_runs_through_the_start():
    b = start_links.boundaries(305, 600)
    assert b[:3] == [0.0, 5.0, 15.0] and b[-2:] == [595.0, 600.0]
    assert 305.0 in b and all(round(y - x, 3) == 10 for x, y in zip(b[1:-2], b[2:-1]))
    assert start_links.start_index(b, 305) == b.index(305.0) == 31


def test_the_grid_from_zero_and_on_a_multiple():
    assert start_links.boundaries(0, 25) == [0.0, 10.0, 20.0, 25.0]
    assert start_links.boundaries(20, 25) == [0.0, 10.0, 20.0, 25.0]
    assert start_links.boundaries(12.5, 30) == [0.0, 2.5, 12.5, 22.5, 30.0]
    assert start_links.start_index([0.0, 10.0, 20.0, 25.0], 0) == 0


def test_the_playlist_is_the_whole_file_and_starts_at_the_line():
    b = start_links.boundaries(305, 600)
    text = start_links.playlist(b, 305)
    lines = text.splitlines()
    assert lines[0] == "#EXTM3U" and lines[-1] == "#EXT-X-ENDLIST"
    assert "#EXT-X-PLAYLIST-TYPE:VOD" in lines
    assert "#EXT-X-START:TIME-OFFSET=305.000,PRECISE=YES" in lines
    names = [x for x in lines if not x.startswith("#")]
    assert names == [f"{i:05d}.ts" for i in range(len(b) - 1)]
    durs = [float(x[8:-1]) for x in lines if x.startswith("#EXTINF:")]
    assert durs[0] == 5.0 and round(sum(durs), 3) == 600.0
    assert "#EXT-X-TARGETDURATION:10" in lines


# --- probing ---------------------------------------------------------------------------


def test_the_source_copies_the_video_and_the_audio_ts_carries(monkeypatch):
    monkeypatch.setattr(start_links, "_run", FakeProbe())
    src = start_links.probe_source(Path("film.mkv"))
    # The default audio first; FLAC can't go into MPEG-TS; subtitles never do.
    assert src == start_links.Source(duration=600.0, offset=0.0, maps=(0, 2, 1), key=0)


def test_cover_art_is_not_video_and_audio_alone_works(monkeypatch):
    listing = _ffprobe_streams(_stream(0, "audio", "mp3"), _stream(1, "video", "mjpeg", pic=1))
    monkeypatch.setattr(start_links, "_run", FakeProbe(listing))
    assert start_links.probe_source(Path("a.mp3")).maps == (0,)
    assert start_links.probe_source(Path("a.mp3")).key == 0


@pytest.mark.parametrize("listing", [
    _ffprobe_streams(_stream(0, "video", "av1"), _stream(1, "audio", "aac")),
    _ffprobe_streams(_stream(0, "audio", "flac")),
    _ffprobe_streams(_stream(0, "video", "h264"), duration="N/A"),
])
def test_what_ts_cant_carry_is_refused(monkeypatch, listing):
    monkeypatch.setattr(start_links, "_run", FakeProbe(listing))
    with pytest.raises(start_links.StartLinkError):
        start_links.probe_source(Path("film.mkv"))


def test_the_keyframe_at_or_before(monkeypatch):
    probe = FakeProbe()
    monkeypatch.setattr(start_links, "_run", probe)
    assert start_links.keyframe_at_or_before(Path("f"), 0, 305.0, 0.0) == 304.0
    assert start_links.keyframe_at_or_before(Path("f"), 0, 304.0, 0.0) == 304.0
    assert start_links.keyframe_at_or_before(Path("f"), 0, 0.0, 0.0) == 0.0
    assert probe.calls[0][probe.calls[0].index("-select_streams") + 1] == "0"


def test_a_long_gop_widens_the_window_and_finds_the_same_keyframe(monkeypatch):
    probe = FakeProbe(keyframes=[0.0, 100.0, 400.0])
    monkeypatch.setattr(start_links, "_run", probe)
    assert start_links.keyframe_at_or_before(Path("f"), 0, 390.0, 0.0) == 100.0
    assert len(probe.calls) == 3          # 30 s, 120 s, then 600 s back
    assert start_links.keyframe_at_or_before(Path("f"), 0, 50.0, 0.0) == 0.0


def test_ffprobe_failing_is_an_error_not_a_guess(monkeypatch):
    monkeypatch.setattr(start_links, "_run",
                        lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="x"))
    with pytest.raises(RuntimeError):
        start_links.keyframe_at_or_before(Path("f"), 0, 100.0, 0.0)


def test_neighbouring_segments_share_their_cut(monkeypatch, tmp_path):
    probe = FakeProbe()
    monkeypatch.setattr(start_links, "_run", probe)
    f = tmp_path / "f.mkv"
    f.write_bytes(b"x")
    cuts, b = start_links.Cuts(), start_links.boundaries(305, 600)
    s30, s31, s32 = (cuts.segment(f, SRC, b, j) for j in (30, 31, 32))
    assert s30 == (292.0, 304.0) and s31 == (304.0, 312.0) and s32 == (312.0, 324.0)
    assert len(probe.calls) == 4          # each cut probed once
    assert cuts.segment(f, SRC, b, 0) == (0.0, 4.0)
    assert cuts.segment(f, SRC, b, len(b) - 2) == (592.0, None)


def test_the_segment_command_copies_and_keeps_the_timestamps():
    cmd = start_links.segment_command(Path("/m/f.mkv"), SRC, 304.0, 312.0)
    assert cmd[cmd.index("-ss") + 1] == "304.000500"
    assert cmd[cmd.index("-to") + 1] == "311.999500"
    assert [cmd[i + 1] for i, a in enumerate(cmd) if a == "-map"] == ["0:0", "0:2", "0:1"]
    assert cmd[cmd.index("-c") + 1] == "copy" and "-copyts" in cmd
    assert cmd[-3:] == ["-f", "mpegts", "pipe:1"]
    assert "-c:v" not in cmd and "-c:a" not in cmd      # never a transcode
    first = start_links.segment_command(Path("/m/f.mkv"), SRC, 0.0, None)
    assert "-ss" not in first and "-to" not in first
    # A container that starts later: -ss counts from its start, -to in its own time.
    late = start_links.Source(duration=600.0, offset=1.4, maps=(0,), key=0)
    cmd = start_links.segment_command(Path("/m/f.ts"), late, 305.4, 313.4)
    assert cmd[cmd.index("-ss") + 1] == "304.000500"
    assert cmd[cmd.index("-to") + 1] == "313.399500"


# --- the app ---------------------------------------------------------------------------


class FakeProc:
    def __init__(self, chunks=(b"\x47" * 188, b"\x47" * 188)):
        self.returncode = None
        self.killed = False
        self._chunks = list(chunks)
        self.stdout = SimpleNamespace(read=self._read)

    async def _read(self, _n):
        if self._chunks:
            return self._chunks.pop(0)
        self.returncode = 0
        return b""

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        return self.returncode


@pytest.fixture
def env(tmp_path, monkeypatch):
    media = tmp_path / "media"
    (media / "Film (2001)").mkdir(parents=True)
    film = media / "Film (2001)" / "Film (2001).mkv"
    film.write_bytes(b"film")
    (media / "Film (2001)" / "Film (2001).en.srt").write_text(
        "1\n00:05:04,000 --> 00:05:06,000\nThe line.\n", encoding="utf-8")
    clips = tmp_path / "clips"
    clips.mkdir()
    probe = FakeProbe()
    monkeypatch.setattr(start_links, "_run", probe)
    spawned: list[list[str]] = []
    procs: list[FakeProc] = []

    async def spawn(*cmd, **_kw):
        spawned.append(list(cmd))
        procs.append(FakeProc())
        return procs[-1]

    monkeypatch.setattr(app_mod.asyncio, "create_subprocess_exec", spawn)
    clock = Clock()
    settings = Settings.from_env({"QC_MEDIA_ROOTS": str(media), "QC_CLIPS_DIR": str(clips),
                                  "QC_STATE_DIR": str(tmp_path / "state")})
    c = TestClient(create_app(settings, clock=clock), client=("127.0.0.1", 50000))
    return SimpleNamespace(c=c, clock=clock, film=film, probe=probe, spawned=spawned,
                           procs=procs, state=tmp_path / "state")


def _pair(c: TestClient, scopes: list[str]) -> tuple[str, str]:
    req = c.post("/api/pair", json={"name": "LineFinder", "host": "iphone", "scopes": scopes},
                 headers=CSRF).json()
    assert c.post(f"/api/pair/{req['id']}/approve", headers=CSRF).status_code == 200
    got = c.get(f"/api/pair/{req['id']}", headers={"X-Pair-Secret": req["poll_secret"]}).json()
    return got["token"], got["client_id"]


def _link(env, token: str | None = None, **q) -> dict:
    headers = {"X-API-Key": token} if token else {}
    r = env.c.get("/api/media/link", params={"path": str(env.film), **q}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _path(url: str) -> str:
    return urlsplit(url).path


def _seg(url: str, j: int) -> str:
    return _path(url).rsplit("/", 1)[0] + f"/{j:05d}.ts"


def _payload(url: str) -> dict:
    raw = _path(url).split("/")[4].split(".")[0]
    return json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))


def _gone(r) -> None:
    assert r.status_code == 404 and r.json() == GONE


def test_a_start_link_is_a_playlist_that_starts_at_the_keyframe(env):
    token, cid = _pair(env.c, ["media.link"])
    got = _link(env, token, start=305, subtitles="eng")
    assert got["starts_at"] == 304.0
    assert got["url"].startswith("http://testserver/api/media/s/")
    assert got["url"].endswith("/Film%20%282001%29.m3u8")
    body = _payload(got["url"])
    assert body["k"] == "hls" and body["c"] == cid and body["s"] == 305.0
    assert body["d"] == 600.0 and body["m"] == [0, 2, 1] and body["i"] == 0
    r = env.c.get(_path(got["url"]))                 # no token: what VLC sends
    assert r.status_code == 200
    assert r.headers["content-type"] == start_links.PLAYLIST_TYPE
    assert "#EXT-X-START:TIME-OFFSET=305.000,PRECISE=YES" in r.text
    assert r.text == start_links.playlist(start_links.boundaries(305, 600), 305)
    h = env.c.head(_path(got["url"]))
    assert h.status_code == 200 and h.content == b""
    assert h.headers["content-length"] == str(len(r.content))
    # The subtitles keep the film's timeline: the segments do too (-copyts).
    subs = env.c.get(_path(got["subtitles_url"]))
    assert subs.status_code == 200 and "00:05:04,000 --> 00:05:06,000" in subs.text


def test_a_segment_is_cut_by_stream_copy_and_the_cut_ends(env):
    url = _link(env, start=305)["url"]
    r = env.c.get(_seg(url, 31))
    assert r.status_code == 200 and r.content == b"\x47" * 376
    assert r.headers["content-type"] == start_links.SEGMENT_TYPE
    cmd = env.spawned[-1]
    assert cmd[0] == "ffmpeg" and cmd[cmd.index("-ss") + 1] == "304.000500"
    assert cmd[cmd.index("-to") + 1] == "311.999500" and str(env.film) in cmd
    assert env.procs[-1].returncode is not None
    h = env.c.head(_seg(url, 32))
    assert h.status_code == 200 and h.content == b"" and len(env.spawned) == 1


def test_an_empty_segment_spawns_nothing(env, monkeypatch):
    monkeypatch.setattr(start_links, "_run", FakeProbe(keyframes=[0.0, 300.0, 320.0]))
    url = _link(env, start=305)["url"]
    # 305 and 315 both fall in the GOP from 300: segment 31 is empty, 32 holds it.
    r = env.c.get(_seg(url, 31))
    assert r.status_code == 200 and r.content == b"" and env.spawned == []
    assert env.c.get(_seg(url, 32)).status_code == 200
    cmd = env.spawned[-1]
    assert cmd[cmd.index("-ss") + 1] == "300.000500"


def test_only_the_playlist_and_its_segments(env):
    url = _link(env, start=305)["url"]
    base = _path(url).rsplit("/", 1)[0]
    for name in ("00061.ts", "99999.ts", "1.ts", "Film.mkv", "00001.mp4"):
        _gone(env.c.get(f"{base}/{name}"))
    assert env.c.get(f"{base}/other-name.m3u8").status_code == 200


def test_a_segment_that_cant_be_cut_is_a_502_without_detail(env, monkeypatch):
    url = _link(env, start=305)["url"]
    monkeypatch.setattr(start_links, "_run",
                        lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr=""))
    r = env.c.get(_seg(url, 40))
    assert r.status_code == 502 and "/" not in r.text and env.spawned == []


def test_start_past_the_end_or_an_unstreamable_file_is_refused(env, monkeypatch):
    r = env.c.get("/api/media/link", params={"path": str(env.film), "start": 600})
    assert r.status_code == 400
    assert env.c.get("/api/media/link", params={"path": str(env.film),
                                                "start": -1}).status_code == 422
    monkeypatch.setattr(start_links, "_run", FakeProbe(
        _ffprobe_streams(_stream(0, "video", "av1"))))
    r = env.c.get("/api/media/link", params={"path": str(env.film), "start": 10})
    assert r.status_code == 422 and "av1" in r.json()["detail"]


def test_a_start_link_expires(env):
    url = _link(env, start=305)["url"]
    env.clock.t = T0 + media_links.LIFETIME - 1
    assert env.c.get(_path(url)).status_code == 200
    assert env.c.get(_seg(url, 31)).status_code == 200
    env.clock.t = T0 + media_links.LIFETIME
    _gone(env.c.get(_path(url)))
    _gone(env.c.get(_seg(url, 31)))


def test_a_start_link_dies_with_its_client_and_its_key(env):
    token, cid = _pair(env.c, ["media.link"])
    url = _link(env, token, start=305)["url"]
    assert env.c.get(_seg(url, 31)).status_code == 200
    assert env.c.delete(f"/api/clients/{cid}", headers=CSRF).json() == {"removed": True}
    _gone(env.c.get(_path(url)))
    _gone(env.c.get(_seg(url, 31)))
    url = _link(env, start=305)["url"]
    (env.state / media_links.KEY_FILE).unlink()
    _gone(env.c.get(_seg(url, 31)))


def test_a_tampered_start_link_is_the_same_404(env):
    url = _link(env, start=305)["url"]
    prefix, tok, name = _path(url).rsplit("/", 2)
    sig = tok.split(".")[1]
    for change in ({"s": 10.0}, {"m": [3]}, {"k": "file"}):
        forged = {**_payload(url), **change}
        raw = base64.urlsafe_b64encode(json.dumps(forged).encode()).rstrip(b"=").decode()
        _gone(env.c.get(f"{prefix}/{raw}.{sig}/{name}"))


@pytest.mark.parametrize("bad", [{"s": 700.0}, {"m": []}, {"i": 9}, {"d": "600"},
                                 {"s": True}, {"m": [0, -1]}])
def test_a_signed_start_link_with_bad_fields_is_refused(tmp_path, bad):
    """Fields the server signed itself are checked anyway (a key leak can't crash it)."""
    from quipclipper_web import pairing
    links = media_links.MediaLinks(tmp_path, pairing.ClientStore(tmp_path), clock=Clock())
    good = {"k": "hls", "p": "/m/f.mkv", "s": 305.0, "d": 600.0, "o": 0.0, "m": [0, 1],
            "i": 0}
    assert links.verify(links.issue(good, "")[0]) is not None
    assert links.verify(links.issue({**good, **bad}, "")[0]) is None


def test_a_link_without_start_is_unchanged(env):
    got = _link(env)
    assert got["starts_at"] is None and got["url"].endswith("/Film%20%282001%29.mkv")
    assert _payload(got["url"])["k"] == "file" and env.probe.calls == []


# --- the process ends with its response ------------------------------------------------


def _scope():
    return {"type": "http", "method": "GET", "path": "/", "headers": [],
            "asgi": {"version": "3.0", "spec_version": "2.4"}}


def test_the_cut_is_killed_when_the_client_goes_away():
    proc, running = FakeProc(chunks=[b"a" * 10] * 5), set()
    running.add(proc)
    resp = app_mod._ProcStream(proc, running, media_type=start_links.SEGMENT_TYPE)
    sent = []

    async def send(msg):
        sent.append(msg)
        if msg["type"] == "http.response.body" and msg.get("body"):
            raise OSError("client gone")

    async def receive():
        await asyncio.sleep(3600)

    with pytest.raises((OSError, ClientDisconnect)):
        asyncio.run(resp(_scope(), receive, send))
    assert proc.killed and not running


def test_the_cut_is_killed_when_the_response_is_cancelled():
    proc, running = FakeProc(chunks=[]), set()
    running.add(proc)
    proc.stdout = SimpleNamespace(read=lambda _n: asyncio.sleep(3600))
    resp = app_mod._ProcStream(proc, running, media_type=start_links.SEGMENT_TYPE)

    async def main():
        async def receive():
            await asyncio.sleep(3600)

        async def send(_msg):
            pass

        task = asyncio.ensure_future(resp(_scope(), receive, send))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    assert proc.killed and not running


# --- end to end, with a real ffmpeg ----------------------------------------------------


@pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
                    reason="needs ffmpeg and ffprobe")
def test_the_segments_play_the_film_once_through(tmp_path):
    media = tmp_path / "media"
    media.mkdir()
    film = media / "film.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
                    "-i", "testsrc=size=160x90:rate=25", "-f", "lavfi",
                    "-i", "sine=frequency=440:sample_rate=48000", "-t", "30",
                    "-c:v", "mpeg4", "-g", "50", "-bf", "0", "-c:a", "mp2", "-shortest",
                    str(film)], check=True, timeout=120)
    settings = Settings.from_env({"QC_MEDIA_ROOTS": str(media),
                                  "QC_CLIPS_DIR": str(tmp_path),
                                  "QC_STATE_DIR": str(tmp_path / "state")})
    c = TestClient(create_app(settings), client=("127.0.0.1", 50000))
    got = c.get("/api/media/link", params={"path": str(film), "start": 13}).json()
    assert got["starts_at"] == 12.0                    # a keyframe every 2 s
    text = c.get(_path(got["url"])).text
    names = [x for x in text.splitlines() if x and not x.startswith("#")]
    assert len(names) == 4                             # 0-3, 3-13, 13-23, 23-30
    out = tmp_path / "all.ts"
    with out.open("wb") as fh:
        for j in range(len(names)):
            r = c.get(_seg(got["url"], j))
            assert r.status_code == 200
            fh.write(r.content)
            if j == 2:                                 # the line's segment
                (tmp_path / "s2.ts").write_bytes(r.content)

    def video_pts(path: Path) -> list[float]:
        rows = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                               "-show_entries", "packet=pts_time", "-of", "csv=p=0",
                               str(path)], capture_output=True, text=True, check=True)
        return [round(float(x.strip(",")), 3) for x in rows.stdout.split()]

    pts = video_pts(out)
    assert pts == sorted(set(pts)) and len(pts) == len(video_pts(film)) == 750
    # The film's own timeline, every frame once (the TS muxer may hold the very first
    # frame back 10 ms; later segments are exact).
    assert pts[0] <= 0.01 and pts[-1] == 29.96
    assert video_pts(tmp_path / "s2.ts")[0] == 12.0
