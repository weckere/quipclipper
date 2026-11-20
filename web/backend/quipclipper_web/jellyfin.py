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
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from quipclipper.subtitles import (
    _FORCED_RE, IMAGE_SUBTITLE_CODECS, SubtitleTrack, best_track,
)
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
              timeout: float | None = None, accept: str = "application/json",
              body: bytes | None = None):
        qs = ("?" + urllib.parse.urlencode(query)) if query else ""
        headers = {"Authorization": f'MediaBrowser Token="{self.api_key}"', "Accept": accept}
        if body is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.url + path + qs, data=body, method=method,
                                     headers=headers)
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

    def post(self, path: str, query: dict[str, Any] | None = None, *,
             body: bytes | None = None) -> None:
        with self._open("POST", path, query, body=body) as resp:
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

    def command(self, session_id: str, name: str, arguments: dict[str, str]) -> None:
        """A general command (``SetSubtitleStreamIndex``, …) to a client."""
        body = json.dumps({"Name": name, "Arguments": arguments}).encode()
        self.post(f"/Sessions/{urllib.parse.quote(session_id, safe='')}/Command", body=body)

    def media_sources(self, item_id: str) -> list[dict]:
        """An item's media sources, each with its ``MediaStreams`` (Jellyfin's indexes)."""
        data = self.get("/Items", {"Ids": item_id, "Fields": "MediaSources,MediaStreams"})
        items = (data.get("Items") or []) if isinstance(data, dict) else []
        item = items[0] if items and isinstance(items[0], dict) else {}
        sources = [m for m in item.get("MediaSources") or [] if isinstance(m, dict)]
        if not sources and isinstance(item.get("MediaStreams"), list):
            sources = [{"Id": item.get("Id"), "MediaStreams": item["MediaStreams"]}]
        return sources

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
                note = "Not in QuipClipper's library (no path mapping for it)."
            else:
                try:
                    path = str(library.resolve_within_roots(mapped, roots))
                except library.PathNotAllowed:
                    note = "Not in QuipClipper's library (outside the media roots)."
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


# --- Subtitles on play (M27) ---------------------------------------------------------------

SUBTITLE_COMMAND = "SetSubtitleStreamIndex"
# How long a play waits for the client to start playing the item before it gives up on
# subtitles (the play itself has already been sent), and how often it asks.
# 15 s: a client reports its position every few seconds, and a play is taken as started
# only once that position has moved (LineFinder gives the whole request 30 s).
SUBTITLE_WAIT = 15.0
SUBTITLE_POLL = 0.5
# After the command: how long to wait for the client to report the stream on (each try).
SUBTITLE_CONFIRM = 4.0
# The clock and sleep the wait uses: module attributes so tests replace them (no real sleeps).
_monotonic = time.monotonic
_sleep = time.sleep


def supported_commands(s: dict) -> list[str]:
    """The general commands a session says it takes (top level, or under Capabilities)."""
    cmds = s.get("SupportedCommands")
    if not isinstance(cmds, list):
        cmds = (s.get("Capabilities") or {}).get("SupportedCommands")
    return [c for c in cmds or [] if isinstance(c, str)]


def lang_matches(stream_lang: Any, want: str) -> bool:
    """A stream's language against the asked one, by prefix (``en`` matches ``eng``), the
    way subtitle languages are matched elsewhere."""
    have = str(stream_lang or "").casefold()
    want = want.casefold()
    return bool(have) and (have.startswith(want) or want.startswith(have))


