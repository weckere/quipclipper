"""The clips library as clients see it (M18, D-033): metadata sidecars, a listing that
walks, searches and filters it, and a FLAC conversion cache.

Every clip or set file quipclipper writes into the clips library gets a hidden sidecar
next to it, ``.<file name>.qc.json``: where it came from (source, line, padding, cue),
what it is (kind, codec, channels, duration) and what Jellyfin knows about the source.
Dot-files are hidden from listings, the library and Jellyfin, so the sidecar never shows
up as a clip. Old clips get a best-effort one from ffprobe and the file name
(:func:`backfill`). The fields are a contract for LeadStinger (docs/API.md, "Clips
library contract").
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import subprocess
import threading
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from quipclipper.clip import FALLBACK_AUDIO_EXT, LOSSLESS_AUDIO_EXT
from quipclipper.naming import clean_title, slugify
from quipclipper.subtitles import AUDIO_EXTS

log = logging.getLogger(__name__)

SCHEMA = "quipclipper.clip-meta"
VERSION = 1
SUFFIX = ".qc.json"
SET_EXT = ".lsset"
# Every extension a cut can write: the video containers, a GIF, and each lossless audio
# cut's codec-matched container (an AAC line is .m4a, AC-3 .ac3, TrueHD .thd, …).
CLIP_AUDIO_EXTS = tuple(sorted({f".{e}" for e in LOSSLESS_AUDIO_EXT.values()}
                               | {f".{FALLBACK_AUDIO_EXT}"}))
CLIP_EXTS = (".mkv", ".mp4", ".m4v", ".webm", ".gif", *CLIP_AUDIO_EXTS)
KINDS = ("audio", "video", "gif", "set")
# The listing's cursor: "v1." and the newest file's mtime in nanoseconds.
_CURSOR_RE = re.compile(r"^v1\.(\d{1,20})$")
_TS_RE = re.compile(r"(?:^|_)(\d{2})-(\d{2})-(\d{2})(?:_|$)")
_SXXEXX_RE = re.compile(r"(?i)s(\d{1,2})e(\d{1,3})")
_YEAR_RE = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- sidecars ---------------------------------------------------------------------------


def sidecar_path(clip: Path) -> Path:
    return clip.with_name(f".{clip.name}{SUFFIX}")


# Files a job is writing into the library right now, as (folder, name prefix): the
# listing leaves them out until the clip and its sidecar are both complete, so a client
# polling with ``since`` never sees a half-written file or a clip without its metadata.
_writing: set[tuple[str, str]] = set()
_writing_lock = threading.Lock()


@contextlib.contextmanager
def writing(out: Path):
    """Hide ``out`` (and, for a channel split's base name, every ``<out>…`` file the
    split writes) from the listing while the block runs."""
    key = (str(out.parent.resolve()), out.name)
    with _writing_lock:
        _writing.add(key)
    try:
        yield
    finally:
        with _writing_lock:
            _writing.discard(key)


def in_progress(f: Path) -> bool:
    if not _writing:
        return False
    folder = str(f.parent.resolve())
    with _writing_lock:
        return any(folder == d and f.name.startswith(n) for d, n in _writing)


def publish(paths) -> None:
    """Stamp freshly written clips (and their sidecars written) with the time they were
    published, so they are newer than any ``since`` cursor handed out while they were
    being written, however long the sidecar took."""
    for p in paths:
        with contextlib.suppress(OSError):
            os.utime(p)


def is_listed(f: Path) -> bool:
    """A clip or set file the listing shows (never a dot-file: macOS leaves "._name"
    AppleDouble files with the clip's own extension on SMB shares, nor one a job is
    still writing)."""
    return (not f.name.startswith(".") and f.suffix.lower() in (*CLIP_EXTS, SET_EXT)
            and f.is_file() and not in_progress(f))


def kind_of(f: Path, meta: dict | None = None) -> str:
    if f.suffix.lower() == SET_EXT:
        return "set"
    if meta and meta.get("kind") in KINDS:
        return meta["kind"]
    ext = f.suffix.lower()
    if ext == ".gif":
        return "gif"
    return "audio" if ext in AUDIO_EXTS or ext in CLIP_AUDIO_EXTS else "video"


def read(clip: Path) -> dict | None:
    """The clip's sidecar, or None (missing, unreadable, or not ours)."""
    try:
        data = json.loads(sidecar_path(clip).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        return None
    return data


def write(clip: Path, meta: dict) -> Path:
    """Write the clip's sidecar atomically; ``schema``, ``version``, ``file`` and
    ``created`` are filled in (``created`` only when missing)."""
    data = {"schema": SCHEMA, "version": VERSION, "file": clip.name, **meta}
    data.setdefault("created", _iso(time.time()))
    target = sidecar_path(clip)
    tmp = target.with_name(f"{target.name}.tmp{os.getpid()}.{threading.get_ident()}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n")
    os.replace(tmp, target)
    return target


def move(src: Path, dst: Path) -> None:
    """Move a clip and its sidecar together (the sidecar's ``file`` follows)."""
    os.replace(src, dst)
    old = sidecar_path(src)
    if not old.is_file():
        return
    meta = read(src)
    if meta is None:
        os.replace(old, sidecar_path(dst))
        return
    meta["file"] = dst.name
    write(dst, meta)
    old.unlink(missing_ok=True)


def delete(clip: Path) -> None:
    """Delete a clip and its sidecar."""
    clip.unlink(missing_ok=True)
    sidecar_path(clip).unlink(missing_ok=True)


def probe(path: Path) -> dict:
    """``codec``, ``channels``, ``sample_rate`` (the first audio stream), ``video_codec``
    and ``duration`` from ffprobe; missing values are None."""
    out = {"codec": None, "channels": None, "sample_rate": None, "video_codec": None,
           "duration": None}
    try:
        res = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=duration:stream=codec_type,codec_name,channels,sample_rate",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=60,
        )
        data = json.loads(res.stdout or "{}")
    except (OSError, subprocess.SubprocessError, ValueError):
        return out
    for s in data.get("streams") or []:
        if s.get("codec_type") == "audio" and out["codec"] is None:
            out["codec"] = s.get("codec_name")
            out["channels"] = s.get("channels")
            with contextlib.suppress(TypeError, ValueError):
                out["sample_rate"] = int(s.get("sample_rate"))
        elif s.get("codec_type") == "video" and out["video_codec"] is None:
            out["video_codec"] = s.get("codec_name")
    with contextlib.suppress(KeyError, TypeError, ValueError):
        out["duration"] = round(float(data["format"]["duration"]), 3)
    return out


