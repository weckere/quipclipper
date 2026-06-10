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
import threading
from pathlib import Path

from quipclipper.models import Cue
from quipclipper.subtitles import find_sidecar, resolve_subtitles


class SubtitleCache:
    def __init__(self, cache_dir: Path) -> None:
        self._dir = cache_dir / "sub_cache"
        self._dir.mkdir(parents=True, exist_ok=True)
        # Per-key locks dedupe concurrent cold extractions of the same
        # (video, track) — e.g. the VTT track and the script panel both
        # request the same subtitles when an item is opened.
        self._inflight: dict[str, threading.Lock] = {}
        self._inflight_guard = threading.Lock()

    def _source_mtime(self, video: Path) -> float:
        """Mtime of the subtitle source — sidecar if present, else the video."""
        sidecar = find_sidecar(video)
        target = sidecar if sidecar else video
        try:
            return Path(target).stat().st_mtime
        except OSError:
            return 0.0

    def _cache_key(self, video: Path, track: int | None = None) -> str:
        # Track must be part of the key: different subtitle streams in the
        # same container have different cues.  None = backend auto-select.
        tkey = "auto" if track is None else str(track)
        raw = f"{video}:{self._source_mtime(video)}:{tkey}"
        return hashlib.sha256(raw.encode()).hexdigest()

    def _cache_path(self, video: Path, track: int | None = None) -> Path:
        return self._dir / (self._cache_key(video, track) + ".json")

    def _inflight_lock(self, key: str) -> threading.Lock:
        with self._inflight_guard:
            lk = self._inflight.get(key)
            if lk is None:
                lk = threading.Lock()
                self._inflight[key] = lk
            return lk

    def is_cached(self, video: Path, track: int | None = None) -> bool:
        try:
            return self._cache_path(video, track).exists()
        except OSError:
            return False

    def _read(self, cp: Path) -> list[Cue] | None:
        if not cp.exists():
            return None
        try:
            data = json.loads(cp.read_text())
            cues_raw = data["cues"] if isinstance(data, dict) else data
            return [
                Cue(index=c["index"], start=c["start"], end=c["end"], text=c["text"])
                for c in cues_raw
            ]
        except (json.JSONDecodeError, KeyError, TypeError):
            cp.unlink(missing_ok=True)
            return None

    def _write(self, cp: Path, video: Path, cues: list[Cue]) -> None:
        """Atomically write cues to a cache path (temp file + rename)."""
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

    def resolve(self, video: Path, track: int | None = None) -> list[Cue]:
        cp = self._cache_path(video, track)

        # Fast path: serve from cache without taking any lock.
        cues = self._read(cp)
        if cues is not None:
            return cues

        # Cold path: serialize concurrent extractions of the same key so we
        # only run ffmpeg once.  Re-check the cache after acquiring the lock.
        with self._inflight_lock(self._cache_key(video, track)):
            cues = self._read(cp)
            if cues is not None:
                return cues

            resolved = resolve_subtitles(subs=None, video=video, track=track)
            cues = resolved.cues
            self._write(cp, video, cues)

            # Dual-key: when auto-selecting (track is None), also store under
            # the concrete track index that auto-selection landed on.  That way
            # pre-indexing (which resolves None) warms the cache for the item
            # view, which requests subtitles by that explicit index.
            if track is None and resolved.track is not None:
                concrete = self._cache_path(video, resolved.track)
                if concrete != cp and self._read(concrete) is None:
                    self._write(concrete, video, cues)

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