def pick_subtitle(streams: list[dict], lang: str, *, track: int | None = None,
                  sidecar: str | None = None) -> tuple[dict | None, str]:
    """The subtitle stream to turn on and how it was chosen: ``track`` (the line's
    embedded track s:N, the N-th embedded subtitle stream), else ``sidecar`` (the line's
    subtitle file, by name), when that stream is in ``lang`` (or untagged); else the
    stream in ``lang`` search's ``best_track`` ranks first, never a forced-only one.
    (None, why) when there is none."""
    subs = sorted((m for m in streams if isinstance(m, dict) and m.get("Type") == "Subtitle"
                   and isinstance(m.get("Index"), int)), key=lambda m: m["Index"])

    def fits(m: dict) -> bool:
        return not m.get("Language") or m.get("Language") == "und" or lang_matches(
            m.get("Language"), lang)

    if track is not None:
        embedded = [m for m in subs if not m.get("IsExternal")]
        if 0 <= track < len(embedded) and fits(embedded[track]):
            return embedded[track], "track"
    if sidecar:
        name = sidecar.casefold()
        for m in subs:
            path = str(m.get("Path") or "")
            if m.get("IsExternal") and path.replace("\\", "/").rsplit("/", 1)[-1].casefold() == name \
                    and fits(m):
                return m, "sidecar"
    # The language's best stream by search's own ranking (best_track: text above image,
    # full dialogue above SDH above commentary), so play turns on what search would read.
    # Forced-only streams never count.
    ranked = []
    for pos, m in enumerate(subs):
        title = " ".join(str(m.get(k) or "") for k in ("Title", "DisplayTitle")).strip()
        if not lang_matches(m.get("Language"), lang) or m.get("IsForced") \
                or _FORCED_RE.search(title):
            continue
        codec = str(m.get("Codec") or "subrip").lower()
        if m.get("IsTextSubtitleStream") is False and codec not in IMAGE_SUBTITLE_CODECS:
            codec = "dvd_subtitle"  # an image stream Jellyfin names some other way
        ranked.append(SubtitleTrack(pos, codec, str(m.get("Language")), title or None,
                                    False, bool(m.get("IsHearingImpaired"))))
    if ranked:
        return subs[best_track(ranked, (lang,)).index], "language"
    return None, f"This file has no {lang} subtitles that aren't forced-only."


def _session(client: JellyfinClient, session_id: str) -> dict | None:
    """One session as Jellyfin lists it now; None when it's gone or Jellyfin didn't answer."""
    try:
        raw = client.sessions(CONTROLLABLE_WITHIN)
    except JellyfinError:
        return None
    return next((x for x in raw if x.get("Id") == session_id), None)


def _poll(probe: Callable[[], Any], within: float) -> Any:
    """``probe()`` every ``SUBTITLE_POLL`` seconds until it returns something, for at most
    ``within`` seconds; None when it never does."""
    deadline = _monotonic() + within
    while True:
        found = probe()
        if found is not None:
            return found
        if _monotonic() + SUBTITLE_POLL > deadline:
            return None
        _sleep(SUBTITLE_POLL)


def wait_playing(client: JellyfinClient, session_id: str, item_id: str,
                 within: float = SUBTITLE_WAIT) -> dict | None:
    """The session once it reports ``item_id`` as its NowPlayingItem (a seek: the client
    was already playing it); None when it doesn't within ``within`` seconds."""
    def probe():
        s = _session(client, session_id)
        return s if s and (s.get("NowPlayingItem") or {}).get("Id") == item_id else None
    return _poll(probe, within)


def wait_started(client: JellyfinClient, session_id: str, item_id: str,
                 within: float = SUBTITLE_WAIT) -> dict | None:
    """The session once this play of ``item_id`` has really started: the item is its
    NowPlayingItem, not paused, and its PositionTicks went up between two looks. A client
    reports the item at once, before its player has the file's tracks (a subtitle command
    then fails), and may still show it from an earlier playback; neither moves the
    position, so both are passed over. None when it doesn't start within ``within``."""
    last: list[float | None] = [None]

    def probe():
        s = _session(client, session_id)
        play = (s or {}).get("PlayState") or {}
        pos = play.get("PositionTicks")
        if not s or (s.get("NowPlayingItem") or {}).get("Id") != item_id \
                or play.get("IsPaused") or not isinstance(pos, (int, float)):
            last[0] = None
            return None
        moved = last[0] is not None and pos > last[0]
        last[0] = pos
        return s if moved else None
    return _poll(probe, within)


