"""Compute clip boundaries and cut them out with ffmpeg."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from clipper.models import Match, format_timestamp

# Output kind -> default file extension.
DEFAULT_EXT = {"audio": "mp3", "video": "mp4", "gif": "gif"}


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


def _ffmpeg_args(
    *,
    source: Path,
    rng: ClipRange,
    kind: str,
    out: Path,
    fps: int,
    width: int,
) -> list[str]:
    # Seek before -i for a fast keyframe seek, then -ss/-to relative trimming.
    base = ["ffmpeg", "-y", "-v", "error", "-ss", f"{rng.start:.3f}", "-to", f"{rng.end:.3f}", "-i", str(source)]
    if kind == "audio":
        return base + ["-vn", str(out)]
    if kind == "video":
        return base + ["-c:v", "libx264", "-c:a", "aac", "-movflags", "+faststart", str(out)]
    if kind == "gif":
        # Scale and cap fps; flag stills size with -1 to preserve aspect ratio.
        vf = f"fps={fps},scale={width}:-1:flags=lanczos"
        return base + ["-an", "-vf", vf, str(out)]
    raise ValueError(f"Unknown clip kind: {kind!r}")


def cut_clip(
    source: str | Path,
    rng: ClipRange,
    *,
    kind: str = "audio",
    out: str | Path | None = None,
    fps: int = 15,
    width: int = 480,
) -> Path:
    """Cut `source` between `rng.start` and `rng.end` into the chosen `kind`.

    Returns the path of the written clip. Requires ffmpeg on PATH.
    """
    source = Path(source)
    if kind not in DEFAULT_EXT:
        raise ValueError(f"kind must be one of {sorted(DEFAULT_EXT)}, got {kind!r}")
    if out is None:
        ts = format_timestamp(rng.start).replace(":", "-").replace(".", "_")
        out = source.with_name(f"{source.stem}_{ts}.{DEFAULT_EXT[kind]}")
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)

    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg not found on PATH. Install ffmpeg to cut clips.")
    if not source.exists():
        raise RuntimeError(f"Video file not found: {source}")

    args = _ffmpeg_args(source=source, rng=rng, kind=kind, out=out, fps=fps, width=width)
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed:\n{proc.stderr.strip()}")
    return out
