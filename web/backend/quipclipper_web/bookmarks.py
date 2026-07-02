"""Persistent bookmark store.

Bookmarks are per-video-file named timestamps and in/out pairs, stored as a
JSON file in ``state_dir``.  Concurrency is coarse-grained (a threading lock
around read/write cycles) — fine for a single-user LAN app.

Each bookmark has:
- ``id``:    auto-generated UUID
- ``path``:  absolute path to the source video (matches the media root paths)
- ``label``: user-provided name (e.g. "Disappointed!")
- ``start``: in-point in seconds
- ``end``:   out-point in seconds (may equal start for a single timestamp)
- ``created``: ISO-8601 timestamp
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class Bookmark:
    id: str
    path: str
    label: str
    start: float
    end: float
    # Padding applied around the cue range at preview/clip time. Part of the
    # selection (B16); defaults keep older bookmarks (which lack these) valid.
    before: float = 0.0
    after: float = 0.0
    # Stream selection saved with the bookmark (B17): subtitle track index,
    # audio stream index, and channel subset (whole/front/center/lfe/side/back).
    sub_track: int | None = None
    audio_track: int | None = None
    channel_subset: str | None = None
    # The matched dialogue line, kept so exports from a bookmark fill the {cue}
    # naming token (older bookmarks lack it and export with an empty {cue}).
    cue: str = ""
    created: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_stored(cls, d: dict) -> "Bookmark":
        """Build from a stored JSON record, ignoring unknown keys.

        A record written by a newer version (or hand-edited) may carry extra
        keys; ``Bookmark(**d)`` would raise TypeError and take down the whole
        list/get. Drop anything that isn't a dataclass field so one odd record
        can't break every read."""
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


class BookmarkStore:
    """Thread-safe, file-backed bookmark store."""

    def __init__(self, state_dir: Path) -> None:
        self._file = state_dir / "bookmarks.json"
        self._lock = threading.Lock()
        state_dir.mkdir(parents=True, exist_ok=True)

    def _read(self) -> list[dict]:
        if not self._file.exists():
            return []
        try:
            return json.loads(self._file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []

    def _write(self, data: list[dict]) -> None:
        tmp = self._file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(self._file)

    def list_for_path(self, path: str) -> list[Bookmark]:
        """Return all bookmarks for a given video path, newest first."""
        with self._lock:
            raw = self._read()
        return sorted(
            [Bookmark.from_stored(d) for d in raw if d.get("path") == path],
            key=lambda b: b.created,
            reverse=True,
        )

    def list_all(self, limit: int = 500) -> list[Bookmark]:
        """Return all bookmarks across all files, newest first."""
        with self._lock:
            raw = self._read()
        bookmarks = sorted(
            [Bookmark.from_stored(d) for d in raw],
            key=lambda b: b.created,
            reverse=True,
        )
        return bookmarks[:limit]

    def add(
        self, path: str, label: str, start: float, end: float,
        before: float = 0.0, after: float = 0.0,
        sub_track: int | None = None, audio_track: int | None = None,
        channel_subset: str | None = None, cue: str = "",
    ) -> Bookmark:
        """Create a new bookmark and persist it."""
        bm = Bookmark(
            id=uuid.uuid4().hex[:12],
            path=path,
            label=label,
            start=start,
            end=end,
            before=before,
            after=after,
            sub_track=sub_track,
            audio_track=audio_track,
            channel_subset=channel_subset,
            cue=cue,
        )
        with self._lock:
            data = self._read()
            data.append(bm.to_dict())
            self._write(data)
        return bm

    # Fields a client may change on an existing bookmark.
    _UPDATABLE = ("label", "before", "after", "start", "end")

    def update(self, bookmark_id: str, **changes) -> Bookmark | None:
        """Apply changes to a bookmark and persist. Returns the updated bookmark,
        or None if not found. Only ``_UPDATABLE`` fields with non-None values
        are applied."""
        changes = {k: v for k, v in changes.items() if k in self._UPDATABLE and v is not None}
        with self._lock:
            data = self._read()
            for d in data:
                if d.get("id") == bookmark_id:
                    d.update(changes)
                    self._write(data)
                    return Bookmark.from_stored(d)
        return None

    def delete(self, bookmark_id: str) -> bool:
        """Remove a bookmark by id. Returns True if found and deleted."""
        with self._lock:
            data = self._read()
            filtered = [d for d in data if d.get("id") != bookmark_id]
            if len(filtered) == len(data):
                return False
            self._write(filtered)
            return True

    def clear_all(self) -> int:
        """Delete every bookmark. Returns the number removed."""
        with self._lock:
            data = self._read()
            n = len(data)
            self._write([])
            return n

    def get(self, bookmark_id: str) -> Bookmark | None:
        """Look up a single bookmark by id."""
        with self._lock:
            for d in self._read():
                if d.get("id") == bookmark_id:
                    return Bookmark.from_stored(d)
        return None
