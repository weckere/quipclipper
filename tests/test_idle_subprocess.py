"""at_idle_priority (M20 library index): every ffprobe/ffmpeg a thread starts inside it
runs through quipclipper.idle, and a cancel kills the one in flight."""

from __future__ import annotations

import os
import stat
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from quipclipper import idle, subtitles
from quipclipper.subtitles import Cancelled, at_idle_priority, list_embedded_tracks

# Records the priority it runs at, then answers like `ffprobe -of json` with no streams.
FAKE_PROBE = textwrap.dedent('''\
    #!{python}
    import json, os, sys, time
    rec = {{"nice": os.nice(0)}}
    if hasattr(os, "sched_getscheduler"):
        rec["sched_idle"] = os.sched_getscheduler(0) == os.SCHED_IDLE
    with open(os.environ["FAKE_PROBE_OUT"], "a") as f:
        f.write(json.dumps(rec) + "\\n")
    time.sleep(float(os.environ.get("FAKE_PROBE_SLEEP") or 0))
    print(json.dumps({{"streams": []}}))
''')


@pytest.fixture
def fake_probe(tmp_path: Path, monkeypatch) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    probe = bin_dir / "ffprobe"
    probe.write_text(FAKE_PROBE.format(python=sys.executable))
    probe.chmod(probe.stat().st_mode | stat.S_IXUSR)
    out = tmp_path / "probes.jsonl"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_PROBE_OUT", str(out))
    return out


def _records(out: Path) -> list[dict]:
    import json
    return [json.loads(line) for line in out.read_text().splitlines()]


def test_outside_the_context_a_probe_runs_at_normal_priority(fake_probe, tmp_path):
    video = tmp_path / "film.mkv"
    video.write_bytes(b"")
    assert list_embedded_tracks(video) == []
    assert _records(fake_probe)[0]["nice"] == os.nice(0)


def test_inside_the_context_a_probe_runs_at_idle_priority(fake_probe, tmp_path):
    video = tmp_path / "film.mkv"
    video.write_bytes(b"")
    with at_idle_priority():
        assert list_embedded_tracks(video) == []
    rec = _records(fake_probe)[0]
    assert rec["nice"] == 19
    if sys.platform.startswith("linux"):
        assert rec["sched_idle"] is True


def test_the_idle_wrapper_carries_the_command(monkeypatch, tmp_path):
    seen = {}

    class Proc:
        returncode = 0

        def __init__(self, argv, **kw):
            seen["argv"], seen["kw"] = argv, kw

        def communicate(self, timeout=None):
            return '{"streams": []}', ""

        def poll(self):
            return 0

    monkeypatch.setattr(subtitles.shutil, "which", lambda _: "/usr/bin/ffprobe")
    monkeypatch.setattr(subtitles.subprocess, "Popen", Proc)
    with at_idle_priority():
        list_embedded_tracks(tmp_path / "film.mkv")
    assert seen["argv"][:3] == idle.wrap([])[:3] == [sys.executable, "-m", "quipclipper.idle"]
    assert seen["argv"][3] == "ffprobe"
    assert seen["kw"]["env"]["PYTHONPATH"]  # the bare interpreter finds quipclipper.idle


def test_the_context_is_per_thread(fake_probe, tmp_path):
    """Another thread (a search request) keeps running its probes at normal priority."""
    video = tmp_path / "film.mkv"
    video.write_bytes(b"")
    with at_idle_priority():
        t = threading.Thread(target=list_embedded_tracks, args=(video,))
        t.start()
        t.join()
    assert _records(fake_probe)[0]["nice"] == os.nice(0)


def test_a_cancel_kills_the_probe_in_flight(fake_probe, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_PROBE_SLEEP", "30")
    video = tmp_path / "film.mkv"
    video.write_bytes(b"")
    stop = time.monotonic() + 0.8
    t0 = time.monotonic()
    with pytest.raises(Cancelled), at_idle_priority(lambda: time.monotonic() > stop):
        list_embedded_tracks(video)
    assert time.monotonic() - t0 < 10


def test_cancelled_is_not_a_runtime_error():
    """Callers treat RuntimeError as "no subtitles here"; a cancel must not look like it."""
    assert not issubclass(Cancelled, RuntimeError)


def test_idle_children_run_in_a_private_folder(monkeypatch):
    # JB-12 (review 2): they ran in the shared temp folder, first on their import path
    # (python -m), where any local user could plant a quipclipper package.
    seen = {}

    class Proc:
        returncode = 0

        def __init__(self, argv, **kw):
            seen.update(kw)

        def communicate(self, timeout=None):
            return "", ""

        def poll(self):
            return 0

    monkeypatch.setattr(subtitles.subprocess, "Popen", Proc)
    with at_idle_priority():
        subtitles._run(["true"], 5)
    cwd = Path(seen["cwd"])
    assert cwd.resolve() != Path(subtitles.tempfile.gettempdir()).resolve()
    assert stat.S_IMODE(cwd.stat().st_mode) == 0o700 and not any(cwd.iterdir())
