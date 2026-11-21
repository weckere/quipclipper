"""Media links that start at a line (M27, D-137), for players that take no start time.

VLC for iOS opens a URL but accepts no start position, so ``GET /api/media/link?start=``
links an HLS playlist instead of the raw file: a VOD playlist over the **whole** file whose
``EXT-X-START`` points at the line. VLC (libvlc 3, tried with desktop 3.0.24) starts there,
shows the film's full length and seeks backwards and forwards like in the file itself.

Segments are cut on demand, one ffmpeg per request, by stream copy into MPEG-TS (no
transcode, nothing on disk). ``-copyts`` keeps the film's own timestamps, so a subtitle
file made for the film stays in sync and needs no shift.

The segment grid is the file's timeline cut every :data:`STEP` seconds *through the start
point*: boundaries at ``start + k * STEP`` between 0 and the end, so one segment begins
exactly at ``start``. A segment ``[b_j, b_j+1)`` holds the packets from the keyframe at or
before ``b_j`` up to the keyframe at or before ``b_j+1``. Neighbours agree on that cut, so
nothing is played twice or skipped; when two boundaries fall in one GOP the earlier segment
is empty (VLC skips it). The playlist's durations are the grid's, not the cuts': a seek
lands within a GOP of where it was aimed.

Only codecs MPEG-TS carries are linked: other video is refused at issue, other audio
(FLAC, PCM, Vorbis…) is left out, and subtitles never go into the stream (the link's
``subtitles_url`` serves them).
"""

from __future__ import annotations

import json
import math
import re
import subprocess
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

STEP = 10.0                 # s: nominal segment length
SEGMENT_RE = re.compile(r"^(\d{5})\.ts$")
PLAYLIST_SUFFIX = ".m3u8"
PLAYLIST_TYPE = "application/vnd.apple.mpegurl"
SEGMENT_TYPE = "video/mp2t"
MAX_SEGMENTS = 99_999       # what a five-digit name holds: 11 days at STEP
MAX_PROCS = 8               # segment cuts running at once, across every link
_EPS = 0.0005               # s: below any frame interval, above ffprobe's rounding
_PROBE_TIMEOUT = 30
# Windows (s before the boundary) to look for its keyframe in, widest last: almost every
# film has one within 30 s; a longer GOP costs a longer read, never a wrong cut.
_WINDOWS = (30.0, 120.0, 600.0, math.inf)
_run = subprocess.run       # the tests' seam for ffprobe

# What ffmpeg's MPEG-TS muxer carries so that players read it. AV1, VP9 and FLAC come out
# as unreadable data streams (ffmpeg 9), so they aren't here.
TS_VIDEO = frozenset({"h264", "hevc", "mpeg2video", "mpeg1video", "mpeg4", "vc1"})
TS_AUDIO = frozenset({"aac", "ac3", "eac3", "mp2", "mp3", "dts", "truehd", "opus"})


class StartLinkError(ValueError):
    """The file can't be linked from a start point (said in words for the reply)."""


@dataclass(frozen=True)
class Source:
    """What a start link needs to know of its file, signed into the link."""

    duration: float          # s
    offset: float            # s: the container's start time (0 for most files)
    maps: tuple[int, ...]    # absolute stream indices to copy: the video, then audio
    key: int                 # the stream whose keyframes the cuts follow


