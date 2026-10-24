"""Re-index single files when another app says their subtitles changed (M20).

Search reads only indexed files (D-042), and an index entry is keyed by the mtime of
its subtitle source, so a subtitle Bazarr downloads or upgrades (or a file Sonarr or
Radarr imports, upgrades or renames) drops that file out of search until it is indexed
again. ``POST /api/index/file`` (``quipclipper index.file``) lets those apps say so:

- **Any path they know.** A media file or a subtitle file (whose media file is found
  next to it by name), in the app's own path space: ``QC_PATH_MAP`` maps container
  mount points onto the paths here (:func:`media_for`). A Sonarr/Radarr webhook body is
  read too (:func:`webhook_paths`).
- **Queued, deduplicated, one at a time.** :class:`FileQueue` keeps the files waiting,
  each once, and one job drains it in the library index's single-worker pool, so it
  never runs beside a library run (D-043). A library run takes them between its own
  files, so a hook never waits behind a two-day run.
- **Idle priority.** The job runs every ffprobe/ffmpeg through :mod:`quipclipper.idle`,
  like the library run (``at_idle_priority``).

A file whose index entry is already current is left alone (``current``), unless the
caller forces a fresh read.
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from quipclipper.subtitles import AUDIO_EXTS, SUBTITLE_EXTS, VIDEO_EXTS, Cancelled

from .jellyfin import map_path

MEDIA_EXTS = frozenset(VIDEO_EXTS) | frozenset(AUDIO_EXTS)
# Results kept for GET /api/index/file (newest last).
RECENT = 50
# Most paths one request may name (a view's Re-index sends up to index_changes.LIST_MAX).
MAX_PATHS = 1000


def map_in(path: str, path_map: tuple[tuple[str, str], ...]) -> str:
    """``path`` as another app reports it → the same path here (``QC_PATH_MAP``). The
    longest matching prefix wins; a path no prefix matches is used as it is."""
    return map_path(path, path_map) or path


def media_for(path: Path) -> Path | None:
    """The media file a subtitle file belongs to: the one in its folder whose name, up
    to its extension, starts the subtitle's name (``Show.S01E01.mkv`` for
    ``Show.S01E01.en.forced.srt``), the longest such name if several do. A media path
    is itself. None when nothing matches."""
    ext = path.suffix.lower()
    if ext in MEDIA_EXTS:
        return path
    if ext not in SUBTITLE_EXTS:
        return None
    low = path.name.lower()
    best: Path | None = None
    try:
        names = os.listdir(path.parent)
    except OSError:
        return None
    for name in names:
        stem, e = os.path.splitext(name)
        if e.lower() in MEDIA_EXTS and low.startswith(stem.lower() + "."):
            if (best is None or len(stem) > len(best.stem)) and (path.parent / name).is_file():
                best = path.parent / name
    return best


def webhook_paths(body: Any) -> tuple[list[str], str | None]:
    """The file paths in a Sonarr or Radarr webhook body (Connect → Webhook), and its
    event type. Import/upgrade (``Download``) carries ``episodeFile`` / ``episodeFiles``
    / ``movieFile``; ``Rename`` carries ``renamedEpisodeFiles`` / ``renamedMovieFiles``
    (their new paths). Other events (``Test``, ``Grab``, …) carry none."""
    if not isinstance(body, dict):
        return [], None
    out: list[str] = []
    for key in ("episodeFile", "movieFile"):
        f = body.get(key)
        if isinstance(f, dict) and isinstance(f.get("path"), str):
            out.append(f["path"])
    for key in ("episodeFiles", "renamedEpisodeFiles", "renamedMovieFiles"):
        files = body.get(key)
        for f in files if isinstance(files, list) else []:
            if isinstance(f, dict) and isinstance(f.get("path"), str):
                out.append(f["path"])
    event = body.get("eventType")
    return list(dict.fromkeys(out)), event if isinstance(event, str) else None


class FileQueue:
    """Files waiting to be re-indexed, each once, drained by one job at a time.

    :meth:`add` queues files and, when no drainer job is pending, submits one (through
    ``submit``, which returns its job id). The drainer takes files with :meth:`take`; the
    ``take`` that finds the queue empty also retires it, under the same lock as
    :meth:`add`, so a file added as the job finishes always gets a new job. A library run
    takes files with :meth:`take_nowait`, which never retires the drainer.

    ``alive(job_id)`` says whether the drainer will still take files: a job cancelled
    while queued never runs, and one that failed or is being cancelled stops, so
    :meth:`add` submits a new one rather than leaving files waiting on a dead job.
    ``take`` and ``retire`` name their job, so an old one stopping never retires the
    drainer that replaced it.
    """

    def __init__(self, now: Callable[[], float] = time.time,
                 alive: Callable[[str], bool] = lambda _id: True) -> None:
        self._lock = threading.Lock()
        self._pending: dict[str, tuple[Path, bool]] = {}  # path -> (path, force)
        self._job_id: str | None = None
        self._recent: deque[dict[str, Any]] = deque(maxlen=RECENT)
        self._now = now
        self._alive = alive

    def add(self, files: list[tuple[Path, bool]],
            submit: Callable[[], str]) -> tuple[str | None, list[bool]]:
        """Queue ``(file, force)`` pairs. Returns the drainer job's id and, per file,
        whether it was newly queued (False: it was waiting already; a force request
        upgrades the waiting one)."""
        added: list[bool] = []
        with self._lock:
            for f, force in files:
                key = str(f)
                prev = self._pending.get(key)
                added.append(prev is None)
                self._pending[key] = (f, force or bool(prev and prev[1]))
            if self._job_id is not None and not self._alive(self._job_id):
                self._job_id = None
            if self._pending and self._job_id is None:
                try:
                    self._job_id = submit()
                except BaseException:
                    for (f, _), new in zip(files, added):
                        if new:
                            self._pending.pop(str(f), None)
                    raise
            return self._job_id, added

    def take(self, job_id: str | None = None) -> tuple[Path, bool] | None:
        """The next file for the drainer job ``job_id``; None (and the job is retired)
        when empty."""
        with self._lock:
            if not self._pending:
                if job_id is None or self._job_id == job_id:
                    self._job_id = None
                return None
            return self._pending.pop(next(iter(self._pending)))

    def take_nowait(self) -> tuple[Path, bool] | None:
        """The next file, for a library run between its own files."""
        with self._lock:
            if not self._pending:
                return None
            return self._pending.pop(next(iter(self._pending)))

    def release(self) -> str | None:
        """A library run emptied the queue between its own files: the drainer job still
        waiting behind it has nothing left to do. Returns its id (to cancel) and forgets
        it, so the queue reads idle; a file added later gets a new job."""
        with self._lock:
            if self._pending or self._job_id is None:
                return None
            job_id, self._job_id = self._job_id, None
            return job_id

    def put_back(self, f: Path, force: bool) -> None:
        """Return a file a library run took but didn't finish (it was cancelled, not
        the hook), first in line."""
        with self._lock:
            key = str(f)
            prev = self._pending.pop(key, None)
            self._pending = {key: (f, force or bool(prev and prev[1])), **self._pending}

    def retire(self, job_id: str | None = None) -> list[Path]:
        """The drainer ``job_id`` was cancelled: drop what is waiting and return it, so
        the next request starts a fresh job. Nothing when another job has taken over."""
        with self._lock:
            if job_id is not None and self._job_id not in (None, job_id):
                return []
            dropped = [f for f, _ in self._pending.values()]
            self._pending.clear()
            self._job_id = None
            return dropped

    def record(self, path: Path, outcome: str, error: str | None = None) -> dict[str, Any]:
        entry = {"path": str(path), "status": outcome, "at": self._now()}
        if error:
            entry["error"] = error[:300]
        with self._lock:
            self._recent.append(entry)
        return entry

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {"job_id": self._job_id,
                    "pending": [str(f) for f, _ in self._pending.values()],
                    "recent": list(self._recent)}


def drain(queue: FileQueue, index: Callable[[Path, bool], str], *,
          take: Callable[[], tuple[Path, bool] | None] | None = None,
          cancelled: Callable[[], bool] = lambda: False,
          progress: Callable[[str], None] = lambda _s: None,
          put_back: bool = False) -> dict[str, int]:
    """Re-index queued files until ``take`` (default :meth:`FileQueue.take`) runs dry
    or ``cancelled()``. ``index(file, force)`` returns "indexed", "current" or
    "no_subs", or raises for a file it couldn't tell about. Every outcome is recorded.
    Returns counts by outcome (and "failed"). ``put_back``: a cancel stops someone
    else's job (a library run's), so the file it interrupts waits for the hook job."""
    take = take or queue.take
    counts: dict[str, int] = {}
    while not cancelled():
        item = take()
        if item is None:
            break
        f, force = item
        progress(f.name)
        try:
            outcome = index(f, force)
        except Cancelled:
            if put_back:
                queue.put_back(f, force)
            else:
                queue.record(f, "cancelled")
            raise
        except Exception as exc:  # noqa: BLE001 -- one bad file never stops the queue
            outcome = "failed"
            queue.record(f, outcome, str(exc) or type(exc).__name__)
        else:
            queue.record(f, outcome)
        counts[outcome] = counts.get(outcome, 0) + 1
    return counts
