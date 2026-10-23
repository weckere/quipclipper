"""Run a command at idle priority: ``python -m quipclipper.idle CMD [ARGS...]``.

Machine transcription (M21) must never take CPU or disk time from anything else on
the host, above all the on-demand HLS encodes a player is waiting for. This lowers
its own priority as far as an unprivileged process can, then ``exec``s the command,
so every thread the command starts inherits it from the first instruction:

- CPU: ``SCHED_IDLE`` on Linux (runs only when a core would otherwise be idle) and
  nice 19; on macOS the Darwin background policy (``PRIO_DARWIN_BG``) and nice 19.
- Disk: the idle I/O class (``ioprio_set``, Linux), so a read of the media file
  waits behind every other reader (under the BFQ/CFQ schedulers; ``none`` and
  ``mq-deadline`` ignore I/O classes).

Each step is best effort: a platform without one keeps the others. If no CPU step
takes effect, the command isn't run at all. Lowering a
priority never needs a privilege, so this works under a hardened systemd unit
(``NoNewPrivileges``, ``RestrictRealtime``) too.
"""

from __future__ import annotations

import ctypes
import os
import platform
import sys

# ioprio_set(2): who = IOPRIO_WHO_PROCESS, class IOPRIO_CLASS_IDLE in the top bits.
_IOPRIO_WHO_PROCESS = 1
_IOPRIO_CLASS_IDLE = 3
_IOPRIO_CLASS_SHIFT = 13
_SYS_IOPRIO_SET = {"x86_64": 251, "amd64": 251, "aarch64": 30, "arm64": 30,
                   "i386": 289, "i686": 289, "armv7l": 314, "riscv64": 30}


def _ioprio_idle() -> bool:
    nr = _SYS_IOPRIO_SET.get(platform.machine().lower())
    if nr is None or not sys.platform.startswith("linux"):
        return False
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        return libc.syscall(nr, _IOPRIO_WHO_PROCESS, 0,
                            _IOPRIO_CLASS_IDLE << _IOPRIO_CLASS_SHIFT) == 0
    except (OSError, AttributeError):
        return False


def lower_priority() -> list[str]:
    """Drop this process to idle CPU and I/O priority. Returns what took effect."""
    done = []
    try:
        os.nice(19 - os.nice(0))
        done.append("nice19")
    except OSError:
        pass
    if hasattr(os, "SCHED_IDLE"):
        try:
            os.sched_setscheduler(0, os.SCHED_IDLE, os.sched_param(0))
            done.append("sched_idle")
        except OSError:
            pass
    if hasattr(os, "PRIO_DARWIN_BG"):
        try:
            os.setpriority(os.PRIO_DARWIN_PROCESS, 0, os.PRIO_DARWIN_BG)
            done.append("darwin_bg")
        except OSError:
            pass
    if _ioprio_idle():
        done.append("io_idle")
    return done


def wrap(argv: list[str]) -> list[str]:
    """``argv`` run through this module (idle CPU and I/O priority)."""
    return [sys.executable, "-m", "quipclipper.idle", *argv]


def child_env() -> dict[str, str]:
    """The environment for :func:`wrap`: this process's import path as PYTHONPATH.
    A packaged app (Nix's buildPythonApplication) sets its path up inside its own entry
    script, so the bare interpreter it runs on wouldn't find ``quipclipper.idle``."""
    paths = [p for p in sys.path if p and os.path.isdir(p)]
    return {**os.environ, "PYTHONPATH": os.pathsep.join(paths)}


def main(argv: list[str]) -> None:
    if not argv:
        sys.exit("usage: python -m quipclipper.idle CMD [ARGS...]")
    done = lower_priority()
    if not {"nice19", "sched_idle", "darwin_bg"} & set(done):
        # Never run the command at full priority: this host is shared (D-037).
        sys.exit(f"{argv[0]}: couldn't lower the CPU priority, so not running it")
    try:
        os.execvp(argv[0], argv)
    except OSError as exc:
        sys.exit(f"{argv[0]}: {exc.strerror or exc}")


if __name__ == "__main__":
    main(sys.argv[1:])
