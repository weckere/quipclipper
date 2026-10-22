"""JobRegistry: shutting down with cancel (M21 release review, BE-3)."""

from __future__ import annotations

import threading
import time

from quipclipper_web.jobs import JobRegistry


def test_shutdown_with_cancel_stops_the_running_job_and_drops_the_queue():
    reg = JobRegistry(max_workers=1)
    started = threading.Event()
    ran = []

    def slow(job):
        started.set()
        deadline = time.monotonic() + 10
        while not job.cancel_requested and time.monotonic() < deadline:
            time.sleep(0.01)
        return []

    running = reg.submit(slow, cancellable=True)
    queued = reg.submit(lambda job: ran.append(job.id) or [], cancellable=True)
    assert started.wait(5)
    reg.shutdown(cancel=True)
    deadline = time.monotonic() + 5
    while running.finished is None:
        assert time.monotonic() < deadline, "the running job never stopped"
        time.sleep(0.01)
    assert running.status.value == "cancelled"
    time.sleep(0.1)
    assert ran == [] and queued.status.value == "queued"
