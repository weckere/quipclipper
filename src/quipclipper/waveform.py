"""Waveform peaks and cue-point snapping, for drawing and trimming at any zoom.

Audio comes from ffmpeg as signed 16-bit mono PCM (``array('h')``); everything
else here is plain arithmetic on those arrays, so it needs no numpy. The same
maths as LeadStinger's ``src/waveform.rs`` (peak pyramid, outward zero-crossing
search), kept consistent so both apps snap the same way.

- :class:`PeakPyramid` holds min/max peaks at power-of-eight resolutions, so a
  whole film draws in a few thousand slice scans.
- :func:`bins` turns raw samples into ``count`` min/max pairs (the detail view).
- :func:`nearest_zero_crossing` and :func:`nearest_quiet` are the snap helpers.
"""

from __future__ import annotations

import math
import subprocess
import sys
from array import array
from pathlib import Path
from typing import Iterator, Sequence

# Pyramid levels merge this many buckets of the level below.
FACTOR = 8
# Full scale of a signed 16-bit sample.
FULL_SCALE = 32768.0


def _empty() -> array:
    return array("h")


def minmax_buckets(samples: Sequence[int], bucket: int) -> tuple[array, array]:
    """Min and max of each ``bucket`` samples (the last bucket may be short)."""
    if bucket < 1:
        raise ValueError("bucket must be at least 1")
    mins, maxs = _empty(), _empty()
    for i in range(0, len(samples), bucket):
        chunk = samples[i:i + bucket]
        mins.append(min(chunk))
        maxs.append(max(chunk))
    return mins, maxs


