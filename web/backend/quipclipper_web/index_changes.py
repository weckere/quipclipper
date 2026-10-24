"""Notice subtitles that changed since indexing (M20): a check, never a re-index.

A file's index entry is keyed by its subtitle source's mtime, so when a sidecar is
replaced, or the file itself, its entry no longer matches and search leaves the file
out (D-042). :meth:`SubtitleCache.index_state` tells such a file ("changed": it has
entries, none at the current mtime) from one never indexed. This module keeps the set
of changed files the server knows about, for the library's "Index out of date: N files"
badge and its Re-index button:

- **When something is viewed.** A folder's index status and a folder search classify
  every file they cover anyway; they :meth:`ChangeLog.note` what they saw.
- **Once an hour** (``QC_INDEX_CHECK_INTERVAL`` seconds; 0 = never), a background pass
  over the library roots: directory listings and ``stat`` calls only, never a subtitle
  file's content, never an extraction, on its own thread at the idle I/O class (not
  idle CPU: the thread holds the interpreter lock, so a starved one would stall every
  request). A pass still running when the next is due is skipped, and so is a pass
  while a library run or a re-index job reads the disks.
- **Re-indexing a file** (the hooks' job, a library run) :meth:`ChangeLog.forget`\\ s it.

Nothing here re-indexes: that happens only when someone asks (the badge's or a
banner's button, the CLI) or when Bazarr/Sonarr/Radarr call the re-index hook.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

# Changed files listed in a response (all are counted, and all are kept on disk).
LIST_MAX = 1000
# The first pass after a start, so a restart shows the badge without waiting an hour.
FIRST_DELAY = 120.0
# The longest interval honoured (a week): Event.wait overflows on huge ones.
MAX_INTERVAL = 7 * 86400.0
# A forget's write to disk waits this long for others (a re-index job forgets each file).
SAVE_EVERY = 5.0


def lower_thread_priority() -> list[str]:
    """Put the calling thread (only) in the idle I/O class (Linux, ``ioprio_set`` with
    who 0). Not its CPU priority: this thread runs Python and holds the interpreter lock
    while it does, so at ``SCHED_IDLE`` or nice 19 the kernel would park it holding the
    lock whenever the CPU is busy, stalling every request thread behind it (the review's
    JB-1: up to a second per request). Its listings and stats are I/O, and blocking I/O
    lets go of the lock. Call it on a thread of its own: a pool thread would keep it.
    Returns what took effect."""
    from quipclipper.idle import _ioprio_idle  # Linux-only syscall wrapper

    return ["io_idle"] if _ioprio_idle() else []


class ChangeLog:
    """The changed files the server knows about, kept in ``index_changes.json``."""

    def __init__(self, state_dir: Path, now: Callable[[], float] = time.time) -> None:
        self._path = state_dir / "index_changes.json"
        self._lock = threading.Lock()
        self._now = now
        data = self._load()
        self._files: set[str] = set(data.get("files") or [])
        self._last: dict[str, Any] = data.get("last") or {}
        # Files re-indexed while a pass runs: its (older) view mustn't bring them back.
        self._forgot: set[str] = set()
        self._passing = False
        self._dirty = False
        self._saved_at = 0.0

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self._path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self) -> None:
        """Write the set out. Call with ``_lock`` held."""
        self._dirty, self._saved_at = False, self._now()
        with contextlib.suppress(OSError):
            fd, tmp = tempfile.mkstemp(dir=self._path.parent, suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump({"files": sorted(self._files), "last": self._last}, f)
                os.replace(tmp, self._path)
            except OSError:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)

    def note(self, states: Iterable[tuple[Path | str, str]]) -> None:
        """What a check saw: each ``(file, index_state)``; "changed" files are added,
        every other state takes the file out."""
        with self._lock:
            before = set(self._files)
            for f, st in states:
                if st == "changed":
                    self._files.add(str(f))
                else:
                    self._files.discard(str(f))
            if self._files != before:
                self._save()

    def forget(self, f: Path | str) -> None:
        """``f`` was re-indexed (or found current): it is no longer out of date. Written
        out at most every ``SAVE_EVERY`` s (a job forgetting each of thousands of files
        rewrote the whole list each time); :meth:`flush` writes the rest."""
        with self._lock:
            if self._passing:
                self._forgot.add(str(f))
            if str(f) in self._files:
                self._files.discard(str(f))
                self._dirty = True
                if self._now() - self._saved_at >= SAVE_EVERY:
                    self._save()

    def flush(self) -> None:
        """Write out forgets still held back (a job ended, the server stops)."""
        with self._lock:
            if self._dirty:
                self._save()

    def begin_pass(self) -> None:
        with self._lock:
            self._forgot = set()
            self._passing = True

    def replace(self, files: Iterable[str], last: dict[str, Any]) -> None:
        """A whole-library pass: its result replaces the set (less what was re-indexed
        while it ran)."""
        with self._lock:
            self._files = set(files) - self._forgot
            self._last = last
            self._forgot, self._passing = set(), False
            self._save()

    def set_last(self, last: dict[str, Any]) -> None:
        with self._lock:
            self._last = last
            self._forgot, self._passing = set(), False
            self._save()

    def files(self) -> list[str]:
        with self._lock:
            return sorted(self._files)

    def summary(self) -> dict[str, Any]:
        with self._lock:
            return {"changed": len(self._files), "files": sorted(self._files)[:LIST_MAX],
                    "last_check": dict(self._last) or None}


class HourlyCheck:
    """The timestamp-only pass over the library, every ``interval`` seconds on a thread
    of its own at idle priority. ``scan()`` yields ``(file, index_state)`` for every
    media file under the roots; it must only list folders and stat files."""

    def __init__(self, log: ChangeLog, scan: Callable[[], Iterable[tuple[Path, str]]],
                 interval: float, first_delay: float = FIRST_DELAY,
                 now: Callable[[], float] = time.time,
                 busy: Callable[[], bool] = lambda: False) -> None:
        self.log = log
        self._scan = scan
        # ``busy()``: a library run or a re-index job is reading the disks; this waits.
        self._other_busy = busy
        self.interval = min(MAX_INTERVAL, max(0.0, interval))
        self._first = min(first_delay, self.interval) if self.interval else 0.0
        self._now = now
        self._stop = threading.Event()
        self._busy = threading.Lock()
        self._thread: threading.Thread | None = None
        self.priority: list[str] = []

    @property
    def running(self) -> bool:
        return self._busy.locked()

    def start(self) -> None:
        """Start the timer thread; nothing when the interval is 0 (off)."""
        if not self.interval or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="qc-index-check",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        self.priority = lower_thread_priority()
        wait = self._first
        while not self._stop.wait(wait):
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001 -- the hourly check must outlive one bad pass
                with contextlib.suppress(Exception):
                    self.log.set_last({"at": self._now(), "files": 0, "changed": 0,
                                       "duration_s": 0.0, "error": str(exc)[:300]})
            wait = self.interval

    def run_once(self) -> dict[str, Any] | None:
        """One pass; None (skipped) when one is still running, or while a library run
        or a re-index job is indexing (one reader on the disks at a time)."""
        if self._other_busy():
            return None
        if not self._busy.acquire(blocking=False):
            return None
        try:
            started = self._now()
            self.log.begin_pass()
            changed: list[str] = []
            files = 0
            error = None
            try:
                for f, st in self._scan():
                    if self._stop.is_set():
                        return None
                    files += 1
                    if st == "changed":
                        changed.append(str(f))
            except OSError as exc:  # a root gone (unmounted): keep the last result
                error = str(exc)
            last = {"at": started, "files": files, "changed": len(changed),
                    "duration_s": round(self._now() - started, 1)}
            if error:
                last["error"] = error[:300]
                self.log.set_last(last)
            else:
                self.log.replace(changed, last)
            return last
        finally:
            self._busy.release()
