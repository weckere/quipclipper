"""Waveform peaks for the workspace's waveform strip (M18).

Two sources, both from ffmpeg:

- **Overview:** a peak pyramid per (source, audio stream, mtime), built once in the
  background from a whole-file decode at 8 kHz mono and cached in the state dir as
  ``peaks/<key>.qcpk`` (20 ms buckets of int16 min/max: about 1.4 MB per 2-hour film).
  It serves zoomed-out views.
- **Detail:** for windows up to :data:`DETAIL_MAX` seconds, a decode of just that
  window at up to 48 kHz. It serves zoomed-in views down to single samples, and the
  snap helpers.

The maths lives in the engine (:mod:`quipclipper.waveform`); this module only knows
where audio comes from and where the cache lives.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import os
import subprocess
import sys
import threading
import time
from array import array
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from quipclipper import waveform

OVERVIEW_RATE = 8000       # Hz, mono
OVERVIEW_BUCKET = 160      # samples per level-0 entry: 20 ms
DETAIL_MAX = 120.0         # seconds: longer windows come from the overview
DETAIL_FULL_RATE_MAX = 15.0  # seconds: shorter windows decode at the native rate
DETAIL_RATE = 16000        # Hz for detail windows longer than that
MAX_RATE = 48000
MAX_BINS = 8192
CACHE_LIMIT = 512 * 1024 * 1024  # bytes of overview files kept in the state dir


@dataclass
class AudioSource:
    """Where ffmpeg reads one item's audio from.

    ``input`` is a file path, URL or ffmpeg protocol string; ``basis`` identifies the
    content for the cache (path + mtime + size, or a YouTube id); ``cleanup`` removes
    a temp copy once a decode is done."""

    input: str
    basis: str
    audio: int = 0
    input_flags: tuple[str, ...] = ()
    duration: float | None = None
    has_video: bool = False
    video_input: str | None = None  # where keyframes are read (None: no video)
    make_input: Callable[[], tuple[str, Callable[[], None] | None]] | None = None

    @property
    def key(self) -> str:
        return hashlib.sha1(f"{self.basis}|a:{self.audio}".encode()).hexdigest()[:24]

    def open(self) -> tuple[str, Callable[[], None] | None]:
        """The ffmpeg input to read now, and what to call once done with it."""
        if self.make_input is not None:
            return self.make_input()
        return self.input, None


def file_basis(p: Path) -> str:
    st = p.stat()
    return f"{p.resolve()}|{st.st_mtime_ns}|{st.st_size}"


def encode(data) -> str:
    """int16 samples as base64 little-endian bytes (the wire format)."""
    a = data
    if sys.byteorder == "big":  # pragma: no cover
        a = type(data)(data.typecode, data)
        a.byteswap()
    return base64.b64encode(a.tobytes()).decode("ascii")


@dataclass
class _Build:
    state: str = "queued"  # queued | building | ready | error
    progress: float = 0.0
    error: str | None = None
    started: float = field(default_factory=time.monotonic)


def _status(b: _Build) -> dict:
    out = {"state": b.state, "progress": round(b.progress, 3)}
    if b.error:
        out["error"] = b.error
    return out


class PeakStore:
    """Overview pyramids on disk plus a small in-memory LRU, built one at a time."""

    def __init__(self, state_dir: Path, *, max_memory: int = 4,
                 cache_limit: int = CACHE_LIMIT) -> None:
        self.dir = Path(state_dir) / "peaks"
        self._mem: OrderedDict[str, waveform.PeakPyramid] = OrderedDict()
        self._max_memory = max_memory
        self._cache_limit = cache_limit
        self._builds: dict[str, _Build] = {}
        self._rates: dict[str, int | None] = {}
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="qc-peaks")

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _file(self, key: str) -> Path:
        return self.dir / f"{key}.qcpk"

    def get(self, key: str) -> waveform.PeakPyramid | None:
        """The finished overview, or None."""
        with self._lock:
            pyr = self._mem.get(key)
            if pyr is not None:
                self._mem.move_to_end(key)
                return pyr
        f = self._file(key)
        try:
            pyr = waveform.PeakPyramid.from_bytes(f.read_bytes())
        except (OSError, ValueError):
            return None
        with contextlib.suppress(OSError):
            os.utime(f)  # recently used: pruned last
        self._remember(key, pyr)
        return pyr

    def _remember(self, key: str, pyr: waveform.PeakPyramid) -> None:
        with self._lock:
            self._mem[key] = pyr
            self._mem.move_to_end(key)
            while len(self._mem) > self._max_memory:
                self._mem.popitem(last=False)

    def status(self, key: str) -> dict:
        """``{state, progress, error}`` for the overview of ``key``."""
        if self.get(key) is not None:
            return {"state": "ready", "progress": 1.0}
        with self._lock:
            b = self._builds.get(key)
        return _status(b) if b is not None else {"state": "none", "progress": 0.0}

    def ensure(self, src: AudioSource) -> dict:
        """Start building the overview of ``src`` unless it exists or is under way.
        A failed build is retried after a minute."""
        key = src.key
        if self.get(key) is not None:
            return {"state": "ready", "progress": 1.0}
        with self._lock:
            b = self._builds.get(key)
            if b is not None and (b.state != "error" or time.monotonic() - b.started < 60):
                return _status(b)
            b = self._builds[key] = _Build()
        self._pool.submit(self._build, src, b)
        return {"state": "queued", "progress": 0.0}

    def _build(self, src: AudioSource, b: _Build) -> None:
        b.state = "building"
        try:
            pyr = self._decode_overview(src, b)
        except Exception as exc:  # noqa: BLE001 - surfaced to the client as the build's error
            b.state, b.error = "error", str(exc) or exc.__class__.__name__
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self._file(src.key).with_suffix(".tmp")
        tmp.write_bytes(pyr.to_bytes())
        os.replace(tmp, self._file(src.key))
        self._remember(src.key, pyr)
        b.state, b.progress = "ready", 1.0
        with self._lock:
            self._builds.pop(src.key, None)
        self._prune()

    def _decode_overview(self, src: AudioSource, b: _Build) -> waveform.PeakPyramid:
        inp, cleanup = src.open()
        try:
            cmd = waveform.decode_args(inp, audio=src.audio, rate=OVERVIEW_RATE,
                                       input_flags=src.input_flags)
            kw = {}
            if hasattr(os, "nice"):
                kw["preexec_fn"] = lambda: os.nice(10)  # behind playback and clip jobs
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kw)
            try:
                mins, maxs = array("h"), array("h")
                expect = (src.duration or 0) * OVERVIEW_RATE
                assert proc.stdout is not None
                for lo, hi, total in waveform.stream_buckets(proc.stdout, OVERVIEW_BUCKET):
                    mins.extend(lo)
                    maxs.extend(hi)
                    if expect:
                        b.progress = min(0.99, total / expect)
                err = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
                rc = proc.wait()
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
            if rc != 0 or not mins:
                lines = err.strip().splitlines()
                raise RuntimeError("ffmpeg could not decode the audio"
                                   + (f": {lines[-1]}" if lines else ""))
            return waveform.PeakPyramid(mins, maxs, OVERVIEW_BUCKET, OVERVIEW_RATE)
        finally:
            if cleanup:
                cleanup()

    def _prune(self) -> None:
        """Keep the overview files under the cache limit, oldest-used first."""
        try:
            files = [(f.stat().st_mtime, f.stat().st_size, f) for f in self.dir.glob("*.qcpk")]
        except OSError:
            return
        total = sum(s for _, s, _ in files)
        for _, size, f in sorted(files):
            if total <= self._cache_limit:
                break
            with contextlib.suppress(OSError):
                f.unlink()
                total -= size

    # --- detail ---------------------------------------------------------------

    def native_rate(self, src: AudioSource) -> int:
        """The stream's sample rate, capped at 48 kHz (probed once per source)."""
        with self._lock:
            known = src.key in self._rates
            rate = self._rates.get(src.key)
        if not known:
            inp, cleanup = src.open()
            try:
                rate = waveform.native_rate(inp, src.audio, src.input_flags)
            finally:
                if cleanup:
                    cleanup()
            with self._lock:
                self._rates[src.key] = rate
        return min(rate or MAX_RATE, MAX_RATE)

    def decode_window(self, src: AudioSource, start: float, duration: float, rate: int):
        """``(samples, t0)``: ``duration`` seconds decoded from ``t0``, which is ``start``
        moved onto the sample grid. A seek between two samples starts on whichever the
        decoder rounds to (PCM and AAC: the nearer; DTS: not always), so every decode,
        snap and cut asks for a time on the grid, and they all agree on the sample."""
        t0 = round(start * rate) / rate
        inp, cleanup = src.open()
        try:
            return waveform.decode(inp, audio=src.audio, rate=rate, start=t0,
                                   duration=duration, input_flags=src.input_flags), t0
        finally:
            if cleanup:
                cleanup()


