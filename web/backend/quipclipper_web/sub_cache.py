"""Cache for extracted subtitle cues.

Wraps resolve_subtitles with a file-based JSON cache keyed by video path
and mtime, so repeated searches skip the expensive ffmpeg extraction.
"""

from __future__ import annotations

import hashlib
import json
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

    def resolve(self, video: Path, track: int | None = None) -> list[Cue]:
        cp = self._cache_path(video)
        if cp.exists():
            data = json.loads(cp.read_text())
            return [Cue(index=c["index"], start=c["start"], end=c["end"], text=c["text"]) for c in data]

        resolved = resolve_subtitles(subs=None, video=video, track=track)
        cues = resolved.cues

        cp.write_text(json.dumps(
            [{"index": c.index, "start": c.start, "end": c.end, "text": c.text} for c in cues],
        ))
        return cues
