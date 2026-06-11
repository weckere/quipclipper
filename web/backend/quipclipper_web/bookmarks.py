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
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class Bookmark:
    id: str
    path: str
    label: str
    start: float
    end: float
    created: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        return asdict(self)


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
            [Bookmark(**d) for d in raw if d.get("path") == path],
            key=lambda b: b.created,
            reverse=True,
        )

    def list_all(self, limit: int = 500) -> list[Bookmark]:
        """Return all bookmarks across all files, newest first."""
        with self._lock:
            raw = self._read()
        bookmarks = sorted(
            [Bookmark(**d) for d in raw],
            key=lambda b: b.created,
            reverse=True,
        )
        return bookmarks[:limit]

    def add(self, path: str, label: str, start: float, end: float) -> Bookmark:
        """Create a new bookmark and persist it."""
        bm = Bookmark(
            id=uuid.uuid4().hex[:12],
            path=path,
            label=label,
            start=start,
            end=end,
        )
        with self._lock:
            data = self._read()
            data.append(bm.to_dict())
            self._write(data)
        return bm

    def delete(self, bookmark_id: str) -> bool:
        """Remove a bookmark by id. Returns True if found and deleted."""
        with self._lock:
            data = self._read()
            filtered = [d for d in data if d.get("id") != bookmark_id]
            if len(filtered) == len(data):
                return False
            self._write(filtered)
            return True

    def get(self, bookmark_id: str) -> Bookmark | None:
        """Look up a single bookmark by id."""
        with self._lock:
            for d in self._read():
                if d.get("id") == bookmark_id:
                    return Bookmark(**d)
        return None
