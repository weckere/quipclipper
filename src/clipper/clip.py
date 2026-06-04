"""Compute clip boundaries and cut them out with ffmpeg.

Lossless mode (the default for audio/video) uses ffmpeg stream copy (``-c copy``)
so the original encoded bytes are copied straight through — no re-encode, no
quality loss, and very fast. The tradeoff, inherent to every codec, is that a
copy can only start at a keyframe: with ``-ss`` placed before ``-i`` ffmpeg snaps
the start to the nearest keyframe at or before the requested time, so a lossless
clip may begin a fraction early. For dialogue clips that is just a little extra
lead-in (and we already pad the start), so it is a non-issue. When you need
frame-exact boundaries or a specific codec/container, pass ``lossless=False`` to
re-encode. GIF output is always a re-encode.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from clipper.models import Match, format_timestamp

# Source audio codec -> a container extension that can hold it via stream copy.
LOSSLESS_AUDIO_EXT = {
    "aac": "m4a",
    "alac": "m4a",
    "ac3": "ac3",
    "eac3": "eac3",
    "mp3": "mp3",
    "opus": "opus",
    "vorbis": "ogg",
    "flac": "flac",
    "dts": "dts",
    "truehd": "thd",
    "pcm_s16le": "wav",
    "pcm_s24le": "wav",
    "pcm_s32le": "wav",
}
# Matroska holds essentially any codec, so it is the safe lossless fallback.
FALLBACK_AUDIO_EXT = "mka"
LOSSLESS_VIDEO_EXT = "mkv"
# Extensions used when re-encoding instead of stream-copying.
REENCODE_EXT = {"audio": "mp3", "video": "mp4"}


@dataclass(frozen=True)
class ClipRange:
    start: float
    end: float

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


def compute_range(
    match: Match,
    *,
    before: float = 0.5,
    after: float = 0.5,
) -> ClipRange:
    """Clip the matched cues' own span, padded by `before`/`after` seconds.

    `before` extends the start earlier; `after` extends the end later. The start
    is clamped to 0 so padding before the opening line never goes negative.
    """
    start = max(0.0, match.start - before)
    end = match.end + after
    return ClipRange(start=start, end=end)


def probe_audio_codec(source: str | Path) -> str | None:
    """Return the codec name of the first audio stream, or None if unavailable."""
    if shutil.which("ffprobe") is None:
        return None
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "a:0",
        "-show_entries", "stream=codec_name",
        "-of", "json", str(source),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        return None
    streams = json.loads(proc.stdout or "{}").get("streams", [])
    return streams[0].get("codec_name") if streams else None


def output_extension(kind: str, *, lossless: bool, audio_codec: str | None = None) -> str:
    """Pick a file extension appropriate for the kind and copy/encode mode."""
    if kind == "gif":
        return "gif"
    if not lossless:
        return REENCODE_EXT[kind]
    if kind == "video":
        return LOSSLESS_VIDEO_EXT
    # lossless audio: match the source codec's natural container
    if audio_codec and audio_codec in LOSSLESS_AUDIO_EXT:
        return LOSSLESS_AUDIO_EXT[audio_codec]
    return FALLBACK_AUDIO_EXT


def _ffmpeg_args(
    *,
    source: Path,
    rng: ClipRange,
    kind: str,
    out: Path,
    lossless: bool,
    fps: int,
    width: int,
) -> list[str]:
    # -ss before -i seeks fast (to a keyframe for copy; decode-accurate for
    # re-encode on modern ffmpeg); -t gives the duration unambiguously.
    base = [
        "ffmpeg", "-y", "-v", "error",
        "-ss", f"{rng.start:.3f}",
        "-i", str(source),
        "-t", f"{rng.duration:.3f}",
    ]
    if kind == "gif":
        # Scale and cap fps; height -1 preserves aspect ratio. Always re-encoded.
        vf = f"fps={fps},scale={width}:-1:flags=lanczos"
        return base + ["-an", "-vf", vf, str(out)]
    if lossless:
        # -avoid_negative_ts make_zero cleans up timestamps after a keyframe seek.
        common = ["-c", "copy", "-avoid_negative_ts", "make_zero"]
        if kind == "audio":
            return base + ["-map", "0:a:0", "-vn"] + common + [str(out)]
        return base + ["-map", "0:v:0?", "-map", "0:a?"] + common + [str(out)]
    # re-encode
    if kind == "audio":
        return base + ["-vn", str(out)]
    return base + ["-c:v", "libx264", "-c:a", "aac", "-movflags", "+faststart", str(out)]


def cut_clip(
    source: str | Path,
    rng: ClipRange,
    *,
    kind: str = "audio",
    lossless: bool = True,
    out: str | Path | None = None,
    fps: int = 15,
    width: int = 480,
) -> Path:
    """Cut `source` between `rng.start` and `rng.end` into the chosen `kind`.

    With `lossless=True` (default) audio/video are stream-copied (no quality
    loss); GIF is always re-encoded. Returns the path written. Requires ffmpeg.
    """
    source = Path(source)
    if kind not in ("audio", "video", "gif"):
        raise ValueError(f"kind must be audio, video, or gif, got {kind!r}")
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg not found on PATH. Install ffmpeg to cut clips.")
    if not source.exists():
        raise RuntimeError(f"Video file not found: {source}")

    if out is None:
        codec = probe_audio_codec(source) if (lossless and kind == "audio") else None
        ext = output_extension(kind, lossless=lossless, audio_codec=codec)
        ts = format_timestamp(rng.start).replace(":", "-").replace(".", "_")
        out = source.with_name(f"{source.stem}_{ts}.{ext}")
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)

    args = _ffmpeg_args(
        source=source, rng=rng, kind=kind, out=out, lossless=lossless, fps=fps, width=width
    )
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed:\n{proc.stderr.strip()}")
    return out
