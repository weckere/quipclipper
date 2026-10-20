"""Jellyfin "Now playing" (M15): what is playing on each Jellyfin client, and where.

quipclipper reads media from disk, so Jellyfin is used for one thing only: its live
sessions (``GET /Sessions``) and their playheads, so the web app can open the file a TV
is playing at the moment it is at (D-025). The server's API key authenticates; there is
no user sign-in. Jellyfin reports its own paths (inside its container), and the path map
turns them into media-root paths.

``urllib`` keeps this free of an HTTP client dependency (httpx is test-only).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from quipclipper_web import library

TICKS = 10_000_000  # Jellyfin ticks per second
ACTIVE_WITHIN = 600  # seconds: sessions seen this recently
# A playing client reports its position every ~10 s. Between reports the playhead is
# estimated from the last report's age, but never by more than this: a client that
# stopped reporting is not assumed to have kept playing.
MAX_EXTRAPOLATE = 30.0


class JellyfinError(Exception):
    """Jellyfin could not be reached or answered with an error."""


class JellyfinClient:
    def __init__(self, url: str, api_key: str, timeout: float = 5.0):
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def get(self, path: str, query: dict[str, Any] | None = None) -> Any:
        qs = ("?" + urllib.parse.urlencode(query)) if query else ""
        req = urllib.request.Request(self.url + path + qs, headers={
            "Authorization": f'MediaBrowser Token="{self.api_key}"',
            "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8") or "null")
        except urllib.error.HTTPError as exc:
            hint = " (check the API key)" if exc.code in (401, 403) else ""
            raise JellyfinError(f"Jellyfin answered {exc.code}{hint}") from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            reason = getattr(exc, "reason", exc)
            raise JellyfinError(f"Jellyfin is unreachable at {self.url}: {reason}") from exc

    def sessions(self) -> list[dict]:
        data = self.get("/Sessions", {"activeWithinSeconds": ACTIVE_WITHIN})
        return [s for s in data if isinstance(s, dict)] if isinstance(data, list) else []

    def item_path(self, item_id: str) -> str | None:
        """An item's path, for a session whose NowPlayingItem left it out."""
        data = self.get("/Items", {"Ids": item_id, "Fields": "Path"})
        items = (data.get("Items") or []) if isinstance(data, dict) else []
        return items[0].get("Path") if items and isinstance(items[0], dict) else None


def map_path(path: str, path_map: tuple[tuple[str, str], ...]) -> str | None:
    """Jellyfin's path for a file → the same file under quipclipper's roots.

    The longest matching prefix wins, matched on whole path components, so ``/video``
    never captures ``/videos/…``. None when no prefix matches."""
    best: tuple[str, str] | None = None
    for src, dst in path_map:
        if path == src or path.startswith(src.rstrip("/") + "/"):
            if best is None or len(src) > len(best[0]):
                best = (src, dst)
    if best is None:
        return None
    rest = path[len(best[0]):].lstrip("/")
    return str(Path(best[1]) / rest) if rest else best[1]


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.rstrip("Z")
    # Jellyfin writes 7 fractional digits; fromisoformat takes at most 6.
    if "." in text:
        head, frac = text.split(".", 1)
        text = f"{head}.{frac[:6].ljust(6, '0')}"  # Python 3.10 wants exactly 6
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _seconds(ticks: Any) -> float | None:
    return round(ticks / TICKS, 3) if isinstance(ticks, (int, float)) else None


def now_playing(
    raw: list[dict],
    path_map: tuple[tuple[str, str], ...],
    roots: list[Path],
    *,
    item_path: Callable[[str], str | None] | None = None,
    now: datetime | None = None,
) -> list[dict]:
    """The sessions that have something playing (or paused), newest report first."""
    now = now or datetime.now(timezone.utc)
    out = []
    for s in raw:
        item = s.get("NowPlayingItem")
        if not isinstance(item, dict):
            continue
        play = s.get("PlayState") or {}
        paused = bool(play.get("IsPaused"))
        reported = _seconds(play.get("PositionTicks")) or 0.0
        duration = _seconds(item.get("RunTimeTicks"))
        checked_in = _parse_time(s.get("LastPlaybackCheckIn"))
        position = reported
        if not paused and checked_in is not None:
            age = (now - checked_in).total_seconds()
            position = reported + min(max(age, 0.0), MAX_EXTRAPOLATE)
            if duration:
                position = min(position, duration)
        jf_path = item.get("Path")
        if not jf_path and item_path is not None and item.get("Id"):
            try:
                jf_path = item_path(str(item["Id"]))
            except JellyfinError:
                jf_path = None
        path, note = None, None
        if not jf_path:
            note = "Jellyfin didn't say which file this is."
        else:
            mapped = map_path(jf_path, path_map)
            if mapped is None:
                note = "Not in quipclipper's library (no path mapping for it)."
            else:
                try:
                    path = str(library.resolve_within_roots(mapped, roots))
                except library.PathNotAllowed:
                    note = "Not in quipclipper's library (outside the media roots)."
        out.append({
            "id": s.get("Id"),
            "user": s.get("UserName"),
            "device": s.get("DeviceName"),
            "client": s.get("Client"),
            "item_id": item.get("Id"),
            "type": item.get("Type"),
            "title": item.get("Name"),
            "series": item.get("SeriesName"),
            "season": item.get("ParentIndexNumber") if item.get("SeriesName") else None,
            "episode": item.get("IndexNumber") if item.get("SeriesName") else None,
            "year": item.get("ProductionYear"),
            "duration": duration,
            "position": round(position, 3),
            "reported_position": reported,
            "reported_at": checked_in.isoformat().replace("+00:00", "Z") if checked_in else None,
            "paused": paused,
            "jellyfin_path": jf_path,
            "path": path,
            "note": note,
        })
    out.sort(key=lambda r: r["reported_at"] or "", reverse=True)
    return out
