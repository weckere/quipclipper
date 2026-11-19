"""What a big folder search lists, kept for the next one (M27).

A dialogue search over folders lists every folder below them and stats each media
file's subtitle source to tell which files are indexed. On a home library (~40k files
in ~6k folders on a FUSE mount) that alone took ~9 s, before any line was scored.

For a folder set with at least ``min_files`` media files, :class:`ListingCache` keeps
the listing, and the search takes each file's subtitle-source mtime as last read
(:meth:`~quipclipper_web.sub_cache.SubtitleCache.index_entry` with ``seen``). Which
entries exist is still read fresh every time (one listing of the local cache folder), so
a file indexed or re-indexed through QuipClipper is searched at once. Changes made
outside it (a new file, a file deleted, a subtitle replaced without a re-index hook) are
seen by the next search after a refresh: a search finding the listing older than
``refresh_after`` seconds starts one in the background and is answered from the old
listing meanwhile. A smaller folder set is listed afresh every time, as before.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Hashable
from typing import Any

MIN_FILES = 2000  # smaller folder sets list in well under a second
REFRESH_AFTER = 60.0  # seconds a listing is used before a search refreshes it
KEEP = 8  # folder sets kept (least recently searched dropped first)


class ListingCache:
    def __init__(self, min_files: int | None = None, refresh_after: float | None = None,
                 keep: int = KEEP, now: Callable[[], float] = time.monotonic,
                 background: Callable[[Callable[[], None]], None] | None = None) -> None:
        self.min_files = MIN_FILES if min_files is None else min_files
        self.refresh_after = REFRESH_AFTER if refresh_after is None else refresh_after
        self.keep = keep
        self._now = now
        self._background = background or _thread
        self._lock = threading.Lock()
        # key -> [listing, taken at, a refresh is running]
        self._kept: OrderedDict[Hashable, list[Any]] = OrderedDict()

    def get(self, key: Hashable, build: Callable[[bool], Any],
            size: Callable[[Any], int]) -> tuple[Any, bool]:
        """``(listing, kept)``: the listing kept for ``key`` (``kept`` True), or a fresh
        ``build(False)``, kept when ``size(listing)`` reaches ``min_files``. A kept
        listing older than ``refresh_after`` is rebuilt in the background with
        ``build(True)`` (which should read every file's mtime afresh)."""
        with self._lock:
            rec = self._kept.get(key)
            if rec is not None:
                self._kept.move_to_end(key)
                if not rec[2] and self._now() - rec[1] >= self.refresh_after:
                    rec[2] = True
                    self._background(lambda: self._refresh(key, build, size))
                return rec[0], True
        listing = build(False)
        self._keep(key, listing, size)
        return listing, False

    def _keep(self, key: Hashable, listing: Any, size: Callable[[Any], int]) -> None:
        with self._lock:
            if size(listing) >= self.min_files:
                self._kept[key] = [listing, self._now(), False]
                self._kept.move_to_end(key)
                while len(self._kept) > self.keep:
                    self._kept.popitem(last=False)
            else:
                self._kept.pop(key, None)

    def _refresh(self, key: Hashable, build: Callable[[bool], Any],
                 size: Callable[[Any], int]) -> None:
        try:
            listing = build(True)
        except Exception:  # noqa: BLE001 - keep the old one; the next search tries again
            with self._lock:
                rec = self._kept.get(key)
                if rec is not None:
                    rec[1], rec[2] = self._now(), False
            return
        self._keep(key, listing, size)

    def clear(self) -> None:
        with self._lock:
            self._kept.clear()


def _thread(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, name="qc-listing-refresh", daemon=True).start()