class PeakPyramid:
    """Min/max peaks of a mono signal at ``bucket * FACTOR**k`` samples per entry.

    ``levels[0]`` holds one (min, max) per ``bucket`` samples; each later level
    merges ``FACTOR`` entries of the one below, down to a single entry.
    """

    def __init__(self, mins: array, maxs: array, bucket: int, rate: float) -> None:
        if len(mins) != len(maxs):
            raise ValueError("mins and maxs differ in length")
        self.bucket = bucket
        self.rate = float(rate)
        self.levels: list[tuple[array, array]] = [(mins, maxs)]
        while len(self.levels[-1][0]) > 1:
            lo, hi = self.levels[-1]
            self.levels.append((
                array("h", (min(lo[i:i + FACTOR]) for i in range(0, len(lo), FACTOR))),
                array("h", (max(hi[i:i + FACTOR]) for i in range(0, len(hi), FACTOR))),
            ))

    @classmethod
    def from_samples(cls, samples: Sequence[int], bucket: int, rate: float) -> "PeakPyramid":
        mins, maxs = minmax_buckets(samples, bucket)
        return cls(mins, maxs, bucket, rate)

    @property
    def entries(self) -> int:
        """Entries at the finest level."""
        return len(self.levels[0][0])

    @property
    def bucket_seconds(self) -> float:
        return self.bucket / self.rate

    @property
    def duration(self) -> float:
        """Seconds covered (the last bucket counted whole)."""
        return self.entries * self.bucket_seconds

    def peak(self) -> int:
        """The largest absolute sample anywhere (0 for silence or no audio)."""
        lo, hi = self.levels[-1]
        if not lo:
            return 0
        return max(abs(lo[0]), abs(hi[0]))

    def range(self, a: int, b: int) -> tuple[int, int] | None:
        """Exact (min, max) over level-0 entries ``a..b``, or None when empty. Each
        step takes the coarsest entry that starts at the cursor and ends inside the
        range, so it costs about ``FACTOR * levels`` comparisons."""
        a, b = max(0, a), min(self.entries, b)
        if a >= b:
            return None
        lo_v, hi_v = 32767, -32768
        pos = a
        while pos < b:
            k = 0
            while (k + 1 < len(self.levels) and pos % FACTOR ** (k + 1) == 0
                   and pos + FACTOR ** (k + 1) <= b):
                k += 1
            width = FACTOR ** k
            if k == 0:
                # Finest level: take the run up to the next aligned boundary in one slice.
                nxt = min(b, (pos // FACTOR + 1) * FACTOR)
                if nxt == pos:
                    nxt = pos + 1
                lo, hi = self.levels[0]
                lo_v, hi_v = min(lo_v, min(lo[pos:nxt])), max(hi_v, max(hi[pos:nxt]))
                pos = nxt
            else:
                lo, hi = self.levels[k]
                j = pos // width
                lo_v, hi_v = min(lo_v, lo[j]), max(hi_v, hi[j])
                pos += width
        return lo_v, hi_v

    def bins(self, start: float, end: float, count: int) -> array:
        """``count`` interleaved (min, max) pairs covering ``start..end`` seconds.

        Bin ``i`` covers level-0 entries ``floor(first + i*per_bin)`` up to
        ``floor(first + (i+1)*per_bin)`` (at least one), merged exactly through
        :meth:`range`. A bin outside the audio is (0, 0)."""
        if count < 1 or not end > start:
            return array("h", [0, 0] * max(0, count))
        first = start / self.bucket_seconds
        per_bin = (end - start) / self.bucket_seconds / count
        out = array("h")
        for i in range(count):
            a = math.floor(first + i * per_bin)
            b = max(math.floor(first + (i + 1) * per_bin), a + 1)
            out.extend(self.range(a, b) or (0, 0))
        return out

    # --- persistence: a small binary file, header line then the level-0 arrays ---

    def to_bytes(self) -> bytes:
        head = f"QCPK1 {self.bucket} {self.rate:g} {self.entries}\n".encode()
        lo, hi = self.levels[0]
        return head + lo.tobytes() + hi.tobytes()

    @classmethod
    def from_bytes(cls, data: bytes) -> "PeakPyramid":
        nl = data.index(b"\n")
        magic, bucket, rate, entries = data[:nl].decode().split()
        if magic != "QCPK1":
            raise ValueError("not a quipclipper peak file")
        n = int(entries)
        body = memoryview(data)[nl + 1:]
        if len(body) != 4 * n:
            raise ValueError("peak file is truncated")
        lo, hi = array("h"), array("h")
        lo.frombytes(body[:2 * n])
        hi.frombytes(body[2 * n:])
        return cls(lo, hi, int(bucket), float(rate))


def _bin_pairs(mins: Sequence[int], maxs: Sequence[int], first: float, per_bin: float,
               count: int) -> array:
    """Bin ``i`` covers entries ``floor(first + i*per_bin) .. floor(first + (i+1)*per_bin)``;
    zoomed past one entry per bin it shows that single entry (as LeadStinger does)."""
    out = array("h")
    n = len(mins)
    for i in range(count):
        a = math.floor(first + i * per_bin)
        b = math.floor(first + (i + 1) * per_bin)
        if b <= a:
            b = a + 1
        a, b = max(0, a), min(n, b)
        if a >= b:
            out.extend((0, 0))
        else:
            out.extend((min(mins[a:b]), max(maxs[a:b])))
    return out


def bins(samples: Sequence[int], count: int, first: float = 0.0,
         per_bin: float | None = None) -> array:
    """``count`` interleaved (min, max) pairs over raw ``samples``: by default the
    whole array split evenly; ``first``/``per_bin`` (in samples) pick a span."""
    if count < 1:
        return array("h")
    if per_bin is None:
        per_bin = len(samples) / count
    return _bin_pairs(samples, samples, first, per_bin, count)


def nearest_zero_crossing(samples: Sequence[int], index: int, max_distance: int) -> int | None:
    """The zero crossing nearest ``index``, or None within ``max_distance`` samples.

    A crossing is a sample that is exactly 0, or, across a sign change, whichever
    of the two samples is smaller in magnitude (so a cut there starts as close to
    silence as the signal allows). Ties go to the earlier sample."""
    n = len(samples)
    if n == 0:
        return None
    index = min(max(index, 0), n - 1)
    lo, hi = max(0, index - max_distance), min(n - 1, index + max_distance)
    best: int | None = None
    for i in range(max(0, lo - 1), hi + 1):
        s = samples[i]
        if s == 0:
            c = i
        elif i + 1 < n and ((s < 0 < samples[i + 1]) or (samples[i + 1] < 0 < s)):
            c = i if abs(s) <= abs(samples[i + 1]) else i + 1
        else:
            continue
        if lo <= c <= hi and (best is None or abs(c - index) < abs(best - index)):
            best = c
    return best


def rms_db(samples: Sequence[int]) -> float:
    """RMS level of ``samples`` in dBFS (-inf for digital silence)."""
    if not samples:
        return float("-inf")
    power = math.fsum(s * s for s in samples) / len(samples)
    return 10 * math.log10(power / (FULL_SCALE * FULL_SCALE)) if power else float("-inf")


def nearest_quiet(samples: Sequence[int], index: int, rate: float, *,
                  threshold_db: float = -45.0, window: float = 0.02,
                  hop: float = 0.005) -> tuple[int, float, bool]:
    """The centre of the quiet window nearest ``index``: ``(sample, level_db, quiet)``.

    Windows of ``window`` seconds step by ``hop`` across ``samples``; a window is
    quiet when its RMS is below ``threshold_db``. With no quiet window, returns the
    quietest one and ``quiet=False``."""
    n = len(samples)
    w = max(1, round(window * rate))
    step = max(1, round(hop * rate))
    if n == 0:
        return index, float("-inf"), False
    if n <= w:
        level = rms_db(samples)
        return n // 2, level, level < threshold_db
    best: tuple[float, int, float] | None = None  # (distance, centre, level)
    quietest: tuple[float, int] | None = None
    for a in range(0, n - w + 1, step):
        centre = a + w // 2
        level = rms_db(samples[a:a + w])
        if quietest is None or level < quietest[0]:
            quietest = (level, centre)
        if level < threshold_db:
            dist = abs(centre - index)
            if best is None or dist < best[0]:
                best = (dist, centre, level)
    if best is not None:
        return best[1], best[2], True
    assert quietest is not None
    return quietest[1], quietest[0], False


# --- decoding --------------------------------------------------------------------


def decode_args(source: str | Path, *, audio: int = 0, rate: int | None = None,
                start: float | None = None, duration: float | None = None,
                input_flags: Sequence[str] = ()) -> list[str]:
    """ffmpeg arguments that write one audio stream as s16le mono to stdout.

    ``-ss`` before ``-i`` with a decode is sample-accurate, the same seek the
    clip cutter uses, so a time here is a time there."""
    cmd = ["ffmpeg", "-nostdin", "-v", "error", *input_flags]
    if start:
        cmd += ["-ss", f"{start:.6f}"]
    cmd += ["-i", str(source)]
    if duration is not None:
        cmd += ["-t", f"{duration:.6f}"]
    cmd += ["-map", f"0:a:{audio}", "-vn", "-sn", "-dn", "-ac", "1"]
    if rate:
        cmd += ["-ar", str(rate)]
    return cmd + ["-f", "s16le", "-acodec", "pcm_s16le", "-"]


def _to_array(raw: bytes) -> array:
    a = array("h")
    a.frombytes(raw[: len(raw) // 2 * 2])
    if a.itemsize != 2:  # pragma: no cover - array('h') is 16-bit everywhere we run
        raise RuntimeError("array('h') is not 16-bit here")
    if sys.byteorder == "big":  # pragma: no cover
        a.byteswap()
    return a


def decode(source: str | Path, *, audio: int = 0, rate: int | None = None,
           start: float | None = None, duration: float | None = None,
           input_flags: Sequence[str] = (), timeout: float = 120) -> array:
    """Decode a span of one audio stream to mono s16 samples."""
    try:
        r = subprocess.run(decode_args(source, audio=audio, rate=rate, start=start,
                                       duration=duration, input_flags=input_flags),
                           capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"ffmpeg took over {timeout:.0f} s to decode the audio") from exc
    if r.returncode != 0:
        msg = r.stderr.decode(errors="replace").strip().splitlines()
        raise RuntimeError("ffmpeg could not decode the audio"
                           + (f": {msg[-1]}" if msg else ""))
    return _to_array(r.stdout)


def stream_buckets(proc_stdout, bucket: int, chunk_samples: int = 1 << 16
                   ) -> Iterator[tuple[array, array, int]]:
    """Read s16le from a pipe and yield (mins, maxs, samples_so_far) per chunk, so a
    whole film is bucketed without holding its samples in memory."""
    carry = b""
    total = 0
    step = chunk_samples * 2
    step -= step % (bucket * 2)  # whole buckets per chunk
    step = max(step, bucket * 2)
    while True:
        data = proc_stdout.read(step - len(carry))
        if not data:
            break
        carry += data
        if len(carry) < step:
            continue
        a = _to_array(carry)
        carry = b""
        total += len(a)
        yield (*minmax_buckets(a, bucket), total)
    if len(carry) >= 2:
        a = _to_array(carry)
        total += len(a)
        yield (*minmax_buckets(a, bucket), total)


def native_rate(source: str | Path, audio: int = 0, input_flags: Sequence[str] = ()) -> int | None:
    """The sample rate of audio stream ``a:audio``, or None when ffprobe can't tell."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", *input_flags, "-select_streams", f"a:{audio}",
             "-show_entries", "stream=sample_rate", "-of", "csv=p=0", str(source)],
            capture_output=True, text=True, timeout=20)
    except (subprocess.TimeoutExpired, OSError):
        return None
    try:
        return int(r.stdout.strip().splitlines()[0])
    except (ValueError, IndexError):
        return None


def keyframes(source: str | Path, start: float, end: float) -> list[float]:
    """Video keyframe times in ``start..end`` (empty for audio, or when ffprobe fails).
    A lossless (stream-copy) video cut can only start on one of these."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "packet=pts_time,flags", "-of", "csv=p=0",
             "-read_intervals", f"{max(0.0, start - 1):.3f}%{end:.3f}", str(source)],
            capture_output=True, text=True, timeout=20)
    except (subprocess.TimeoutExpired, OSError):
        return []
    out = []
    for line in r.stdout.splitlines():
        pts, _, flags = line.partition(",")
        if "K" not in flags:
            continue
        try:
            t = float(pts)
        except ValueError:
            continue
        if start <= t <= end:
            out.append(round(t, 6))
    return sorted(set(out))
