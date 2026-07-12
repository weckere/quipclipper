"""Async clip-job system.

Clip operations (re-encodes, gifs, channel splits, remux-first) can take real
time, so they run in a background thread pool.  ``POST /api/clip`` enqueues a
job and returns an id; ``GET /api/jobs/{id}`` polls for status; finished jobs
expose a download path.

The registry lives in memory — restarting the server clears it.  For a
single-server, single-user appliance this is fine.
"""

from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable


class Status(str, Enum):
    queued = "queued"
    running = "running"
    done = "done"
    failed = "failed"
    cancelled = "cancelled"


@dataclass
class Job:
    id: str
    status: Status = Status.queued
    created: float = field(default_factory=time.time)
    started: float | None = None
    finished: float | None = None
    result_paths: list[Path] = field(default_factory=list)
    error: str | None = None
    # For the frontend: a human label summarising the request.
    label: str = ""
    # A long job (e.g. folder subtitle backfill) can publish a progress string
    # ("12/263 …") and honour a cooperative cancel by checking cancel_requested.
    progress: str | None = None
    cancel_requested: bool = False

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "status": self.status.value,
            "label": self.label,
            "created": self.created,
        }
        if self.progress is not None:
            d["progress"] = self.progress
        if self.started is not None:
            d["started"] = self.started
        if self.finished is not None:
            d["finished"] = self.finished
            d["elapsed"] = round(self.finished - (self.started or self.created), 2)
        if self.result_paths:
            d["files"] = [
                {"name": p.name, "size": p.stat().st_size if p.exists() else 0}
                for p in self.result_paths
            ]
        if self.error:
            d["error"] = self.error
        return d


class JobRegistry:
    """Thread-safe job queue backed by a bounded thread pool."""

    # Finished jobs older than this are pruned on the next submit, so the
    # registry doesn't grow unboundedly on a long-running server.
    MAX_FINISHED_AGE = 24 * 3600

    def __init__(self, max_workers: int = 2) -> None:
        self._pool = ThreadPoolExecutor(max_workers=max_workers)
        self._jobs: dict[str, Job] = {}
        self._futures: dict[str, Future] = {}
        self._lock = threading.Lock()

    def submit(self, fn: Callable[[Job], list[Path]], *, label: str = "") -> Job:
        job = Job(id=uuid.uuid4().hex[:12], label=label)
        cutoff = time.time() - self.MAX_FINISHED_AGE
        with self._lock:
            for jid, j in list(self._jobs.items()):
                if j.finished is not None and j.finished < cutoff:
                    del self._jobs[jid]
                    self._futures.pop(jid, None)
            self._jobs[job.id] = job
            self._futures[job.id] = self._pool.submit(self._run, job, fn)
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def cancel(self, job_id: str) -> bool:
        """Cancel a job. Returns True if the job existed and was cancelled/pruned.

        A queued job that hasn't started is pulled from the pool and marked
        cancelled. A job that's already finished (done/failed/cancelled) is simply
        pruned from the registry. A *running* job can't be interrupted from here
        (its ffmpeg subprocess has its own timeout), so this returns False for it —
        the caller surfaces that as "can't cancel a running job".
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            if job.status in (Status.done, Status.failed, Status.cancelled):
                del self._jobs[job_id]
                self._futures.pop(job_id, None)
                return True
            if job.status == Status.running:
                return False  # in flight; the subprocess timeout will free it
            # Queued: try to yank the future from the pool before it starts.
            fut = self._futures.get(job_id)
            if fut is not None and fut.cancel():
                job.status = Status.cancelled
                job.finished = time.time()
                self._futures.pop(job_id, None)
                return True
            # The worker grabbed it between our checks — treat as running.
            return False

    def list_recent(self, limit: int = 20) -> list[Job]:
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j.created, reverse=True)
        return jobs[:limit]

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False)

    def request_cancel(self, job_id: str) -> bool:
        """Ask a running job to stop at its next checkpoint (cooperative — the
        job function must poll ``job.cancel_requested``). Returns True if found.
        A queued/finished job is handled by :meth:`cancel` instead."""
        job = self._jobs.get(job_id)
        if job is None:
            return False
        job.cancel_requested = True
        return True

    def _run(self, job: Job, fn: Callable[[Job], list[Path]]) -> None:
        if job.status == Status.cancelled:  # cancelled after submit, before start
            return
        job.status = Status.running
        job.started = time.time()
        try:
            job.result_paths = fn(job)
            job.status = Status.cancelled if job.cancel_requested else Status.done
        except Exception as exc:
            job.error = str(exc)
            job.status = Status.failed
        finally:
            job.finished = time.time()
