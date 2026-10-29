"""Jellyfin "Now playing" (M15): what is playing on each Jellyfin client, and where.
Since M22, "Play on…" too: start a library file on a client at a line.

quipclipper reads media from disk, so Jellyfin is used for what only it knows: its live
sessions (``GET /Sessions``) and their playheads, so the web app can open the file a TV
is playing at the moment it is at (D-025), and since M18 its titles and artwork (see
``jellyfin_index``) and a library scan after a clip lands. The server's API key
authenticates; there is no user sign-in. Jellyfin reports its own paths (inside its
container), and the path map turns them into media-root paths.

``urllib`` keeps this free of an HTTP client dependency (httpx is test-only).
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from quipclipper_web import library

_HEX32 = re.compile(r"[0-9a-f]{32}\Z")  # a Jellyfin item id or image tag
TICKS = 10_000_000  # Jellyfin ticks per second
ACTIVE_WITHIN = 600  # seconds: sessions seen this recently
# Play on… (M22) also offers an idle client that can be controlled when it was last active
# this long ago: a TV app parked on its home screen keeps its socket open for hours, and
# Jellyfin calls a session controllable only while that socket is open.
CONTROLLABLE_WITHIN = 86400
# A playing client reports its position every ~10 s. Between reports the playhead is
# estimated from the last report's age, but never by more than this: a client that
# stopped reporting is not assumed to have kept playing.
MAX_EXTRAPOLATE = 30.0


# The path index (M18): one listing of every playable item, read in pages.
INDEX_TYPES = "Movie,Episode,Audio,AudioBook"
INDEX_FIELDS = ("Path,ProviderIds,ParentIndexNumber,IndexNumber,SeriesName,ProductionYear,"
                "ImageTags")
INDEX_TIMEOUT = 120.0  # a page of thousands of items takes Jellyfin a few seconds
MAX_IMAGE_BYTES = 4 * 1024 * 1024  # a scaled poster is ~50 KB; anything past this is refused


class JellyfinError(Exception):
    """Jellyfin could not be reached or answered with an error."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status  # Jellyfin's HTTP status, when it answered


