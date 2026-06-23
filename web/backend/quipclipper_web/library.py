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

from quipclipper.subtitles import AUDIO_EXTS, VIDEO_EXTS, find_sidecar


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
    has_sidecar: bool  # a sidecar subtitle/transcript sits next to this file
    is_audio: bool = False  # audio-only source (podcast/audiobook)


def _is_video(p: Path) -> bool:
    return p.suffix.lower() in VIDEO_EXTS


def _is_audio(p: Path) -> bool:
    return p.suffix.lower() in AUDIO_EXTS


def _media_entry(child: Path) -> Entry | None:
    """An Entry for a clippable media file, else None. Video files always qualify
    (they may carry embedded subtitles); audio-only files (podcasts, audiobooks)
    qualify only when a sidecar transcript sits next to them."""
    if _is_video(child):
        return Entry(child.name, str(child), False, True, find_sidecar(child) is not None)
    if _is_audio(child) and find_sidecar(child) is not None:
        return Entry(child.name, str(child), False, False, True, is_audio=True)
    return None


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
        else:
            e = _media_entry(child)
            if e is not None:
                files.append(e)
    dirs.sort(key=lambda e: e.name.lower())
    files.sort(key=lambda e: e.name.lower())
    return dirs + files


def search(query: str, roots: list[Path], max_results: int = 50) -> list[Entry]:
    """Search for folders and video files matching `query` across all roots.

    Walks one level of subdirectories under each root.  Results are ranked by
    how early in the name the query appears (case-insensitive), then
    alphabetically.  Hidden entries are skipped.
    """
    q = query.lower()
    hits: list[tuple[int, Entry]] = []

    for root in roots:
        r = _real(root)
        if not r.is_dir():
            continue
        for child in r.iterdir():
            if child.name.startswith("."):
                continue
            name_lower = child.name.lower()
            if q not in name_lower:
                continue
            pos = name_lower.index(q)
            if child.is_dir():
                hits.append((pos, Entry(child.name, str(child), True, False, False)))
            else:
                e = _media_entry(child)
                if e is not None:
                    hits.append((pos, e))

    hits.sort(key=lambda t: (t[0], t[1].name.lower()))
    return [e for _, e in hits[:max_results]]


def search_within(query: str, path: str | Path, roots: list[Path], max_results: int = 50) -> list[Entry]:
    """Search for entries matching `query` within a specific directory."""
    target = resolve_within_roots(path, roots)
    if not target.is_dir():
        return []

    q = query.lower()
    hits: list[tuple[int, Entry]] = []

    for child in target.iterdir():
        if child.name.startswith("."):
            continue
        name_lower = child.name.lower()
        if q not in name_lower:
            continue
        pos = name_lower.index(q)
        if child.is_dir():
            hits.append((pos, Entry(child.name, str(child), True, False, False)))
        else:
            e = _media_entry(child)
            if e is not None:
                hits.append((pos, e))

    hits.sort(key=lambda t: (t[0], t[1].name.lower()))
    return [e for _, e in hits[:max_results]]


def entry_dict(e: Entry) -> dict:
    return asdict(e)
