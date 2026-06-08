"""Cache for extracted subtitle cues.

Wraps resolve_subtitles with a file-based JSON cache keyed by video path
and mtime, so repeated searches skip the expensive ffmpeg extraction.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
from pathlib import Path

from quipclipper.models import Cue
from quipclipper.subtitles import resolve_subtitles


class SubtitleCache:
    def __init__(self, cache_dir: Path) -> None:
        self._dir = cache_dir / "sub_cache"
        self._dir.mkdir(parents=True, exist_ok=True)

    def _cache_key(self, video: Path) -> str:
        mtime = video.stat().st_mtime
        raw = f"{video}:{mtime}"
        return hashlib.sha256(raw.encode()).hexdigest()

    def _cache_path(self, video: Path) -> Path:
        return self._dir / (self._cache_key(video) + ".json")

    def is_cached(self, video: Path) -> bool:
        try:
            return self._cache_path(video).exists()
        except OSError:
            return False

    def resolve(self, video: Path, track: int | None = None) -> list[Cue]:
        cp = self._cache_path(video)

        # Read from cache, handling corrupt/partial files gracefully.
        if cp.exists():
            try:
                data = json.loads(cp.read_text())
                return [Cue(index=c["index"], start=c["start"], end=c["end"], text=c["text"]) for c in data]
            except (json.JSONDecodeError, KeyError, TypeError):
                cp.unlink(missing_ok=True)

        resolved = resolve_subtitles(subs=None, video=video, track=track)
        cues = resolved.cues

        # Atomic write: temp file + rename so readers never see partial data.
        payload = json.dumps(
            [{"index": c.index, "start": c.start, "end": c.end, "text": c.text} for c in cues],
        )
        try:
            fd, tmp = tempfile.mkstemp(dir=self._dir, suffix=".tmp")
            with os.fdopen(fd, "w") as f:
                f.write(payload)
            os.replace(tmp, cp)
        except OSError:
            # Best-effort cleanup; extraction result is still returned.
            with contextlib.suppress(OSError):
                os.unlink(tmp)

        return cues