def probe_source(path: Path) -> Source:
    """Duration, start time and the streams to copy, or :class:`StartLinkError`."""
    try:
        out = _run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=duration,start_time:stream=index,codec_type,codec_name"
             ":stream_disposition=default,attached_pic",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=_PROBE_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StartLinkError("ffprobe couldn't read the file.") from exc
    try:
        info = json.loads(out.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise StartLinkError("ffprobe couldn't read the file.") from exc
    fmt = info.get("format") or {}
    try:
        duration = float(fmt.get("duration"))
    except (TypeError, ValueError):
        raise StartLinkError("The file's length is unknown.") from None
    try:
        offset = float(fmt.get("start_time") or 0.0)
    except ValueError:
        offset = 0.0
    video, audio, other_video = None, [], None
    for s in info.get("streams") or []:
        disp = s.get("disposition") or {}
        kind, codec, idx = s.get("codec_type"), s.get("codec_name"), s.get("index")
        if not isinstance(idx, int):
            continue
        if kind == "video" and not disp.get("attached_pic"):
            if video is None and codec in TS_VIDEO:
                video = idx
            elif video is None and other_video is None:
                other_video = codec or "?"
        elif kind == "audio" and codec in TS_AUDIO:
            audio.append((not disp.get("default"), idx))   # the default track first
    if video is None and other_video is not None:
        raise StartLinkError(f"Its video ({other_video}) can't be streamed from a start "
                             "point without re-encoding; open the plain link instead.")
    if video is None and not audio:
        raise StartLinkError("It has no video or audio that can be streamed from a start "
                             "point without re-encoding.")
    order = tuple(i for _, i in sorted(audio))
    maps = ((video,) if video is not None else ()) + order
    return Source(duration=duration, offset=offset, maps=maps,
                  key=video if video is not None else order[0])


def keyframe_at_or_before(path: Path, key: int, t: float, offset: float) -> float:
    """The timestamp (s, the file's own) of the last keyframe of stream ``key`` at or
    before ``t``; ``offset`` (the file's start) when there is none. RuntimeError when
    ffprobe fails. The answer is the same whichever window finds it."""
    if t <= offset + _EPS:
        return offset
    for window in _WINDOWS:
        lo = max(offset, t - window)
        try:
            out = _run(
                ["ffprobe", "-v", "error", "-select_streams", str(key),
                 "-show_entries", "packet=pts_time,flags", "-of", "csv=p=0",
                 "-read_intervals", f"{lo:.6f}%{t + _EPS:.6f}", str(path)],
                capture_output=True, text=True, timeout=_PROBE_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError("ffprobe couldn't read the file's keyframes.") from exc
        if out.returncode != 0:
            raise RuntimeError("ffprobe couldn't read the file's keyframes.")
        best = None
        for line in out.stdout.splitlines():
            pts, _, flags = line.partition(",")
            if "K" not in flags:
                continue
            try:
                p = float(pts)
            except ValueError:
                continue
            if p <= t + _EPS and (best is None or p > best):
                best = p
        if best is not None:
            return best
        if lo <= offset:
            return offset
    return offset


def boundaries(start: float, duration: float, step: float = STEP) -> list[float]:
    """The segment grid (s from the file's start): 0, every ``step`` through ``start``,
    and the end, rounded to the millisecond."""
    k = math.floor(start / step)
    inner = {round(start + i * step, 3) for i in range(-k - 1, math.ceil((duration - start)
                                                                          / step) + 1)}
    end = round(duration, 3)
    return sorted({0.0, end} | {b for b in inner if 0 < b < end})


def start_index(bounds: list[float], start: float) -> int:
    """The segment that begins at ``start`` (0 when start is 0)."""
    s = round(start, 3)
    return bounds.index(s) if s in bounds else 0


def playlist(bounds: list[float], start: float) -> str:
    """The VOD playlist: every segment of the file, starting at ``start``."""
    lens = [bounds[i + 1] - bounds[i] for i in range(len(bounds) - 1)]
    lines = ["#EXTM3U", "#EXT-X-VERSION:3",
             f"#EXT-X-TARGETDURATION:{math.ceil(max(lens, default=STEP))}",
             "#EXT-X-PLAYLIST-TYPE:VOD", "#EXT-X-MEDIA-SEQUENCE:0",
             f"#EXT-X-START:TIME-OFFSET={bounds[start_index(bounds, start)]:.3f},PRECISE=YES"]
    for i, n in enumerate(lens):
        lines += [f"#EXTINF:{n:.3f},", f"{i:05d}.ts"]
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def segment_command(path: Path, src: Source, c0: float, c1: float | None) -> list[str]:
    """ffmpeg cutting ``[c0, c1)`` (the file's own timestamps; ``c1`` None: to the end)
    by stream copy into MPEG-TS on stdout, keeping the timestamps."""
    cmd = ["ffmpeg", "-nostdin", "-v", "error"]
    if c0 > src.offset + _EPS:
        # Input seek lands on the keyframe at or before; c0 is one, nudged past
        # ffprobe's 6-decimal rounding so it isn't the one before.
        cmd += ["-ss", f"{c0 - src.offset + _EPS:.6f}"]
    cmd += ["-i", str(path)]
    if c1 is not None:
        cmd += ["-to", f"{c1 - _EPS:.6f}"]   # with -copyts: the file's own timestamps
    for i in src.maps:
        cmd += ["-map", f"0:{i}"]
    cmd += ["-c", "copy", "-copyts", "-muxdelay", "0", "-muxpreload", "0",
            "-f", "mpegts", "pipe:1"]
    return cmd


class Cuts:
    """Keyframe cut points, remembered: a segment's end is the next one's start."""

    def __init__(self, size: int = 4096):
        self._seen: OrderedDict[tuple, float] = OrderedDict()
        self._lock = threading.Lock()
        self._size = size

    def at(self, path: Path, src: Source, t: float) -> float:
        """The cut for grid point ``t`` (s from the file's start), in the file's own time."""
        try:
            mtime = path.stat().st_mtime_ns
        except OSError:
            mtime = 0
        k = (str(path), mtime, src.key, round(t, 3))
        with self._lock:
            if k in self._seen:
                self._seen.move_to_end(k)
                return self._seen[k]
        got = keyframe_at_or_before(path, src.key, src.offset + t, src.offset)
        with self._lock:
            self._seen[k] = got
            while len(self._seen) > self._size:
                self._seen.popitem(last=False)
        return got

    def segment(self, path: Path, src: Source, bounds: list[float],
                j: int) -> tuple[float, float | None]:
        """Segment ``j``'s cut ``(c0, c1)``; ``c1`` None for the last one."""
        c0 = src.offset if j == 0 else self.at(path, src, bounds[j])
        c1 = None if j + 2 >= len(bounds) else self.at(path, src, bounds[j + 1])
        return c0, c1
