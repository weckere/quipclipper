"""Cache for extracted subtitle cues.

Wraps resolve_subtitles with a file-based JSON cache so repeated searches
skip the expensive ffmpeg extraction.

Change detection
----------------
The cache key incorporates the modification time of the *subtitle source*:
the sidecar ``.srt``/``.ass`` mtime when one exists, otherwise the video
file's own mtime (embedded subtitle edits rewrite the container, bumping
that mtime).  So when subtitles change, the key changes and the next
search re-extracts automatically.  ``is_cached()`` therefore doubles as a
"are the indexed subs still current?" check.

Each cache file stores the originating video path so stale entries (left
behind when the source mtime changes) can be located and cleared.

No subtitles
------------
A file with no usable subtitles (no sidecar, and no embedded text track) gets a
``.nosubs`` marker under the same mtime-based key, so the folder index can tell
"nothing to index" from "not indexed yet". Adding a sidecar or remuxing in a
track changes the key, which retires the marker on its own.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
import threading
from pathlib import Path

from quipclipper.models import Cue
from quipclipper.subtitles import (
    find_sidecar, is_text_subtitle, list_embedded_tracks, resolve_subtitles,
)

_ENTRY_GLOBS = ("*.json", "*.nosubs")
# How much of an entry to read for the video path it belongs to (written first): a
# path's JSON fits in 4 KiB (the whole entry is read when it doesn't).
_HEAD = 4096
_VIDEO_PREFIX = '{"video": '


def _digest(raw: str) -> str:
    """An entry's key. ``surrogateescape``: a file name that isn't UTF-8 (a latin-1 byte
    in a Linux name) hashes like its bytes instead of raising mid-scan."""
    return hashlib.sha256(raw.encode("utf-8", "surrogateescape")).hexdigest()


class SubtitleCache:
    def __init__(self, cache_dir: Path, default_langs=None) -> None:
        self._dir = cache_dir / "sub_cache"
        self._dir.mkdir(parents=True, exist_ok=True)
        # Server-wide subtitle-language preference (B14) used when auto-selecting
        # (track is None). Kept fixed so the "auto" cache key stays consistent
        # across search / pre-indexing regardless of any per-request override.
        self._default_langs = default_langs
        # Per-key locks dedupe concurrent cold extractions of the same
        # (video, track) — e.g. the VTT track and the script panel both
        # request the same subtitles when an item is opened.
        self._inflight: dict[str, threading.Lock] = {}
        self._inflight_guard = threading.Lock()
        # video path -> its entry file names, read once on first need and kept current
        # (see _owners).
        self._owner_map: dict[str, set[str]] | None = None
        self._owners_lock = threading.Lock()
        # While the map is built: the entries written and removed meanwhile.
        self._owner_log: list[tuple[bool, str, str]] | None = None
        self._owners_built = threading.Condition(self._owners_lock)

    def _source_mtime(self, video: Path, siblings: list[Path] | None = None) -> float:
        """Mtime of the subtitle source — sidecar if present, else the video.

        Pass the server language preference so the *same* sidecar `resolve` picks
        (language-aware) is the one whose mtime keys the cache."""
        sidecar = find_sidecar(video, self._default_langs, siblings)
        target = sidecar if sidecar else video
        try:
            return Path(target).stat().st_mtime
        except OSError:
            return 0.0

    def _cache_key(self, video: Path, track: int | None = None,
                   siblings: list[Path] | None = None) -> str:
        # Track must be part of the key: different subtitle streams in the
        # same container have different cues.  None = backend auto-select.
        tkey = "auto" if track is None else str(track)
        raw = f"{video}:{self._source_mtime(video, siblings)}:{tkey}"
        return _digest(raw)

    def _cache_path(self, video: Path, track: int | None = None,
                    siblings: list[Path] | None = None) -> Path:
        return self._dir / (self._cache_key(video, track, siblings) + ".json")

    def _inflight_lock(self, key: str) -> threading.Lock:
        with self._inflight_guard:
            lk = self._inflight.get(key)
            if lk is None:
                lk = threading.Lock()
                self._inflight[key] = lk
            return lk

    def _prune_inflight(self, key: str) -> None:
        """Drop a lock entry once no thread holds it, so the map stays small.

        A waiter that already fetched the old Lock object keeps working with
        it; the worst case of this benign race is one duplicate extraction,
        and the atomic cache write makes that harmless.
        """
        with self._inflight_guard:
            lk = self._inflight.get(key)
            if lk is not None and not lk.locked():
                del self._inflight[key]

    def is_cached(self, video: Path, track: int | None = None) -> bool:
        try:
            return self._cache_path(video, track).exists()
        except OSError:
            return False

    def entry_names(self) -> set[str]:
        """The cache folder's file names, for :meth:`index_state` over many files."""
        try:
            return set(os.listdir(self._dir))
        except OSError:
            return set()

    def index_state(self, video: Path, siblings: list[Path] | None = None,
                    names: set[str] | None = None) -> str:
        """For a library row: "indexed" (the auto-selected subtitles are cached and
        current), "no_subs" (indexing found none to cache), "changed" (indexed once, but
        its subtitle source changed since: see :meth:`_changed`) or "not_indexed". One
        key, so one stat of the source and at most two lookups here; ``siblings`` (the
        folder's listing) spares listing the folder again, and ``names`` (from
        :meth:`entry_names`) the lookups. Never reads a subtitle file."""
        try:
            mtime = self._source_mtime(video, siblings)
            key = self._key_at(video, mtime)
            if names is not None:
                if key + ".json" in names:
                    return "indexed"
                if key + ".nosubs" in names:
                    return "no_subs"
            else:
                if (self._dir / (key + ".json")).exists():
                    return "indexed"
                if (self._dir / (key + ".nosubs")).exists():
                    return "no_subs"
            if self._changed(video, mtime):
                return "changed"
        except (OSError, ValueError):  # ValueError: a name the file system can't take
            pass
        return "not_indexed"

    @staticmethod
    def _key_at(video: Path | str, mtime: float, tkey: str = "auto") -> str:
        return _digest(f"{video}:{mtime}:{tkey}")

    def _changed(self, video: Path, mtime: float) -> bool:
        """True when ``video`` has cache entries (from the owner map, kept in memory)
        but none was written at its subtitle source's current ``mtime``: it was indexed,
        and its sidecar or the file itself changed since. A file only ever opened with
        an explicit track, at the current mtime, is not "changed", just not indexed."""
        target = str(video)
        owners = self._owners()
        with self._owners_lock:
            names = list(owners.get(target, ()))
        if not names:
            return False
        current = {self._key_at(target, mtime, t) for t in ("auto", *map(str, range(16)))}
        return not any(n.rsplit(".", 1)[0] in current for n in names)

    # --- files with no usable subtitles -------------------------------------

    def _nosubs_path(self, video: Path) -> Path:
        return self._dir / (self._cache_key(video) + ".nosubs")

    def has_no_subs(self, video: Path) -> bool:
        """True when indexing found no usable subtitles at the current source mtime."""
        try:
            return self._nosubs_path(video).exists()
        except OSError:
            return False

    def mark_no_subs(self, video: Path) -> bool:
        """Record that *video* has nothing to index, if that is really so.

        Called after an extraction failed: only a file with no sidecar and no
        embedded TEXT track is marked (image-only PGS/VobSub tracks count as none).
        A probe error means "unknown", so nothing is marked. Returns True if marked."""
        try:
            if find_sidecar(video, self._default_langs):
                return False
            if any(is_text_subtitle(t) for t in list_embedded_tracks(video)):
                return False
        except (OSError, RuntimeError, ValueError):
            return False
        with contextlib.suppress(OSError):
            marker = self._nosubs_path(video)
            marker.write_text(json.dumps({"video": str(video)}))
            self._own(video, marker.name)
            return True
        return False

    def _entries(self):
        for pattern in _ENTRY_GLOBS:
            yield from self._dir.glob(pattern)

    # --- which entries belong to which file -----------------------------------
    # Sweeping one file's stale entries (after every extraction) and clearing a file or
    # a folder need that file's entries. Finding them by reading every entry cost a full
    # read of the cache per extraction, so indexing a library took time quadratic in its
    # size (wizrock: ~42 KB an entry, ~33k files). Instead each entry's owner is read
    # once, from its head, and the map is kept current as entries come and go.

    def _owners(self) -> dict[str, set[str]]:
        """``video path -> its entry file names``, read from the entries' heads on first
        need. Call it WITHOUT ``_owners_lock`` held, and use the map under it: it is
        built outside the lock, since reading a library-sized cache's heads takes a
        while and every cache write waits on that lock; the writes made meanwhile are
        logged and applied after. Once built it stays, kept current by _own/_disown."""
        with self._owners_lock:
            while self._owner_map is None and self._owner_log is not None:
                self._owners_built.wait()  # another thread is building it
            if self._owner_map is not None:
                return self._owner_map
            self._owner_log = []
        owners: dict[str, set[str]] | None = None
        try:
            built: dict[str, set[str]] = {}
            for f in self._entries():
                video = self._stored_video(f)
                if video is not None:
                    built.setdefault(video, set()).add(f.name)
            owners = built
        finally:
            with self._owners_lock:
                log, self._owner_log = self._owner_log or [], None
                if owners is not None:
                    for add, video, name in log:
                        if add:
                            owners.setdefault(video, set()).add(name)
                        elif video in owners:
                            owners[video].discard(name)
                            if not owners[video]:
                                del owners[video]
                    self._owner_map = owners
                self._owners_built.notify_all()
        return owners

    def _own(self, video: Path | str, name: str) -> None:
        with self._owners_lock:
            if self._owner_map is not None:
                self._owner_map.setdefault(str(video), set()).add(name)
            elif self._owner_log is not None:  # being built: applied when it is
                self._owner_log.append((True, str(video), name))
            # else the first _owners() reads it from disk

    def _disown(self, video: str, name: str) -> None:
        with self._owners_lock:
            if self._owner_map is not None and video in self._owner_map:
                self._owner_map[video].discard(name)
                if not self._owner_map[video]:
                    del self._owner_map[video]
            elif self._owner_log is not None:
                self._owner_log.append((False, video, name))

    def _entries_of(self, videos) -> list[tuple[str, Path]]:
        """``(video, entry)`` for every entry of the given video paths (a predicate on
        the path, or one path)."""
        match = videos if callable(videos) else (lambda v, t=str(videos): v == t)
        owners = self._owners()
        with self._owners_lock:
            return [(v, self._dir / n) for v, names in owners.items() if match(v)
                    for n in sorted(names)]

    def _unlink(self, video: str, entry: Path) -> bool:
        try:
            entry.unlink()
        except FileNotFoundError:
            self._disown(video, entry.name)
            return False
        except OSError:
            return False
        self._disown(video, entry.name)
        return True

    def _read(self, cp: Path) -> list[Cue] | None:
        if not cp.exists():
            return None
        try:
            data = json.loads(cp.read_text())
            cues_raw = data["cues"] if isinstance(data, dict) else data
            return [
                Cue(index=c["index"], start=c["start"], end=c["end"], text=c["text"],
                    speaker=c.get("speaker"))
                for c in cues_raw
            ]
        except OSError:
            # An unreadable entry (e.g. owned by a stale UID after a host user
            # migration) is a cache MISS, not an error: fall through to a fresh
            # extraction rather than 500ing every subtitle load. Don't unlink —
            # we likely can't, and the file may become readable again.
            return None
        except (json.JSONDecodeError, KeyError, TypeError):
            cp.unlink(missing_ok=True)
            return None

    def _write(self, cp: Path, video: Path, cues: list[Cue]) -> None:
        """Atomically write cues to a cache path (temp file + rename)."""
        payload = json.dumps(
            {
                "video": str(video),
                "cues": [
                    {"index": c.index, "start": c.start, "end": c.end, "text": c.text,
                     "speaker": c.speaker}
                    for c in cues
                ],
            },
        )
        tmp = None  # so a mkstemp failure doesn't NameError the cleanup below
        try:
            fd, tmp = tempfile.mkstemp(dir=self._dir, suffix=".tmp")
            with os.fdopen(fd, "w") as f:
                f.write(payload)
            os.replace(tmp, cp)
            self._own(video, cp.name)
        except OSError:
            # Best-effort cleanup; extraction result is still returned.
            if tmp is not None:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)

    def cached(self, video: Path, track: int | None = None,
               siblings: list[Path] | None = None) -> list[Cue] | None:
        """``video``'s cached cues, or None when they aren't cached: never extracts."""
        try:
            return self._read(self._cache_path(video, track, siblings))
        except OSError:
            return None

    def resolve(self, video: Path, track: int | None = None, fresh: bool = False) -> list[Cue]:
        """``video``'s cues, extracted and cached when they aren't cached yet.
        ``fresh``: read them again even so; the old entries stay until the new ones are
        written, so a re-read cut short (cancelled) leaves the index as it was."""
        cp = self._cache_path(video, track)

        # Fast path: serve from cache without taking any lock.
        cues = None if fresh else self._read(cp)
        if cues is not None:
            return cues

        # Cold path: serialize concurrent extractions of the same key so we
        # only run ffmpeg once.  Re-check the cache after acquiring the lock.
        key = self._cache_key(video, track)
        try:
            with self._inflight_lock(key):
                cues = None if fresh else self._read(cp)
                if cues is not None:
                    return cues

                resolved = resolve_subtitles(
                    subs=None, video=video, track=track, langs=self._default_langs,
                )
                cues = resolved.cues
                self._write(cp, video, cues)

                # Dual-key: when auto-selecting (track is None), also store under
                # the concrete track index that auto-selection landed on.  That way
                # pre-indexing (which resolves None) warms the cache for the item
                # view, which requests subtitles by that explicit index.
                if track is None and resolved.track is not None:
                    concrete = self._cache_path(video, resolved.track)
                    if concrete != cp and (fresh or self._read(concrete) is None):
                        self._write(concrete, video, cues)

                # Sweep stale entries for this video written under a superseded
                # source mtime — otherwise every subtitle edit leaves the old
                # JSON behind forever (R5). The just-written current-key files
                # are kept; only differently-keyed entries for the same path go.
                self._sweep_stale(video)
        finally:
            self._prune_inflight(key)

        return cues

    # --- cache maintenance ---------------------------------------------------

    def _stored_video(self, cache_file: Path) -> str | None:
        """Return the video path recorded in a cache file, if any. Reads only the head
        of an entry this class wrote (the path comes first), so not every cue."""
        try:
            with open(cache_file, "rb", buffering=0) as fh:  # unbuffered: just the head
                raw = fh.read(_HEAD)
                head = raw.decode("utf-8", "ignore")  # a character cut at the end
                if head.startswith(_VIDEO_PREFIX):
                    with contextlib.suppress(json.JSONDecodeError):
                        video, _ = json.JSONDecoder().raw_decode(head, len(_VIDEO_PREFIX))
                        return video if isinstance(video, str) else None
                data = json.loads((raw + fh.read()).decode("utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            return None
        return data.get("video") if isinstance(data, dict) else None

    def _sweep_stale(self, video: Path) -> int:
        """Remove cache entries for *video* keyed on a *different* source mtime.

        A cache filename embeds the source mtime, so an entry whose key doesn't
        match the video's current mtime-based key (for any track) is stale — it
        was written before the subtitles last changed and can never be read
        again. Returns the number of stale files removed. Best-effort.
        """
        removed = 0
        target = str(video)
        current_mtime = self._source_mtime(video)
        for _, f in self._entries_of(target):
            # Recompute the key prefix for this file's video at the current
            # mtime; a stale file's name won't match any current-mtime key.
            key_stem = f.stem
            raw_auto = f"{target}:{current_mtime}:auto"
            if _digest(raw_auto) == key_stem:
                continue  # current auto entry
            # A concrete-track key: brute-forcing every track is wasteful, so
            # instead reconstruct only the concrete track we might have dual-keyed
            # by reading the stored mtime portion — cheaper to just keep entries
            # whose mtime matches the current one.
            if self._entry_mtime_matches(f, current_mtime, target):
                continue
            removed += self._unlink(target, f)
        return removed

    def _entry_mtime_matches(self, cache_file: Path, mtime: float,
                             stored: str | None = None) -> bool:
        """True if this cache entry's key was built from *mtime*.

        The key is a hash so the mtime isn't recoverable directly; instead we
        recompute the hash for the entry's stored video across the plausible
        track keys (auto + any small track index) at *mtime* and check for a
        match. Cheap and bounded — subtitle tracks are single digits."""
        stored = stored or self._stored_video(cache_file)
        if stored is None:
            return False
        key_stem = cache_file.stem
        for tkey in ("auto", *(str(t) for t in range(16))):
            raw = f"{stored}:{mtime}:{tkey}"
            if _digest(raw) == key_stem:
                return True
        return False

    def clear(self, video: Path) -> int:
        """Remove all cache entries for a video (current key + stale orphans).

        Returns the number of cache files removed.
        """
        removed = 0
        target = str(video)
        # The current-key file, and older entries written under a different source
        # mtime, and markers.
        cp = self._cache_path(video)
        if cp.exists():
            removed += self._unlink(target, cp)
        for _, f in self._entries_of(target):
            removed += self._unlink(target, f)
        return removed

    def clear_under(self, folder: Path) -> int:
        """Remove every cache entry whose source video lives under *folder*.

        Returns the number of cache files removed.
        """
        prefix = str(folder)
        under = lambda v: v == prefix or v.startswith(prefix + os.sep)  # noqa: E731
        return sum(self._unlink(v, f) for v, f in self._entries_of(under))