def peaks_response(store: PeakStore, src: AudioSource, start: float, end: float | None,
                   bins: int, want_keyframes: bool) -> tuple[int, dict]:
    """Status code and body for ``GET /api/items/peaks``."""
    pyr = store.get(src.key)
    overview = store.status(src.key) if pyr is not None else store.ensure(src)
    duration = src.duration or (pyr.duration if pyr is not None else None)
    if end is None:
        end = duration
    if end is None:
        raise ValueError("The file's duration is unknown: pass end.")
    if duration:
        end = min(end, duration)
    if not end > start:
        raise ValueError("end must be after start.")
    bins = max(1, min(bins, MAX_BINS))
    span = end - start
    body: dict = {"start": start, "end": end, "bins": bins, "audio": src.audio,
                  "duration": duration, "overview": overview,
                  "peak": pyr.peak() if pyr is not None else None}
    use_detail = span <= DETAIL_MAX and (
        pyr is None or span / bins < 2 * pyr.bucket_seconds)
    if use_detail:
        native = store.native_rate(src)
        rate = native if span <= DETAIL_FULL_RATE_MAX else min(native, DETAIL_RATE)
        samples, t0 = store.decode_window(src, start, span, rate)
        if not samples:
            raise RuntimeError("No audio in that window.")
        body.update(source="detail", rate=rate)
        if len(samples) <= bins:
            body.update(kind="samples", t0=round(t0, 9), count=len(samples), data=encode(samples))
        else:
            body.update(kind="peaks", data=encode(waveform.bins(samples, bins)))
        if body["peak"] is None:
            body["peak"] = max(abs(min(samples)), abs(max(samples)))
    elif pyr is not None:
        body.update(source="overview", rate=pyr.rate, kind="peaks",
                    data=encode(pyr.bins(start, end, bins)))
    else:
        body.update(kind="pending")
        return 202, body
    if want_keyframes and src.video_input and span <= KEYFRAME_MAX:
        body["keyframes"] = waveform.keyframes(src.video_input, start, end)
    return 200, body


