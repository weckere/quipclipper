"""Safe browsing of the configured media roots.

Every path the client supplies is resolved with ``realpath`` and must land
inside one of the configured media roots; anything else raises
:class:`PathNotAllowed`. This is the security boundary for the whole app — the
clip endpoints shell out to ffmpeg with these paths, so nothing may point
outside the whitelist (including via ``..`` or a symlink that escapes a root).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, asdict
from pathlib import Path

from quipclipper.subtitles import VIDEO_EXTS, find_sidecar


class PathNotAllowed(Exception):
    """Raised when a requested path is outside every configured media root."""


def _real(p: str | Path) -> Path:
    return Path(os.path.realpath(p))


def resolve_within_roots(path: str | Path, roots: list[Path]) -> Path:
    """Return the realpath of `path`, or raise if it escapes the media roots."""
    if not roots:
        raise PathNotAllowed("No media roots are configured.")
    target = _real(path)
    for root in roots:
        r = _real(root)
        if target == r or r in target.parents:
            return target
    raise PathNotAllowed(f"Path is outside the configured media roots: {path}")


@dataclass(frozen=True)
class Entry:
    """One row in a directory listing."""

    name: str
    path: str
    is_dir: bool
    is_video: bool
    has_sidecar: bool  # a sidecar subtitle sits next to this video


def _is_video(p: Path) -> bool:
    return p.suffix.lower() in VIDEO_EXTS


def browse(path: str | None, roots: list[Path]) -> list[Entry]:
    """List media roots (when `path` is empty) or the contents of a directory.

    Only sub-directories and video files are returned, directories first, each
    sorted case-insensitively. Hidden entries (dotfiles) are skipped.
    """
    if not path:
        return [
            Entry(name=r.name or str(r), path=str(_real(r)), is_dir=True, is_video=False, has_sidecar=False)
            for r in roots
        ]

    target = resolve_within_roots(path, roots)
    if not target.exists():
        raise FileNotFoundError(f"No such directory: {path}")
    if not target.is_dir():
        raise NotADirectoryError(f"Not a directory: {path}")

    dirs: list[Entry] = []
    files: list[Entry] = []
    for child in target.iterdir():
        if child.name.startswith("."):
            continue
        if child.is_dir():
            dirs.append(Entry(child.name, str(child), True, False, False))
        elif _is_video(child):
            files.append(
                Entry(child.name, str(child), False, True, find_sidecar(child) is not None)
            )
    dirs.sort(key=lambda e: e.name.lower())
    files.sort(key=lambda e: e.name.lower())
    return dirs + files


def entry_dict(e: Entry) -> dict:
    return asdict(e)
