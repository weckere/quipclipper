"""Jellyfin titles and artwork (M18): what Jellyfin knows about each library file.

One listing of every movie, episode, track and audiobook (``GET /Items?Recursive=true``,
read in pages) is mapped through the path map onto the media roots and kept as a
path → item index: the real episode title, series, season, episode and year for clip
names, and which image to show as a file's or a folder's poster. The index is cached
on disk in the state dir, so a restart doesn't wait for Jellyfin, and rebuilt in the
background once it is an hour old (or on demand). A file it doesn't hold falls back to
the names parsed from its path.

:class:`LibraryRefresher` asks Jellyfin to scan the library holding the clips folder a
few seconds after a clip lands there, so the clip shows up on the TV (debounced: a
batch of clips is one scan).
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator

from quipclipper_web.jellyfin import JellyfinClient, JellyfinError, map_path

INDEX_FILE = "jellyfin-index.json"
INDEX_VERSION = 2  # 2: movies in collections are listed (CollapseBoxSetItems=false)
REFRESH_EVERY = 3600.0  # seconds: an index older than this is rebuilt in the background
PAGE_SIZE = 5000
# After a failed build, wait this long before trying again on the next lookup.
RETRY_AFTER = 300.0
REFRESH_DELAY = 10.0  # seconds after the last clip lands before the library scan

# Jellyfin item ids and image tags are 32 hex digits.
ITEM_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SEASON_RE = re.compile(r"(?i)^(season\s*\d+|specials|extras)$")


def _int(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _art(item_id, tag, image_type: str = "Primary", wide: bool = False) -> dict | None:
    if not (isinstance(item_id, str) and ITEM_ID_RE.match(item_id)):
        return None
    if not (isinstance(tag, str) and ITEM_ID_RE.match(tag)):
        return None
    art = {"id": item_id, "type": image_type, "tag": tag}
    if wide:
        art["wide"] = True  # a 16:9 still, not a 2:3 poster
    return art


def _file_art(item: dict) -> dict | None:
    """The image for a file's own row: an episode's still, a movie's poster, a cover."""
    tags = item.get("ImageTags") or {}
    own = tags.get("Primary")
    kind = item.get("Type")
    if kind == "Episode":
        return (_art(item.get("Id"), own, wide=True)
                or _art(item.get("SeriesId"), item.get("SeriesPrimaryImageTag")))
    if kind in ("Audio", "AudioBook"):
        return _art(item.get("Id"), own) or _art(item.get("AlbumId"),
                                                  item.get("AlbumPrimaryImageTag"))
    return _art(item.get("Id"), own) or _art(item.get("Id"), tags.get("Thumb"), "Thumb",
                                             wide=True)


def file_entry(item: dict) -> dict:
    """What the index keeps about one file (see ``jellyfin.lookup`` in docs/API.md)."""
    kind = item.get("Type")
    episode = kind == "Episode"
    name = item.get("Name") if isinstance(item.get("Name"), str) else None
    series = item.get("SeriesName") if isinstance(item.get("SeriesName"), str) else None
    entry = {
        "id": item.get("Id"),
        "type": kind,
        "title": name.strip() if name else None,
        # The series for an episode, the movie for a movie: what {show} names.
        "show": (series if episode else name if kind == "Movie" else
                 item.get("Album") if isinstance(item.get("Album"), str) else None),
        "season": _int(item.get("ParentIndexNumber")) if episode else None,
        "episode": _int(item.get("IndexNumber")) if episode else None,
        "year": _int(item.get("ProductionYear")),
        "provider_ids": {k: v for k, v in (item.get("ProviderIds") or {}).items()
                         if isinstance(k, str) and isinstance(v, str)},
        "jellyfin_path": item.get("Path"),
        "art": _file_art(item),
    }
    if entry["show"]:
        entry["show"] = entry["show"].strip()
    return entry


def naming_meta(entry: dict | None) -> dict | None:
    """The clip-naming values (``title``, ``show``, ``season``, ``episode``, ``year``)."""
    if not entry:
        return None
    return {k: entry.get(k) for k in ("title", "show", "season", "episode", "year")}