KEYFRAME_MAX = 60.0  # seconds: reading packets over a longer window costs too much I/O
SNAP_RADIUS = {"zero": 0.01, "silence": 1.0}
SNAP_MAX_RADIUS = {"zero": 0.5, "silence": 10.0}


def snap(store: PeakStore, src: AudioSource, t: float, mode: str, radius: float | None,
         threshold_db: float) -> dict:
    """Body for ``GET /api/items/snap``: the nearest zero crossing or quiet point."""
    r = SNAP_RADIUS[mode] if radius is None else min(radius, SNAP_MAX_RADIUS[mode])
    start = max(0.0, t - r)
    end = t + r
    if src.duration:
        end = min(end, src.duration)
    rate = store.native_rate(src) if mode == "zero" else min(store.native_rate(src), DETAIL_RATE)
    samples, t0 = store.decode_window(src, start, max(end - start, 1 / rate), rate)
    out: dict = {"t": t, "mode": mode, "radius": r, "rate": rate, "audio": src.audio}
    if not samples:
        return {**out, "found": False, "snapped": t, "delta": 0.0}
    index = round((t - t0) * rate)
    if mode == "zero":
        i = waveform.nearest_zero_crossing(samples, index, round(r * rate))
        found = i is not None
        if i is None:
            i = min(max(index, 0), len(samples) - 1)
        out["value"] = samples[i]
    else:
        i, level, found = waveform.nearest_quiet(samples, index, rate, threshold_db=threshold_db)
        out["level_db"] = round(level, 1) if level != float("-inf") else None
        out["threshold_db"] = threshold_db
    snapped = round(t0 + i / rate, 6)
    return {**out, "found": found, "snapped": snapped, "delta": round(snapped - t, 6)}