def jellyfin_fields(entry: dict | None) -> dict | None:
    """What the sidecar keeps of a Jellyfin index entry."""
    if not entry:
        return None
    return {k: entry.get(k) for k in ("id", "type", "show", "season", "episode", "title",
                                      "year")}


def guess_from_name(clip: Path, clips_root: Path) -> dict:
    """Best-effort metadata from a clip's name and folder (the naming templates put the
    source's name in the folder or before the timestamp, the cue after it)."""
    stem = clip.stem
    rel_parent = clip.parent.relative_to(clips_root) if clip.parent != clips_root else None
    source = None
    clip_start = None
    cue = None
    m = _TS_RE.search(stem)
    if m:
        clip_start = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
        before = stem[:m.start()].strip("_")
        after = stem[m.end():].strip("_")
        source = before or None
        cue = after.replace("_", " ").strip() or None
    if source is None and rel_parent is not None and rel_parent.name != "LeadStinger":
        source = rel_parent.name
    guess: dict = {"source": {"path": None, "name": (source or "").replace("_", " ") or None,
                              "stem": source},
                   "cue": cue}
    if clip_start is not None:
        guess["clip"] = {"start": float(clip_start), "end": None}
    hay = f"{source or ''} {stem}"
    se = _SXXEXX_RE.search(hay)
    year = _YEAR_RE.search(hay)
    if se or year:
        guess["parsed"] = {"season": int(se.group(1)) if se else None,
                           "episode": int(se.group(2)) if se else None,
                           "year": int(year.group(0)) if year else None}
    return guess


