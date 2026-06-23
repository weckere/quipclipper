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
import os
import tempfile
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from starlette.responses import StreamingResponse

from quipclipper.epub import (
    EpubBook,
    MOCue,
    extract_audio_member,
    is_media_overlay_epub,
    read_epub,
)

_REF = "#seg="

# Codec/MIME for an embedded audio member, by extension — Storyteller emits AAC in
# MP4. Used to synthesize item_info (no ffprobe) and pick the playback MIME type.
_AUDIO_CODEC = {
    ".mp4": "aac", ".m4a": "aac", ".m4b": "aac", ".aac": "aac",
    ".mp3": "mp3", ".ogg": "vorbis", ".oga": "vorbis", ".opus": "opus", ".flac": "flac",
}
_AUDIO_MIME = {"aac": "audio/mp4", "mp3": "audio/mpeg", "vorbis": "audio/ogg",
               "opus": "audio/ogg", "flac": "audio/flac"}


def _member_codec(member: str) -> str:
    return _AUDIO_CODEC.get(Path(member).suffix.lower(), "aac")


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


def segment_item_info(epub: Path, index: int) -> dict:
    """`item_info`-shaped dict for a segment, *synthesized* — no extraction, no
    ffprobe (so opening a chapter writes nothing). The narration is a single audio
    stream; duration comes from the cue timings, codec from the member extension."""
    book = book_for(epub)
    seg = _segment(book, index)
    codec = _member_codec(seg.audio)
    duration = max((c.end for c in seg.cues), default=0.0)
    stream = {
        "kind": "audio", "index": 0, "selector": "a:0", "codec": codec,
        "language": None, "title": None, "channels": None, "channel_layout": None,
        "forced": False, "hearing_impaired": False, "attached_pic": False,
        "groups": [], "label": f"a:0  {codec}",
    }
    return {
        "name": seg.title or f"Part {seg.index + 1}",
        "path": make_ref(epub, index),
        "size": None,
        "duration": duration,
        "streams": [stream],
        "subtitle_tracks": [],
        "best_track": None,
        "has_sidecar": True,
        "book_title": book.title or epub.stem,
    }


def _parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """Parse a single-range ``Range: bytes=start-end`` header to inclusive
    (start, end), clamped to the resource size; None if absent/unsatisfiable."""
    if not header or not header.startswith("bytes=") or size <= 0:
        return None
    spec = header[len("bytes="):].split(",", 1)[0].strip()
    lo, _, hi = spec.partition("-")
    try:
        if lo == "":  # suffix range: last N bytes
            start, end = max(0, size - int(hi)), size - 1
        else:
            start, end = int(lo), (int(hi) if hi else size - 1)
    except ValueError:
        return None
    end = min(end, size - 1)
    if start > end or start >= size:
        return None
    return start, end


def _stream_member(z: zipfile.ZipFile, f, length: int, chunk: int = 65536) -> Iterator[bytes]:
    """Yield ``length`` bytes from an open zip member, closing both afterwards."""
    remaining = length
    try:
        while remaining > 0:
            data = f.read(min(chunk, remaining))
            if not data:
                break
            remaining -= len(data)
            yield data
    finally:
        f.close()
        z.close()


def segment_audio_response(epub: Path, index: int, range_header: str | None) -> StreamingResponse:
    """Serve a segment's narration **directly from the EPUB zip** with HTTP Range
    support — streamed in chunks, no copy on disk and ~constant memory. Seeking
    works because ``ZipExtFile`` is seekable."""
    seg = _segment(book_for(epub), index)
    z = zipfile.ZipFile(epub)
    try:
        size = z.getinfo(seg.audio).file_size
    except KeyError as exc:
        z.close()
        raise FileNotFoundError(f"audio member {seg.audio!r} missing from {epub.name}") from exc
    mime = _AUDIO_MIME.get(_member_codec(seg.audio), "audio/mp4")
    rng = _parse_range(range_header, size)
    if rng is None:
        return StreamingResponse(
            _stream_member(z, z.open(seg.audio), size),
            media_type=mime,
            headers={"Accept-Ranges": "bytes", "Content-Length": str(size)},
        )
    start, end = rng
    f = z.open(seg.audio)
    if start:
        f.seek(start)
    return StreamingResponse(
        _stream_member(z, f, end - start + 1),
        status_code=206,
        media_type=mime,
        headers={
            "Accept-Ranges": "bytes",
            "Content-Length": str(end - start + 1),
            "Content-Range": f"bytes {start}-{end}/{size}",
        },
    )


def extract_segment_temp(epub: Path, index: int) -> Path:
    """Extract a segment's audio member to a temp file for clipping (ffmpeg needs a
    seekable file). The caller deletes it once the cut finishes — nothing persists."""
    member = _segment(book_for(epub), index).audio
    fd, tmp = tempfile.mkstemp(prefix="qc-epub-", suffix=Path(member).suffix or ".bin")
    os.close(fd)
    return extract_audio_member(epub, member, tmp)


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
