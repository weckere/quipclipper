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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable


class Status(str, Enum):
    queued = "queued"
    running = "running"
    done = "done"
    failed = "failed"


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

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "status": self.status.value,
            "label": self.label,
            "created": self.created,
        }
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

    def __init__(self, max_workers: int = 2) -> None:
        self._pool = ThreadPoolExecutor(max_workers=max_workers)
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def submit(self, fn: Callable[[], list[Path]], *, label: str = "") -> Job:
        job = Job(id=uuid.uuid4().hex[:12], label=label)
        with self._lock:
            self._jobs[job.id] = job
        self._pool.submit(self._run, job, fn)
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list_recent(self, limit: int = 20) -> list[Job]:
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j.created, reverse=True)
        return jobs[:limit]

    def _run(self, job: Job, fn: Callable[[], list[Path]]) -> None:
        job.status = Status.running
        job.started = time.time()
        try:
            job.result_paths = fn()
            job.status = Status.done
        except Exception as exc:
            job.error = str(exc)
            job.status = Status.failed
        finally:
            job.finished = time.time()
