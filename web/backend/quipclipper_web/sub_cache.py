"""Cache for extracted subtitle cues.

Wraps resolve_subtitles with a file-based JSON cache so repeated searches
skip the expensive ffmpeg extraction.

Change detection
----------------
The cache key incorporates the modification time of the *subtitle source*:
the sidecar ``.srt``/``.ass`` mtime when one exists, otherwise the video
file's own mtime (embedded subtitle edits rewrite the container, bumping
that mtime).  So when subtitles change, the key changes and the next
search re-extracts automatically.  ``is_cached()`` therefore doubles as a
"are the indexed subs still current?" check.

Each cache file stores the originating video path so stale entries (left
behind when the source mtime changes) can be located and cleared.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
from pathlib import Path

from quipclipper.models import Cue
from quipclipper.subtitles import find_sidecar, resolve_subtitles


class SubtitleCache:
    def __init__(self, cache_dir: Path) -> None:
        self._dir = cache_dir / "sub_cache"
        self._dir.mkdir(parents=True, exist_ok=True)

    def _source_mtime(self, video: Path) -> float:
        """Mtime of the subtitle source — sidecar if present, else the video."""
        sidecar = find_sidecar(video)
        target = sidecar if sidecar else video
        try:
            return Path(target).stat().st_mtime
        except OSError:
            return 0.0

    def _cache_key(self, video: Path) -> str:
        raw = f"{video}:{self._source_mtime(video)}"
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
                cues_raw = data["cues"] if isinstance(data, dict) else data
                return [
                    Cue(index=c["index"], start=c["start"], end=c["end"], text=c["text"])
                    for c in cues_raw
                ]
            except (json.JSONDecodeError, KeyError, TypeError):
                cp.unlink(missing_ok=True)

        resolved = resolve_subtitles(subs=None, video=video, track=track)
        cues = resolved.cues

        # Atomic write: temp file + rename so readers never see partial data.
        # Store the video path so stale entries can be located later.
        payload = json.dumps(
            {
                "video": str(video),
                "cues": [
                    {"index": c.index, "start": c.start, "end": c.end, "text": c.text}
                    for c in cues
                ],
            },
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

    # --- cache maintenance ---------------------------------------------------

    def _stored_video(self, cache_file: Path) -> str | None:
        """Return the video path recorded in a cache file, if any."""
        try:
            data = json.loads(cache_file.read_text())
        except (json.JSONDecodeError, OSError):
            return None
        return data.get("video") if isinstance(data, dict) else None

    def clear(self, video: Path) -> int:
        """Remove all cache entries for a video (current key + stale orphans).

        Returns the number of cache files removed.
        """
        removed = 0
        target = str(video)
        # Current-key file (covers the common case fast).
        cp = self._cache_path(video)
        if cp.exists():
            with contextlib.suppress(OSError):
                cp.unlink()
                removed += 1
        # Orphans: older entries written under a different source mtime.
        for f in self._dir.glob("*.json"):
            if f == cp:
                continue
            if self._stored_video(f) == target:
                with contextlib.suppress(OSError):
                    f.unlink()
                    removed += 1
        return removed

    def clear_under(self, folder: Path) -> int:
        """Remove every cache entry whose source video lives under *folder*.

        Returns the number of cache files removed.
        """
        removed = 0
        prefix = str(folder)
        for f in self._dir.glob("*.json"):
            stored = self._stored_video(f)
            if stored is not None and (stored == prefix or stored.startswith(prefix + os.sep)):
                with contextlib.suppress(OSError):
                    f.unlink()
                    removed += 1
        return removed