def _folder_entries(item: dict, local: str, roots: set[str]) -> list[tuple[str, dict]]:
    """The folders a file says something about: an episode's show (and season) folder,
    a movie's own folder. A media root itself is never a show or a movie."""
    parent = os.path.dirname(local)
    out: list[tuple[str, dict]] = []
    if item.get("Type") == "Episode":
        series = _art(item.get("SeriesId"), item.get("SeriesPrimaryImageTag"))
        show_dir = parent
        if _SEASON_RE.match(os.path.basename(parent)):
            show_dir = os.path.dirname(parent)
            season_id = item.get("SeasonId")
            season_art = None
            if item.get("ParentPrimaryImageItemId") == season_id:
                season_art = _art(season_id, item.get("ParentPrimaryImageTag"))
            series_name = item.get("SeriesName") or ""
            season_name = item.get("SeasonName") or os.path.basename(parent)
            out.append((parent, {
                "id": season_id, "type": "Season",
                "title": f"{series_name} · {season_name}" if series_name else season_name,
                "art": season_art or series,
            }))
        out.append((show_dir, {"id": item.get("SeriesId"), "type": "Series",
                               "title": item.get("SeriesName"), "art": series}))
    elif item.get("Type") == "Movie":
        out.append((parent, {"id": item.get("Id"), "type": "Movie",
                             "title": item.get("Name"), "year": _int(item.get("ProductionYear")),
                             "art": _file_art(item)}))
    return [(d, e) for d, e in out if d not in roots and isinstance(e.get("id"), str)]


def build_index(items: Iterable[dict], path_map: tuple[tuple[str, str], ...],
                roots: list[Path]) -> tuple[dict[str, dict], dict[str, dict]]:
    """``(files, folders)``: local path → entry, for every item whose path maps onto disk.

    A folder two different shows or movies claim (two movies sharing a folder) gets no
    entry: its poster would be a guess."""
    files: dict[str, dict] = {}
    folders: dict[str, dict] = {}
    clashes: set[str] = set()
    root_set = {os.path.realpath(r) for r in roots}
    for item in items:
        jf_path = item.get("Path")
        if not isinstance(jf_path, str) or item.get("Type") not in (
                "Movie", "Episode", "Audio", "AudioBook"):
            continue
        local = map_path(jf_path, path_map)
        if local is None:
            continue
        files[local] = file_entry(item)
        for folder, entry in _folder_entries(item, local, root_set):
            prev = folders.get(folder)
            if prev is not None and prev["id"] != entry["id"]:
                clashes.add(folder)
            folders.setdefault(folder, entry)
    for folder in clashes:
        folders.pop(folder, None)
    return files, folders


def _real_map(path_map: tuple[tuple[str, str], ...]) -> tuple[tuple[str, str], ...]:
    """The path map with each local side resolved once, so index keys match the
    realpaths the library routes hand out (no per-file realpath over 25,000 files)."""
    return tuple((src, os.path.realpath(dst)) for src, dst in path_map)


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z")