class JellyfinClient:
    def __init__(self, url: str, api_key: str, timeout: float = 5.0):
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def _open(self, method: str, path: str, query: dict[str, Any] | None = None, *,
              timeout: float | None = None, accept: str = "application/json"):
        qs = ("?" + urllib.parse.urlencode(query)) if query else ""
        req = urllib.request.Request(self.url + path + qs, method=method, headers={
            "Authorization": f'MediaBrowser Token="{self.api_key}"',
            "Accept": accept,
        })
        try:
            return urllib.request.urlopen(req, timeout=timeout or self.timeout)
        except urllib.error.HTTPError as exc:
            hint = " (check the API key)" if exc.code in (401, 403) else ""
            raise JellyfinError(f"Jellyfin answered {exc.code}{hint}", status=exc.code) from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            reason = getattr(exc, "reason", exc)
            raise JellyfinError(f"Jellyfin is unreachable at {self.url}: {reason}") from exc

    def get(self, path: str, query: dict[str, Any] | None = None, *,
            timeout: float | None = None) -> Any:
        try:
            with self._open("GET", path, query, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8") or "null")
        except (OSError, ValueError) as exc:  # a reply cut off or not JSON
            raise JellyfinError(f"Jellyfin sent a bad reply: {exc}") from exc

    def post(self, path: str, query: dict[str, Any] | None = None) -> None:
        with self._open("POST", path, query) as resp:
            resp.read()

    def image(self, item_id: str, image_type: str, max_width: int,
              tag: str | None = None) -> tuple[bytes, str]:
        """One of an item's images, scaled to at most ``max_width``: (bytes, content type)."""
        query: dict[str, Any] = {"maxWidth": max_width, "quality": 85}
        if tag:
            query["tag"] = tag
        try:
            with self._open("GET", f"/Items/{item_id}/Images/{image_type}", query,
                            accept="image/*") as resp:
                ctype = resp.headers.get_content_type()
                return resp.read(MAX_IMAGE_BYTES + 1), ctype
        except OSError as exc:
            raise JellyfinError(f"Jellyfin sent a bad reply: {exc}") from exc

    def items_page(self, start: int, limit: int) -> list[dict]:
        """One page of every movie, episode, track and audiobook, with what the index needs."""
        data = self.get("/Items", {
            "Recursive": "true", "IncludeItemTypes": INDEX_TYPES, "Fields": INDEX_FIELDS,
            # Without this, movies in a collection come back as the collection (a BoxSet),
            # so they'd be missing from the index.
            "CollapseBoxSetItems": "false",
            "StartIndex": start, "Limit": limit, "EnableUserData": "false",
            "EnableTotalRecordCount": "false", "ImageTypeLimit": 1,
            "EnableImageTypes": "Primary,Thumb",
        }, timeout=INDEX_TIMEOUT)
        items = (data.get("Items") or []) if isinstance(data, dict) else []
        return [i for i in items if isinstance(i, dict)]

    def sample_paths(self, library_id: str, limit: int, start: int = 0) -> list[str]:
        """The paths of a page of files in one library (any kind Jellyfin plays, home videos
        too), from ``start``: what the path map is derived from when none is set."""
        data = self.get("/Items", {
            "ParentId": library_id, "Recursive": "true", "Fields": "Path",
            "IncludeItemTypes": INDEX_TYPES + ",Video", "StartIndex": start, "Limit": limit,
            "EnableUserData": "false", "EnableTotalRecordCount": "false",
            "EnableImages": "false",
        })
        items = (data.get("Items") or []) if isinstance(data, dict) else []
        return [i["Path"] for i in items if isinstance(i, dict) and isinstance(i.get("Path"), str)]

    def virtual_folders(self) -> list[dict]:
        """Jellyfin's libraries: name, ``ItemId`` and the folders (``Locations``) in each."""
        data = self.get("/Library/VirtualFolders")
        return [f for f in data if isinstance(f, dict)] if isinstance(data, list) else []

    def refresh_library(self, library_id: str) -> None:
        """Scan one library for new and changed files (Jellyfin's "Scan library")."""
        self.post(f"/Items/{library_id}/Refresh", {
            "Recursive": "true", "MetadataRefreshMode": "Default",
            "ImageRefreshMode": "Default", "ReplaceAllMetadata": "false",
            "ReplaceAllImages": "false",
        })

    def sessions(self, active_within: int = ACTIVE_WITHIN) -> list[dict]:
        data = self.get("/Sessions", {"activeWithinSeconds": active_within})
        return [s for s in data if isinstance(s, dict)] if isinstance(data, list) else []

    def play_now(self, session_id: str, item_id: str, at: float) -> None:
        """Start an item on a client at ``at`` seconds (Jellyfin's PlayNow)."""
        self.post(f"/Sessions/{urllib.parse.quote(session_id, safe='')}/Playing", {
            "playCommand": "PlayNow", "itemIds": item_id,
            "startPositionTicks": int(round(at * TICKS)),
        })

    def playstate(self, session_id: str, command: str, at: float | None = None) -> None:
        """Seek, Unpause, … on a client that is already playing."""
        query = {"seekPositionTicks": int(round(at * TICKS))} if at is not None else None
        self.post(f"/Sessions/{urllib.parse.quote(session_id, safe='')}/Playing/{command}",
                  query)

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


def session_art(item: dict) -> dict | None:
    """The poster for a playing item: the series' for an episode, else its own."""
    def art(item_id, tag):
        ok = all(isinstance(v, str) and _HEX32.match(v) for v in (item_id, tag))
        return {"id": item_id, "type": "Primary", "tag": tag} if ok else None
    if item.get("Type") == "Episode":
        found = art(item.get("SeriesId"), item.get("SeriesPrimaryImageTag"))
        if found:
            return found
    return (art(item.get("Id"), (item.get("ImageTags") or {}).get("Primary"))
            or art(item.get("AlbumId"), item.get("AlbumPrimaryImageTag")))


def media_type(item_type: Any) -> str:
    """The media type a client must play for an item: Audio for tracks, else Video."""
    return "Audio" if item_type in ("Audio", "AudioBook") else "Video"


# Clients that say they take remote control but don't start what they're told to play,
# by Jellyfin's ``Client`` name (checked 2026-10-26). Nothing in what they
# report tells them apart, so they're named here.
IGNORES_PLAY = {
    "Wholphin": "Wholphin takes the command but doesn't play it (seen with 1.0.9).",
}


def control(s: dict) -> tuple[bool, str | None]:
    """Whether a client takes "play this" from quipclipper (M22), and if not, why.

    A client says so itself: ``SupportsRemoteControl`` (its socket to Jellyfin is open and
    it obeys commands) and the media it can be told to play. Infuse reports neither;
    Wholphin reports both and ignores the command (``IGNORES_PLAY``)."""
    if not s.get("SupportsRemoteControl"):
        return False, "This app doesn't take remote control from Jellyfin."
    if s.get("Client") in IGNORES_PLAY:
        return False, IGNORES_PLAY[s["Client"]]
    if not {"Video", "Audio"} & set(s.get("PlayableMediaTypes") or []):
        return False, "This app can't be told to play anything."
    return True, None


def _session_fields(s: dict) -> dict:
    can_play, why = control(s)
    return {
        "id": s.get("Id"),
        "user": s.get("UserName"),
        "device": s.get("DeviceName"),
        "client": s.get("Client"),
        "can_play": can_play,
        "control_note": why,
        "media_types": [m for m in s.get("PlayableMediaTypes") or [] if isinstance(m, str)],
        "last_active": s.get("LastActivityDate"),
    }


def now_playing(
    raw: list[dict],
    path_map: tuple[tuple[str, str], ...],
    roots: list[Path],
    *,
    item_path: Callable[[str], str | None] | None = None,
    now: datetime | None = None,
    include_idle: bool = False,
) -> list[dict]:
    """The sessions that have something playing (or paused), newest report first.

    ``include_idle`` (Play on…, M22) adds the sessions with nothing playing, after them,
    most recently active first: each one seen within ``ACTIVE_WITHIN``, and any that can
    be controlled (``raw`` may then reach further back, ``CONTROLLABLE_WITHIN``)."""
    now = now or datetime.now(timezone.utc)
    out, idle = [], []
    for s in raw:
        if include_idle:  # raw reaches back further: keep only what the default would, or
            seen = _parse_time(s.get("LastActivityDate"))  # what can be controlled
            recent = seen is not None and (now - seen).total_seconds() <= ACTIVE_WITHIN
            if not (recent or control(s)[0]):
                continue
        item = s.get("NowPlayingItem")
        if not isinstance(item, dict):
            # No user: an API key's session (another app asking Jellyfin), not a player.
            if include_idle and s.get("UserName"):
                idle.append({**_session_fields(s), "idle": True})
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
            **_session_fields(s),
            "idle": False,
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
            "art": session_art(item),
        })
    out.sort(key=lambda r: r["reported_at"] or "", reverse=True)
    idle.sort(key=lambda r: r["last_active"] or "", reverse=True)
    return out + idle
