"""FastAPI application factory.

Routes:
- Phase 0: health, config
- Phase 1: library browse, items, subtitles (WebVTT), media streaming
- Phase 2: dialogue search
- Phase 3: clip jobs (POST /api/clip, GET /api/jobs)
- Phase 4: bookmarks (GET/POST/DELETE /api/bookmarks)
- Phase 5: clips library (GET /api/clips)
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import hashlib
import hmac
import ipaddress
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from quipclipper.clip import (
    ClipRange,
    compute_range,
    cut_clip,
    group_category,
    group_channels,
    output_extension,
    probe_audio_streams,
    probe_channel_layout,
    split_audio_channels,
    vaapi_h264_available,
)
from quipclipper import transcribe as stt
from quipclipper.models import Match, format_timestamp
from quipclipper.naming import (
    DEFAULT_CLIP_TEMPLATE,
    clean_title,
    clip_template_context,
    render_clip_template,
    resolve_template,
    slugify,
)
from quipclipper.mkv import (
    cut_with_mkvmerge,
    is_matroska,
    mkvmerge_available,
)
from quipclipper.search import search as engine_search
from quipclipper.subtitles import (
    AUDIO_EXTS,
    SUBTITLE_EXTS,
    VIDEO_EXTS,
    Cancelled,
    at_idle_priority,
    find_sidecar,
    is_text_subtitle,
    list_embedded_tracks,
    load_subtitles,
)
from quipclipper_web import (
    __version__, clipmeta, deeplinks, epub_items, file_index, jellyfin, jellyfin_index, lib_index,
    library, lslink, lsset, media, pairing, peaks, youtube_items,
)
from quipclipper_web import index_changes as index_changes_mod
from quipclipper_web.bookmarks import BookmarkStore
from quipclipper_web.config import Settings
from quipclipper_web.jobs import JobRegistry
from quipclipper_web.sub_cache import SubtitleCache

def _is_media_file(p: Path, transcribed=None) -> bool:
    """A real, indexable media file: a video, or an audio-only file (podcast/
    audiobook) that has a sidecar transcript, or (``transcribed``, a predicate) a
    machine transcript (M21).

    Excludes hidden/dotfiles — notably macOS AppleDouble ``._*`` sidecars, which
    share a media file's name and extension on non-Mac filesystems (NAS/SMB) but
    are tiny resource-fork metadata blobs that ffprobe rejects. The library
    browser already skips dotfiles; the recursive folder scans must too, or these
    junk files inflate the index count and can never be indexed.
    """
    if not p.is_file() or p.name.startswith("."):
        return False
    ext = p.suffix.lower()
    if ext in VIDEO_EXTS:
        return True
    # Audio is only indexable with a transcript (nothing to extract embedded subs
    # from); the cheap sidecar glob keeps un-transcribed audio out of the scan.
    return ext in AUDIO_EXTS and (find_sidecar(p) is not None
                                  or bool(transcribed and transcribed(p)))


def _is_bare_audio(p: Path) -> bool:
    """An audio file with no sidecar transcript (what a folder transcription can give
    one). Hidden files are skipped as in ``_is_media_file``."""
    return (p.is_file() and not p.name.startswith(".") and p.suffix.lower() in AUDIO_EXTS
            and find_sidecar(p) is None)


# The clips listing's contract with LeadStinger (docs/API.md); bump on a breaking change.
CLIPS_CONTRACT = "quipclipper.clips/1"

# Upper bound on how many media files a single recursive folder operation
# (indexing, transcription) will touch. The nginx-side timeout is 600s,
# so a huge tree can't be swept in one request — cap it and tell the client.
INDEX_CAP = 500
# A folder or library search: the longest query (scoring is quadratic in its words, and
# a whole-library search runs it over every indexed line: 300 characters is ~0.9 s per
# 20k lines) and the most matches returned (5 per file over ~30k files otherwise).
QUERY_MAX = 300
FOLDER_MATCHES_MAX = 500


def _walk_dirs(top: Path) -> Iterator[tuple[Path, list[os.DirEntry]]]:
    """``(folder, its files)`` for ``top`` and every folder below it. The
    entries' cached file types spare a stat per file (a library holds ~70k files on a
    FUSE mount); symlinked folders aren't followed, as with ``Path.rglob``, and hidden
    ones (``.snapshots``, ``.Trash-1000``) are skipped, as the library browser hides
    them."""
    stack = [top]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                entries = list(it)
        except OSError:
            continue
        files = []
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    if not e.name.startswith("."):
                        stack.append(Path(e.path))
                elif e.is_file():
                    files.append(e)
            except OSError:
                continue
        yield d, files


# Output naming (B9) lives in the engine so the CLI names clips the same way.
_clean_title = clean_title
_slugify = slugify
_clip_template_context = clip_template_context
_render_clip_template = render_clip_template


# The VAAPI render node used for hardware H.264 encoding. Override with
# QC_VAAPI_DEVICE for a non-default node (e.g. a second GPU's renderD129, or the
# device the NixOS module / docker-compose passes in).
_VAAPI_DEVICE = os.environ.get("QC_VAAPI_DEVICE") or "/dev/dri/renderD128"

# How long an /api/media/hls playlist request waits for ffmpeg to produce the
# first segment before giving up with a 504. A stream-copied source manages it
# almost immediately, but a software H.264 re-encode (venc=1, no VAAPI) of a
# long high-resolution source has to encode a whole 6s segment first and can
# take far longer than the 15s this used to allow — timing out on exactly the
# files that need the re-encode most. The proxies in front of this
# (nix/nixos-module.nix, web/nginx/nginx.conf) allow longer still, so a caller
# sees this clean 504 rather than a gateway timeout.
_HLS_START_TIMEOUT = 120.0
_HLS_START_POLL = 0.1


def _vaapi_h264_available() -> bool:
    """True if the host iGPU can hardware-encode H.264 via VAAPI / Quick Sync.
    Probed once (in the engine) with a tiny test encode; both the browser-preview
    transcode and the clip re-encode fall back to software (libx264) when False."""
    return vaapi_h264_available(_VAAPI_DEVICE)


def _live_pan_filter(p: Path, audio_index: int, chan: str) -> str | None:
    """A `pan` filter that downmixes one channel group of an audio stream to
    stereo/mono for live preview (B17). ``chan`` is a group label
    (front/center/lfe/side/back) or category (surround). None if unresolved."""
    channels = probe_channel_layout(p, audio_index)
    if not channels:
        return None
    for label, chans in group_channels(channels):
        if label == chan or group_category(label) == chan:
            if len(chans) == 2:
                return f"pan=stereo|c0={chans[0]}|c1={chans[1]}"
            return f"pan=mono|c0={chans[0]}"
    return None


# Containers/extensions a browser can usually play in a <video>/<audio> element.
# Used to hint the UI and to serve source/clip files with a real Content-Type;
# the engine still works on anything ffmpeg can read. Audio types matter for
# in-browser playback — iOS Safari refuses to play application/octet-stream.
_BROWSER_MIME = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".mov": "video/quicktime",
    ".ts": "video/mp2t",
    ".avi": "video/x-msvideo",
    # Audio
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".m4b": "audio/mp4",
    ".aac": "audio/aac",
    ".mka": "audio/x-matroska",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/ogg",
}


class BookmarkCreate(BaseModel):
    """JSON body for POST /api/bookmarks."""

    path: str
    label: str = ""
    start: float
    end: float
    before: float = Field(0.0, ge=0, le=60)
    after: float = Field(0.0, ge=0, le=60)
    sub_track: int | None = None
    audio_track: int | None = None
    channel_subset: str | None = None
    cue: str = ""  # matched dialogue, for the {cue} token when exporting the bookmark


class BookmarkUpdate(BaseModel):
    """JSON body for PATCH /api/bookmarks/{id} — all fields optional."""

    label: str | None = None
    before: float | None = Field(None, ge=0, le=60)
    after: float | None = Field(None, ge=0, le=60)


class LeadStingerItem(BaseModel):
    """One line for a LeadStinger set: a search hit, a bookmark or a clips-library file."""

    path: str
    # No start: the whole file (a clips-library entry), cut with no handles.
    start: float | None = Field(None, ge=0)
    end: float | None = Field(None, ge=0)
    title: str | None = Field(None, max_length=200)
    notes: str | None = Field(None, max_length=1000)
    track: int | None = Field(None, ge=0)
    # Padding cut either side of the line, as for clipping (D-026): it becomes the
    # handle audio around the tile's trim, which sits on the line. 0 = no handles.
    before: float = Field(0.0, ge=0, le=60)
    after: float = Field(0.0, ge=0, le=60)


class LeadStingerSetRequest(BaseModel):
    """JSON body for POST /api/leadstinger/set (M14, D-024, D-026)."""

    # None: the source's title for one clip, the shared title for a batch from one
    # show or movie, else "quipclipper <date>". LeadStinger numbers a taken name.
    name: str | None = Field(None, min_length=1, max_length=80)
    items: list[LeadStingerItem] = Field(..., min_length=1, max_length=200)


class LeadStingerQuipRequest(BaseModel):
    """JSON body for POST /api/leadstinger/quip (M14, LeadStinger's D-076)."""

    path: str
    # The line's span in the source; the clip gets `before`/`after` seconds either side.
    start: float = Field(..., ge=0)
    end: float = Field(..., gt=0)
    # The tile's title (the line); trimmed to one line of 200 characters in the link.
    title: str | None = Field(None, max_length=1000)
    # Padding cut either side of the line, kept as handle audio around the tile's trim
    # (D-026) so the cut can be moved in LeadStinger. The web UI sends its Before/After.
    before: float = Field(2.0, ge=0, le=60)
    after: float = Field(2.0, ge=0, le=60)
    # The LeadStinger set to put the tile in (created there if missing); else its current set.
    set: str | None = Field(None, max_length=100)


class ClipRequest(BaseModel):
    """JSON body for POST /api/clip — full CLI option parity."""

    path: str
    # Time range — either explicit start/end OR search-based (query + match_index).
    start: float | None = None
    end: float | None = None
    query: str | None = None
    # A negative index would bypass the >= len(matches) bounds check and index
    # from the end of the list (or IndexError -> 500), so pin it non-negative.
    match_index: int = Field(0, ge=0)
    # The search a match_index counts in: the same tuning as GET /api/search, so the
    # index a search returned points at the same line here.
    min_score: float = Field(60.0, ge=0, le=100)
    max_span: int = Field(3, ge=1, le=10)
    track: int | None = Field(None, ge=0)
    # Matched dialogue for the {cue} naming token when clipping by start/end
    # (the frontend sends the selected cue's text; search-based clips derive it).
    cue_text: str | None = None
    # Clip options
    kind: str = Field("video", pattern="^(audio|video|gif)$")
    lossless: bool = True
    # Full-mix lossless audio re-encode (audio kind, no split): keep all channels
    # (e.g. 5.1) in one WAV/FLAC file. None = passthrough/MP3 per `lossless`.
    audio_format: str | None = Field(None, pattern="^(wav|flac)$")
    before: float = Field(2.0, ge=0, le=60)
    after: float = Field(2.0, ge=0, le=60)
    # Stream indices must be non-negative: a negative index selects the last
    # stream (codecs[-1]) or produces a bad ffmpeg a:N specifier.
    audio_tracks: list[Annotated[int, Field(ge=0)]] | None = None
    # Backend
    backend: str = Field("auto", pattern="^(auto|ffmpeg|mkvmerge)$")
    chapters: bool = True
    remux_first: bool = False
    embed_subs: bool = True
    # Channel split
    split_channels: bool = False
    split_format: str = Field("wav", pattern="^(wav|flac|original)$")
    include_lfe: bool = True
    # Which channel groups to export when splitting (subset of
    # center|front|surround|lfe). None = all groups (honours include_lfe).
    split_groups: list[str] | None = None
    # Output naming: a path template (see _render_clip_template). "/" makes
    # subfolders; absent tokens are dropped. None/blank = DEFAULT_CLIP_TEMPLATE.
    template: str | None = None
    # Subtitle track (s:N) to mark as the default in the saved video clip (B17c);
    # all subtitle streams are still kept. None = leave source defaults.
    default_sub_track: int | None = Field(None, ge=0)


# A whisper model's name as a request gives it; only names the server was configured
# with are run (WhisperConfig.model_path), never a path.
STT_MODEL_NAME = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"


class TranscribeRequest(BaseModel):
    """JSON body for POST /api/items/transcribe (M21): what to transcribe, and with what."""

    path: str
    # range: start..end; gaps: between subtitle lines; file: all of it. None: range when
    # start is given, else gaps.
    mode: str | None = Field(None, pattern="^(range|gaps|file)$")
    start: float | None = Field(None, ge=0, le=stt.MAX_SECONDS, allow_inf_nan=False)
    end: float | None = Field(None, gt=0, le=stt.MAX_SECONDS, allow_inf_nan=False)
    # A model this server has (QC_WHISPER_MODELS names it; small.en by default).
    model: str | None = Field(None, pattern=STT_MODEL_NAME)
    audio: int = Field(0, ge=0)
    redo: bool = False


class FolderTranscribeRequest(BaseModel):
    """JSON body for POST /api/search/folder/transcribe (M21, phase 5)."""

    path: str
    # nosubs: whole files with no subtitles (audio without a transcript too); gaps: the
    # gaps between every subtitled file's lines.
    mode: str = Field("nosubs", pattern="^(nosubs|gaps)$")
    model: str | None = Field(None, pattern=STT_MODEL_NAME)


class PairStart(BaseModel):
    """Body of POST /api/pair (quipclipper.pair/1): who is asking. No token."""

    name: str = Field(..., min_length=1, max_length=pairing.NAME_MAX)
    host: str = Field("", max_length=pairing.HOST_MAX)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return pairing.check_label(v, "name", 1, pairing.NAME_MAX)

    @field_validator("host")
    @classmethod
    def _host(cls, v: str) -> str:
        return pairing.check_label(v, "host", 0, pairing.HOST_MAX)


class YouTubeAdd(BaseModel):
    """Body of POST /api/youtube. Module-level (like ClipRequest) so FastAPI can
    resolve the string annotation under ``from __future__ import annotations``."""

    url: str


# CSRF guard: state-changing requests must carry this header. A cross-site
# "simple request" (form post / no-cors fetch) can't set a custom header, and
# adding one forces a CORS preflight this API never grants — so a malicious page
# can't ride the browser's cached basic-auth credentials to POST here. The
# frontend sends it on every request (see apiFetch in app.js).
_CSRF_HEADER = "x-quipclipper"
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# Shared-secret header the front proxy injects (see Settings.proxy_secret).
_PROXY_SECRET_HEADER = "x-quip-proxy-secret"
# Programmatic clients (agents/scripts) present an API token here, as
# `Authorization: Bearer <token>`, or as the HTTP Basic password.
_API_KEY_HEADER = "x-api-key"
# The only routes a hook token (Settings.hook_tokens) opens: queue re-indexes and read
# how they went. Anything else it is presented to answers 403.
_HOOK_ROUTES = frozenset({"/api/index/file"})
# The login the password gate gives a hook token (web/nginx/40-quip-auth.sh, HOOKS.md).
_HOOK_USER = "hook"
_HOOK_ONLY = {"detail": "This token may only queue re-indexes (/api/index/file)."}


def _basic_user(request: Request) -> str | None:
    """The user name in an HTTP Basic ``Authorization`` header, if there is one."""
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "basic" or not value:
        return None
    try:
        return base64.b64decode(value).decode("utf-8", "replace").partition(":")[0]
    except (binascii.Error, ValueError):
        return None


def _token_ok(candidate: str, tokens: frozenset[str]) -> bool:
    """Constant-time check that `candidate` is one of the configured tokens."""
    try:
        # any() short-circuits but each compare_digest is itself constant-time, so
        # a per-token timing signal can't reveal a token's contents.
        return any(hmac.compare_digest(candidate, t) for t in tokens)
    except TypeError:
        # compare_digest rejects non-ASCII str (headers decode as latin-1); such
        # a value can never match a configured token — mismatch, not a 500.
        return False


def _api_token_status(request: Request, tokens: frozenset[str]) -> str | None:
    """Classify a request's API-token attempt.

    Returns "ok" for a valid token (X-API-Key, Bearer, or the Basic password),
    "bad" when an *explicit* token header (X-API-Key / Bearer) is present but
    wrong (→ 401), or None when no token was offered — a plain Basic user
    password that isn't a token counts as None, so real browser users fall
    through to the normal CSRF rules instead of being rejected.
    """
    key = request.headers.get(_API_KEY_HEADER)
    if key is not None:
        return "ok" if _token_ok(key, tokens) else "bad"
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    scheme = scheme.lower()
    if scheme == "bearer" and value:
        return "ok" if _token_ok(value, tokens) else "bad"
    if scheme == "basic" and value:
        try:
            _, _, password = base64.b64decode(value).decode("utf-8", "replace").partition(":")
        except (binascii.Error, ValueError):
            return None
        if password and _token_ok(password, tokens):
            return "ok"
    return None


def _basic_password(request: Request) -> str | None:
    """The password in an HTTP Basic ``Authorization`` header, if there is one."""
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "basic" or not value:
        return None
    try:
        return base64.b64decode(value).decode("utf-8", "replace").partition(":")[2] or None
    except (binascii.Error, ValueError):
        return None


def _explicit_client_tokens(request: Request) -> list[str]:
    """Every client token (``qcc_…``) the request names outright: in any ``X-API-Key``
    header or any ``Authorization: Bearer``. The nginx password gate lets a request that
    carries one through only once this backend has checked it (``/api/clients/check``), so
    this must see at least everything that rule sees."""
    found = []
    for key in request.headers.getlist(_API_KEY_HEADER):
        for part in key.split(","):  # a proxy may join repeated headers with ", "
            if part.strip().startswith(pairing.TOKEN_PREFIX):
                found.append(part.strip())
    for auth in request.headers.getlist("authorization"):
        scheme, _, value = auth.strip().partition(" ")
        if scheme.lower() == "bearer" and value.strip().startswith(pairing.TOKEN_PREFIX):
            found.append(value.strip())
    return found


def _client_token_status(request: Request, clients: pairing.ClientStore):
    """("ok", client) for a paired app's token; ("bad", None) when a ``qcc_`` token is named
    outright (X-API-Key / Bearer) and isn't a current one, so a revoked app learns it was
    revoked (401); (None, None) when no client token is offered. A ``qcc_`` Basic password
    that isn't a client's is no answer either way: it may be a person's password."""
    explicit = _explicit_client_tokens(request)
    if explicit:
        found = [clients.authenticate(t) for t in explicit]
        if any(c is None for c in found) or len({c["id"] for c in found}) != 1:
            return "bad", None
        return "ok", found[0]
    password = _basic_password(request)
    if pairing.is_client_token(password):
        client = clients.authenticate(password)
        if client is not None:
            return "ok", client
    return None, None


def _client_address(request: Request) -> str:
    """Where a request came from: the address the bundled nginx reports (``X-Real-IP``,
    which it always sets), else the connection's peer."""
    real = request.headers.get("x-real-ip", "").strip()
    try:
        return str(ipaddress.ip_address(real))
    except ValueError:
        return request.client.host if request.client else "unknown"


_PAIR_ROUTE = re.compile(r"^/api/pair(?:/|$)")
_CLIENT_REVOKED = {"detail": "This client token isn't valid: the app was disconnected "
                             "(revoked) here, or never paired. Pair it again."}
_PERSON_ONLY = ("A paired app can't approve, deny or list pairing requests, or list or revoke "
                "apps: do it in QuipClipper's web UI, or with its own API token.")


# Images the Jellyfin proxy passes on: raster formats only, so a crafted image (an SVG
# with script) can never run as this site's page.
_IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/webp", "image/gif"})
_IMAGE_MAX_WIDTH = 800  # px: the widest artwork the UI asks for, at 2x


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    # Jellyfin titles and artwork (M18): the path index, and a library scan after
    # clips land. Both exist only with Jellyfin configured.
    def _jf_client() -> jellyfin.JellyfinClient:
        return jellyfin.JellyfinClient(settings.jellyfin_url, settings.jellyfin_api_key)

    jf_index = jf_refresher = None
    if settings.jellyfin_enabled:
        jf_index = jellyfin_index.JellyfinIndex(
            settings.state_dir, _jf_client, settings.jellyfin_path_map, settings.media_roots)
        jf_refresher = jellyfin_index.LibraryRefresher(
            _jf_client, settings.clips_dir, settings.jellyfin_path_map)

    def _clips_landed(job) -> None:
        """A clip job finished: if it wrote into the clips folder, Jellyfin scans it soon."""
        if jf_refresher is None:
            return
        clips_root = settings.clips_dir.resolve()
        if any(Path(p).resolve().is_relative_to(clips_root) for p in job.result_paths):
            jf_refresher.poke()

    jobs = JobRegistry(max_workers=settings.max_concurrent_jobs, on_done=_clips_landed)
    # Full-video downloads run for up to an hour each — give them their own
    # single-worker pool so they can never starve the clip workers.
    dl_jobs = JobRegistry(max_workers=1)
    # Adding a YouTube URL and reindexing its transcript are yt-dlp fetches that
    # can be slow (and unpredictably so). Run them as background jobs in their
    # own pool so the request returns instantly — no proxy read-timeout (504)
    # and no contention with clips or downloads.
    yt_jobs = JobRegistry(max_workers=2)
    bookmarks = BookmarkStore(settings.state_dir)
    sub_cache = SubtitleCache(settings.state_dir, default_langs=settings.subtitle_langs)
    yt_store = youtube_items.YouTubeStore(settings.state_dir, download_dir=settings.youtube_dir)
    flac_cache = clipmeta.FlacCache(settings.state_dir)
    peak_store = peaks.PeakStore(settings.state_dir)
    # Machine transcripts (M21), in the state dir and never next to the media. They run
    # one at a time in their own pool, so a transcription never takes a clip slot; and
    # only when someone asks (D-037): nothing here starts one on its own.
    transcripts = stt.TranscriptStore(settings.state_dir / "transcripts")
    # On where whisper-cli and a model are installed, unless QC_TRANSCRIBE says otherwise
    # (D-053): off, no endpoint here answers, and stored transcripts and library copies stay
    # out of everything.
    stt_on = settings.whisper.enabled
    stt_jobs = JobRegistry(max_workers=1)
    active_stt: dict[str, str] = {}
    # The library index (M20): one job at a time, only when someone starts it.
    lib_jobs = JobRegistry(max_workers=1)
    lib_log = lib_index.RunLog(settings.state_dir)
    lib_active: list[str | None] = [None]
    lib_submit = threading.Lock()
    # Re-index hooks (M20): files another app says changed, drained in the same pool.
    def _drainer_alive(job_id: str) -> bool:
        job = lib_jobs.get(job_id)
        return (job is not None and job.status.value in ("queued", "running")
                and not job.cancel_requested)

    file_queue = file_index.FileQueue(alive=_drainer_alive)
    # Files whose subtitles changed since indexing (M20): noted by views and an hourly
    # timestamp-only pass; only ever re-indexed on request (index_changes).
    index_changes = index_changes_mod.ChangeLog(settings.state_dir)

    def _root_mounted(r: Path) -> bool:
        """A media root that is there: a folder, and not an empty one (an unmounted
        pool leaves its mount point empty, and would read as "all clear")."""
        try:
            with os.scandir(r) as it:
                return next(it, None) is not None
        except OSError:
            return False

    def _lib_roots() -> list[Path]:
        """The media roots that are there, by real path: views, search and the hooks key
        files by real path, so a symlinked root walked as given would index a second
        copy that nothing reads."""
        return [Path(os.path.realpath(r)) for r in settings.media_roots if r.is_dir()]

    def _changes_scan():
        roots = list(settings.media_roots)
        missing = [r for r in roots if not _root_mounted(r)]
        if missing:  # an unmounted pool would read as "all clear"
            raise OSError("a media root is missing or empty: "
                          + ", ".join(str(r) for r in missing))
        folders = _lib_roots()
        found, _, _ = _media_under(folders, set())
        cached = sub_cache.entry_names()
        for c, own in found:
            yield c, sub_cache.index_state(c, siblings=own, names=cached)

    def _lib_busy() -> bool:
        """A library run or a re-index job is reading the media."""
        return any(j.status.value == "running" for j in lib_jobs.list_recent())

    index_check = index_changes_mod.HourlyCheck(index_changes, _changes_scan,
                                                settings.index_check_interval,
                                                busy=_lib_busy)
    stopping = [False]  # the server is shutting down (a library run reads "interrupted")
    # Pairing (quipclipper.pair/1): apps that asked for access, and the ones a person let in.
    clients = pairing.ClientStore(settings.state_dir)
    pairs = pairing.Pairing(clients)
    server_id: list[str] = []  # made on first use, so importing the app writes nothing
    stt_submit = threading.Lock()  # one check-then-submit at a time (no twin jobs)

    def _stt_existing(key: str):
        """The queued or running job for an identical request, if any (finished ones'
        keys are dropped). Call with ``stt_submit`` held."""
        for k, jid in list(active_stt.items()):
            j = stt_jobs.get(jid)
            if j is None or j.status.value not in ("queued", "running"):
                del active_stt[k]
        return stt_jobs.get(active_stt.get(key, ""))

    def _has_machine(p: Path, siblings: list[Path] | None = None,
                     stored: dict[str, stt.Stamp] | None = None) -> bool:
        """Cheap: ``p`` has a current machine transcript (not one of the file it
        replaced), stored or copied to the library. ``siblings`` (the folder's listing)
        and ``stored`` (``transcripts.names()``) spare a listing and a stat when checking
        many files."""
        return stt_on and (transcripts.exists(p, stored)
                           or bool(stt.find_machine_sidecars(p, siblings)))
    # video id -> last download / add job id, so a repeat POST returns the
    # in-flight job instead of queueing a duplicate.
    active_downloads: dict[str, str] = {}
    active_adds: dict[str, str] = {}

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if jf_index is not None:
            jf_index.refresh_if_stale()  # build (or freshen) the path index in the background
        index_check.start()  # a check only: it never re-indexes
        yield
        index_check.stop()
        stopping[0] = True
        if jf_refresher is not None:
            jf_refresher.cancel()
        jobs.shutdown()
        dl_jobs.shutdown()
        yt_jobs.shutdown()
        stt_jobs.shutdown(cancel=True)  # stop whisper now, not after every queued run
        lib_jobs.shutdown(cancel=True)  # a rerun resumes from the cache
        index_changes.flush()
        peak_store.shutdown()

    app = FastAPI(title="quipclipper-web", version=__version__, lifespan=lifespan)
    app.state.settings = settings

    @app.middleware("http")
    async def _security_gate(request: Request, call_next):
        # S1 — proxy shared secret (opt-in). When configured, every request must
        # carry the matching secret that nginx injects; direct hits on the backend
        # port (bypassing nginx) are refused. /api/health is exempt so container
        # liveness probes can hit the backend directly.
        if settings.proxy_secret and request.url.path != "/api/health":
            supplied = request.headers.get(_PROXY_SECRET_HEADER, "")
            if not hmac.compare_digest(supplied, settings.proxy_secret):
                return JSONResponse({"detail": "Forbidden"}, status_code=403)
        # A programmatic client authenticated with an API token is a machine, not
        # a browser, so it's exempt from the CSRF-header requirement below. An
        # explicit but wrong token (X-API-Key / Bearer) is rejected outright.
        # Pairing switched off (QC_PAIRING=0): every /api/pair* route is gone, before a
        # body is even read, so an app sees the 404 of a QuipClipper without pairing.
        if not settings.pairing and _PAIR_ROUTE.match(request.url.path):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        api_client = False
        hook_route = request.url.path in _HOOK_ROUTES
        request.state.client = None
        # A paired app's own token (qcc_…) counts as a machine client like QC_API_TOKEN,
        # also when no QC_API_TOKEN is set. Named outright and not current: 401.
        client_status, client = _client_token_status(request, clients)
        if client_status == "bad":
            return JSONResponse(_CLIENT_REVOKED, status_code=401)
        if client_status == "ok":
            api_client = True
            request.state.client = client
            request.state.token = True
        elif settings.api_tokens or settings.hook_tokens:
            status = _api_token_status(request, settings.api_tokens | settings.hook_tokens)
            # With only hook tokens set, a token the hook route doesn't know is someone
            # else's (a front proxy's own Bearer): only that route refuses it.
            if status == "bad" and (settings.api_tokens or hook_route):
                return JSONResponse({"detail": "Invalid API token."}, status_code=401)
            api_client = status == "ok"
            request.state.token = api_client
            # A hook token (low privilege) only queues re-indexes: no other route.
            if (api_client and settings.hook_tokens and not hook_route
                    and _api_token_status(request, settings.api_tokens) != "ok"):
                return JSONResponse(_HOOK_ONLY, status_code=403)
        # The password gate's "hook" login (docs/HOOKS.md) is for the hook route only,
        # even when this server doesn't know its token (not set here, or rotated):
        # it must never pass as a person.
        if not hook_route and _basic_user(request) == _HOOK_USER:
            return JSONResponse(_HOOK_ONLY, status_code=403)
        # S2 — CSRF: state-changing browser requests must carry the custom header.
        if (
            not api_client
            and request.method in _UNSAFE_METHODS
            and _CSRF_HEADER not in request.headers
        ):
            return JSONResponse(
                {"detail": f"Missing {_CSRF_HEADER} header (CSRF guard)."},
                status_code=403,
            )
        return await call_next(request)

    @app.get("/api/health")
    def health() -> dict:
        """Liveness probe plus a quick check that the cutting tools are present."""
        return {
            "status": "ok",
            "service": "quipclipper-web",
            "version": __version__,
            "tools": {
                "ffmpeg": shutil.which("ffmpeg") is not None,
                "ffprobe": shutil.which("ffprobe") is not None,
                "mkvmerge": shutil.which("mkvmerge") is not None,
                "yt-dlp": shutil.which("yt-dlp") is not None,
                **({"whisper-cli": settings.whisper.binary_path() is not None}
                   if stt_on else {}),
            },
            "jellyfin": settings.jellyfin_enabled,  # Now playing (M15) configured
            # Machine transcription (M21): whisper.cpp, its models and thread cap; only
            # while it's on (D-053).
            **({"transcribe": settings.whisper.describe()} if stt_on else {}),
        }

    def _server_id() -> str:
        if not server_id:
            server_id.append(pairing.server_id(settings.state_dir))
        return server_id[0]

    def _link_server(request: Request) -> str | None:
        """The ``server`` in Send to LeadStinger links: QC_PUBLIC_URL, else the URL the
        advertiser announces, else the base URL this request reached the app at. Raises
        LinkError for a bad QC_PUBLIC_URL."""
        base = lslink.request_base(request.headers, request.url.scheme,
                                   request.scope.get("root_path") or "")
        return lslink.link_server(settings.public_url,
                                  pairing.advertised_url(settings.state_dir), base)

    @app.get("/api/config")
    def config(request: Request) -> dict:
        """Non-secret configuration the frontend needs to render itself."""
        try:
            link_server = _link_server(request)
        except lslink.LinkError:
            link_server = None
        return {
            "media_roots": [str(p) for p in settings.media_roots],
            "auth_required": settings.auth_required,
            "max_concurrent_jobs": settings.max_concurrent_jobs,
            "hw_encode": _vaapi_h264_available(),  # iGPU H.264 (B23) for video re-encode
            "subtitle_langs": settings.subtitle_langs,  # default auto-select prefs (B14)
            # This server's base URL as set (QC_PUBLIC_URL); null = not set.
            "public_url": settings.public_url or None,
            # The server Send to LeadStinger and Connect LeadStinger links name for this
            # browser: QC_PUBLIC_URL, else the advertised URL, else the address it used.
            "link_server": link_server,
            # Pairing (quipclipper.pair/1): apps ask at /api/pair; and who this server is.
            "pairing": settings.pairing,
            "server_id": _server_id(),
            "server_name": pairing.server_name(settings.server_name, settings.state_dir),
            # Jellyfin URL + API key set: the Now playing card and scope show (M15).
            "jellyfin": settings.jellyfin_enabled,
            # Machine transcription (M21): whether it's set up, and the models to offer;
            # only while it's on (D-053).
            **({"transcribe": {k: v for k, v in settings.whisper.describe().items()
                               if k in ("available", "models", "default_model", "threads",
                                        "language")}} if stt_on else {}),
        }

    # --- Jellyfin now playing (M15) ---------------------------------------------

    @app.get("/api/jellyfin/sessions")
    def jellyfin_sessions() -> dict:
        """What each Jellyfin client is playing, its playhead and the file in the library.

        ``position`` is the playhead estimated from the client's last report (clients
        report every ~10 s while playing; a paused one is exact). ``path`` is the file
        under the media roots, or null with a ``note`` saying why."""
        if not settings.jellyfin_enabled:
            raise HTTPException(status_code=503, detail="Jellyfin isn't configured "
                                "(QC_JELLYFIN_URL and an API key).")
        client = _jf_client()
        try:
            raw = client.sessions()
        except jellyfin.JellyfinError as exc:
            raise HTTPException(status_code=502, detail=str(exc))
        sessions = jellyfin.now_playing(
            raw, settings.jellyfin_path_map, settings.media_roots,
            item_path=client.item_path)
        for s in sessions:  # the item's own artwork; else what the index has for the file
            if s.get("art") is None and s.get("path") and jf_index is not None:
                known = jf_index.lookup(s["path"])
                s["art"] = known.get("art") if known else None
        return {"sessions": sessions}

    def _jf_required() -> jellyfin_index.JellyfinIndex:
        if jf_index is None:
            raise HTTPException(status_code=503, detail="Jellyfin isn't configured "
                                "(QC_JELLYFIN_URL and an API key).")
        return jf_index

    def _jf_known(p: Path, is_dir: bool | None = None) -> dict | None:
        """Jellyfin's poster and title for a library row or an item (M18), when it knows
        the file or folder. Never asks Jellyfin: it reads the index."""
        if jf_index is None:
            return None
        if is_dir is None:
            is_dir = p.is_dir()
        known = jf_index.folder(p) if is_dir else jf_index.lookup(p)
        if not known:
            return None
        out = {k: known.get(k) for k in ("id", "type", "title", "show", "season", "episode",
                                          "year", "art") if known.get(k) is not None}
        return out or None

    def _with_jf(entries: list[dict]) -> list[dict]:
        if jf_index is None:
            return entries
        for e in entries:
            if e.get("is_youtube") or not e.get("path") or "#seg=" in e["path"]:
                continue
            known = _jf_known(Path(e["path"]), bool(e.get("is_dir")))
            if known:
                e["jellyfin"] = known
        return entries

    @app.get("/api/jellyfin/lookup")
    def jellyfin_lookup(path: str = Query(...)) -> dict:
        """What Jellyfin knows about a library file or folder: its item id, title,
        series, season, episode, year, provider ids and artwork. ``item`` is null for
        a file Jellyfin doesn't know (its clip names use the parsed values)."""
        index = _jf_required()
        p = _resolve(path)
        if not p.exists():
            raise HTTPException(status_code=404, detail=f"Not found: {path}")
        item = index.folder(p) if p.is_dir() else index.lookup(p)
        meta = None
        if p.is_file():
            ctx = clip_template_context(p, ClipRange(start=0.0, end=0.0), "",
                                        jellyfin_index.naming_meta(item))
            meta = {k: ctx[k] for k in ("title", "show", "season", "episode", "year",
                                        "folder")}
        return {"path": str(p), "known": item is not None, "item": item,
                "naming": meta, "index": index.status()}

    @app.post("/api/jellyfin/index")
    def jellyfin_reindex() -> dict:
        """Rebuild the path index from Jellyfin now (it is rebuilt hourly on its own),
        and report its size. Waits for a rebuild already running."""
        index = _jf_required()
        try:
            return {"index": index.build()}
        except jellyfin.JellyfinError as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    @app.get("/api/jellyfin/image/{item_id}")
    def jellyfin_image(
        item_id: str,
        type: str = Query("Primary", pattern="^(Primary|Thumb)$"),
        w: int = Query(300, ge=16),
        tag: str | None = Query(None),
    ) -> Response:
        """A Jellyfin item's poster (Primary) or still (Thumb), scaled to at most ``w``
        pixels wide (capped), through this server so the API key never reaches the
        browser. With ``tag`` (the image's version) it is cached for good."""
        _jf_required()
        if not jellyfin_index.ITEM_ID_RE.match(item_id):
            raise HTTPException(status_code=422, detail="Not a Jellyfin item id.")
        if tag is not None and not jellyfin_index.ITEM_ID_RE.match(tag):
            raise HTTPException(status_code=422, detail="Not a Jellyfin image tag.")
        try:
            body, ctype = _jf_client().image(item_id, type, min(w, _IMAGE_MAX_WIDTH), tag)
        except jellyfin.JellyfinError as exc:
            if exc.status == 404:
                raise HTTPException(status_code=404, detail="Jellyfin has no such image.")
            raise HTTPException(status_code=502, detail=str(exc))
        if ctype not in _IMAGE_TYPES or len(body) > jellyfin.MAX_IMAGE_BYTES:
            raise HTTPException(status_code=502, detail="Jellyfin sent something that "
                                "isn't a poster.")
        cache = "private, max-age=31536000, immutable" if tag else "private, max-age=86400"
        return Response(body, media_type=ctype, headers={
            "Cache-Control": cache, "X-Content-Type-Options": "nosniff"})

    # --- library browsing ----------------------------------------------------

    def _no_nul(path: str) -> None:
        # The OS can't take a NUL in a path: Path.resolve() raises ValueError (a 500).
        if "\x00" in path:
            raise HTTPException(status_code=400, detail="Path contains a NUL byte.")

    def _clip_target(rel: str) -> Path:
        """A path in the clips library, checked to stay inside it (403) or 400."""
        _no_nul(rel)
        target = (settings.clips_dir / rel).resolve()
        if not target.is_relative_to(settings.clips_dir.resolve()):
            raise HTTPException(status_code=403, detail="Path outside clips dir.")
        return target

    def _resolve(path: str) -> Path:
        """Resolve a client path within the media roots, mapping errors to HTTP."""
        _no_nul(path)
        try:
            return library.resolve_within_roots(path, settings.media_roots)
        except library.PathNotAllowed as exc:
            raise HTTPException(status_code=403, detail=str(exc))

    def _resolve_any(path: str) -> Path:
        """Resolve a path within media roots OR clips dir."""
        _no_nul(path)
        try:
            return library.resolve_within_roots(path, settings.media_roots)
        except library.PathNotAllowed:
            pass
        # Try clips dir
        target = Path(path).resolve()
        clips_root = settings.clips_dir.resolve()
        if clips_root.is_dir() and (target == clips_root or clips_root in target.parents):
            return target
        raise HTTPException(status_code=403, detail=f"Path is outside allowed directories: {path}")

    # --- machine transcript lines (M21) -------------------------------------------

    def _machine_lines(p: Path, captions: bool = False,
                       siblings: list[Path] | None = None) -> list:
        """A file's machine transcript lines: the stored transcript and any copy in the
        library next to it. The player's captions show only a library copy (D-037). None
        while transcription is off (D-053)."""
        return (stt.machine_cues(p, transcripts, stored=not captions, siblings=siblings)
                if stt_on else [])

    def _machine_mode(machine: str) -> str:
        """A request's ``machine`` choice: transcription off, machine lines are left out, and
        asking for them alone is an error that says why."""
        if stt_on:
            return machine
        if machine == "only":
            raise HTTPException(status_code=404, detail=stt.OFF_REASON)
        return "exclude"

    def _file_cues(p: Path, track: int | None = None, machine: str = "include",
                   captions: bool = False, heard: list | None = None) -> list:
        """A library file's cues with its machine lines mixed in (marked, subtitle lines
        winning), left out (``exclude``) or alone (``only``). A file with no subtitles but
        a transcript has only machine lines; with neither, the subtitle error stands."""
        if machine != "exclude" and heard is None:
            heard = _machine_lines(p, captions)
        heard = heard if machine != "exclude" else []
        if machine == "only":
            return heard
        try:
            real = sub_cache.resolve(p, track=track)
        except (ValueError, FileNotFoundError, RuntimeError):
            if not heard:
                raise
            real = []
        else:
            index_changes.forget(p)  # read at its current subtitles: no longer out of date
        return stt.merge_cues(real, heard) if heard else real

    def _transcript_info(p: Path) -> dict:
        """What /api/items says about a file's transcript, for the workspace card."""
        t = transcripts.load(p)
        return {
            "stored": t.summary() if t else None,
            "library": [x.name for x in stt.find_machine_sidecars(p)],
            "writable": os.access(p.parent, os.W_OK),
            "sidecar_name": stt.machine_sidecar_path(
                p, t.language if t else settings.whisper.language).name,
        }

    # --- deep links (M18) ----------------------------------------------------

    def _item_link(path: str, at: float | None = None,
                   sel: tuple[float, float] | None = None, track: int | None = None) -> str | None:
        """An absolute link to this moment in the web app, or None without QC_PUBLIC_URL."""
        if not settings.public_url:
            return None
        return deeplinks.link(settings.public_url, deeplinks.item_route(path, at, sel, track))

    def _with_link(d: dict, link: str | None) -> dict:
        """``d`` with its ``link`` field, left out when links are off (no QC_PUBLIC_URL)."""
        if link:
            d["link"] = link
        return d

    @app.get("/api/library/roots")
    def roots() -> dict:
        return {"roots": [str(p) for p in settings.media_roots]}

    @app.get("/api/library/browse")
    def browse(path: str | None = None) -> dict:
        # The "▶ YouTube" pseudo-folder lists every added video (yt: refs) —
        # branch before _resolve, like the EPUB one below; a yt: ref is not a
        # filesystem path.
        if youtube_items.is_folder_ref(path):
            return {"path": path,
                    "entries": [youtube_items.entry_dict(m) for m in yt_store.list_all()]}
        # A synced EPUB audiobook browses into its audio segments like a folder.
        if path:
            ep = _resolve(path)
            if epub_items.is_epub_book(ep):
                return {"path": path, "entries": epub_items.segment_entries(ep)}
        try:
            entries = library.browse(path, settings.media_roots, transcribed=_has_machine,
                                     indexed=sub_cache.index_state)
        except library.PathNotAllowed as exc:
            raise HTTPException(status_code=403, detail=str(exc))
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except NotADirectoryError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        out = _with_jf([library.entry_dict(e) for e in entries])
        if not path:
            # Root listing: the YouTube pseudo-folder rides along after the roots.
            out.append(youtube_items.folder_entry())
        return {"path": path, "entries": out}

    @app.get("/api/library/search")
    def search_library(
        query: str = Query(..., min_length=1),
        path: str | None = None,
        limit: int = Query(50, ge=1, le=200),
    ) -> dict:
        """Search for folders/files by name across media roots or within a folder."""
        if path:
            try:
                entries = library.search_within(query, path, settings.media_roots, max_results=limit)
            except library.PathNotAllowed as exc:
                raise HTTPException(status_code=403, detail=str(exc))
        else:
            entries = library.search(query, settings.media_roots, max_results=limit)
        return {"query": query, "count": len(entries),
                "entries": _with_jf([library.entry_dict(e) for e in entries])}

    # --- YouTube sources -------------------------------------------------------
    # A pasted watch URL becomes a persistent virtual item (ref "yt:<id>") whose
    # transcript is fetched by yt-dlp — no video download; playback and cutting
    # stream from resolved googlevideo URLs on demand (see youtube_items).

    @app.post("/api/youtube")
    def youtube_add(req: YouTubeAdd) -> dict:
        """Add a YouTube video: fetch metadata + subtitles (no video download).

        The fetch runs as a **background job** (yt-dlp can be slow and its
        latency is unpredictable — a synchronous fetch risks a proxy 504). The
        URL is validated synchronously; then either the already-added entry is
        returned immediately (job_id null), or a job is queued — poll it like a
        clip/download job, and re-list the YouTube folder when it's done. A
        repeat POST while an add is in flight returns the same job."""
        vid = youtube_items.extract_video_id(req.url)
        if vid is None:
            raise HTTPException(status_code=400, detail="Not a recognised YouTube video URL.")
        if yt_store.is_added(vid):
            try:
                meta = yt_store.meta(vid)
            except RuntimeError as exc:  # corrupt info.json
                raise HTTPException(status_code=500, detail=str(exc))
            return {"job_id": None, "status": "done", "entry": youtube_items.entry_dict(meta)}
        running = yt_jobs.get(active_adds.get(vid, ""))
        if running is not None and running.status.value in ("queued", "running"):
            return {"job_id": running.id, "status": running.status.value}
        url, langs = req.url, settings.subtitle_langs

        def _do_add(_job) -> list:
            yt_store.add(url, langs)  # RuntimeError → the job fails with its message
            return []

        job = yt_jobs.submit(_do_add, label=f"add: {vid}")
        active_adds[vid] = job.id
        return {"job_id": job.id, "status": job.status.value}

    @app.delete("/api/youtube/{video_id}")
    def youtube_remove(video_id: str) -> dict:
        """Remove an added video (its metadata + stored transcript + download)."""
        if not yt_store.remove(video_id):
            raise HTTPException(status_code=404, detail=f"Unknown YouTube video: {video_id}")
        return {"deleted": video_id}

    @app.post("/api/youtube/{video_id}/download")
    def youtube_download(video_id: str) -> dict:
        """Download the full video to the server (async job). Once present, the
        local file transparently replaces stream URLs everywhere: raw playback
        with native seeking, accurate keyframe probes, and cutting — no network
        at cut time. Idempotent twice over: an existing download short-circuits
        (job_id null), and a repeat POST while a download is in flight returns
        the SAME job instead of queueing a duplicate. Downloads run in their own
        single-worker pool, so they never block clip jobs. Poll the returned job
        like a clip job."""
        try:
            meta = yt_store.meta(video_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        if meta.get("downloaded"):
            return {"job_id": None, "status": "done", "size": meta.get("download_size")}
        running = dl_jobs.get(active_downloads.get(video_id, ""))
        if running is not None and running.status.value in ("queued", "running"):
            return {"job_id": running.id, "status": running.status.value}
        job = dl_jobs.submit(
            lambda _job: [yt_store.download(video_id)],
            label=f"download: {meta.get('title') or video_id}",
        )
        active_downloads[video_id] = job.id
        return {"job_id": job.id, "status": job.status.value}

    @app.delete("/api/youtube/{video_id}/download")
    def youtube_remove_download(video_id: str) -> dict:
        """Delete the local video file (the transcript and metadata stay)."""
        try:
            yt_store.meta(video_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        if not yt_store.remove_download(video_id):
            raise HTTPException(status_code=404, detail="No download to remove.")
        return {"removed": video_id}

    # --- file inspection -----------------------------------------------------

    @app.get("/api/items")
    def item(path: str = Query(...), langs: str | None = Query(None)) -> dict:
        # A YouTube ref (yt:<id>): synthesized info from stored metadata. When
        # the video has been downloaded, probe the real local file instead so
        # duration/codecs are exact (the ref/flags stay virtual either way).
        vid = youtube_items.parse_ref(path)
        if vid is not None:
            try:
                synthesized = youtube_items.item_info(yt_store.meta(vid))
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc))
            except RuntimeError as exc:  # corrupt info.json — clean error, not a traceback
                raise HTTPException(status_code=500, detail=str(exc))
            local = yt_store.video_path(vid)
            if local is not None:
                with contextlib.suppress(RuntimeError):
                    probed = media.item_info(local, langs=settings.subtitle_langs)
                    synthesized.update(
                        duration=probed["duration"], streams=probed["streams"],
                        size=probed["size"],
                    )
            return synthesized
        # An EPUB segment ref (<epub>#seg=N): probe the extracted chapter audio.
        epub_ref, seg = epub_items.parse_ref(path)
        if seg is not None:
            ep = _resolve(epub_ref)
            try:
                return epub_items.segment_item_info(ep, seg)
            except (IndexError, ValueError) as exc:
                raise HTTPException(status_code=404, detail=str(exc))
        p = _resolve_any(path)
        if not p.exists():
            raise HTTPException(status_code=404, detail=f"Not found: {path}")
        if not p.is_file():
            raise HTTPException(status_code=400, detail=f"Not a file: {path}")
        # Per-request subtitle-language preference overrides the server default
        # for which track auto-selects (B14); blank uses the server default.
        pref = [s.strip() for s in langs.split(",") if s.strip()] if langs else None
        try:
            info = media.item_info(p, langs=pref or settings.subtitle_langs)
        except RuntimeError as exc:  # ffprobe failure
            raise HTTPException(status_code=500, detail=str(exc))
        # A library video with a detectable YouTube id but no subtitles can have
        # its transcript backfilled from YouTube (writable roots only). The UI
        # shows the "Fetch subtitles" action off this hint.
        if not info.get("has_sidecar") and not info.get("subtitle_tracks"):
            yid = youtube_items.youtube_id_for_file(p)
            if yid is not None:
                info["youtube_id"] = yid
                info["backfill_writable"] = os.access(p.parent, os.W_OK)
        known = _jf_known(p, False)
        if known:
            info["jellyfin"] = known  # title, series, poster (M18)
        if stt_on and settings.clips_dir.resolve() not in p.resolve().parents:
            info["transcript"] = _transcript_info(p)  # machine transcription (M21)
        return info

    @app.get("/api/items/link")
    def item_link(
        path: str = Query(...),
        at: float | None = Query(None, ge=0, allow_inf_nan=False),
        sel: str | None = Query(None),
        track: int | None = Query(None, ge=0),
    ) -> dict:
        """A link that opens ``path`` in the web app at ``at`` seconds, with ``sel``
        ("START-END", seconds) selected: the Copy link button's twin. Built from
        QC_PUBLIC_URL; the path is checked the way opening it would check it."""
        if not settings.public_url:
            raise HTTPException(status_code=503, detail="Links need QC_PUBLIC_URL: this "
                                "server's base URL as browsers reach it.")
        rng = None
        if sel:
            try:
                rng = deeplinks.parse_sel(sel)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc))
        vid = youtube_items.parse_ref(path)
        epub_ref, seg = epub_items.parse_ref(path)
        if vid is not None:
            try:
                yt_store.meta(vid)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc))
            except RuntimeError as exc:
                raise HTTPException(status_code=500, detail=str(exc))
        elif seg is not None:
            if not _resolve(epub_ref).is_file():
                raise HTTPException(status_code=404, detail=f"Not found: {epub_ref}")
        else:
            p = _resolve_any(path)
            if not p.is_file():
                raise HTTPException(status_code=404, detail=f"Not a file: {path}")
        return {"path": path, "at": at if at is not None else (rng[0] if rng else None),
                "sel": list(rng) if rng else None, "track": track,
                "link": _item_link(path, at, rng, track)}

    # --- waveform (M18) ------------------------------------------------------

    _durations: dict[str, float | None] = {}

    def _audio_source(path: str, audio: int) -> peaks.AudioSource:
        """Where ffmpeg reads ``path``'s audio stream ``a:audio``, with the same source
        resolution and path checks as the other item endpoints."""
        vid = youtube_items.parse_ref(path)
        if vid is not None:
            try:
                meta = yt_store.meta(vid)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc))
            except RuntimeError as exc:
                raise HTTPException(status_code=500, detail=str(exc))
            local = yt_store.video_path(vid)
            if local is None:
                # Not downloaded: decode the resolved stream (the audio half of a DASH pair).
                try:
                    video_url, audio_url = youtube_items.resolve_stream_urls(meta["webpage_url"])
                except RuntimeError as exc:
                    raise HTTPException(status_code=502, detail=str(exc))
                return peaks.AudioSource(
                    input=audio_url or video_url, basis=f"yt:{vid}", audio=0,
                    input_flags=("-reconnect", "1", "-reconnect_streamed", "1",
                                 "-reconnect_delay_max", "5"),
                    duration=meta.get("duration"))
            p = local
        else:
            epub_ref, seg = epub_items.parse_ref(path)
            if seg is not None:
                ep = _resolve(epub_ref)
                if not ep.is_file():
                    raise HTTPException(status_code=404, detail=f"Not found: {epub_ref}")
                try:
                    epub_items.segment_cues(ep, seg)  # the segment exists
                except (IndexError, ValueError) as exc:
                    raise HTTPException(status_code=404, detail=str(exc))
                return peaks.AudioSource(
                    input="", basis=f"{peaks.file_basis(ep)}#seg={seg}", audio=0,
                    make_input=lambda: epub_items.segment_audio_input(ep, seg))
            p = _resolve_any(path)
            if not p.is_file():
                raise HTTPException(status_code=404, detail=f"Not found: {path}")
        basis = peaks.file_basis(p)
        if basis not in _durations:
            _durations[basis] = media.probe_duration(p)
        return peaks.AudioSource(input=str(p), basis=basis, audio=audio,
                                 duration=_durations[basis], video_input=str(p))

    @app.get("/api/items/peaks")
    def item_peaks(
        path: str = Query(...),
        start: float = Query(0.0, ge=0, allow_inf_nan=False),
        end: float | None = Query(None, gt=0, allow_inf_nan=False),
        bins: int = Query(1000, ge=1, le=peaks.MAX_BINS),
        audio: int = Query(0, ge=0),
        keyframes: bool = Query(False),
    ) -> JSONResponse:
        """Waveform peaks of ``start..end`` in ``bins`` (min, max) pairs, as base64
        int16. Long windows come from the cached overview (202 while it builds), short
        ones from a decode of just that window, down to raw samples."""
        src = _audio_source(path, audio)
        try:
            status, body = peaks.peaks_response(peak_store, src, start, end, bins, keyframes)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        except RuntimeError as exc:  # no such audio stream, or ffmpeg failed
            raise HTTPException(status_code=422, detail=str(exc))
        body["path"] = path
        return JSONResponse(body, status_code=status)

    @app.get("/api/items/snap")
    def item_snap(
        path: str = Query(...),
        t: float = Query(..., ge=0, allow_inf_nan=False),
        mode: str = Query("zero", pattern="^(zero|silence)$"),
        radius: float | None = Query(None, gt=0, le=10),
        db: float = Query(-45.0, ge=-120, le=0),
        audio: int = Query(0, ge=0),
    ) -> dict:
        """The nearest zero crossing (``mode=zero``) or quiet point (RMS below ``db``
        dBFS, ``mode=silence``) to ``t`` within ``radius`` seconds."""
        src = _audio_source(path, audio)
        try:
            body = peaks.snap(peak_store, src, t, mode, radius, db)
        except RuntimeError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        body["path"] = path
        return body

    @app.get("/api/items/subtitles")
    def subtitles(
        path: str = Query(...),
        track: int | None = Query(None, ge=0),
        offset: float = Query(0, ge=0),
        fmt: str = Query("vtt"),
        machine: str = Query("include", pattern="^(include|exclude|only)$"),
    ) -> Response:
        machine = _machine_mode(machine)
        # YouTube ref: cues come from the stored transcript (SubtitleCache bypassed).
        vid = youtube_items.parse_ref(path)
        if vid is not None:
            try:
                cues = yt_store.cues(vid)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc))
            except RuntimeError as exc:
                raise HTTPException(status_code=500, detail=str(exc))
            if fmt == "json":
                return Response(
                    content=json.dumps(
                        [{"start": c.start, "end": c.end, "text": c.text, "speaker": c.speaker}
                         for c in cues]),
                    media_type="application/json",
                )
            return Response(content=media.cues_to_vtt(cues, offset=offset), media_type="text/vtt")
        # EPUB segment: cues come from the media overlay, not a sidecar/track.
        epub_ref, seg = epub_items.parse_ref(path)
        if seg is not None:
            ep = _resolve(epub_ref)
            try:
                cues = epub_items.segment_cues(ep, seg)
            except (IndexError, ValueError) as exc:
                raise HTTPException(status_code=404, detail=str(exc))
            if fmt == "json":
                return Response(
                    content=json.dumps(
                        [{"start": c.start, "end": c.end, "text": c.text, "speaker": c.speaker}
                         for c in cues]),
                    media_type="application/json",
                )
            return Response(content=media.cues_to_vtt(cues, offset=offset), media_type="text/vtt")
        p = _resolve_any(path)
        try:
            # Use the cache: extraction from large files is slow (~9s for a
            # 2160p remux), and the VTT track + script panel request the same
            # subtitles when an item opens.  The cache + in-flight lock means
            # one extraction serves all callers and later views are instant.
            # Machine lines (M21) join the script; the captions (vtt) get only a copy
            # that was put in the library.
            cues = _file_cues(p, track, machine, captions=fmt != "json")
        except ValueError as exc:  # multiple tracks, none auto-selectable
            raise HTTPException(status_code=409, detail=str(exc))
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc))
        if fmt == "json":
            return Response(
                content=json.dumps(
                    [{"start": c.start, "end": c.end, "text": c.text, "speaker": c.speaker,
                      "source": c.source}
                     for c in cues],
                ),
                media_type="application/json",
            )
        return Response(
            content=media.cues_to_vtt(cues, offset=offset),
            media_type="text/vtt",
        )

    # --- dialogue search -------------------------------------------------------

    @app.get("/api/search")
    def search_dialogue(
        path: str = Query(...),
        query: str = Query(..., min_length=1),
        track: int | None = Query(None, ge=0),
        limit: int = Query(10, ge=1, le=100),
        min_score: float = Query(60.0, ge=0, le=100),
        max_span: int = Query(3, ge=1, le=10),
        machine: str = Query("include", pattern="^(include|exclude|only)$"),
    ) -> dict:
        machine = _machine_mode(machine)
        vid = youtube_items.parse_ref(path)
        epub_ref, seg = epub_items.parse_ref(path)
        if vid is not None:
            try:
                cues = yt_store.cues(vid)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc))
            except RuntimeError as exc:
                raise HTTPException(status_code=500, detail=str(exc))
        elif seg is not None:
            try:
                cues = epub_items.segment_cues(_resolve(epub_ref), seg)
            except (IndexError, ValueError) as exc:
                raise HTTPException(status_code=404, detail=str(exc))
        else:
            p = _resolve_any(path)
            try:
                cues = _file_cues(p, track, machine)
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc))
            except FileNotFoundError as exc:
                raise HTTPException(status_code=404, detail=str(exc))
            except RuntimeError as exc:
                raise HTTPException(status_code=500, detail=str(exc))

        matches = engine_search(
            query,
            cues,
            limit=limit,
            min_score=min_score,
            max_span=max_span,
        )
        return {
            "query": query,
            "count": len(matches),
            "matches": [
                _with_link({
                    "index": i,
                    "score": round(m.score, 1),
                    "text": m.text,
                    "speaker": m.cues[0].speaker if m.cues else None,
                    "start": m.start,
                    "end": m.end,
                    "start_ts": format_timestamp(m.start),
                    "end_ts": format_timestamp(m.end),
                    "cue_count": len(m.cues),
                    "machine": m.machine,  # a machine transcript line (M21)
                }, _item_link(path, sel=(m.start, m.end), track=track))
                for i, m in enumerate(matches)
            ],
        }

    # --- folder dialogue search ------------------------------------------------

    def _media_under(folders: list[Path], stored: dict[str, stt.Stamp], seen: set[Path] | None = None,
                     seen_books: set[Path] | None = None,
                     tick: Callable[[int, int], None] | None = None,
                     ) -> tuple[list[tuple[Path, list[Path]]], int, int]:
        """Every media file under ``folders``: ``(files, books, bare_audio)``.

        ``files`` is ``(file, its own sidecar files)`` for each video, and each audio file
        with a sidecar or (``stored``: ``transcripts.names()``) a machine transcript, once
        each by real path (``seen`` carries that across calls). ``books`` counts the
        read-along EPUBs (``seen_books`` likewise); ``bare_audio`` the audio files with
        no transcript of any kind. One listing per folder serves each file's sidecar
        lookups (a library is ~70k files on a FUSE mount). ``tick(folders, files)`` runs
        after each folder listed (a library walk takes minutes on a cold pool): the
        library job's progress, and its cancel (``tick`` raises :class:`Cancelled`)."""
        seen = set() if seen is None else seen
        seen_books = set() if seen_books is None else seen_books
        found: list[tuple[Path, list[Path]]] = []
        books = bare = walked = 0
        for folder in folders:
            for d, files in _walk_dirs(folder):
                if tick is not None:
                    tick(walked, len(found))
                walked += 1
                real_dir = os.path.realpath(d)
                named = [(e.name.lower(), os.path.splitext(e.name)[1].lower(), e.name, e)
                         for e in files if not e.name.startswith(".")]  # no ._* (see _is_media_file)
                subs = [(low, d / name) for low, ext, name, _ in named if ext in SUBTITLE_EXTS]
                for low, ext, name, e in named:
                    if ext != ".epub" and ext not in VIDEO_EXTS and ext not in AUDIO_EXTS:
                        continue
                    # Once per real file: a linked file (a "favourites" folder of links)
                    # is the file it points at.
                    rp = (Path(os.path.realpath(e.path)) if e.is_symlink()
                          else Path(real_dir, name))
                    c = d / name
                    if ext == ".epub":
                        if rp not in seen_books and epub_items.is_epub_book(c):
                            seen_books.add(rp)
                            books += 1
                        continue
                    if rp in seen:
                        continue
                    # Its own sidecars (``<stem>.…``), the only siblings its lookups read.
                    prefix = os.path.splitext(low)[0] + "."
                    own = [p for n, p in subs if n.startswith(prefix)]
                    if ext not in VIDEO_EXTS and find_sidecar(c, siblings=own) is None:
                        if not _has_machine(c, own, stored):
                            seen.add(rp)
                            bare += 1
                            continue
                    seen.add(rp)
                    found.append((c, own))
        return found, books, bare

    @app.get("/api/search/folder")
    def search_dialogue_folder(
        path: list[str] = Query(...),
        query: str = Query(..., min_length=1, max_length=QUERY_MAX),
        limit: int = Query(5, ge=1, le=20),
        min_score: float = Query(60.0, ge=0, le=100),
        max_span: int = Query(3, ge=1, le=10),
        machine: str = Query("include", pattern="^(include|exclude|only)$"),
    ) -> dict:
        """Search dialogue across all video files in one or more folders.

        Accepts one or more ``path`` query params (repeat the param to search
        several folders at once, e.g. the folders returned by a library
        search).  For each video with subtitles (sidecar or embedded), run the
        search engine and collect the top results.  Returns a flat list of
        matches grouped by source file, sorted best-score-first across all files.
        """
        machine = _machine_mode(machine)
        # Resolve and validate every requested target: a folder to scan, an
        # EPUB audiobook to search directly (its chapters are searched like
        # files), or the YouTube pseudo-folder (every stored transcript).
        folders: list[Path] = []
        books: list[Path] = []
        yt_videos: list[dict] = []
        seen: set[Path] = set()
        seen_books: set[Path] = set()
        for p in path:
            if youtube_items.is_folder_ref(p):
                if not yt_videos:
                    yt_videos = yt_store.list_all()
                continue
            target = _resolve(p)
            if epub_items.is_epub_book(target):
                rp = target.resolve()
                if rp not in seen_books:
                    seen_books.add(rp)
                    books.append(target)
            elif target.is_dir():
                folders.append(target)
            else:
                raise HTTPException(status_code=400, detail=f"Not a directory or book: {p}")

        all_hits: list[dict] = []

        # Every media file under the folders, de-duplicated (folders may overlap) and
        # sorted by name for a deterministic order. Only indexed files are searched:
        # their subtitles are cached, so a search reads them and never runs ffmpeg
        # (extraction takes 1-9 s a file; the folder's Index does it). A file not
        # indexed is searched on its stored machine transcript if it has one, else
        # counted in `not_indexed`, or in `changed` when it was indexed but its subtitles
        # changed since (listed in `changed_files` for the banner's Re-index, and noted
        # for the out-of-date badge). A read-along book found in a folder is counted in
        # `books_not_searched`: it has no index, and parsing it each search is slow
        # (wizrock: 31 books, 30-40 s); a book given as a path is searched. One listing
        # per folder serves each file's sidecar lookups, and the per-file stats of the
        # index check run in threads (a library is ~70k files on a FUSE mount).
        stored = transcripts.names() if stt_on and machine != "exclude" else {}
        found, books_not_searched, _ = _media_under(folders, stored, seen, seen_books)

        cached = sub_cache.entry_names()

        def _classify(item: tuple[Path, list[Path]]) -> tuple[Path, list[Path], str]:
            c, subs = item
            state = sub_cache.index_state(c, siblings=subs, names=cached)
            if state == "not_indexed" and machine != "exclude" and _has_machine(c, subs, stored):
                state = "transcript"  # searchable on its machine lines alone
            return c, subs, state

        with ThreadPoolExecutor(max_workers=8) as pool:
            classified = list(pool.map(_classify, found))
        videos = sorted((x for x in classified if x[2] not in ("not_indexed", "changed")),
                        key=lambda x: (x[0].name.lower(), str(x[0])))
        changed_files = sorted(str(c) for c, _, st in classified if st == "changed")
        not_indexed = len(classified) - len(videos) - len(changed_files)
        index_changes.note((c, st) for c, _, st in classified)
        books.sort(key=lambda p: p.name.lower())

        def _search_one(item: tuple[Path, list[Path], str]) -> list[dict]:
            video, subs, state = item
            heard = _machine_lines(video, siblings=subs) if machine != "exclude" else []
            real = (sub_cache.cached(video, siblings=subs)
                    if state == "indexed" and machine != "only" else None)
            cues = stt.merge_cues(real, heard) if real and heard else real or heard
            if not cues:
                return []
            matches = engine_search(
                query, cues,
                limit=limit, min_score=min_score, max_span=max_span,
            )
            return [
                {
                    "file": video.name,
                    "path": str(video),
                    "score": round(m.score, 1),
                    "text": m.text,
                    "speaker": m.cues[0].speaker if m.cues else None,
                    "start": m.start,
                    "end": m.end,
                    "start_ts": format_timestamp(m.start),
                    "end_ts": format_timestamp(m.end),
                    "cue_count": len(m.cues),
                    "machine": m.machine,
                    "_fit": m.fit,
                }
                for m in matches
            ]

        def _search_book(epub: Path) -> list[dict]:
            """Search every chapter of an EPUB audiobook; hits point at the chapter
            (a segment ref) so opening one lands on the right line."""
            try:
                book = epub_items.book_for(epub)
                segs = epub_items.segments(book)
            except (ValueError, OSError):
                return []
            cand: list[tuple] = []
            for s in segs:
                for m in engine_search(
                    query, [c.as_cue() for c in s.cues],
                    limit=limit, min_score=min_score, max_span=max_span,
                ):
                    cand.append((s, m))
            cand.sort(key=lambda sm: (-sm[1].score, -sm[1].fit, sm[0].index, sm[1].start))
            title = book.title or epub.stem
            return [
                {
                    "file": f"{title} — {s.title or f'Part {s.index + 1}'}",
                    "path": epub_items.make_ref(epub, s.index),
                    "score": round(m.score, 1),
                    "text": m.text,
                    "speaker": None,
                    "start": m.start,
                    "end": m.end,
                    "start_ts": format_timestamp(m.start),
                    "end_ts": format_timestamp(m.end),
                    "cue_count": len(m.cues),
                    "_fit": m.fit,
                }
                for s, m in cand[:limit]
            ]

        def _search_yt(meta: dict) -> list[dict]:
            """Search one stored YouTube transcript; hits point at the yt: ref."""
            try:
                cues = yt_store.cues(meta["id"])
            except (KeyError, RuntimeError):
                return []
            matches = engine_search(
                query, cues,
                limit=limit, min_score=min_score, max_span=max_span,
            )
            return [
                {
                    "file": meta.get("title") or meta["id"],
                    "path": youtube_items.make_ref(meta["id"]),
                    "score": round(m.score, 1),
                    "text": m.text,
                    "speaker": m.cues[0].speaker if m.cues else None,
                    "start": m.start,
                    "end": m.end,
                    "start_ts": format_timestamp(m.start),
                    "end_ts": format_timestamp(m.end),
                    "cue_count": len(m.cues),
                    "_fit": m.fit,
                }
                for m in matches
            ]

        if machine == "only":  # books and YouTube items have no machine transcripts
            books, yt_videos = [], []
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(_search_one, v): v for v in videos}
            futures.update({pool.submit(_search_book, b): b for b in books})
            futures.update({pool.submit(_search_yt, m): m["id"] for m in yt_videos})
            for fut in as_completed(futures):
                all_hits.extend(fut.result())

        # Best score first (D-041: 100 only for the words verbatim); at an equal score
        # a subtitle line before a machine one, then the line closest to the whole
        # query, then file and time, so equal hits keep one order however the
        # threads finish.
        all_hits.sort(key=lambda h: (-h["score"], h.get("machine", False),
                                     -h["_fit"], h["path"], h["start"]))
        shown = all_hits[:FOLDER_MATCHES_MAX]
        for h in shown:
            del h["_fit"]  # a sort key only, not part of the response
            _with_link(h, _item_link(h["path"], sel=(h["start"], h["end"])))

        return {
            "query": query,
            "folders": path,
            "files_scanned": len(videos) + len(books) + len(yt_videos),
            # Not searched, so results may be partial: files whose subtitles aren't
            # indexed, and read-along books inside the folders.
            "not_indexed": not_indexed,
            # Indexed once, but their subtitles changed since: left out until re-indexed
            # (POST /api/index/file with these paths).
            "changed": len(changed_files),
            "changed_files": changed_files[:index_changes_mod.LIST_MAX],
            "books_not_searched": books_not_searched,
            "capped": bool(not_indexed or changed_files or books_not_searched),
            # Every match is counted; the best FOLDER_MATCHES_MAX are returned.
            "count": len(all_hits),
            "truncated": len(all_hits) > FOLDER_MATCHES_MAX,
            "matches": shown,
        }

    # --- folder subtitle index -------------------------------------------------

    def _folder_videos(self_path: str, cap: int | None = None) -> tuple[list[Path], bool]:
        """Recursively scan a folder for videos, sorted by name.

        With *cap*, scanning stops once more than *cap* videos are found and
        the second element of the result is True ("skipped").
        """
        folder = _resolve(self_path)
        if not folder.is_dir():
            raise HTTPException(status_code=400, detail=f"Not a directory: {self_path}")
        videos: list[Path] = []
        for c in folder.rglob("*"):
            if _is_media_file(c, _has_machine):
                videos.append(c)
                if cap is not None and len(videos) > cap:
                    return videos, True
        videos.sort(key=lambda p: p.name.lower())
        return videos, False

    @app.get("/api/search/folder/index-status")
    def folder_index_status(path: str = Query(...)) -> dict:
        """How much of a folder of any size is indexed. One listing per folder and the
        cache's names (no extraction, no probe), so a whole library root takes seconds."""
        folder = _resolve(path)
        if not folder.is_dir():
            raise HTTPException(status_code=400, detail=f"Not a directory: {path}")
        stored = transcripts.names() if stt_on else {}
        found, _, bare = _media_under([folder], stored)
        cached = sub_cache.entry_names()

        def _state(item: tuple[Path, list[Path]]) -> tuple[str, bool]:
            c, own = item
            return (sub_cache.index_state(c, siblings=own, names=cached),
                    _has_machine(c, own, stored))

        with ThreadPoolExecutor(max_workers=8) as pool:
            states = list(pool.map(_state, found))
        # A file with no subtitles still has none to index, even with a machine transcript.
        indexed = sum(1 for st, _ in states if st == "indexed")
        no_subs = sum(1 for st, _ in states if st == "no_subs")
        # Indexed once, but their subtitles changed since (the view's banner re-indexes
        # exactly these); what this check saw also keeps the out-of-date badge current.
        changed = sorted(str(c) for (c, _), (st, _) in zip(found, states) if st == "changed")
        index_changes.note((c, st) for (c, _), (st, _) in zip(found, states))
        # Audio with no transcript of any kind isn't a media file yet (nothing to search),
        # but a folder transcription can give it one. ``skipped`` is always false now
        # (no folder is too big to count); over ``cap`` files, Index runs as a job.
        return {"total": len(found), "indexed": indexed, "no_subs": no_subs,
                "changed": len(changed), "changed_files": changed[:index_changes_mod.LIST_MAX],
                "transcribed": sum(1 for _, heard in states if heard),
                "audio_untranscribed": bare if stt_on else 0, "skipped": False,
                "cap": INDEX_CAP}

    # This and /api/items/subtitles/reindex take their input via the query string
    # and no body, so they would be CORS "simple requests" — the classic CSRF
    # vector. The _security_gate middleware neutralises it: every state-changing
    # request must carry the X-Quipclipper header, which a cross-site page can't
    # set without triggering a preflight this API never grants.
    @app.post("/api/search/folder/index")
    def folder_index(
        path: str = Query(...),
        force: bool = Query(False),
    ) -> StreamingResponse:
        """Index (extract + cache) subtitles for every video under a folder.

        With ``force=true``, clears the existing cache for the folder first so
        subtitles are re-extracted even if they were already cached — use this
        when the underlying subtitle files have changed.
        """
        videos, skipped = _folder_videos(path, cap=INDEX_CAP)
        if skipped:
            raise HTTPException(
                status_code=400,
                detail=f"Folder has more than {INDEX_CAP} videos — too large to index in one request.",
            )
        if force:
            folder = _resolve(path)
            sub_cache.clear_under(folder)

        def generate():
            total = len(videos)
            done = 0
            try:
                for v in videos:
                    if not sub_cache.is_cached(v) and not sub_cache.has_no_subs(v):
                        try:
                            sub_cache.resolve(v)
                            index_changes.forget(v)
                        except (ValueError, FileNotFoundError, RuntimeError):
                            # Nothing to index (no sidecar, no text track)? Remember it,
                            # so the folder stops offering to index this file.
                            if sub_cache.mark_no_subs(v):
                                index_changes.forget(v)
                    done += 1
                    yield f"{done}/{total} {v.name}\n"
            finally:
                index_changes.flush()

        return StreamingResponse(generate(), media_type="text/plain")

    # --- library index (M20) --------------------------------------------------------
    # Every file under the library roots, or under one folder of any size, as one
    # background job at idle priority (quipclipper_web.lib_index). Only these endpoints
    # start it: no timer, no startup or scan hook (as D-037 holds for transcription).

    def _lib_job():
        """The queued or running library index job, if any."""
        job = lib_jobs.get(lib_active[0] or "")
        return job if job is not None and job.status.value in ("queued", "running") else None

    # Query-string-only POST like /api/search/folder/index (same CSRF guard).
    @app.post("/api/search/library/index")
    def library_index(path: str | None = Query(None)) -> dict:
        """Start indexing every file under the library roots (or ``path``) that isn't
        indexed yet: one file at a time at idle priority, skipping what is indexed and
        current or known to have no subtitles. One job at a time: while one runs this
        returns it (``existing``)."""
        if path:
            folders = [_resolve(path)]
            if not folders[0].is_dir():
                raise HTTPException(status_code=400, detail=f"Not a directory: {path}")
        else:
            folders = _lib_roots()
        paths = [str(f) for f in folders]

        def scan(job):
            def tick(walked: int, files: int) -> None:
                if job.cancel_requested:
                    raise Cancelled
                job.progress = f"Scanning: {walked} folders, {files} files so far…"

            stored = transcripts.names() if stt_on else {}
            found, _, _ = _media_under(folders, stored, tick=tick)
            cached = sub_cache.entry_names()
            for c, own in found:
                yield c, sub_cache.index_state(c, siblings=own, names=cached)

        def index(f: Path) -> str:
            try:
                sub_cache.resolve(f)
                index_changes.forget(f)
                return "indexed"
            except (ValueError, FileNotFoundError, RuntimeError):
                # Nothing to index (no sidecar, no text track)? Remember it, so no run
                # tries it again; a file it can't tell about stays for the next run.
                if sub_cache.mark_no_subs(f):
                    index_changes.forget(f)
                    return "no_subs"
                raise

        def run_job(job) -> list[Path]:
            def hooks() -> None:
                # Files the re-index hooks queued meanwhile go first (same thread, same
                # priority), so a hook never waits for the whole run.
                file_index.drain(file_queue, _reindex_one, take=file_queue.take_nowait,
                                 cancelled=lambda: job.cancel_requested, put_back=True)
                # The re-index job queued behind this run would only find the queue
                # empty, days later; meanwhile it reads as pending in Jobs and the views.
                idle = file_queue.release()
                if idle is not None and lib_jobs.cancel(idle):
                    lib_jobs.cancel(idle)  # and out of Jobs: it never had work

            try:
                lib_index.run(job, paths=paths, scan=lambda: scan(job), index=index, log=lib_log,
                              whole_library=not path, between=hooks,
                              stopping=lambda: stopping[0])
            finally:
                index_changes.flush()
            return []

        with lib_submit:
            prev = _lib_job()
            if prev is not None:
                return {"job_id": prev.id, "existing": True,
                        "paths": (prev.result or {}).get("paths", [])}
            what = folders[0].name if path else "whole library"
            job = lib_jobs.submit(run_job, label=f"Index subtitles: {what} (low priority)",
                                  cancellable=True)
            lib_active[0] = job.id
        return {"job_id": job.id, "existing": False, "paths": paths}

    @app.get("/api/search/library/index")
    def library_index_status() -> dict:
        """The running library index job (``job``, or null) and the last run's summary
        (``last``: kept across restarts; "interrupted" if a restart cut it short)."""
        job = _lib_job()
        return {"job": _job_dict(job) if job is not None else None,
                "last": lib_index.last_run(lib_log, live=job is not None)}

    # --- re-index hooks (M20) ---------------------------------------------------------
    # Bazarr, Sonarr, Radarr (or anything) say a file's subtitles changed; the file is
    # queued and re-indexed by one job at idle priority (quipclipper_web.file_index).
    # Callers are machines: an API token, or a hook token that opens nothing else.

    def _reindex_one(f: Path, force: bool) -> str:
        """Bring one file's index entry up to date: "indexed", "current" (it already
        was), "no_subs" or "missing". Runs inside at_idle_priority."""
        outcome = _reindex_state(f, force)
        index_changes.forget(f)
        return outcome

    def _reindex_state(f: Path, force: bool) -> str:
        if not f.is_file():
            return "missing"
        if not force:  # forced: read again, the old entry kept until the new one lands
            state = sub_cache.index_state(f)
            if state == "indexed":
                return "current"
            if state == "no_subs":
                return "no_subs"
        try:
            if force:
                sub_cache.resolve(f, fresh=True)
            else:
                sub_cache.resolve(f)
            return "indexed"
        except (ValueError, FileNotFoundError, RuntimeError):
            if sub_cache.mark_no_subs(f):
                return "no_subs"
            raise

    def _file_job(job) -> list[Path]:
        done = [0]

        def progress(name: str) -> None:
            done[0] += 1
            job.progress = f"{done[0]} {name}"

        cancelled = lambda: job.cancel_requested  # noqa: E731
        counts: dict[str, int] = {}
        try:
            with at_idle_priority(cancelled):
                counts = file_index.drain(file_queue, _reindex_one, cancelled=cancelled,
                                          progress=progress,
                                          take=lambda: file_queue.take(job.id))
        except Cancelled:
            pass
        finally:
            if job.cancel_requested:
                file_queue.retire(job.id)
            index_changes.flush()
        job.result = counts
        tally = ", ".join(f"{n} {k}" for k, n in counts.items())
        if job.cancel_requested:
            job.progress = "Stopped: " + (tally or "nothing re-indexed yet")
        else:
            job.progress = "Done: " + (tally or "nothing to do")
        return []

    def _submit_file_job() -> str:
        return lib_jobs.submit(_file_job, label="Re-index changed subtitles (low priority)",
                               cancellable=True).id

    def _enqueue(files: list[Path], force: bool) -> tuple[str | None, list[bool]]:
        """Queue checked media files for the re-index job: its id, and per file whether
        it was newly queued."""
        if not files:
            return file_queue.status()["job_id"], []
        return file_queue.add([(m, force) for m in files], _submit_file_job)

    def _inside_roots(p: str) -> bool:
        try:
            library.resolve_within_roots(p, settings.media_roots)
            return True
        except library.PathNotAllowed:
            return False

    def _queue_files(raw: list[str], force: bool, limit: bool = True) -> dict:
        """Map, check and queue ``raw`` paths; one result row per path. A path already
        under a media root is this server's own (the web app's Re-index, the change
        list) and isn't mapped again: ``/data=/data/media`` would map it twice."""
        if limit and len(raw) > file_index.MAX_PATHS:
            raise HTTPException(status_code=400,
                                detail=f"At most {file_index.MAX_PATHS} paths at a time.")
        rows: list[dict] = []
        ok: list[tuple[int, Path]] = []
        for given in raw:
            row: dict = {"path": given}
            rows.append(row)
            given_s = given.strip()
            mapped = (given_s if not settings.path_map or "\0" in given_s
                      or _inside_roots(given_s)
                      else file_index.map_in(given_s, settings.path_map))
            if not mapped or "\0" in mapped:
                row["status"] = "invalid"
                continue
            try:
                p = library.resolve_within_roots(mapped, settings.media_roots)
            except library.PathNotAllowed:
                row.update(status="outside_roots", mapped=mapped)
                continue
            media = file_index.media_for(p)
            if media is not None and media != p:
                try:  # found beside the subtitle: a link there mustn't lead outside
                    media = library.resolve_within_roots(media, settings.media_roots)
                except library.PathNotAllowed:
                    row.update(status="outside_roots", mapped=mapped)
                    continue
            if media is None:
                row["status"] = ("no_media" if p.suffix.lower() in SUBTITLE_EXTS
                                 else "not_media")
            elif not media.is_file():
                row.update(status="not_found", media=str(media))
            else:
                row["media"] = str(media)
                ok.append((len(rows) - 1, media))
        job_id, added = _enqueue([m for _, m in ok], force)
        for (i, _), new in zip(ok, added):
            rows[i]["status"] = "queued" if new else "already_queued"
        # Files, not paths: Bazarr names an episode and its subtitle, one file between them.
        return {"job_id": job_id, "queued": len({m for _, m in ok}), "files": rows}

    # A JSON body (or a query-string POST): the same CSRF guard as every other POST.
    # The query is read by hand, so the CLI's registry sends the body (PA-5).
    @app.post("/api/index/file")
    async def index_files(request: Request) -> JSONResponse:
        """Re-index the files whose subtitles changed: each path a media file or a
        subtitle file (its media file is found next to it), in the caller's own paths
        (QC_PATH_MAP). A JSON body, ``{"path": [...], "force": bool}`` (``paths`` works
        too), or a Sonarr/Radarr webhook; or the same as ``?path=…&force=true``, which
        a proxy limits to ~8 KB (HTTP 414). Queued and deduplicated; one job
        re-indexes them at idle priority. 202 when at least one file was queued, 200
        for a webhook event with no file (Test), 422 when no path could be queued."""
        raw = list(request.query_params.getlist("path"))
        force = (request.query_params.get("force") or "").lower() in ("1", "true", "yes", "on")
        event = None
        if (request.headers.get("content-type") or "").lower().startswith("application/json"):
            try:
                body = await request.json()
            except ValueError:
                raise HTTPException(status_code=400, detail="Body is not JSON.")
            listed = next((body[k] for k in ("path", "paths") if isinstance(body, dict)
                           and isinstance(body.get(k), list)), None)
            if listed is not None:
                raw += [x for x in listed if isinstance(x, str)]
                force = force or body.get("force") is True
            else:
                found, event = file_index.webhook_paths(body)
                raw += found
        if not raw:
            if event is not None:
                return JSONResponse({"job_id": None, "queued": 0, "files": [],
                                     "event": event})
            raise HTTPException(status_code=400, detail="No path given.")
        result = await asyncio.to_thread(_queue_files, raw, force)
        if event is not None:
            result["event"] = event
        if not result["queued"]:
            return JSONResponse({"detail": "Nothing to re-index.", **result},
                                status_code=422)
        return JSONResponse(result, status_code=202)

    @app.get("/api/index/file")
    def index_files_status(request: Request) -> dict:
        """The re-index hooks' queue: the job draining it (or null), the files waiting,
        and the last results (status per file: indexed, current, no_subs, missing,
        failed or cancelled). ``token``: whether this request's token was accepted (a
        hook's check: with none set here, its POSTs would be refused)."""
        st = file_queue.status()
        job = lib_jobs.get(st["job_id"] or "")
        return {**st, "job": _job_dict(job) if job is not None else None,
                "token": getattr(request.state, "token", False)}

    # --- subtitles changed since indexing (M20) --------------------------------------
    # A check, never a re-index: these report what changed, and the one POST queues those
    # files for the re-index job above, only because someone pressed its button.

    @app.get("/api/index/changes")
    def index_changes_status() -> dict:
        """Files whose subtitles changed since they were indexed, as the hourly
        timestamp-only check and the views since found them: ``{changed, files,
        last_check, interval_s, checking}``."""
        return {**index_changes.summary(), "interval_s": index_check.interval,
                "checking": index_check.running}

    @app.post("/api/index/changes/reindex")
    def index_changes_reindex() -> JSONResponse:
        """Queue every file the checks found changed for the re-index job (idle
        priority, one at a time) and return at once, like POST /api/index/file. These
        are this server's own media paths (its checks walked the roots for them), so
        the request only checks each is under a root by name: no stat per file on the
        media disks. A file gone or moved since is the job's to find ("missing")."""
        files = index_changes.files()
        if not files:
            return JSONResponse({"job_id": None, "queued": 0, "files": []})
        roots = [os.path.normpath(str(r)) for r in settings.media_roots]
        rows: list[dict] = []
        ok: list[Path] = []
        for f in files:
            norm = os.path.normpath(f)
            if norm == f and any(norm == r or norm.startswith(r.rstrip(os.sep) + os.sep)
                                 for r in roots):
                rows.append({"path": f, "media": f})
                ok.append(Path(f))
            else:
                rows.append({"path": f, "status": "outside_roots"})
                index_changes.forget(f)
        job_id, added = _enqueue(ok, False)
        new = iter(added)
        for row in rows:
            if "media" in row:
                row["status"] = "queued" if next(new) else "already_queued"
        result = {"job_id": job_id, "queued": len(ok), "files": rows}
        return JSONResponse(result, status_code=202 if ok else 200)

    # Query-string-only POST like /api/search/folder/index — same CSRF surface,
    # covered by the same X-Quipclipper requirement in _security_gate.
    @app.post("/api/items/subtitles/reindex")
    def reindex_item_subtitles(path: str = Query(...)) -> dict:
        """Clear and re-extract the subtitle cache for a single video file.

        Use this when a file's subtitles have changed and you want the script
        and dialogue search to reflect the new subtitles immediately.
        """
        # YouTube ref: re-fetch the transcript from YouTube via yt-dlp.
        vid = youtube_items.parse_ref(path)
        if vid is not None:
            try:
                cues = yt_store.refresh_subs(vid, settings.subtitle_langs)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc))
            except RuntimeError as exc:
                raise HTTPException(status_code=502, detail=str(exc))
            return {"cleared": 1, "cues": cues}
        p = _resolve_any(path)
        if not p.is_file():
            raise HTTPException(status_code=404, detail=f"Not found: {path}")
        cleared = sub_cache.clear(p)
        try:
            cues = sub_cache.resolve(p)
        except (ValueError, FileNotFoundError, RuntimeError) as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        index_changes.forget(p)
        return {"cleared": cleared, "cues": len(cues)}

    # --- subtitle backfill from YouTube ----------------------------------------
    # For a *library* video that came from YouTube (a Pinchflat/yt-dlp download,
    # with the id in its name) but has no subtitles, fetch the transcript from
    # YouTube and write a .vtt sidecar next to it — making it dialogue-searchable.
    # Requires the folder to be writable (writableMediaRoots). yt-dlp is slow, so
    # these run as background jobs.

    # --- machine transcription (M21) -------------------------------------------------
    # Only ever on request (D-037): these endpoints are the only way a transcription
    # starts. Each runs as a job in stt_jobs (one at a time) at idle priority, with at
    # most 3 whisper threads (quipclipper.transcribe), and can be cancelled between or
    # during chunks; finished chunks are kept.

    def _stt_on() -> None:
        if not stt_on:
            raise HTTPException(status_code=404, detail=stt.OFF_REASON)

    def _stt_required() -> None:
        _stt_on()
        if not settings.whisper.available:
            raise HTTPException(status_code=503, detail="Transcription isn't set up on this "
                                "server: it needs whisper.cpp and a model (QC_WHISPER_MODEL).")

    def _library_media(path: str) -> Path:
        """A library file a transcript can belong to (not a YouTube item, an EPUB chapter
        or a clip)."""
        _stt_on()
        if youtube_items.parse_ref(path) is not None or epub_items.parse_ref(path)[1] is not None:
            raise HTTPException(status_code=422, detail="Transcription works on library "
                                "files, not YouTube items or EPUB chapters.")
        p = _resolve(path)
        if not p.is_file():
            raise HTTPException(status_code=404, detail=f"Not a file: {path}")
        return p

    def _clock(t: float) -> str:
        t = int(max(t, 0))
        h, rest = divmod(t, 3600)
        return f"{h}:{rest // 60:02d}:{rest % 60:02d}" if h else f"{rest // 60}:{rest % 60:02d}"

    def _transcribe(job, p: Path, mode: str, *, start=None, end=None, model: str,
                    audio: int = 0, redo: bool = False, prefix: str = "") -> dict:
        """Transcribe what ``mode`` asks of one file; the job's progress follows it. Returns
        the job's result (also used by the folder job). A file not indexed yet has its
        subtitles extracted first, at idle priority as the folder index does (BE-4);
        Cancel stops that with :class:`Cancelled`."""
        try:
            with at_idle_priority(lambda: job.cancel_requested):
                real = sub_cache.resolve(p)
        except (ValueError, FileNotFoundError, RuntimeError):
            real = []
        duration = media.probe_duration(p)
        have = transcripts.load(p)
        ranges = stt.plan_ranges(mode, real=real, duration=duration,
                                 done=have.ranges if have else [], start=start, end=end,
                                 redo=redo)
        result = {"path": str(p), "mode": mode, "model": model,
                  "transcribed": [list(r) for r in ranges]}
        if not ranges:
            result.update(new_lines=0, note="Nothing new to transcribe.",
                          transcript=have.summary() if have else None)
            return result

        def progress(done: float, total: float) -> None:
            job.progress = f"{prefix}{_clock(done)} of {_clock(total)} transcribed"

        t = None
        try:
            t = stt.run(transcripts, p, ranges, cfg=settings.whisper, model=model,
                        audio=audio, real=real, duration=duration, vad=stt.use_vad(mode),
                        second_look=stt.use_second_look(mode),
                        progress=progress, cancelled=lambda: job.cancel_requested)
        except stt.Cancelled:
            t = transcripts.load(p)
        heard = [c for c in (t.cues if t else []) if any(a <= c.start < b for a, b in ranges)]
        result.update(new_lines=len(heard), transcript=t.summary() if t else None)
        return result

    @app.post("/api/items/transcribe")
    def transcribe_item(req: TranscribeRequest) -> dict:
        """Start transcribing a range, a file's gaps or a whole file (a job; poll it)."""
        _stt_required()
        p = _library_media(req.path)
        mode = req.mode or ("range" if req.start is not None else "gaps")
        if mode == "range" and (req.start is None or req.end is None or req.end <= req.start):
            raise HTTPException(status_code=422, detail="A range needs a start before its end.")
        model = req.model or settings.whisper.default_model
        try:
            settings.whisper.model_path(model)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        key = json.dumps([str(p), mode, req.start, req.end, model, req.audio, req.redo])
        what = {"range": f"{_clock(req.start or 0)}–{_clock(req.end or 0)}",
                "gaps": "the gaps", "file": "the whole file"}[mode]

        def run_job(job) -> list[Path]:
            with contextlib.suppress(Cancelled):  # cancelled while extracting subtitles
                job.result = _transcribe(job, p, mode, start=req.start, end=req.end,
                                         model=model, audio=req.audio, redo=req.redo)
            return []

        with stt_submit:
            prev = _stt_existing(key)
            if prev is not None:
                return {"job_id": prev.id, "existing": True}
            job = stt_jobs.submit(run_job, label=f"Transcribe {what} of {p.name} ({model})",
                                  cancellable=True)
            active_stt[key] = job.id
        return {"job_id": job.id}

    @app.get("/api/items/transcript")
    def item_transcript(
        path: str = Query(...),
        fmt: str = Query("json", pattern="^(json|srt|vtt)$"),
    ) -> Response:
        """A file's machine transcript: what was transcribed and the lines heard (json), or
        the lines as SubRip / WebVTT to save."""
        p = _library_media(path)
        heard = stt.machine_cues(p, transcripts)
        if fmt in ("srt", "vtt"):
            name = stt.machine_sidecar_path(p, settings.whisper.language)
            body = stt.cues_to_srt(heard) if fmt == "srt" else stt.cues_to_vtt(heard)
            return Response(body, media_type="application/x-subrip" if fmt == "srt" else "text/vtt",
                            headers={"Content-Disposition": "attachment; filename*=UTF-8''"
                                     + quote(name.with_suffix("." + fmt).name)})
        return JSONResponse({"path": path, **_transcript_info(p),
                             "lines": [{"start": c.start, "end": c.end, "text": c.text,
                                        "source": c.source} for c in heard]})

    @app.delete("/api/items/transcript")
    def delete_item_transcript(path: str = Query(...)) -> dict:
        """Delete a file's stored transcript (a copy in the library stays)."""
        p = _library_media(path)
        return {"path": path, "deleted": transcripts.clear(p)}

    @app.post("/api/items/transcript/library")
    def copy_transcript_to_library(path: str = Query(...)) -> dict:
        """Copy a file's machine lines into the library as <name>.<lang>.whisper.srt next to
        it (writable folders only). From then on the captions and Jellyfin show them."""
        p = _library_media(path)
        t = transcripts.load(p)
        if t is None or not t.cues:
            raise HTTPException(status_code=404, detail="This file has no machine transcript "
                                "to copy: transcribe it first.")
        if not os.access(p.parent, os.W_OK):
            raise HTTPException(status_code=403, detail="That folder is read-only — add it to "
                                "writableMediaRoots to copy transcripts into it.")
        try:
            real = sub_cache.resolve(p)
        except (ValueError, FileNotFoundError, RuntimeError):
            real = []
        lines = stt.library_lines(real, t.cues)
        try:
            side = stt.write_sidecar(p, lines, t.language)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"Couldn't write the sidecar: {exc}")
        return {"path": path, "sidecar": side.name, "lines": len(lines)}

    @app.delete("/api/items/transcript/library")
    def remove_transcript_from_library(path: str = Query(...)) -> dict:
        """Remove the machine transcript sidecar(s) (….whisper.srt) next to a file."""
        p = _library_media(path)
        found = stt.find_machine_sidecars(p)
        if found and not os.access(p.parent, os.W_OK):
            raise HTTPException(status_code=403, detail="That folder is read-only.")
        removed = []
        for side in found:
            with contextlib.suppress(FileNotFoundError):
                side.unlink()
                removed.append(side.name)
        return {"path": path, "removed": removed}

    # Stored transcripts no file needs any more: the media was deleted (or, with stale,
    # replaced since). Never swept on a timer or a scan (D-037): listed here, and deleted
    # only by the POST below. A transcript under a root that's missing or empty (a disk
    # that isn't mounted) is never counted.
    @app.get("/api/transcripts/orphans")
    def transcript_orphans(stale: bool = Query(False)) -> dict:
        """The stored transcripts a clean would delete (nothing is deleted)."""
        _stt_on()
        found = transcripts.orphans(settings.media_roots, stale=stale)
        return {"count": len(found), "orphans": [o.to_json() for o in found[:200]]}

    @app.post("/api/transcripts/clean")
    def clean_transcripts(stale: bool = Query(False)) -> dict:
        """Delete the stored transcripts of media files that are gone (with ``stale``,
        also of files replaced since). Copies in the library are never touched."""
        _stt_on()
        removed = transcripts.clean(settings.media_roots, stale=stale)
        return {"count": len(removed), "removed": [o.to_json() for o in removed[:200]]}

    @app.post("/api/search/folder/transcribe")
    def transcribe_folder(req: FolderTranscribeRequest) -> dict:
        """Transcribe a folder's files one after another (a job; cancellable): whole files
        with no subtitles (``nosubs``), or the gaps in every subtitled file (``gaps``).
        Files already done are skipped. ``{job_id: null, candidates: 0}`` = nothing to do."""
        _stt_required()
        folder = _resolve(req.path)
        if not folder.is_dir():
            raise HTTPException(status_code=400, detail=f"Not a directory: {req.path}")
        model = req.model or settings.whisper.default_model
        try:
            settings.whisper.model_path(model)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        videos, skipped = _folder_videos(req.path, cap=INDEX_CAP)
        if skipped:
            raise HTTPException(status_code=400, detail=f"Folder has more than {INDEX_CAP} "
                                "files — too many to transcribe in one go; pick a subfolder.")
        bare: list[Path] = []
        if req.mode == "nosubs":
            seen = set(videos)
            for c in folder.rglob("*"):
                if c not in seen and _is_bare_audio(c):
                    bare.append(c)
                    if len(videos) + len(bare) > INDEX_CAP:
                        raise HTTPException(status_code=400, detail=f"Folder has more than "
                                            f"{INDEX_CAP} files — too many to transcribe in "
                                            "one go; pick a subfolder.")
            bare.sort(key=lambda c: c.name.lower())
        candidates = len(videos) + len(bare)
        if not candidates:
            return {"job_id": None, "candidates": 0}
        key = json.dumps(["folder", str(folder), req.mode, model])

        def has_subs(v: Path) -> bool | None:
            """Whether ``v`` has subtitles; None when that can't be told (then it's
            skipped: a whole-file run of a subtitled film costs hours)."""
            if sub_cache.has_no_subs(v):
                return False
            if req.mode == "nosubs" and not sub_cache.is_cached(v):
                # Only whether, not what: a probe, not a full extraction at normal priority.
                try:
                    return bool(find_sidecar(v, settings.subtitle_langs)) or any(
                        is_text_subtitle(t) for t in list_embedded_tracks(v))
                except (OSError, ValueError, RuntimeError):
                    return None
            try:
                return bool(sub_cache.resolve(v))
            except (ValueError, FileNotFoundError, RuntimeError):
                return False if sub_cache.mark_no_subs(v) else None

        def inside(v: Path) -> Path | None:
            try:
                return _resolve(str(v))
            except HTTPException:
                return None

        video_set = {r for r in map(inside, videos) if r is not None}

        def run_job(job) -> list[Path]:
            files = []
            todo = [*videos, *bare]
            # Telling a file's gaps needs its subtitle lines, and extracting them is the
            # folder index's work: it runs at the same idle priority (BE-4), and Cancel
            # stops an extraction in flight.
            try:
                with at_idle_priority(lambda: job.cancel_requested):
                    for i, v in enumerate(todo, 1):
                        if job.cancel_requested:
                            break
                        job.progress = f"{i}/{len(todo)} files: checking {v.name}"
                        one = {"name": v.name, "path": str(v), "new_lines": 0}
                        v = inside(v)  # keyed and checked as the file itself (a link may lead out)
                        if v is None:
                            continue
                        subbed = has_subs(v) if v in video_set else False
                        if subbed is None:
                            one["error"] = "Couldn't tell whether it has subtitles; skipped."
                            files.append(one)
                            continue
                        if (req.mode == "gaps") != subbed:
                            continue  # nosubs skips subtitled files; gaps skips the rest
                        try:
                            r = _transcribe(job, v, "gaps" if subbed else "file", model=model,
                                            prefix=f"{i}/{len(todo)} files, {v.name}: ")
                            one["new_lines"] = r.get("new_lines", 0)
                            if r.get("transcribed"):
                                files.append(one)
                        except (ValueError, RuntimeError, OSError) as exc:
                            one["error"] = str(exc)
                            files.append(one)
            except Cancelled:
                pass
            done = [f for f in files if not f.get("error")]
            job.result = {
                "path": str(folder), "mode": req.mode, "model": model, "files": files,
                "new_lines": sum(f["new_lines"] for f in done),
                "note": (f"Transcribed {len(done)} file{'' if len(done) == 1 else 's'}"
                         if files else "Nothing new to transcribe."),
            }
            return []

        what = "files without subtitles" if req.mode == "nosubs" else "the gaps in every file"
        with stt_submit:
            prev = _stt_existing(key)
            if prev is not None:
                return {"job_id": prev.id, "existing": True, "candidates": candidates}
            job = stt_jobs.submit(run_job, label=f"Transcribe {what} in {folder.name} ({model})",
                                  cancellable=True)
            active_stt[key] = job.id
        return {"job_id": job.id, "candidates": candidates}

    @app.post("/api/media/fetch-subs")
    def fetch_subs_file(path: str = Query(...)) -> dict:
        """Backfill one library video's subtitles from YouTube (async job)."""
        p = _resolve(path)
        if not p.is_file():
            raise HTTPException(status_code=404, detail=f"Not found: {path}")
        if youtube_items.youtube_id_for_file(p) is None:
            raise HTTPException(
                status_code=400, detail="No YouTube id in the file name or a sibling .info.json.")
        if not os.access(p.parent, os.W_OK):
            raise HTTPException(
                status_code=400,
                detail="That folder is read-only — add it to writableMediaRoots to backfill.")

        def _do(job) -> list:
            res = youtube_items.fetch_subs_for_file(p, settings.subtitle_langs, force=True)
            if res["status"] == "ok":
                sub_cache.clear(p)  # drop any "no subs" cache so the sidecar is picked up
            job.progress = res["status"]
            return []

        job = yt_jobs.submit(_do, label=f"subtitles: {p.name}")
        return {"job_id": job.id, "status": job.status.value}

    @app.post("/api/search/folder/fetch-subs")
    def fetch_subs_folder(path: str = Query(...)) -> dict:
        """Backfill subtitles for every YouTube-sourced video in a folder that
        lacks them (async job with progress; cancellable). Skips files that
        already have subtitles, so it's safe to re-run."""
        folder = _resolve(path)
        if not folder.is_dir():
            raise HTTPException(status_code=400, detail=f"Not a directory: {path}")
        if not os.access(folder, os.W_OK):
            raise HTTPException(
                status_code=400,
                detail="That folder is read-only — add it to writableMediaRoots to backfill.")
        videos, _ = _folder_videos(path)
        candidates = [
            v for v in videos
            if youtube_items.youtube_id_for_file(v) is not None and find_sidecar(v) is None
        ]
        if not candidates:
            return {"job_id": None, "status": "done", "candidates": 0}

        def _do(job) -> list:
            fetched = failed = 0
            total = len(candidates)
            for i, v in enumerate(candidates):
                if job.cancel_requested:
                    break
                job.progress = f"{i}/{total} · {fetched} fetched"
                try:
                    res = youtube_items.fetch_subs_for_file(v, settings.subtitle_langs)
                    if res["status"] == "ok":
                        fetched += 1
                        sub_cache.clear(v)
                except Exception:  # one bad video shouldn't abort the run
                    failed += 1
            done = "cancelled" if job.cancel_requested else "done"
            job.progress = f"{done} · {fetched} fetched, {failed} failed of {total}"
            return []

        job = yt_jobs.submit(_do, label=f"backfill subtitles: {folder.name}",
                             cancellable=True)
        return {"job_id": job.id, "status": job.status.value, "candidates": len(candidates)}

    # --- media streaming -------------------------------------------------------

    @app.get("/api/media")
    def stream(request: Request, path: str = Query(...)):
        """Serve a source file with HTTP range support (best-effort preview).

        Starlette's FileResponse handles Range requests (206), so the browser can
        seek. Whether it actually decodes depends on the codec/container.
        """
        # YouTube refs: a downloaded video serves like any local file (Range
        # support → native seeking); without a download there's nothing to
        # serve raw — the player must use the transcode endpoint (the frontend
        # picks the right one; this is defensive).
        yt_vid_media = youtube_items.parse_ref(path)
        if yt_vid_media is not None:
            local = yt_store.video_path(yt_vid_media)
            if local is not None:
                return FileResponse(local, media_type="video/mp4")
            raise HTTPException(
                status_code=400,
                detail="YouTube items stream via /api/media/transcode until downloaded.",
            )
        # EPUB segment: stream the chapter audio straight from the zip (Range-aware,
        # no copy on disk).
        epub_ref, seg = epub_items.parse_ref(path)
        if seg is not None:
            ep = _resolve(epub_ref)
            try:
                return epub_items.segment_audio_response(ep, seg, request.headers.get("range"))
            except (IndexError, ValueError, FileNotFoundError) as exc:
                raise HTTPException(status_code=404, detail=str(exc))
        p = _resolve_any(path)
        if not p.is_file():
            raise HTTPException(status_code=404, detail=f"Not found: {path}")
        mime = _BROWSER_MIME.get(p.suffix.lower(), "application/octet-stream")
        return FileResponse(p, media_type=mime)

    @app.get("/api/media/keyframe")
    def keyframe_before(
        path: str = Query(...),
        time: float = Query(..., ge=0),
    ) -> dict:
        """Return the PTS of the last video keyframe at or before *time*.

        Used by the frontend to know where ffmpeg's ``-ss`` (input seek
        with ``-c:v copy``) will actually land, so subtitle offsets can be
        computed accurately.
        """
        # YouTube: probe the local file when downloaded (accurate offsets);
        # otherwise echo the requested time — subtitle sync after a mid-stream
        # seek can then drift by up to one GOP (a few seconds).
        yt_vid_kf = youtube_items.parse_ref(path)
        if yt_vid_kf is not None:
            local = yt_store.video_path(yt_vid_kf)
            if local is None:
                return {"requested": time, "actual": time}
            return {"requested": time, "actual": media.probe_keyframe_before(local, time)}
        p = _resolve_any(path)
        if not p.is_file():
            raise HTTPException(status_code=404, detail=f"Not found: {path}")
        actual = media.probe_keyframe_before(p, time)
        return {"requested": time, "actual": actual}

    @app.get("/api/media/transcode")
    async def transcode(
        path: str = Query(...),
        start: float | None = Query(None, ge=0),
        end: float | None = Query(None, ge=0),
        audio: int | None = Query(None, ge=0),
        chan: str | None = Query(None),
        venc: int = Query(0),
    ) -> StreamingResponse:
        """Remux with audio transcode to browser-friendly Matroska (desktop).

        Video is stream-copied (no re-encode) by default, audio is transcoded to
        Opus. ``venc=1`` instead **re-encodes the video to H.264** for sources the
        browser can't decode (HEVC on Firefox, MPEG-4 ASP/XviD, MPEG-2, VC1, …) —
        B20/B22 — using the iGPU (`h264_vaapi`/Quick Sync) when available, else
        software `libx264`. Supports optional start/end params for segment-based
        seeking — ffmpeg's -ss is placed before -i for fast seek. (iOS uses the
        HLS path instead; Safari can't demux Matroska/Opus.)

        ``audio`` selects a specific audio stream (a:N); ``chan`` plays only one
        channel group (front/center/lfe/side/back/surround) via a ``pan`` filter
        — both for the stream selector (B17).
        """
        async def _ffmpeg_stream(cmd: list[str]) -> StreamingResponse:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )

            async def _stream():
                assert proc.stdout is not None
                try:
                    while True:
                        chunk = await proc.stdout.read(64 * 1024)
                        if not chunk:
                            break
                        yield chunk
                finally:
                    proc.kill()
                    await proc.wait()

            return StreamingResponse(
                _stream(),
                media_type="video/webm",
                headers={"Accept-Ranges": "none"},
            )

        # YouTube ref: same remux, but the input is 1-2 resolved googlevideo
        # URLs (h264/mp4 + m4a per FORMAT_EXPR) instead of a local file. Video
        # is always h264 so it's stream-copied (`venc` ignored); audio → Opus
        # like every other transcode. `audio`/`chan` don't apply (stereo source).
        # A downloaded video takes the normal local-file path below instead
        # (its state-dir location is handed over directly — it's not a media
        # root, so _resolve_any wouldn't admit it).
        vid = youtube_items.parse_ref(path)
        yt_local = yt_store.video_path(vid) if vid is not None else None
        if vid is not None and yt_local is None:
            try:
                meta = yt_store.meta(vid)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc))
            try:
                video_url, audio_url = await asyncio.to_thread(
                    youtube_items.resolve_stream_urls, meta["webpage_url"])
            except RuntimeError as exc:
                raise HTTPException(status_code=502, detail=str(exc))
            cmd = ["ffmpeg"]
            reconnect = ["-reconnect", "1", "-reconnect_streamed", "1",
                         "-reconnect_delay_max", "5"]
            seek = ["-ss", str(start)] if start is not None else []
            cmd += reconnect + seek + ["-i", video_url]
            if audio_url:
                cmd += reconnect + seek + ["-i", audio_url]
            if end is not None:
                cmd += ["-to", str(end - (start or 0))]
            if audio_url:
                cmd += ["-map", "0:v:0", "-map", "1:a:0"]
            cmd += ["-c:v", "copy", "-c:a", "libopus", "-b:a", "192k", "-ac", "2"]
            if meta.get("duration"):
                cmd += ["-metadata", f"DURATION={meta['duration']}"]
            cmd += ["-f", "matroska", "-"]
            return await _ffmpeg_stream(cmd)

        p = yt_local if yt_local is not None else _resolve_any(path)
        if not p.is_file():
            raise HTTPException(status_code=404, detail=f"Not found: {path}")

        duration = media.probe_duration(p)
        # Which audio stream the pan (if any) operates on; default a:0.
        aidx = audio if audio is not None else (0 if chan and chan != "whole" else None)
        pan = _live_pan_filter(p, aidx or 0, chan) if chan and chan != "whole" else None

        hw = bool(venc) and _vaapi_h264_available()

        cmd = ["ffmpeg"]
        if hw:
            cmd += ["-vaapi_device", _VAAPI_DEVICE]
        if start is not None:
            cmd += ["-ss", str(start)]
        cmd += ["-i", str(p)]
        if end is not None:
            cmd += ["-to", str(end - (start or 0))]
        if aidx is not None:
            # Optional selectors (trailing `?`) — same reasoning as the HLS path:
            # a source with no audio, or a selection past the streams it has,
            # plays as video-only instead of failing the whole stream.
            cmd += ["-map", "0:v:0?", "-map", f"0:a:{aidx}?"]
        # Video: stream-copy by default; re-encode to H.264 when the browser
        # can't decode the source codec (B20/B22). HW (VAAPI) when available.
        if venc:
            if hw:
                cmd += ["-vf", "format=nv12,hwupload", "-c:v", "h264_vaapi", "-qp", "24"]
            else:
                cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"]
        else:
            cmd += ["-c:v", "copy"]
        if pan:
            cmd += ["-filter:a", pan]
        cmd += ["-c:a", "libopus", "-b:a", "192k"]
        if not pan:
            cmd += ["-ac", "2"]
        if duration:
            cmd += ["-metadata", f"DURATION={duration}"]
        cmd += ["-f", "matroska", "-"]

        return await _ffmpeg_stream(cmd)

    # --- HLS (iOS playback) ---------------------------------------------------
    # iOS Safari can't play progressive Matroska/Opus, raw .mkv, or a chunked
    # MP4 (it needs HTTP range support a live ffmpeg pipe can't give). HLS is its
    # native streaming format: ffmpeg segments the file (video stream-copied,
    # audio -> AAC) as fMP4 into a temp dir under the state dir; we serve the
    # playlist + segments and iOS handles seeking natively.
    _hls_root = settings.state_dir / "hls"
    # A sentinel value is registered here *before* the first await when starting
    # a transcode, so two concurrent requests for the same token can't both
    # launch ffmpeg into the same dir (C2). A None entry means "starting".
    _hls_procs: dict[str, asyncio.subprocess.Process | None] = {}
    _hls_lock = asyncio.Lock()
    _HLS_FILE_RE = re.compile(r"^(init\.mp4|seg\d+\.m4s)$")

    def _hls_token(
        p: Path, venc: int = 0, audio: int | None = None, chan: str | None = None
    ) -> str:
        # Include the source mtime so replacing the file (same path) yields a new
        # token and fresh segments — otherwise stale segments are served (C3).
        # `audio`/`chan` are part of the key too: picking a different audio stream
        # or channel subset (B17) produces different segments, so it must not
        # reuse — or overwrite — the dir another selection is streaming from.
        try:
            mtime = p.stat().st_mtime
        except OSError:
            mtime = 0.0
        key = f"{p}:{mtime}:{venc}:{audio}:{chan}"
        return hashlib.sha256(key.encode()).hexdigest()[:16]

    def _hls_complete(playlist: Path) -> bool:
        """True only if the transcode finished — the playlist has EXT-X-ENDLIST.

        A playlist that merely *exists* may be a truncated leftover from a crash
        or restart; reusing it would serve a partial video forever (C1)."""
        try:
            return "#EXT-X-ENDLIST" in playlist.read_text(encoding="utf-8")
        except OSError:
            return False

    def _hls_error(d: Path, limit: int = 300) -> str:
        """ffmpeg's last stderr line for this token — the actual reason a
        transcode refused to start, rather than a bare timeout. Empty when the
        log is missing or says nothing useful."""
        try:
            text = (d / "ffmpeg.log").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        return lines[-1][:limit] if lines else ""

    def _touch_hls(d: Path) -> None:
        """Bump a token dir's mtime so an actively-watched session isn't pruned.

        ffmpeg races ahead and stops writing once the (fast) transcode finishes,
        so the dir mtime would otherwise freeze and _prune_hls could delete a dir
        the viewer is still streaming from (C4). Called on each playlist/segment
        access to keep the last-access time current."""
        with contextlib.suppress(OSError):
            os.utime(d, None)

    def _reap_hls() -> None:
        """Drop registry entries whose ffmpeg has exited, so _hls_procs doesn't
        grow without bound over a long-running server (R4)."""
        for tok, pr in list(_hls_procs.items()):
            if pr is not None and pr.returncode is not None:
                del _hls_procs[tok]

    def _prune_hls(max_age: float = 2 * 3600) -> None:
        if not _hls_root.is_dir():
            return
        now = time.time()
        for d in _hls_root.iterdir():
            with contextlib.suppress(OSError):
                if d.is_dir() and now - d.stat().st_mtime > max_age:
                    shutil.rmtree(d, ignore_errors=True)

    @app.get("/api/media/hls")
    async def hls_playlist(
        path: str = Query(...),
        venc: int = Query(0),
        audio: int | None = Query(None, ge=0),
        chan: str | None = Query(None),
    ) -> Response:
        """Start (or reuse) an on-the-fly HLS transcode; return its playlist.

        Segment/init URIs are rewritten to absolute API paths so the playlist's
        query-string URL doesn't break relative resolution. The player reloads
        this URL to pick up new segments; ffmpeg races ahead of real time and
        appends EXT-X-ENDLIST when done, enabling full seeking. Video is
        stream-copied (iOS decodes H.264/HEVC); ``venc=1`` re-encodes it to H.264
        for codecs the browser can't decode (XviD/MPEG-4 ASP, MPEG-2, VC1, …) —
        B20/B22, hardware via Quick Sync when available.

        Desktop uses this path too (via hls.js): segments are individually
        fetchable and range-served, so a browser that drops its connection
        resumes instead of re-opening — which is what /api/media/transcode, an
        unbounded unseekable pipe, cannot do.

        ``audio`` selects a specific audio stream (a:N); ``chan`` plays only one
        channel group (front/center/lfe/side/back/surround) via a ``pan`` filter
        — the same stream-selector options the transcode path takes (B17). Both
        are part of the token, so each selection segments into its own dir.
        """
        # A downloaded YouTube video segments like any local file (its state-dir
        # location is handed over directly); un-downloaded yt refs have no file
        # to segment — the UI uses the official embed on iOS in that case.
        yt_vid_hls = youtube_items.parse_ref(path)
        if yt_vid_hls is not None:
            p = yt_store.video_path(yt_vid_hls)
            if p is None:
                raise HTTPException(
                    status_code=400,
                    detail="HLS needs a downloaded video — use the official player instead.",
                )
        else:
            p = _resolve_any(path)
        if not p.is_file():
            raise HTTPException(status_code=404, detail=f"Not found: {path}")
        token = _hls_token(p, venc, audio, chan)
        d = _hls_root / token
        playlist = d / "index.m3u8"
        hw = bool(venc) and _vaapi_h264_available()
        # Which audio stream the pan (if any) operates on; default a:0 — mirrors
        # the transcode endpoint so both paths resolve a selection the same way.
        aidx = audio if audio is not None else (0 if chan and chan != "whole" else None)
        pan = _live_pan_filter(p, aidx or 0, chan) if chan and chan != "whole" else None

        # Serialize the check→rebuild→register sequence: the registry write used
        # to happen after an await, so two concurrent requests both passed the
        # "not running" check and each launched ffmpeg into the same dir (C2).
        async with _hls_lock:
            _reap_hls()
            proc = _hls_procs.get(token)
            starting = token in _hls_procs and proc is None  # sentinel: mid-start
            running = proc is not None and proc.returncode is None
            # Reuse an existing transcode only if it's still running/starting OR
            # it completed (playlist has EXT-X-ENDLIST). A merely-existing playlist
            # may be a truncated crash leftover (C1) — rebuild it.
            reusable = running or starting or (playlist.exists() and _hls_complete(playlist))
            if not reusable:
                _prune_hls()
                shutil.rmtree(d, ignore_errors=True)
                d.mkdir(parents=True, exist_ok=True)
                _hls_procs[token] = None  # sentinel before the first await (C2)
                cmd = ["ffmpeg", "-nostdin"]
                if hw:
                    cmd += ["-vaapi_device", _VAAPI_DEVICE]
                cmd += ["-i", str(p)]
                if aidx is not None:
                    # Both selectors are optional (trailing `?`): a source with no
                    # audio — or a stale selection pointing past the streams it
                    # has — segments as video-only instead of failing the whole
                    # transcode, which used to surface as a bare timeout.
                    cmd += ["-map", "0:v:0?", "-map", f"0:a:{aidx}?"]
                if venc:
                    cmd += (["-vf", "format=nv12,hwupload", "-c:v", "h264_vaapi", "-qp", "24"]
                            if hw else ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"])
                else:
                    cmd += ["-c:v", "copy"]
                if pan:
                    cmd += ["-filter:a", pan]
                cmd += ["-c:a", "aac", "-b:a", "192k"]
                if not pan:  # the pan filter already sets the output layout
                    cmd += ["-ac", "2"]
                cmd += [
                    "-f", "hls", "-hls_time", "6", "-hls_list_size", "0",
                    "-hls_playlist_type", "event", "-hls_flags", "independent_segments",
                    "-hls_segment_type", "fmp4", "-hls_fmp4_init_filename", "init.mp4",
                    "-hls_segment_filename", str(d / "seg%05d.m4s"),
                    str(playlist),
                ]
                # Keep stderr: when ffmpeg refuses the command it is the only
                # record of why, and the fail-fast branch below reports it.
                with open(d / "ffmpeg.log", "wb") as log:
                    _hls_procs[token] = await asyncio.create_subprocess_exec(
                        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=log,
                    )

        def _ready() -> bool:
            return playlist.exists() and any(d.glob("seg*.m4s"))

        deadline = time.monotonic() + _HLS_START_TIMEOUT
        while True:
            if _ready():
                break
            # Fail fast: ffmpeg exiting before it wrote a playlist means the
            # command was rejected outright (unreadable source, a selection it
            # can't satisfy). Nothing will appear later, so don't sit out the
            # whole timeout — that turned an instant error into a 120s stall.
            pr = _hls_procs.get(token)
            if pr is not None and pr.returncode is not None:
                if _ready():
                    break  # it finished between the two checks
                why = _hls_error(d)
                raise HTTPException(
                    status_code=502,
                    detail=f"HLS transcode failed to start (ffmpeg exited "
                           f"{pr.returncode})." + (f" {why}" if why else ""),
                )
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(_HLS_START_POLL)
        if not _ready():
            raise HTTPException(
                status_code=504,
                detail=f"HLS transcode did not start within {_HLS_START_TIMEOUT:.0f}s.",
            )

        _touch_hls(d)  # keep an actively-watched session from being pruned (C4)
        base = f"/api/media/hls/{token}/"
        lines = []
        for line in playlist.read_text(encoding="utf-8").splitlines():
            if line.startswith("#EXT-X-MAP:"):
                line = line.replace('URI="init.mp4"', f'URI="{base}init.mp4"')
            elif _HLS_FILE_RE.match(line):
                line = base + line
            lines.append(line)
        return Response(
            content="\n".join(lines) + "\n",
            media_type="application/vnd.apple.mpegurl",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/media/hls/{token}/{name}")
    async def hls_file(token: str, name: str) -> FileResponse:
        if not re.fullmatch(r"[0-9a-f]{16}", token) or not _HLS_FILE_RE.match(name):
            raise HTTPException(status_code=404, detail="Not found")
        d = _hls_root / token
        f = d / name
        for _ in range(50):  # a segment may be requested just before ffmpeg flushes it
            if f.is_file():
                break
            await asyncio.sleep(0.1)
        if not f.is_file():
            raise HTTPException(status_code=404, detail="Segment not ready")
        _touch_hls(d)  # mark the session active so _prune_hls leaves it alone (C4)
        return FileResponse(f, media_type="video/mp4")

    # --- clipping & jobs -------------------------------------------------------

    def _write_sidecars(paths: list[Path], meta: dict) -> None:
        """The metadata sidecar of each file a cut wrote into the clips library (M18).
        A failure is logged, never the cut's: the clip is already there."""
        clips_root = settings.clips_dir.resolve()
        for p in paths:
            try:
                if not Path(p).resolve().is_relative_to(clips_root):
                    continue
                info = clipmeta.probe(Path(p))
                split = {"split": True} if len(paths) > 1 else {}
                clipmeta.write(Path(p), {**meta, **info, **split,
                                         "size": Path(p).stat().st_size})
            except Exception as exc:  # noqa: BLE001
                clipmeta.log.warning("clip metadata for %s: %s", p, exc)

    def _plan_clip(req: ClipRequest, out_dir: Path | None = None,
                   meta_extra: dict | None = None):
        """Validate a clip request and return ``(label, do_cut, title)`` without running it.

        ``title`` is the source's display name (the movie or show, see clean_title).

        ``do_cut(job)`` performs the cut and returns the written paths. With
        ``out_dir`` the clip is written there (flat, by the template's file name)
        instead of the clips library: the LeadStinger set job cuts into a staging
        folder and zips the result. Raises HTTPException for a bad request.

        A clip written into the clips library gets its metadata sidecar (clipmeta);
        ``meta_extra`` overrides its fields (Send to LeadStinger knows the real line).
        """
        # An EPUB segment clips from the extracted chapter audio (always audio);
        # `name_source` drives the naming template (book/chapter, not the cache
        # file), while `video` is the actual media handed to ffmpeg.
        epub_ref, seg = epub_items.parse_ref(req.path)
        name_source: Path | None = None
        epub_temp: Path | None = None
        # A YouTube ref cuts straight from resolved googlevideo stream URLs —
        # ffmpeg reads them over HTTPS, so Exact/lossless semantics match local
        # files with no temp download. (If URL cutting ever proves too fragile —
        # e.g. gated formats ffmpeg can't fetch — the v2 fallback is a yt-dlp
        # temp download fed through this same pipeline, like the EPUB temp.)
        yt_vid = youtube_items.parse_ref(req.path)
        yt_meta: dict | None = None
        yt_aux: str | None = None
        yt_streaming = False  # True when cutting from stream URLs (no local file)
        if yt_vid is not None:
            try:
                yt_meta = yt_store.meta(yt_vid)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc))
            name_source = youtube_items.clip_name_source(yt_meta)
            local = yt_store.video_path(yt_vid)
            if local is not None:
                # Downloaded: cut from the local file — the full pipeline applies
                # (mkvmerge, split_channels, sub-count probes), like any library file.
                video = local
            else:
                yt_streaming = True
                if req.split_channels:
                    raise HTTPException(
                        status_code=400,
                        detail="split_channels isn't available for YouTube sources (stereo audio).",
                    )
                if req.backend == "mkvmerge":
                    raise HTTPException(
                        status_code=400,
                        detail="mkvmerge can't cut from a stream URL; use the ffmpeg backend.",
                    )
                req.backend = "ffmpeg"  # auto would route lossless audio to mkvmerge
                try:
                    video_url, audio_url = youtube_items.resolve_stream_urls(yt_meta["webpage_url"])
                except RuntimeError as exc:
                    raise HTTPException(status_code=502, detail=str(exc))
                if req.kind == "audio":
                    # Cut audio from the audio stream alone (or the muxed progressive).
                    video = audio_url or video_url
                else:
                    video = video_url
                    # gif drops audio entirely; only a video cut needs the DASH pair.
                    yt_aux = audio_url if req.kind == "video" else None
        elif seg is not None:
            ep = _resolve(epub_ref)
            try:
                # ffmpeg/mkvmerge need a seekable file; extract a temp now and
                # delete it as soon as the cut finishes (nothing persists).
                video = epub_temp = epub_items.extract_segment_temp(ep, seg)
                name_source = epub_items.segment_clip_name_source(ep, seg)
            except (IndexError, ValueError) as exc:
                raise HTTPException(status_code=404, detail=str(exc))
            req.kind = "audio"
            req.embed_subs = False
        else:
            video = _resolve_any(req.path)
        # If anything fails before the job is submitted, the extracted EPUB
        # temp (if any) would otherwise leak — do_cut's finally only runs once
        # the job is queued. Wrap the pre-submit path so an early 4xx cleans it
        # up; once submitted, ownership passes to do_cut's finally (R1).
        _submitted = False
        try:
            # URL sources (un-downloaded YouTube) have no local file to stat or sniff.
            if not yt_streaming:
                if not video.is_file():
                    raise HTTPException(status_code=404, detail=f"Not found: {req.path}")

                # An audio-only source (podcast/audiobook) can't produce a video clip;
                # coerce a stray video request to audio so batch export and the CLI don't
                # ask ffmpeg to map a non-existent video stream.
                if req.kind == "video" and video.suffix.lower() in AUDIO_EXTS:
                    req.kind = "audio"
                    req.embed_subs = False

            # Determine the clip range: explicit start (with optional end) or
            # search-based. Omitting end with start set means "to the end of the
            # file" — used by batch export to re-process a whole clip.
            cue_text = ""  # matched dialogue, for the {cue} naming token
            if req.start is not None:
                # The frontend passes the selected cue's text so {cue} still works
                # for cue-range clips (dialogue-search hits and manual Start/End).
                cue_text = req.cue_text or ""
                end = req.end
                if end is None:
                    # No local file to probe for a YouTube ref — the stored
                    # metadata knows the duration.
                    end = yt_meta.get("duration") if yt_meta else media.probe_duration(video)
                    if not end:
                        raise HTTPException(
                            status_code=400,
                            detail="Could not determine file duration for a whole-file clip.",
                        )
                rng = ClipRange(start=max(0.0, req.start - req.before), end=end + req.after)
                line_start, line_end = req.start, end
                label = f"{req.kind} clip {format_timestamp(req.start)}–{format_timestamp(end)}"
            elif req.query:
                try:
                    # YouTube: search the stored transcript; else use the cache —
                    # extraction is slow and the same cues were very likely
                    # already extracted when the user searched in the UI.
                    # The same lines GET /api/search reads by default.
                    cues = (yt_store.cues(yt_vid) if yt_vid is not None
                            else _file_cues(video, req.track, _machine_mode("include")))
                except (KeyError, ValueError, FileNotFoundError, RuntimeError) as exc:
                    raise HTTPException(status_code=400, detail=str(exc))
                matches = engine_search(req.query, cues, limit=req.match_index + 1,
                                        min_score=req.min_score, max_span=req.max_span)
                if req.match_index >= len(matches):
                    raise HTTPException(status_code=404, detail="Match index out of range.")
                m = matches[req.match_index]
                cue_text = m.text
                rng = compute_range(m, before=req.before, after=req.after)
                line_start, line_end = m.start, m.end
                label = f"{req.kind} clip: \"{req.query}\" → {m.text[:60]}"
            else:
                raise HTTPException(
                    status_code=400,
                    detail="Provide either start (with optional end) or query to define the clip range.",
                )

            # Validation
            if req.split_channels and req.kind != "audio":
                raise HTTPException(status_code=400, detail="split_channels only applies to audio.")

            # Full-mix lossless audio (one WAV/FLAC keeping every channel, e.g. 5.1).
            # This is an ffmpeg re-encode, so it bypasses the mkvmerge/passthrough path.
            fullmix = req.kind == "audio" and not req.split_channels and req.audio_format is not None

            # Decide backend (mirrors cli.py logic).
            mkv_capable = (
                req.lossless and req.kind in ("audio", "video")
                and not req.split_channels and not fullmix
            )
            if req.backend == "ffmpeg":
                use_mkvmerge = False
            elif req.backend == "mkvmerge":
                if not mkv_capable:
                    raise HTTPException(
                        status_code=400,
                        detail="mkvmerge backend only supports lossless audio/video cuts.",
                    )
                if not mkvmerge_available():
                    raise HTTPException(status_code=500, detail="mkvmerge not found on PATH.")
                use_mkvmerge = True
            else:  # auto
                use_mkvmerge = mkv_capable and mkvmerge_available()
                # For lossless VIDEO, prefer ffmpeg: mkvmerge can only end a kept
                # range on a keyframe, so on a long-GOP source it extends the clip
                # well past the cut point (a 3 s line became 22 s). ffmpeg's stream
                # copy ends exactly (the start still snaps to the prior keyframe —
                # unavoidable for a copy). Audio passthrough stays on mkvmerge: it
                # keeps every track and now cuts at the exact time (no keyframe snap).
                if use_mkvmerge and req.kind == "video":
                    use_mkvmerge = False

            do_remux = use_mkvmerge and req.remux_first and not is_matroska(video)

            # Resolve subtitles for embedding (if applicable). Only the *sidecar*
            # search subtitle is muxed in — embedded subtitle tracks already ride
            # along in the lossless copy. We therefore only need find_sidecar (a
            # cheap glob) and load_subtitles (parse a text file); no ffmpeg
            # extraction. Populate BOTH the mkvmerge path (file) and the ffmpeg
            # path (cues) so the mkvmerge→ffmpeg fallback in do_cut keeps the subs.
            embed_cues = None
            embed_subs_path = None
            if req.embed_subs and req.kind == "video" and req.lossless:
                if yt_vid is not None:
                    # The stored transcript plays the sidecar's role (no local
                    # files exist for a stream URL).
                    with contextlib.suppress(KeyError, RuntimeError):
                        embed_cues = yt_store.cues(yt_vid) or None
                else:
                    try:
                        sidecar = find_sidecar(video)
                        if sidecar:
                            embed_subs_path = sidecar          # used by the mkvmerge path
                            embed_cues = load_subtitles(sidecar)  # used by the ffmpeg path/fallback
                    except Exception:
                        pass  # non-fatal: skip subtitle embedding

            # Determine extension.
            if fullmix:
                ext = req.audio_format  # "wav" | "flac"
            elif use_mkvmerge:
                ext = "mka" if req.kind == "audio" else "mkv"
            elif req.split_channels:
                ext = req.split_format if req.split_format != "original" else "wav"
            else:
                codecs = None
                if req.lossless and req.kind == "audio":
                    # YouTube audio is m4a/AAC by format selection — probing a
                    # stream URL would be slow, and the answer is known.
                    codecs = ["aac"] if yt_vid is not None else probe_audio_streams(video)
                    if req.audio_tracks:
                        codecs = [codecs[i] for i in req.audio_tracks if i < len(codecs)]
                ext = output_extension(req.kind, lossless=req.lossless, audio_codecs=codecs)

            # Output path from the user template (B9). The default reproduces the
            # Phase 2 layout: {source}/{timestamp}_{cue}_{title}. "/" makes
            # subfolders; absent tokens (e.g. {cue} on non-search clips) are dropped.
            # Split-channel clips get a per-channel suffix appended by the splitter,
            # so they pass the extensionless base.
            # Jellyfin's titles for a library file it knows (M18); synthetic YouTube and
            # EPUB sources, and files it doesn't know, keep the parsed names.
            jf_meta = jf_entry = None
            if jf_index is not None and name_source is None and isinstance(video, Path):
                jf_entry = jf_index.lookup(video)
                jf_meta = jellyfin_index.naming_meta(jf_entry)
            ctx = _clip_template_context(name_source or video, rng, cue_text, jf_meta)
            template = resolve_template(req.template)
            rel = _render_clip_template(template, ctx) or ctx["timestamp"] or "clip"
            clips_root = (out_dir or settings.clips_dir).resolve()
            if out_dir is not None:
                rel = Path(rel).name or "clip"
            out_path = (clips_root / (rel if req.split_channels else f"{rel}.{ext}")).resolve()
            if out_path != clips_root and not out_path.is_relative_to(clips_root):
                raise HTTPException(
                    status_code=400, detail="Naming template resolves outside the clips directory."
                )
            out_path.parent.mkdir(parents=True, exist_ok=True)

            # Don't clobber an existing clip: the engine cuts with ffmpeg -y, so two
            # exports resolving to the same path would overwrite the first (R6). Add a
            # _2/_3/... suffix before the extension until the target is free. (Split-
            # channel clips pass an extensionless base and get per-channel names from
            # the splitter, so uniquifying that base isn't reliable — skip them.)
            if not req.split_channels and out_path.exists():
                stem, suffix = out_path.stem, out_path.suffix
                n = 2
                while True:
                    candidate = out_path.with_name(f"{stem}_{n}{suffix}")
                    if not candidate.exists():
                        out_path = candidate
                        break
                    n += 1

            # Capture all values for the closure.
            _video = video
            _rng = rng
            _kind = req.kind
            _lossless = req.lossless
            _audio_indices = req.audio_tracks
            _use_mkvmerge = use_mkvmerge
            _do_remux = do_remux
            _chapters = req.chapters
            _embed_cues = embed_cues
            _embed_subs_path = embed_subs_path
            _split_channels = req.split_channels
            _split_format = req.split_format
            _include_lfe = req.include_lfe
            _split_groups = req.split_groups
            _audio_codec = req.audio_format if fullmix else None
            _default_sub_track = req.default_sub_track
            _out_path = out_path
            _epub_temp = epub_temp  # transient extracted EPUB audio, deleted post-cut
            _aux_audio = yt_aux  # separate audio URL of a DASH pair (YouTube)
            # Stream-URL cuts re-resolve at run time: googlevideo URLs expire
            # (~6h) and the job may sit queued — the handler's resolution above
            # validated/failed fast, and the TTL cache makes the re-resolve free
            # in the common case.
            _yt_webpage = yt_meta["webpage_url"] if yt_streaming else None
            # Hardware-encode a re-encoded video clip on the iGPU (Quick Sync via
            # VAAPI) when available — the same path the browser preview uses. Lossless
            # cuts are stream copies (no encode); only re-encodes (Exact / --no-lossless,
            # gif) hit the encoder. cut_clip falls back to libx264 if it fails.
            _hw_encode = (not req.lossless) and req.kind == "video" and _vaapi_h264_available()

            # The sidecar (clipmeta, docs/API.md "Clips library contract"): times are
            # the source's, trim is the line inside the clip (a lossless video cut
            # starts on the keyframe before, so there it is approximate).
            _meta = {
                "kind": req.kind, "origin": "clip",
                "source": {
                    "path": req.path, "name": clean_title(name_source or video),
                    "url": (yt_meta or {}).get("webpage_url"),
                },
                "line": {"start": round(line_start, 3), "end": round(line_end, 3)},
                "clip": {"start": round(rng.start, 3), "end": round(rng.end, 3)},
                "padding": {"before": round(max(0.0, line_start - rng.start), 3),
                            "after": round(max(0.0, rng.end - line_end), 3)},
                "trim": {"start": round(max(0.0, line_start - rng.start), 3),
                         "end": round(line_end - rng.start, 3)},
                "cue": cue_text or None,
                "lossless": req.lossless, "audio_format": req.audio_format,
                "jellyfin": clipmeta.jellyfin_fields(jf_entry),
                **(meta_extra or {}),
            }

            def do_cut(job) -> list[Path]:
                # Listed (and so seen by a `since` poll) only once the clip and its
                # sidecar are both there; stamped then, so it's newer than any cursor.
                with clipmeta.writing(_out_path):
                    written = _cut(job)
                    _write_sidecars(written, _meta)
                    clipmeta.publish(written)
                return written

            def _cut(_job) -> list[Path]:
                try:
                    if _split_channels:
                        return split_audio_channels(
                            _video, _rng,
                            audio_index=(_audio_indices[0] if _audio_indices else 0),
                            fmt=_split_format, include_lfe=_include_lfe,
                            categories=_split_groups, out=_out_path,
                        )
                    if _use_mkvmerge:
                        try:
                            return [cut_with_mkvmerge(
                                _video, _rng, kind=_kind, out=_out_path,
                                audio_indices=_audio_indices, keep_subs=True,
                                keep_chapters=_chapters, embed_subs=_embed_subs_path,
                                remux_first=_do_remux, default_sub_track=_default_sub_track,
                            )]
                        except RuntimeError:
                            # mkvmerge can't split some track types (e.g. FLAC).
                            # Fall back to ffmpeg for a lossless copy-codec cut.
                            _out_path.unlink(missing_ok=True)
                            return [cut_clip(
                                _video, _rng, kind=_kind, lossless=_lossless, out=_out_path,
                                audio_indices=_audio_indices, embed_cues=_embed_cues,
                                default_sub_track=_default_sub_track,
                            )]
                    src, aux = _video, _aux_audio
                    if _yt_webpage is not None:
                        v_url, a_url = youtube_items.resolve_stream_urls(_yt_webpage)
                        if _kind == "audio":
                            src, aux = (a_url or v_url), None
                        else:
                            src = v_url
                            aux = a_url if _kind == "video" else None
                    return [cut_clip(
                        src, _rng, kind=_kind, lossless=_lossless, out=_out_path,
                        audio_indices=_audio_indices, embed_cues=_embed_cues,
                        audio_codec=_audio_codec, default_sub_track=_default_sub_track,
                        video_encoder="h264_vaapi" if _hw_encode else "libx264",
                        vaapi_device=_VAAPI_DEVICE if _hw_encode else None,
                        aux_audio=aux,
                    )]
                finally:
                    if _epub_temp is not None:
                        _epub_temp.unlink(missing_ok=True)

            # From here the caller owns the EPUB temp: do_cut's finally removes it.
            _submitted = True
            return label, do_cut, clean_title(name_source or video)
        finally:
            if epub_temp is not None and not _submitted:
                epub_temp.unlink(missing_ok=True)

    @app.post("/api/clip")
    def create_clip(req: ClipRequest) -> dict:
        label, do_cut, _title = _plan_clip(req)
        job = jobs.submit(do_cut, label=label)
        return {"job_id": job.id, "status": job.status.value}

    # --- LeadStinger set files (M14) ---------------------------------------------------

    @app.post("/api/leadstinger/set")
    def create_leadstinger_set(req: LeadStingerSetRequest) -> dict:
        """Cut every item as FLAC with its padding and zip them as a LeadStinger set file.

        A job on the clip pool: it cuts the items one after another into a staging
        folder, then writes ``LeadStinger/<name>.lsset`` in the clips library (its
        download is the job's file). Each item's before/after padding is handle audio
        around the tile's trim, which sits on the line (D-024, D-026).
        """
        items = list(req.items)
        n = len(items)

        def build(job) -> list[Path]:
            staging_root = settings.state_dir / "lsset-staging"
            staging_root.mkdir(parents=True, exist_ok=True)
            staging = Path(tempfile.mkdtemp(prefix="set-", dir=staging_root))
            try:
                tiles, files, sources, items_meta = [], [], [], []
                for i, item in enumerate(items, 1):
                    if job.cancel_requested:
                        return []
                    title = (item.title or "").strip() or Path(item.path).stem
                    job.progress = f"{i - 1}/{n} {title[:60]}"
                    whole = item.start is None
                    before = 0.0 if whole else item.before
                    req_i = ClipRequest(
                        path=item.path, kind="audio", audio_format="flac", lossless=False,
                        start=0.0 if whole else item.start, end=item.end,
                        before=before, after=0.0 if whole else item.after,
                        cue_text=title, track=item.track, template="{timestamp}",
                        embed_subs=False,
                    )
                    try:
                        _label, do_cut, source_title = _plan_clip(req_i, out_dir=staging)
                        written = do_cut(job)
                    except HTTPException as exc:
                        raise RuntimeError(f"item {i} ({title[:60]}): {exc.detail}") from None
                    except RuntimeError as exc:
                        raise RuntimeError(f"item {i} ({title[:60]}): {exc}") from None
                    if not written:
                        raise RuntimeError(f"item {i} ({title[:60]}): nothing was cut")
                    sources.append(source_title)
                    arcname = f"{i:02d} {lsset.safe_name(title)}.flac"
                    trim = lsset.trim_for(None if whole else item.start, item.end, before)
                    where = "" if whole else f" · {format_timestamp(item.start)}"
                    notes = (item.notes or "").strip() or (
                        f"{Path(item.path).stem}{where} · quipclipper")
                    tiles.append((arcname, title, trim, notes))
                    files.append((arcname, written[0]))
                    items_meta.append({
                        "title": title, "notes": notes,
                        "source": {"path": item.path, "name": source_title},
                        "line": (None if whole else {"start": item.start, "end": item.end}),
                        "padding": {"before": before, "after": 0.0 if whole else item.after},
                        "trim_start": trim.start, "trim_end": trim.end,
                    })
                set_name = lsset.safe_name(req.name or lsset.auto_set_name(sources),
                                           fallback="quipclipper")
                # Clips live in a folder named after the set inside the zip.
                files = [(f"{set_name}/{a}", src) for a, src in files]
                tiles = [lsset.tile_entry(f"{set_name}/{a}", title=t, trim=tr, notes=no)
                         for a, t, tr, no in tiles]
                job.progress = f"{n}/{n} zipping"
                out_dir = settings.clips_dir / "LeadStinger"
                out_dir.mkdir(parents=True, exist_ok=True)
                out = lsset.unique_path(out_dir / f"{set_name}{lsset.EXTENSION}")
                with clipmeta.writing(out):
                    lsset.write_lsset(out, lsset.build_manifest(set_name, tiles), files)
                    try:
                        clipmeta.write(out, {"kind": "set", "origin": "leadstinger.set",
                                             "name": set_name, "items": items_meta,
                                             "size": out.stat().st_size})
                    except OSError as exc:
                        clipmeta.log.warning("set metadata for %s: %s", out, exc)
                    clipmeta.publish([out])
                job.progress = f"{n}/{n}"
                return [out]
            finally:
                shutil.rmtree(staging, ignore_errors=True)

        plural = "s" if n != 1 else ""
        named = f' "{req.name}"' if req.name else ""
        job = jobs.submit(build, label=f"LeadStinger set{named} ({n} clip{plural})",
                          cancellable=True)
        return {"job_id": job.id, "status": job.status.value}

    # --- Send to LeadStinger by link (M14 phase 3, LeadStinger's D-076) --------------------

    def _public_url(request: Request) -> str:
        """The server LeadStinger downloads the clip from (``_link_server``), checked."""
        try:
            server = _link_server(request)
        except lslink.LinkError as exc:
            raise HTTPException(status_code=503, detail=f"QC_PUBLIC_URL: {exc}")
        if not server:
            raise HTTPException(
                status_code=503,
                detail="Send to LeadStinger can't tell this server's address from the "
                       "request: set QC_PUBLIC_URL, its base URL as LeadStinger reaches it "
                       "(e.g. https://quip.example).",
            )
        return server

    def _source_duration(path: str) -> float | None:
        """Best effort: the source's length, so the tail handle stops at its end."""
        try:
            yt_vid = youtube_items.parse_ref(path)
            if yt_vid is not None:
                return yt_store.meta(yt_vid).get("duration")
            if epub_items.parse_ref(path)[1] is not None:
                return None  # a chapter segment: its times are not the book's
            return media.probe_duration(_resolve_any(path))
        except Exception:
            return None  # _plan_clip reports a bad path properly

    @app.post("/api/leadstinger/quip")
    def send_to_leadstinger(req: LeadStingerQuipRequest, request: Request) -> dict:
        """Cut one line as FLAC with its padding into the clips library and make its
        ``leadstinger://quip`` link (a job; the link is in the finished job's ``result``).

        The clip goes to ``LeadStinger/`` in the clips library, where LeadStinger downloads
        it with its own API token; ``start``/``end`` in the link are the line inside the clip.
        """
        server = _public_url(request)
        try:
            cut = lslink.handle_cut(req.start, req.end, req.before, req.after,
                                    _source_duration(req.path))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        title, set_name = req.title, req.set
        clip_req = ClipRequest(
            path=req.path, kind="audio", audio_format="flac", lossless=False,
            start=cut.start, end=cut.end, before=0.0, after=0.0, cue_text=title or "",
            template="{source}_{timestamp}_{cue}", embed_subs=False,
        )
        line_meta = {
            "origin": "leadstinger.quip", "cue": title or None, "set": set_name,
            "line": {"start": round(req.start, 3), "end": round(req.end, 3)},
            "padding": {"before": round(cut.line_start, 3),
                        "after": round(max(0.0, cut.end - cut.start - cut.line_end), 3)},
            "trim": {"start": cut.line_start, "end": cut.line_end},
        }
        label, do_cut, _source = _plan_clip(clip_req, out_dir=settings.clips_dir / "LeadStinger",
                                             meta_extra=line_meta)

        def send(job) -> list[Path]:
            written = do_cut(job)
            if not written:
                raise RuntimeError("nothing was cut")
            clip = lslink.clip_path(written[0], settings.clips_dir)
            job.result = {
                "link": lslink.build_link(server, clip, title=title, start=cut.line_start,
                                          end=cut.line_end, set_name=set_name),
                "clip": clip, "start": cut.line_start, "end": cut.line_end,
            }
            return written

        job = jobs.submit(send, label=f"Send to LeadStinger: {label}")
        return {"job_id": job.id, "status": job.status.value}

    @app.get("/api/leadstinger/link")
    def leadstinger_link(
        request: Request,
        clip: str,
        title: str | None = None,
        start: float | None = Query(None, allow_inf_nan=False),
        end: float | None = Query(None, allow_inf_nan=False),
        set: str | None = None,  # noqa: A002 - the link's own parameter name
    ) -> dict:
        """The ``leadstinger://quip`` link for a clip already in the clips library.

        ``clip`` is its path inside the library, as ``/api/clips/download/{path}`` takes
        it. No ``start``/``end``: the tile plays the whole clip."""
        server = _public_url(request)
        target = _clip_target(clip)
        if not target.is_file():
            raise HTTPException(status_code=404, detail="Clip not found.")
        try:
            rel = lslink.clip_path(target, settings.clips_dir)
            link = lslink.build_link(server, rel, title=title, start=start, end=end,
                                     set_name=set)
        except lslink.LinkError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"link": link, "clip": rel}

    # --- pairing (quipclipper.pair/1) and connected apps --------------------------------

    def _person_only(request: Request) -> None:
        """Approving, denying and listing requests, and listing or revoking apps, are for a
        person (the web UI) or the server's own QC_API_TOKEN: never a paired app, so one
        can't let others in or throw them out."""
        if request.state.client is not None:
            raise HTTPException(status_code=403, detail=_PERSON_ONLY)

    @app.post("/api/pair")
    def pair_start(req: PairStart, request: Request) -> dict:
        """An app asks for access: a pending request with a code to show, and the secret
        it polls with. No token; behind the password gate, nginx lets this through."""
        try:
            return pairs.start(req.name, req.host, _client_address(request))
        except pairing.PairError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail)

    @app.get("/api/pair")
    def pair_list(request: Request) -> dict:
        """The requests waiting for a person (no poll secrets)."""
        _person_only(request)
        return {"requests": pairs.pending()}

    @app.get("/api/pair/{pair_id}")
    def pair_poll(pair_id: str, request: Request) -> dict:
        """The app's poll (``X-Pair-Secret``): pending, approved with its token (once),
        denied or expired. An unknown id and a wrong or missing secret both answer 404."""
        found = pairs.poll(pair_id, request.headers.get("x-pair-secret"))
        if found is None:
            raise HTTPException(status_code=404, detail="No such pairing request.")
        return found

    @app.post("/api/pair/{pair_id}/approve")
    def pair_approve(pair_id: str, request: Request) -> dict:
        """Let the app in: it becomes a client, and its next poll collects its token."""
        _person_only(request)
        try:
            done = pairs.approve(pair_id)
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"Can't save the client: {exc}")
        if done is None:
            raise HTTPException(status_code=404, detail="No such pending request (it may "
                                "have expired, or been answered already).")
        return done

    @app.post("/api/pair/{pair_id}/deny")
    def pair_deny(pair_id: str, request: Request) -> dict:
        _person_only(request)
        done = pairs.deny(pair_id)
        if done is None:
            raise HTTPException(status_code=404, detail="No such pending request (it may "
                                "have expired, or been answered already).")
        return done

    @app.get("/api/clients")
    def clients_list(request: Request) -> dict:
        """The paired apps: name, host, when they paired and when last seen."""
        _person_only(request)
        return {"clients": clients.list()}

    @app.get("/api/clients/check")
    def clients_check(request: Request) -> dict:
        """200 when the request carries a current client token, else 401: what the nginx
        password gate asks before it lets a paired app through without a password."""
        if request.state.client is None:
            raise HTTPException(status_code=401, detail="No client token.")
        return {"client_id": request.state.client["id"]}

    @app.delete("/api/clients/{client_id}")
    def clients_revoke(client_id: str, request: Request) -> dict:
        """Revoke one app's token. ``self`` is the caller's own (with a client token)."""
        if client_id == "self":
            if request.state.client is None:
                raise HTTPException(status_code=401, detail="DELETE /api/clients/self "
                                    "revokes the client token it is sent with; none was.")
            return {"removed": clients.remove(request.state.client["id"])}
        _person_only(request)
        try:
            return {"removed": clients.remove(client_id)}
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"Can't save the clients: {exc}")

    def _find_job(job_id: str):
        """A job from any pool (clips, downloads, or YouTube add/reindex), plus
        its registry — the /api/jobs surface presents them as one list."""
        for registry in (jobs, dl_jobs, yt_jobs, stt_jobs, lib_jobs):
            job = registry.get(job_id)
            if job is not None:
                return job, registry
        return None, None

    def _job_dict(job) -> dict:
        """A job as the API shows it: each file in the clips library also carries ``clip``,
        its path there (what ``/api/clips/download/{clip}`` takes)."""
        d = job.to_dict()
        for entry, path in zip(d.get("files") or [], job.result_paths):
            try:
                entry["clip"] = lslink.clip_path(path, settings.clips_dir)
            except lslink.LinkError:
                pass
        return d

    @app.get("/api/jobs")
    def list_jobs() -> dict:
        merged = [*jobs.list_recent(), *dl_jobs.list_recent(), *yt_jobs.list_recent(),
                  *stt_jobs.list_recent(), *lib_jobs.list_recent()]
        merged.sort(key=lambda j: j.created, reverse=True)
        return {"jobs": [_job_dict(j) for j in merged[:20]]}

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str) -> dict:
        job, _ = _find_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found.")
        return _job_dict(job)

    @app.delete("/api/jobs/{job_id}")
    def cancel_job(job_id: str) -> dict:
        """Cancel a job, or prune a finished one (R2).

        A queued job is pulled from the pool and marked cancelled; a finished job
        (done/failed/cancelled) is dropped from the registry. A *running* job that
        polls for cancellation (the folder subtitle backfill) is asked to stop at
        its next checkpoint; a running clip job (its ffmpeg has its own timeout)
        can't be interrupted, so that returns 409.
        """
        job, registry = _find_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found.")
        if registry.cancel(job_id):
            return {"cancelled": job_id}
        # Running: cooperative cancel for jobs that support it.
        if job.status.value == "running" and registry.request_cancel(job_id):
            return {"cancelling": job_id}
        raise HTTPException(status_code=409, detail="Job can't be cancelled.")

    @app.get("/api/jobs/{job_id}/download/{filename}")
    def download_clip(job_id: str, filename: str) -> FileResponse:
        job, _ = _find_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found.")
        if job.status.value != "done":
            raise HTTPException(status_code=409, detail="Job not finished.")
        for p in job.result_paths:
            if p.name == filename:
                mime = _BROWSER_MIME.get(p.suffix.lower(), "application/octet-stream")
                return FileResponse(p, media_type=mime, filename=p.name)
        raise HTTPException(status_code=404, detail="File not found in job results.")

    # --- bookmarks ---------------------------------------------------------------

    def _check_bookmark_path(path: str) -> None:
        """Path-safety for a bookmark key: a real path must resolve within the
        allowed roots; a virtual yt: ref must name a stored video."""
        vid = youtube_items.parse_ref(path)
        if vid is not None:
            try:
                yt_store.meta(vid)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc))
            except RuntimeError as exc:  # corrupt info.json — clean error
                raise HTTPException(status_code=500, detail=str(exc))
            return
        _resolve_any(path)

    def _bookmark_source_name(path: str) -> str:
        """A human display name for a bookmark's source, for the Bookmarks browser
        (which groups by path). A ``yt:`` ref → the stored video title (the raw
        ref is a meaningless id otherwise); everything else → the file's name."""
        vid = youtube_items.parse_ref(path)
        if vid is not None:
            with contextlib.suppress(KeyError, RuntimeError):
                return yt_store.meta(vid).get("title") or path
            return path
        epub_ref, seg = epub_items.parse_ref(path)
        return Path(epub_ref if seg is not None else path).name

    def _bookmark_link(d: dict) -> str | None:
        return _item_link(d["path"], sel=(d["start"], d["end"]), track=d.get("sub_track"))

    def _bookmark_dict(b) -> dict:
        d = b.to_dict()
        d["source_name"] = _bookmark_source_name(d["path"])
        return _with_link(d, _bookmark_link(d))

    @app.get("/api/bookmarks")
    def list_bookmarks(path: str | None = None) -> dict:
        if path:
            _check_bookmark_path(path)
            bms = bookmarks.list_for_path(path)
        else:
            bms = bookmarks.list_all()
        return {"bookmarks": [_bookmark_dict(b) for b in bms]}

    @app.post("/api/bookmarks")
    def create_bookmark(req: BookmarkCreate) -> dict:
        _check_bookmark_path(req.path)
        if not req.label:
            req.label = f"{format_timestamp(req.start)} – {format_timestamp(req.end)}"
        bm = bookmarks.add(
            req.path, req.label, req.start, req.end,
            before=req.before, after=req.after,
            sub_track=req.sub_track, audio_track=req.audio_track,
            channel_subset=req.channel_subset, cue=req.cue,
        )
        d = bm.to_dict()
        return _with_link(d, _bookmark_link(d))

    @app.patch("/api/bookmarks/{bookmark_id}")
    def update_bookmark(bookmark_id: str, req: BookmarkUpdate) -> dict:
        bm = bookmarks.update(
            bookmark_id, label=req.label, before=req.before, after=req.after,
        )
        if bm is None:
            raise HTTPException(status_code=404, detail="Bookmark not found.")
        d = bm.to_dict()
        return _with_link(d, _bookmark_link(d))

    @app.delete("/api/bookmarks")
    def clear_bookmarks() -> dict:
        """Delete every bookmark (the bookmarks page 'Clear all')."""
        return {"deleted": bookmarks.clear_all()}

    @app.delete("/api/bookmarks/{bookmark_id}")
    def delete_bookmark(bookmark_id: str) -> dict:
        if not bookmarks.delete(bookmark_id):
            raise HTTPException(status_code=404, detail="Bookmark not found.")
        return {"deleted": bookmark_id}

    # --- clips library -----------------------------------------------------------

    @app.get("/api/clips")
    def list_clips(
        request: Request,
        folder: str | None = None,
        recursive: bool = False,
        q: str | None = Query(None, max_length=200),
        kind: str | None = Query(None, pattern="^(audio|video|gif|set)$"),
        since: str | None = Query(None, max_length=40),
        sort: str = Query("name", pattern="^(name|newest)$"),
    ):
        """Browse the clips library: one folder's subfolders, clips and set files, or the
        whole tree under it with ``recursive``; ``q``/``kind``/``since`` filter it.

        Each entry carries its metadata sidecar (``meta``). ``cursor`` is the newest
        file's time: pass it back as ``since`` for only what was added after it.
        ``etag`` (also the ETag header; If-None-Match answers 304) changes with any
        file or sidecar in the listing. docs/API.md, "Clips library contract", is the
        contract LeadStinger builds on."""
        since_ns = None
        if since:
            try:
                since_ns = clipmeta.parse_cursor(since)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
        base = settings.clips_dir
        out = {"contract": CLIPS_CONTRACT, "folder": folder, "recursive": recursive,
               "folders": [], "clips": [], "count": 0}
        if not base.is_dir():
            out["cursor"] = since or clipmeta.make_cursor(0)
            out["etag"] = tag = clipmeta.etag([], ())
            if clipmeta.etag_matches(request.headers.get("if-none-match"), tag):
                return Response(status_code=304, headers={"ETag": tag})
            return JSONResponse(out, headers={"ETag": tag})
        root = base.resolve()
        if folder:
            target = _clip_target(folder)
            if not target.is_dir():
                raise HTTPException(status_code=404, detail="Folder not found.")
        else:
            target = root

        # Only show subfolders that hold clips or sets: empty folders left behind
        # after deleting their clips shouldn't clutter the library.
        out["folders"] = sorted(
            d.name for d in target.iterdir()
            if not d.name.startswith(".") and d.is_dir()
            and d.resolve().is_relative_to(root) and clipmeta.has_listed(d)
        )
        # When a front proxy serves the clips dir directly (QC_CLIPS_URL_PREFIX,
        # e.g. "/clips"), point downloads there so large files bypass uvicorn.
        dl_base = settings.clips_url_prefix or "/api/clips/download"
        newest = since_ns or 0
        clips, rows = [], []
        for f in clipmeta.scan(base, target, recursive):
            try:
                st = f.stat()
            except OSError:
                continue
            meta = clipmeta.read(f)
            k = clipmeta.kind_of(f, meta)
            rel = f.relative_to(root).as_posix()
            if kind and k != kind:
                continue
            if q and not clipmeta.matches_query(clipmeta.search_text(f, rel, meta), q):
                continue
            newest = max(newest, st.st_mtime_ns)
            if since_ns is not None and st.st_mtime_ns <= since_ns:
                continue
            try:
                sc_ns = clipmeta.sidecar_path(f).stat().st_mtime_ns
            except OSError:
                sc_ns = 0
            rows.append((rel, st.st_size, st.st_mtime_ns, sc_ns))
            playable = k != "set"
            entry = {
                "name": f.name,
                "path": str(f.resolve()),
                "size": st.st_size,
                "folder": rel.rpartition("/")[0],
                "clip": rel,
                "kind": k,
                "title": clipmeta.title_for(f, meta),
                "mtime": st.st_mtime,
                "modified": clipmeta._iso(st.st_mtime),
                "download_url": f"{dl_base}/{quote(rel)}",
                "stream_url": f"/api/clips/stream/{quote(rel)}" if playable else None,
                "flac_url": (f"/api/clips/download/{quote(rel)}?as=flac"
                             if k in ("audio", "video") else None),
                "meta": meta,
            }
            clips.append(_with_link(entry, _item_link(str(f.resolve())) if playable else None))
        if sort == "newest":
            clips.sort(key=lambda c: (-c["mtime"], c["clip"]))
        else:
            clips.sort(key=lambda c: c["clip"])
        out["clips"] = clips
        out["count"] = len(clips)
        out["cursor"] = clipmeta.make_cursor(newest)
        out["etag"] = tag = clipmeta.etag(
            rows, (folder, recursive, q, kind, since, sort, out["folders"]))
        if clipmeta.etag_matches(request.headers.get("if-none-match"), tag):
            return Response(status_code=304, headers={"ETag": tag})
        return JSONResponse(out, headers={"ETag": tag})

    @app.post("/api/clips/backfill")
    def clips_backfill() -> dict:
        """Give old clips their metadata sidecars (a job; its ``result`` counts them).

        ffprobe for codec, channels and duration, the file name for the source, line and
        timestamp, and Jellyfin for a source it can find by that name. Also re-attaches
        sidecars whose clip moved and deletes those whose clip is gone, and drops the
        FLAC copies of deleted clips. Safe to run again: clips with a sidecar are kept."""
        def run(job) -> list[Path]:
            by_stem = jf_index.by_stem() if jf_index is not None else None
            stats = clipmeta.backfill(
                settings.clips_dir, jellyfin_by_stem=by_stem,
                cancelled=lambda: job.cancel_requested,
                progress=lambda p: setattr(job, "progress", p))
            root = settings.clips_dir.resolve()
            live = {f.relative_to(root).as_posix()
                    for f in clipmeta.scan(settings.clips_dir, root, True)}
            stats["flac_pruned"] = flac_cache.prune(live)
            job.result = stats
            return []

        job = jobs.submit(run, label="Clip metadata backfill", cancellable=True)
        return {"job_id": job.id, "status": job.status.value}

    @app.get("/api/clips/download/{clip_path:path}")
    def download_saved_clip(
        clip_path: str,
        as_: str | None = Query(None, alias="as", pattern="^flac$"),
    ) -> FileResponse:
        """Download a clip or set file from the clips library.

        ``as=flac`` converts a clip (no cut) to a full-mix FLAC keeping its channels
        (D-024's format; the first audio stream), cached until the clip changes; a FLAC
        clip comes as it is. A set file or a GIF has no FLAC: 400."""
        target = _clip_target(clip_path)
        if not target.is_file():
            raise HTTPException(status_code=404, detail="Clip not found.")
        if as_ == "flac":
            k = clipmeta.kind_of(target, clipmeta.read(target))
            if k == "set":
                raise HTTPException(status_code=400,
                                    detail="A set file downloads as it is (no ?as=).")
            if k == "gif":
                raise HTTPException(status_code=400, detail="A GIF has no audio.")
            if target.suffix.lower() != ".flac":
                rel = target.relative_to(settings.clips_dir.resolve()).as_posix()
                try:
                    flac = flac_cache.get(target, rel)
                except RuntimeError as exc:
                    raise HTTPException(status_code=422,
                                        detail=f"Can't convert this clip to FLAC: {exc}")
                return FileResponse(flac, media_type="audio/flac",
                                    filename=f"{target.stem}.flac")
        mime = _BROWSER_MIME.get(target.suffix.lower(), "application/octet-stream")
        return FileResponse(target, media_type=mime, filename=target.name)

    @app.get("/api/clips/stream/{clip_path:path}")
    def stream_saved_clip(clip_path: str) -> FileResponse:
        """Stream a clip for in-browser playback (no Content-Disposition download)."""
        target = _clip_target(clip_path)
        if not target.is_file():
            raise HTTPException(status_code=404, detail="Clip not found.")
        mime = _BROWSER_MIME.get(target.suffix.lower(), "application/octet-stream")
        return FileResponse(target, media_type=mime)

    # Dev mode: serve the frontend when running outside nginx.
    _frontend = Path(__file__).resolve().parent.parent.parent / "frontend"
    if _frontend.is_dir():
        app.mount("/", StaticFiles(directory=_frontend, html=True), name="frontend")

    return app


# Module-level app for `uvicorn quipclipper_web.app:app`.
app = create_app()