def wait_subtitle(client: JellyfinClient, session_id: str, index: int,
                  within: float = SUBTITLE_CONFIRM) -> tuple[bool, Any]:
    """Whether the session reports subtitle stream ``index`` on within ``within`` seconds,
    and the index it reported last (None when it reports none)."""
    seen: list[Any] = [None]

    def probe():
        s = _session(client, session_id)
        seen[0] = ((s or {}).get("PlayState") or {}).get("SubtitleStreamIndex")
        return True if seen[0] == index else None
    return bool(_poll(probe, within)), seen[0]


def _is_full(stream: dict | None, lang: str) -> bool:
    """A subtitle stream in ``lang`` that isn't forced-only."""
    if not stream or stream.get("Type") != "Subtitle":
        return False
    title = " ".join(str(stream.get(k) or "") for k in ("Title", "DisplayTitle"))
    return lang_matches(stream.get("Language"), lang) and not stream.get("IsForced") \
        and not _FORCED_RE.search(title)


def turn_on_subtitles(client: JellyfinClient, target: dict, item_id: str, lang: str, *,
                      command: str = "play", track: int | None = None,
                      sidecar: str | None = None) -> dict:
    """After a play or seek (``command``): wait until the client is really playing
    ``item_id`` (``wait_started`` after a play, ``wait_playing`` after a seek), then turn
    on its ``lang`` subtitles (``pick_subtitle``) with ``SetSubtitleStreamIndex``, unless
    a full ``lang`` stream is already on. ``set`` means confirmed: the client reported the
    stream on afterwards (the command is sent once more when it didn't). Never raises;
    ``note`` says why when it wasn't set."""
    out: dict[str, Any] = {"set": False, "index": None, "language": lang, "title": None,
                           "match": None, "note": None}
    if SUBTITLE_COMMAND not in supported_commands(target):
        out["note"] = f"{target.get('Client')} doesn't take {SUBTITLE_COMMAND}."
        return out
    sid = str(target["Id"])
    try:
        sources = client.media_sources(item_id)
    except JellyfinError as exc:
        out["note"] = f"Couldn't read the file's subtitle streams: {exc}"
        return out
    wait = wait_started if command == "play" else wait_playing
    playing = wait(client, sid, item_id)
    if playing is None:
        out["note"] = (f"The client didn't start playing the file within "
                       f"{SUBTITLE_WAIT:g} s, so subtitles weren't set.")
        return out
    state = playing.get("PlayState") or {}
    source = next((m for m in sources if m.get("Id") == state.get("MediaSourceId")),
                  None) or (sources[0] if sources else {})
    streams = source.get("MediaStreams") or []
    stream, how = pick_subtitle(streams, lang, track=track, sidecar=sidecar)
    if stream is None:
        out["note"] = how
        return out

    def describe(m: dict, match: str) -> None:
        out.update(index=m["Index"], language=m.get("Language") or lang,
                   title=m.get("DisplayTitle") or m.get("Title"), match=match)

    current = state.get("SubtitleStreamIndex")
    on = next((m for m in streams if m.get("Type") == "Subtitle"
               and m.get("Index") == current), None)
    if _is_full(on, lang):  # already showing them: leave the client's choice alone
        describe(on, how if on is stream else "language")
        out["set"] = True
        out["note"] = f"{lang} subtitles were already on (stream {current}); left as they were."
        return out
    describe(stream, how)
    reported = None
    for _ in range(2):  # the command, and once more when the client didn't take it
        try:
            client.command(sid, SUBTITLE_COMMAND, {"Index": str(stream["Index"])})
        except JellyfinError as exc:
            out["note"] = f"Jellyfin refused {SUBTITLE_COMMAND}: {exc}"
            return out
        ok, reported = wait_subtitle(client, sid, stream["Index"])
        if ok:
            out["set"] = True
            break
    if not out["set"]:
        shown = "none" if reported in (None, -1) else f"stream {reported}"
        out["note"] = (f"Sent {SUBTITLE_COMMAND} twice, but the client still reports "
                       f"{shown} on, not stream {stream['Index']}.")
    elif how == "language" and track is not None:
        out["note"] = (f"The line's track (s:{track}) isn't a {lang} stream Jellyfin "
                       "lists; turned on the best one in that language.")
    return out
