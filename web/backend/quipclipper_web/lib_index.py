"""Index a whole library's subtitles (M20): a background job someone starts.

Folder and library dialogue search read only indexed files (D-042), and a folder's
Index refuses more than ``INDEX_CAP`` files in one request. This job indexes every
media file under the library roots, or under one folder of any size, and is built to
stay out of the way of everything else on a shared host:

- **Only on request.** ``POST /api/search/library/index`` (``quipclipper
  index.library``) is the only way it starts: no timer, no startup or scan hook, no
  default that starts it (as D-037 holds for transcription).
- **Idle priority.** Every ffprobe and ffmpeg it runs goes through
  :mod:`quipclipper.idle` (``SCHED_IDLE``, nice 19, idle I/O class), one file at a
  time, so the media disks see one sequential reader: on mergerfs HDD pools a second
  reader turns two sequential reads into seeks, and their schedulers (``none``,
  ``mq-deadline``) don't honour the I/O class.
- **Resumable.** The subtitle cache is the state: a file whose subtitles are cached and
  current, or that is known to have none (``.nosubs``), is skipped, so a rerun (after a
  cancel, a failure or a restart) picks up where the last one stopped.
- **Cancellable** between files and during one (the ffmpeg in flight is killed).

The last run's summary is kept in the state dir, so after a restart the library page
can say how far it got; nothing reads it to start a run.
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

from quipclipper.subtitles import Cancelled, at_idle_priority

# How often the summary on disk is brought up to date during a run.
SAVE_EVERY = 30.0
# Failures listed in the result (they are all counted).
MAX_ERRORS = 20


class RunLog:
    """The last run's summary, as JSON in the state dir (``library_index.json``)."""

    def __init__(self, state_dir: Path) -> None:
        self._path = state_dir / "library_index.json"
        self._lock = threading.Lock()

    def read(self) -> dict[str, Any] | None:
        try:
            data = json.loads(self._path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def write(self, record: dict[str, Any]) -> None:
        with self._lock, contextlib.suppress(OSError):
            fd, tmp = tempfile.mkstemp(dir=self._path.parent, suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(record, f)
                os.replace(tmp, self._path)
            except OSError:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)


def _clock(seconds: float) -> str:
    """``~3 h 20 min`` / ``~12 min`` / ``under a minute``."""
    m = int(seconds // 60)
    if m < 1:
        return "under a minute"
    h, m = divmod(m, 60)
    return f"~{h} h {m} min" if h else f"~{m} min"


def run(job, *, paths: list[str], scan: Callable[[], Iterable[tuple[Path, str]]],
        index: Callable[[Path], str], log: RunLog, whole_library: bool = True,
        now: Callable[[], float] = time.time,
        between: Callable[[], None] | None = None,
        stopping: Callable[[], bool] = lambda: False) -> dict[str, Any]:
    """Index every file ``scan()`` yields as not indexed; the job's ``progress`` and
    ``result`` follow it, and so does ``log``. Returns the result. ``whole_library``
    says whether ``paths`` are the library roots (else one folder of it).

    ``scan`` yields ``(file, state)`` for every media file, state being what the cache
    says: "indexed", "no_subs", "changed" or "not_indexed"; only "not_indexed" and
    "changed" ones (indexed before their subtitles changed) are indexed.
    ``index(file)`` extracts and caches one file's subtitles (it runs inside
    :func:`at_idle_priority`) and returns "indexed", "no_subs", or raises for a file
    it couldn't tell about. ``between()`` runs before each file, at the same priority:
    the re-index hooks' queue (:mod:`.file_index`) is drained there, so a hook's file
    never waits for a whole library run. ``stopping()``: the server is shutting down, so
    a stop is recorded as "interrupted" (a restart cut it short), not "cancelled"."""
    started = now()
    result: dict[str, Any] = {
        "paths": paths, "whole_library": whole_library, "status": "scanning", "started": started, "files": 0,
        "already": 0, "todo": 0, "done": 0, "indexed": 0, "no_subs": 0, "failed": 0,
        "current": None, "eta_s": None, "files_per_hour": None, "errors": [],
    }
    job.result = result

    def save() -> None:
        log.write({**result, "updated": now()})

    try:
        return _run(job, result, scan, index, save, now, between, stopping)
    except Exception as exc:
        result.update(status="failed", current=None, eta_s=None, finished=now(),
                      summary=f"Failed: {exc}")
        save()
        raise


def _run(job, result: dict[str, Any], scan, index, save, now, between=None,
         stopping=lambda: False) -> dict[str, Any]:
    job.progress = "Scanning for files not indexed yet…"
    save()
    todo: list[Path] = []
    for f, state in scan():
        if job.cancel_requested:
            break
        result["files"] += 1
        if state in ("not_indexed", "changed"):
            todo.append(f)
        else:
            result["already"] += 1
        if result["files"] % 500 == 0:
            job.progress = f"Scanning: {result['files']} files so far…"
    result.update(todo=len(todo), status="indexing")
    saved = work_started = now()
    cancelled = lambda: job.cancel_requested  # noqa: E731
    with at_idle_priority(cancelled):
        for i, f in enumerate(todo, 1):
            if between is not None and not job.cancel_requested:
                try:
                    between()
                except Cancelled:
                    break
            if job.cancel_requested:
                break
            result["current"] = str(f)
            left = result["eta_s"]
            job.progress = (f"{i}/{len(todo)} {f.name}"
                            + (f" · {_clock(left)} left" if left is not None else ""))
            try:
                outcome = index(f)
            except Cancelled:
                break
            except Exception as exc:  # noqa: BLE001 -- one bad file never stops a run
                result["failed"] += 1
                if len(result["errors"]) < MAX_ERRORS:
                    result["errors"].append({"path": str(f), "error": str(exc)[:300]})
            else:
                result["no_subs" if outcome == "no_subs" else "indexed"] += 1
            result["done"] = i
            # A rough ETA once a few files are in: the average so far, times what's left.
            if i >= 3:
                spent = now() - work_started
                result["eta_s"] = round(spent / i * (len(todo) - i))
                result["files_per_hour"] = round(i / spent * 3600) if spent > 0 else None
            if now() - saved >= SAVE_EVERY:
                saved = now()
                save()
    stopped = job.cancel_requested
    status = ("interrupted" if stopping() else "cancelled") if stopped else "done"
    result.update(status=status, current=None, eta_s=None, finished=now())
    if not todo and not stopped:
        summary = (f"Nothing to index: all {result['files']} files are indexed or have no "
                   "subtitles.")
    else:
        summary = (f"{result['indexed']} indexed, {result['no_subs']} without subtitles, "
                   f"{result['failed']} failed, of {len(todo)} to do "
                   f"({result['already']} already done).")
    if status == "interrupted":
        summary += " A restart cut it short: run it again to carry on."
    result["summary"] = summary
    job.progress = ("Stopped: " if stopped else "Done: ") + summary
    save()
    return result


def last_run(log: RunLog, live: bool) -> dict[str, Any] | None:
    """The last run's summary. One the log still calls running, with no job running in
    this process, was cut short by a restart: it says "interrupted"."""
    rec = log.read()
    if rec is not None and not live and rec.get("status") in ("scanning", "indexing"):
        rec["status"] = "interrupted"
    return rec