# --- the listing ------------------------------------------------------------------------


def parse_cursor(value: str) -> int:
    m = _CURSOR_RE.match(value.strip())
    if not m:
        raise ValueError("since must be a cursor from an earlier listing (v1.<number>)")
    return int(m.group(1))


def make_cursor(mtime_ns: int) -> str:
    return f"v1.{mtime_ns}"


def scan(base: Path, start: Path, recursive: bool) -> list[Path]:
    """The clip and set files in ``start`` (and below it with ``recursive``). Dot-folders
    are skipped, symlinks are not followed out of ``base``."""
    root = base.resolve()
    out: list[Path] = []
    if not recursive:
        try:
            return [f for f in start.iterdir() if is_listed(f)
                    and f.resolve().is_relative_to(root)]
        except OSError:
            return []
    for dirpath, dirnames, filenames in os.walk(start, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in filenames:
            f = Path(dirpath) / name
            if is_listed(f) and f.resolve().is_relative_to(root):
                out.append(f)
    return out


def has_listed(d: Path) -> bool:
    try:
        return any(is_listed(f) for f in d.iterdir())
    except OSError:
        return False


def title_for(f: Path, meta: dict | None) -> str:
    """A display title: the line for a clip, the set's name for a set, else the name."""
    if meta:
        if meta.get("kind") == "set" and meta.get("name"):
            return meta["name"]
        if meta.get("cue"):
            return meta["cue"]
    return f.stem.replace("_", " ")


def search_text(f: Path, rel: str, meta: dict | None) -> str:
    parts = [f.name, rel]
    if meta:
        parts.append(meta.get("cue") or "")
        parts.append(meta.get("name") or "")
        src = meta.get("source") or {}
        parts += [src.get("name") or "", src.get("path") or ""]
        jf = meta.get("jellyfin") or {}
        parts += [str(jf.get("show") or ""), str(jf.get("title") or "")]
        for item in meta.get("items") or []:
            parts.append(item.get("title") or "")
    return " ".join(parts).lower().replace("_", " ")


def matches_query(text: str, query: str) -> bool:
    """Every word of the query appears in the text (case-insensitive; _ is a space)."""
    words = query.lower().replace("_", " ").split()
    return all(w in text for w in words)


def etag(rows: list[tuple], params: tuple) -> str:
    """The listing's tag. Rows are sorted: a share (mergerfs, SMB) may list a folder
    in a different order each time, which mustn't change an unchanged listing's tag."""
    h = hashlib.sha256(json.dumps([list(params), sorted(rows)], default=str).encode())
    return f'"{h.hexdigest()[:32]}"'


def etag_matches(header: str | None, tag: str) -> bool:
    """If-None-Match against our tag, weakly (RFC 9110): a proxy that compresses the reply
    (nginx gzip) hands the client ``W/"…"``, and the header may list several tags."""
    if not header:
        return False
    if header.strip() == "*":
        return True
    bare = tag.removeprefix("W/")
    return any(t.strip().removeprefix("W/") == bare for t in header.split(","))


# --- backfill ---------------------------------------------------------------------------


def backfill(clips_root: Path, *, jellyfin_by_stem=None, cancelled=lambda: False,
             progress=lambda _s: None) -> dict:
    """Give every clip and set without a sidecar a best-effort one; re-attach an orphaned
    sidecar (its clip moved, e.g. over SMB) to the file with the same name and size; and
    delete the sidecars whose clip is gone.

    ``jellyfin_by_stem(stem)`` returns ``(source path, index entry)`` for a library file
    with that stem, or None: old clips name their source in the folder or file name.
    """
    root = clips_root.resolve()
    stats = {"clips": 0, "written": 0, "kept": 0, "reattached": 0, "pruned": 0,
             "jellyfin": 0, "errors": 0}
    if not root.is_dir():
        return stats
    files: list[Path] = []
    sidecars: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            f = Path(dirpath) / name
            if name.startswith(".") and name.endswith(SUFFIX):
                sidecars.append(f)
            elif is_listed(f) and f.resolve().is_relative_to(root):
                files.append(f)
    have = {sidecar_path(f) for f in files}
    orphans: dict[tuple[str, int | None], Path] = {}
    for sc in sidecars:
        if sc in have:
            continue
        try:
            meta = json.loads(sc.read_text())
        except (OSError, ValueError):
            meta = None
        if isinstance(meta, dict) and meta.get("schema") == SCHEMA and meta.get("file"):
            orphans.setdefault((meta["file"], meta.get("size")), sc)
        else:
            orphans.setdefault((sc.name[1:-len(SUFFIX)], None), sc)
    used: set[Path] = set()
    n = len(files)
    for i, f in enumerate(sorted(files), 1):
        if cancelled():
            break
        stats["clips"] += 1
        progress(f"{i}/{n} {f.name[:60]}")
        if sidecar_path(f).is_file():
            stats["kept"] += 1
            continue
        try:
            size = f.stat().st_size
            orphan = orphans.get((f.name, size))
            if orphan is not None and orphan not in used:
                meta = json.loads(orphan.read_text())
                write(f, meta)
                orphan.unlink(missing_ok=True)
                used.add(orphan)
                stats["reattached"] += 1
                continue
            meta = backfill_meta(f, root, jellyfin_by_stem=jellyfin_by_stem)
            if meta.get("jellyfin"):
                stats["jellyfin"] += 1
            write(f, meta)
            stats["written"] += 1
        except Exception as exc:  # noqa: BLE001 - one bad file never stops the pass
            log.warning("clip metadata backfill: %s: %s", f, exc)
            stats["errors"] += 1
    if not cancelled():
        for sc in orphans.values():
            # A clip being cut has its sidecar before the listing shows the clip.
            if sc not in used and not in_progress(sc.with_name(sc.name[1:-len(SUFFIX)])):
                sc.unlink(missing_ok=True)
                stats["pruned"] += 1
    return stats


def backfill_meta(f: Path, root: Path, *, jellyfin_by_stem=None) -> dict:
    """A best-effort sidecar for a clip quipclipper cut before sidecars existed."""
    st = f.stat()
    meta: dict = {"kind": kind_of(f), "origin": "backfill", "backfilled": True,
                  "created": _iso(st.st_mtime), "size": st.st_size}
    if meta["kind"] == "set":
        meta.update(set_summary(f))
        return meta
    meta.update(guess_from_name(f, root))
    meta.update(probe(f))
    stem = (meta.get("source") or {}).get("stem")
    hit = jellyfin_by_stem(stem) if (jellyfin_by_stem and stem) else None
    if hit:
        src_path, entry = hit
        meta["source"] = {**meta["source"], "path": src_path,
                          "name": clean_title(Path(src_path))}
        meta["jellyfin"] = jellyfin_fields(entry)
        # The default template ends in {folder}: with the source known, drop it from the cue.
        tail = "_" + slugify(clean_title(Path(src_path)))
        cue = (meta.get("cue") or "").replace(" ", "_")
        if len(tail) > 1 and cue.endswith(tail) and len(cue) > len(tail):
            meta["cue"] = cue[:-len(tail)].replace("_", " ")
    meta["line"] = None
    meta["padding"] = None
    meta["trim"] = None
    return meta


def set_summary(f: Path) -> dict:
    """``name`` and ``items`` (each tile's title and trim) from a set file's manifest."""
    try:
        with zipfile.ZipFile(f) as z:
            manifest = json.loads(z.read("manifest.json"))
    except (OSError, KeyError, ValueError, zipfile.BadZipFile):
        return {"name": f.stem, "items": []}
    sets = manifest.get("sets") or []
    first = sets[0] if sets and isinstance(sets[0], dict) else {}
    items = []
    for t in first.get("tiles") or []:
        if isinstance(t, dict):
            items.append({"title": t.get("title"), "trim_start": t.get("trim_start", 0.0),
                          "trim_end": t.get("trim_end"), "notes": t.get("notes")})
    return {"name": first.get("name") or f.stem, "items": items}


# --- ?as=flac ---------------------------------------------------------------------------


FLAC_CACHE_LIMIT = 4 * 1024 ** 3  # bytes of FLAC copies kept in the state dir
FLAC_TIMEOUT = 900                # seconds one conversion may take


class FlacCache:
    """Full-mix FLAC copies of clips (D-024's format: the first audio stream, every
    channel kept), keyed by the clip's path, mtime and size. A changed clip converts
    again; its old copy goes once nobody can still be sending it (an hour on). Copies
    over :data:`FLAC_CACHE_LIMIT` in all go oldest-used first, and two conversions run
    at a time."""

    def __init__(self, state_dir: Path, *, limit: int = FLAC_CACHE_LIMIT):
        self.dir = state_dir / "clips-flac"
        self._limit = limit
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()
        self._slots = threading.BoundedSemaphore(2)

    def _lock(self, key: str) -> threading.Lock:
        with self._guard:
            if len(self._locks) > 1024:  # forget idle ones
                for k in [k for k, lk in self._locks.items() if not lk.locked()]:
                    del self._locks[k]
            return self._locks.setdefault(key, threading.Lock())

    def get(self, clip: Path, rel: str) -> Path:
        """The FLAC for ``clip`` (``rel`` is its path in the library), converting it
        if needed. Raises RuntimeError when ffmpeg fails (e.g. no audio)."""
        st = clip.stat()
        key = hashlib.sha256(rel.encode()).hexdigest()[:32]
        folder = self.dir / key
        target = folder / f"{st.st_mtime_ns}-{st.st_size}.flac"
        with self._lock(key):
            if target.is_file():
                with contextlib.suppress(OSError):
                    os.utime(target)  # recently used: pruned last
                return target
            folder.mkdir(parents=True, exist_ok=True)
            tmp = folder / f".{target.name}.part"
            try:
                with self._slots:
                    res = subprocess.run(
                        ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(clip),
                         "-map", "0:a:0", "-vn", "-sn", "-dn", "-map_metadata", "-1",
                         "-c:a", "flac", "-f", "flac", str(tmp)],
                        capture_output=True, text=True, timeout=FLAC_TIMEOUT,
                    )
            except subprocess.TimeoutExpired:
                tmp.unlink(missing_ok=True)
                raise RuntimeError(f"ffmpeg took over {FLAC_TIMEOUT} s") from None
            if res.returncode != 0 or not tmp.is_file():
                tmp.unlink(missing_ok=True)
                raise RuntimeError((res.stderr or "ffmpeg failed").strip()[-300:])
            now = time.time()
            for old in folder.iterdir():
                with contextlib.suppress(OSError):
                    if old.name != tmp.name and now - old.stat().st_mtime > 3600:
                        old.unlink()
            os.replace(tmp, target)
        self._trim(keep=target)
        return target

    def _trim(self, keep: Path) -> None:
        """Hold the copies under the size limit, oldest-used first."""
        try:
            files = [(f.stat().st_mtime, f.stat().st_size, f)
                     for f in self.dir.glob("*/*.flac") if f != keep]
            total = sum(s for _, s, _ in files) + keep.stat().st_size
        except OSError:
            return
        for _, size, f in sorted(files):
            if total <= self._limit:
                break
            with contextlib.suppress(OSError):
                f.unlink()
                total -= size

    def prune(self, live: set[str]) -> int:
        """Delete the copies of clips no longer in the library (``live``: their paths)."""
        keep = {hashlib.sha256(r.encode()).hexdigest()[:32] for r in live}
        n = 0
        if not self.dir.is_dir():
            return 0
        for d in self.dir.iterdir():
            if d.is_dir() and d.name not in keep:
                for f in d.iterdir():
                    f.unlink(missing_ok=True)
                d.rmdir()
                n += 1
        return n