class JellyfinIndex:
    """The path → item index, on disk under the state dir and in memory."""

    def __init__(self, state_dir: Path, client: Callable[[], JellyfinClient],
                 path_map: tuple[tuple[str, str], ...], roots: list[Path], *,
                 refresh_every: float = REFRESH_EVERY, clock: Callable[[], float] = time.time):
        self.file = Path(state_dir) / INDEX_FILE
        self._client = client
        self._path_map = _real_map(path_map)
        self._roots = roots
        self._refresh_every = refresh_every
        self._clock = clock
        self._files: dict[str, dict] = {}
        self._folders: dict[str, dict] = {}
        self.built: float | None = None
        self.error: str | None = None
        self._failed_at: float | None = None
        self._build_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._load()

    # --- reading ------------------------------------------------------------------

    def _load(self) -> None:
        try:
            data = json.loads(self.file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict) or data.get("version") != INDEX_VERSION:
            return
        if data.get("path_map") != [list(p) for p in self._path_map]:
            return  # mapped with another path map: its keys are wrong now
        files, folders = data.get("files"), data.get("folders")
        if isinstance(files, dict) and isinstance(folders, dict):
            self._files, self._folders = files, folders
            self.built = data.get("built") if isinstance(data.get("built"), (int, float)) else None

    def lookup(self, path: str | Path) -> dict | None:
        """What Jellyfin knows about a file (by its realpath), or None."""
        self.refresh_if_stale()
        return self._files.get(os.path.normpath(str(path)))

    def folder(self, path: str | Path) -> dict | None:
        """The show, season or movie a folder holds, or None."""
        self.refresh_if_stale()
        return self._folders.get(os.path.normpath(str(path)))

    def by_stem(self):
        """A lookup from a file's stem to ``(path, entry)``, for the stems only one file
        in the index has: the clips backfill finds an old clip's source by the name in
        its folder or file name."""
        self.refresh_if_stale()
        found: dict[str, tuple[str, dict] | None] = {}
        for path, entry in self._files.items():
            stem = os.path.splitext(os.path.basename(path))[0]
            found[stem] = None if stem in found else (path, entry)
        return found.get

    def status(self) -> dict:
        return {
            "built": _iso(self.built),
            "files": len(self._files),
            "folders": len(self._folders),
            "building": self._build_lock.locked(),
            "error": self.error,
        }

    # --- building -----------------------------------------------------------------

    def stale(self) -> bool:
        now = self._clock()
        if self._failed_at is not None and now - self._failed_at < RETRY_AFTER:
            return False
        return self.built is None or now - self.built >= self._refresh_every

    def refresh_if_stale(self) -> None:
        """Start a background rebuild when the index is old (lookups keep the old one)."""
        if not self.stale() or self._build_lock.locked():
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._build_quietly, daemon=True,
                                        name="jellyfin-index")
        self._thread.start()

    def _build_quietly(self) -> None:
        try:
            self.build()
        except JellyfinError:
            pass  # recorded in self.error; lookups fall back to names meanwhile

    def build(self) -> dict:
        """Read every item from Jellyfin and replace the index. Waits for a build already
        running instead of starting a second. Raises JellyfinError."""
        with self._build_lock:
            started = self._clock()
            try:
                files, folders = build_index(self._pages(self._client()), self._path_map,
                                             self._roots)
            except JellyfinError as exc:
                self.error = str(exc)
                self._failed_at = self._clock()
                raise
            self._files, self._folders = files, folders
            self.built, self.error, self._failed_at = started, None, None
            self._save()
            return self.status()

    @staticmethod
    def _pages(client: JellyfinClient) -> Iterator[dict]:
        """Every item, a page at a time (a page is dropped once it is indexed)."""
        start = 0
        while True:
            page = client.items_page(start, PAGE_SIZE)
            yield from page
            if len(page) < PAGE_SIZE:
                return
            start += len(page)

    def _save(self) -> None:
        data = {"version": INDEX_VERSION, "built": self.built,
                "path_map": [list(p) for p in self._path_map],
                "files": self._files, "folders": self._folders}
        try:
            self.file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.file.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
            tmp.replace(self.file)
        except OSError:
            pass  # kept in memory; the next start rebuilds it


def library_for(folders: list[dict], target: Path,
                path_map: tuple[tuple[str, str], ...]) -> dict | None:
    """The Jellyfin library (one of ``virtual_folders()``) whose folder holds ``target``:
    the closest one when several do. None when no library maps onto it."""
    want = os.path.realpath(target)
    best: tuple[int, dict] | None = None
    for lib in folders:
        for loc in lib.get("Locations") or []:
            if not isinstance(loc, str):
                continue
            local = map_path(loc, path_map)
            if local is None:
                continue
            local = os.path.realpath(local)
            if want == local or want.startswith(local.rstrip("/") + "/"):
                if best is None or len(local) > best[0]:
                    best = (len(local), lib)
    return best[1] if best else None


class LibraryRefresher:
    """Scan the Jellyfin library holding the clips folder once clips stop landing."""

    def __init__(self, client: Callable[[], JellyfinClient], clips_dir: Path,
                 path_map: tuple[tuple[str, str], ...], delay: float | None = None):
        self._client = client
        self._clips_dir = clips_dir
        self._path_map = path_map
        self._delay = REFRESH_DELAY if delay is None else delay
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()
        self.last: dict | None = None  # the last scan: when, which library, or the error

    def poke(self) -> None:
        """A clip landed: (re)start the countdown to the scan."""
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self._delay, self.refresh)
            self._timer.daemon = True
            self._timer.start()

    def refresh(self) -> dict:
        """Find the clips folder's library and ask Jellyfin to scan it, now."""
        with self._lock:
            self._timer = None
        result: dict = {"at": _iso(time.time()), "library": None, "error": None}
        try:
            client = self._client()
            lib = library_for(client.virtual_folders(), self._clips_dir, self._path_map)
            if lib is None:
                result["error"] = ("No Jellyfin library holds the clips folder "
                                   "(add one, and a path map entry for it).")
            elif not (isinstance(lib.get("ItemId"), str) and ITEM_ID_RE.match(lib["ItemId"])):
                result["error"] = "Jellyfin gave the clips library no id."
            else:
                client.refresh_library(lib["ItemId"])
                result["library"] = lib.get("Name")
        except JellyfinError as exc:
            result["error"] = str(exc)
        self.last = result
        return result

    def cancel(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
