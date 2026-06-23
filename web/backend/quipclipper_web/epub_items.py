"""Adapt EPUB3 media-overlay books to quipclipper's per-file item model.

A synced book browses like a folder; each embedded **audio member** becomes one
audio "segment" item, addressed as ``<epub_path>#seg=<i>``. Grouping by audio
member (rather than by chapter) means consecutive chapters that share one audio
file fold into a single self-contained item — no sub-range extraction or offset
math — so a segment behaves like any other audio file: probe it, play it, search
its cues, clip from it.
"""

from __future__ import annotations

import functools
import hashlib
from dataclasses import dataclass
from pathlib import Path

from quipclipper.epub import (
    EpubBook,
    MOCue,
    extract_audio_member,
    is_media_overlay_epub,
    read_epub,
)
from quipclipper_web import media

_REF = "#seg="


def parse_ref(path: str) -> tuple[str, int | None]:
    """Split ``<epub>#seg=N`` into (epub_path, N); (path, None) if not a segment ref."""
    i = path.rfind(_REF)
    if i == -1:
        return path, None
    try:
        return path[:i], int(path[i + len(_REF):])
    except ValueError:
        return path, None


def make_ref(epub_path: str | Path, seg: int) -> str:
    return f"{epub_path}{_REF}{seg}"


def is_epub_book(p: Path) -> bool:
    return p.is_file() and p.suffix.lower() == ".epub" and is_media_overlay_epub(p)


@functools.lru_cache(maxsize=8)
def _read_cached(path_str: str, mtime: float) -> EpubBook:
    return read_epub(path_str)


def book_for(epub: Path) -> EpubBook:
    """Parse a book (cached by path+mtime — the SMIL/text parse, not the audio)."""
    try:
        mtime = epub.stat().st_mtime
    except OSError:
        mtime = 0.0
    return _read_cached(str(epub), mtime)


@dataclass(frozen=True)
class Segment:
    index: int
    title: str | None
    audio: str  # zip member name
    cues: list[MOCue]  # member-relative, sorted by start


def segments(book: EpubBook) -> list[Segment]:
    """Group the book's cues by embedded audio member (first-appearance order).
    A segment's title is the chapter(s) whose narration lives in that member."""
    idx_title: dict[int, str | None] = {}
    for ch in book.chapters:
        for k in range(ch.first_cue, ch.first_cue + ch.n_cues):
            idx_title[k] = ch.title
    order: list[str] = []
    groups: dict[str, list[MOCue]] = {}
    for c in book.cues:
        if c.audio not in groups:
            groups[c.audio] = []
            order.append(c.audio)
        groups[c.audio].append(c)
    segs: list[Segment] = []
    for i, member in enumerate(order):
        cues = sorted(groups[member], key=lambda c: c.start)
        titles = list(dict.fromkeys(t for t in (idx_title.get(c.index) for c in cues) if t))
        segs.append(Segment(i, " / ".join(titles) or None, member, cues))
    return segs


def _segment(book: EpubBook, index: int) -> Segment:
    segs = segments(book)
    if not 0 <= index < len(segs):
        raise IndexError(f"segment {index} out of range (0–{len(segs) - 1})")
    return segs[index]


def segment_entries(epub: Path) -> list[dict]:
    """Browse listing for a book: one entry per audio segment (an audio item)."""
    book = book_for(epub)
    out: list[dict] = []
    for seg in segments(book):
        out.append({
            "name": seg.title or f"Part {seg.index + 1}",
            "path": make_ref(epub, seg.index),
            "is_dir": False,
            "is_video": False,
            "has_sidecar": True,
            "is_audio": True,
            "is_book": False,
        })
    return out


def _audio_cache_dir(state_dir: Path) -> Path:
    d = state_dir / "epub_audio"
    d.mkdir(parents=True, exist_ok=True)
    return d


def cached_audio(epub: Path, member: str, state_dir: Path) -> Path:
    """Extract one embedded audio member to a cache file (keyed by epub mtime +
    member), returning the path. Idempotent; plain unzip, no transcode."""
    try:
        mtime = epub.stat().st_mtime
    except OSError:
        mtime = 0.0
    key = hashlib.sha256(f"{epub}:{mtime}:{member}".encode()).hexdigest()[:32]
    out = _audio_cache_dir(state_dir) / f"{key}{Path(member).suffix or '.bin'}"
    if not out.exists() or out.stat().st_size == 0:
        extract_audio_member(epub, member, out)
    return out


def segment_audio_path(epub: Path, index: int, state_dir: Path) -> Path:
    return cached_audio(epub, _segment(book_for(epub), index).audio, state_dir)


def segment_item_info(epub: Path, index: int, state_dir: Path) -> dict:
    """`item_info`-shaped dict for a segment: real streams/duration probed from the
    extracted audio, with the book's name/ref substituted in."""
    book = book_for(epub)
    seg = _segment(book, index)
    audio = cached_audio(epub, seg.audio, state_dir)
    info = media.item_info(audio)
    info["name"] = seg.title or f"Part {seg.index + 1}"
    info["path"] = make_ref(epub, index)
    info["has_sidecar"] = True
    info["best_track"] = None
    info["book_title"] = book.title or epub.stem
    return info


def segment_cues(epub: Path, index: int) -> list:
    """The segment's cues as plain Cues (member-relative timing)."""
    return [c.as_cue() for c in _segment(book_for(epub), index).cues]


def segment_clip_name_source(epub: Path, index: int) -> Path:
    """A synthetic path whose stem is the book title and whose parent is the
    segment title, so the clip naming template yields
    ``<Book>/<timestamp>_<cue>_<Chapter>``."""
    book = book_for(epub)
    seg = _segment(book, index)
    chapter = seg.title or f"Part {seg.index + 1}"
    return epub.parent / chapter / f"{book.title or epub.stem}.mka"
