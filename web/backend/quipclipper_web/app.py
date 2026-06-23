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
import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

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
from quipclipper.models import Match, format_timestamp
from quipclipper.mkv import (
    cut_with_mkvmerge,
    is_matroska,
    mkvmerge_available,
)
from quipclipper.search import search as engine_search
from quipclipper.subtitles import AUDIO_EXTS, VIDEO_EXTS, find_sidecar, load_subtitles
from quipclipper_web import __version__, library, media
from quipclipper_web.bookmarks import BookmarkStore
from quipclipper_web.config import Settings
from quipclipper_web.jobs import JobRegistry
from quipclipper_web.sub_cache import SubtitleCache

def _is_media_file(p: Path) -> bool:
    """A real, indexable media file: a video, or an audio-only file (podcast/
    audiobook) that has a sidecar transcript.

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
    return ext in AUDIO_EXTS and find_sidecar(p) is not None


# Output extensions a finished clip can have (for listing the clips library).
_CLIP_EXTS = (".mkv", ".mka", ".mp4", ".m4v", ".webm", ".gif", ".wav", ".flac", ".ogg", ".mp3")

_SEASON_RE = re.compile(r"(?i)^(season\s*\d+|specials|extras)$")


def _clean_title(path: Path) -> str:
    """A clean display/clip title: the containing folder's name, stepping up past
    a ``Season N``/``Specials`` folder to the series name. Falls back to the stem.
    Mirrors the frontend's ``cleanItemName``."""
    parent = path.parent
    name = parent.name
    if _SEASON_RE.match(name) and parent.parent != parent:
        name = parent.parent.name
    return name or path.stem


def _slugify(text: str) -> str:
    """Filesystem-safe slug: keep word chars/spaces/apostrophes/hyphens, collapse
    whitespace, join with underscores. Strips ``/`` and ``.`` so token values
    can't inject path separators or traversal."""
    text = re.sub(r"[^\w\s'\-]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text.replace(" ", "_")


# Output naming (B9). The default reproduces the Phase 2 layout exactly.
DEFAULT_CLIP_TEMPLATE = "{source}/{timestamp}_{cue}_{title}"
_YEAR_RE = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
_SXXEXX_RE = re.compile(r"(?i)s(\d{1,2})e(\d{1,2})")
_TOKEN_RE = re.compile(r"\{(\w+)\}")


def _clip_template_context(video: Path, rng, cue_text: str) -> dict[str, str]:
    """Resolve the placeholder values for a clip. Media-derived tokens (cue,
    title, source) are slugified; the rest are already filesystem-safe."""
    from datetime import date

    from quipclipper.clip import _timestamp_slug

    stem = video.stem
    year_m = _YEAR_RE.search(stem)
    se_m = _SXXEXX_RE.search(stem)
    return {
        "timestamp": _timestamp_slug(rng.start),
        "end": _timestamp_slug(rng.end),
        "duration": str(int(round(rng.end - rng.start))),
        "cue": _slugify(cue_text[:80]) if cue_text else "",
        "title": _slugify(_clean_title(video)),
        "source": stem,
        "year": year_m.group(0) if year_m else "",
        "season": f"{int(se_m.group(1)):02d}" if se_m else "",
        "episode": f"{int(se_m.group(2)):02d}" if se_m else "",
        "date": date.today().isoformat(),
    }


def _render_clip_template(template: str, ctx: dict[str, str]) -> str:
    """Render a clip path template into a safe relative path *without* extension.

    Unknown/absent tokens become empty. Per path segment, runs of separators
    left by dropped tokens are collapsed and edges trimmed; empty, ``.`` and
    ``..`` segments are dropped (traversal is also rejected by the clips-dir
    containment check at the call site). ``/`` yields subfolders."""
    rendered = _TOKEN_RE.sub(lambda mt: ctx.get(mt.group(1), ""), template)
    segments = []
    for raw in rendered.split("/"):
        seg = re.sub(r"_{2,}", "_", raw)
        seg = re.sub(r"-{2,}", "-", seg)
        seg = seg.strip("_-. ")
        if seg in ("", ".", ".."):
            continue
        segments.append(seg)
    return "/".join(segments)


# The VAAPI render node used for hardware H.264 encoding. Override with
# QC_VAAPI_DEVICE for a non-default node (e.g. a second GPU's renderD129, or the
# device the NixOS module / docker-compose passes in).
_VAAPI_DEVICE = os.environ.get("QC_VAAPI_DEVICE") or "/dev/dri/renderD128"


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


# Containers/extensions a browser can usually play in a <video> element. Used to
# hint the UI; the engine still works on anything ffmpeg can read.
_BROWSER_MIME = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".mov": "video/quicktime",
    ".ts": "video/mp2t",
    ".avi": "video/x-msvideo",
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


class ClipRequest(BaseModel):
    """JSON body for POST /api/clip — full CLI option parity."""

    path: str
    # Time range — either explicit start/end OR search-based (query + match_index).
    start: float | None = None
    end: float | None = None
    query: str | None = None
    match_index: int = 0
    track: int | None = None
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
    audio_tracks: list[int] | None = None
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
    default_sub_track: int | None = None


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    jobs = JobRegistry(max_workers=settings.max_concurrent_jobs)
    bookmarks = BookmarkStore(settings.state_dir)
    sub_cache = SubtitleCache(settings.state_dir, default_langs=settings.subtitle_langs)

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        jobs.shutdown()

    app = FastAPI(title="quipclipper-web", version=__version__, lifespan=lifespan)
    app.state.settings = settings

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
            },
        }

    @app.get("/api/config")
    def config() -> dict:
        """Non-secret configuration the frontend needs to render itself."""
        return {
            "media_roots": [str(p) for p in settings.media_roots],
            "auth_required": settings.auth_required,
            "max_concurrent_jobs": settings.max_concurrent_jobs,
            "hw_encode": _vaapi_h264_available(),  # iGPU H.264 (B23) for video re-encode
            "subtitle_langs": settings.subtitle_langs,  # default auto-select prefs (B14)
        }

    # --- library browsing ----------------------------------------------------

    def _resolve(path: str) -> Path:
        """Resolve a client path within the media roots, mapping errors to HTTP."""
        try:
            return library.resolve_within_roots(path, settings.media_roots)
        except library.PathNotAllowed as exc:
            raise HTTPException(status_code=403, detail=str(exc))

    def _resolve_any(path: str) -> Path:
        """Resolve a path within media roots OR clips dir."""
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

    @app.get("/api/library/roots")
    def roots() -> dict:
        return {"roots": [str(p) for p in settings.media_roots]}

    @app.get("/api/library/browse")
    def browse(path: str | None = None) -> dict:
        try:
            entries = library.browse(path, settings.media_roots)
        except library.PathNotAllowed as exc:
            raise HTTPException(status_code=403, detail=str(exc))
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except NotADirectoryError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"path": path, "entries": [library.entry_dict(e) for e in entries]}

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
        return {"query": query, "count": len(entries), "entries": [library.entry_dict(e) for e in entries]}

    # --- file inspection -----------------------------------------------------

    @app.get("/api/items")
    def item(path: str = Query(...), langs: str | None = Query(None)) -> dict:
        p = _resolve_any(path)
        if not p.exists():
            raise HTTPException(status_code=404, detail=f"Not found: {path}")
        if not p.is_file():
            raise HTTPException(status_code=400, detail=f"Not a file: {path}")
        # Per-request subtitle-language preference overrides the server default
        # for which track auto-selects (B14); blank uses the server default.
        pref = [s.strip() for s in langs.split(",") if s.strip()] if langs else None
        try:
            return media.item_info(p, langs=pref or settings.subtitle_langs)
        except RuntimeError as exc:  # ffprobe failure
            raise HTTPException(status_code=500, detail=str(exc))

    @app.get("/api/items/subtitles")
    def subtitles(
        path: str = Query(...),
        track: int | None = Query(None, ge=0),
        offset: float = Query(0, ge=0),
        fmt: str = Query("vtt"),
    ) -> Response:
        p = _resolve_any(path)
        try:
            # Use the cache: extraction from large files is slow (~9s for a
            # 2160p remux), and the VTT track + script panel request the same
            # subtitles when an item opens.  The cache + in-flight lock means
            # one extraction serves all callers and later views are instant.
            cues = sub_cache.resolve(p, track=track)
        except ValueError as exc:  # multiple tracks, none auto-selectable
            raise HTTPException(status_code=409, detail=str(exc))
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc))
        if fmt == "json":
            return Response(
                content=json.dumps(
                    [{"start": c.start, "end": c.end, "text": c.text} for c in cues],
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
    ) -> dict:
        p = _resolve_any(path)
        try:
            cues = sub_cache.resolve(p, track=track)
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
                {
                    "index": i,
                    "score": round(m.score, 1),
                    "text": m.text,
                    "start": m.start,
                    "end": m.end,
                    "start_ts": format_timestamp(m.start),
                    "end_ts": format_timestamp(m.end),
                    "cue_count": len(m.cues),
                }
                for i, m in enumerate(matches)
            ],
        }

    # --- folder dialogue search ------------------------------------------------

    @app.get("/api/search/folder")
    def search_dialogue_folder(
        path: list[str] = Query(...),
        query: str = Query(..., min_length=1),
        limit: int = Query(5, ge=1, le=20),
        min_score: float = Query(60.0, ge=0, le=100),
        max_span: int = Query(3, ge=1, le=10),
    ) -> dict:
        """Search dialogue across all video files in one or more folders.

        Accepts one or more ``path`` query params (repeat the param to search
        several folders at once, e.g. the folders returned by a library
        search).  For each video with subtitles (sidecar or embedded), run the
        search engine and collect the top results.  Returns a flat list of
        matches grouped by source file, sorted best-score-first across all files.
        """
        # Resolve and validate every requested folder.
        folders: list[Path] = []
        for p in path:
            folder = _resolve(p)
            if not folder.is_dir():
                raise HTTPException(status_code=400, detail=f"Not a directory: {p}")
            folders.append(folder)

        all_hits: list[dict] = []

        # Collect video files recursively across all folders, de-duplicated
        # (folders may overlap) and sorted for deterministic order.
        seen: set[Path] = set()
        videos: list[Path] = []
        for folder in folders:
            for c in folder.rglob("*"):
                if _is_media_file(c):
                    rp = c.resolve()
                    if rp not in seen:
                        seen.add(rp)
                        videos.append(c)
        videos.sort(key=lambda p: p.name.lower())

        def _search_one(video: Path) -> list[dict]:
            try:
                cues = sub_cache.resolve(video)
            except (ValueError, FileNotFoundError, RuntimeError):
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
                    "start": m.start,
                    "end": m.end,
                    "start_ts": format_timestamp(m.start),
                    "end_ts": format_timestamp(m.end),
                    "cue_count": len(m.cues),
                }
                for m in matches
            ]

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(_search_one, v): v for v in videos}
            for fut in as_completed(futures):
                all_hits.extend(fut.result())

        all_hits.sort(key=lambda h: -h["score"])

        return {
            "query": query,
            "folders": path,
            "files_scanned": len(videos),
            "count": len(all_hits),
            "matches": all_hits,
        }

    # --- folder subtitle index -------------------------------------------------

    # Folders above this size are too large to index in one request (the
    # nginx-side timeout is 600s); both index-status and (re)indexing skip them.
    INDEX_CAP = 500

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
            if _is_media_file(c):
                videos.append(c)
                if cap is not None and len(videos) > cap:
                    return videos, True
        videos.sort(key=lambda p: p.name.lower())
        return videos, False

    @app.get("/api/search/folder/index-status")
    def folder_index_status(path: str = Query(...)) -> dict:
        videos, skipped = _folder_videos(path, cap=INDEX_CAP)
        if skipped:
            return {"total": len(videos), "indexed": 0, "skipped": True}
        indexed = sum(1 for v in videos if sub_cache.is_cached(v))
        return {"total": len(videos), "indexed": indexed, "skipped": False}

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
            for v in videos:
                if not sub_cache.is_cached(v):
                    try:
                        sub_cache.resolve(v)
                    except (ValueError, FileNotFoundError, RuntimeError):
                        pass
                done += 1
                yield f"{done}/{total} {v.name}\n"

        return StreamingResponse(generate(), media_type="text/plain")

    @app.post("/api/items/subtitles/reindex")
    def reindex_item_subtitles(path: str = Query(...)) -> dict:
        """Clear and re-extract the subtitle cache for a single video file.

        Use this when a file's subtitles have changed and you want the script
        and dialogue search to reflect the new subtitles immediately.
        """
        p = _resolve_any(path)
        if not p.is_file():
            raise HTTPException(status_code=404, detail=f"Not found: {path}")
        cleared = sub_cache.clear(p)
        try:
            cues = sub_cache.resolve(p)
        except (ValueError, FileNotFoundError, RuntimeError) as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return {"cleared": cleared, "cues": len(cues)}

    # --- media streaming -------------------------------------------------------

    @app.get("/api/media")
    def stream(path: str = Query(...)) -> FileResponse:
        """Serve a source file with HTTP range support (best-effort preview).

        Starlette's FileResponse handles Range requests (206), so the browser can
        seek. Whether it actually decodes depends on the codec/container.
        """
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
        p = _resolve_any(path)
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
            cmd += ["-map", "0:v:0?", "-map", f"0:a:{aidx}"]
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

    # --- HLS (iOS playback) ---------------------------------------------------
    # iOS Safari can't play progressive Matroska/Opus, raw .mkv, or a chunked
    # MP4 (it needs HTTP range support a live ffmpeg pipe can't give). HLS is its
    # native streaming format: ffmpeg segments the file (video stream-copied,
    # audio -> AAC) as fMP4 into a temp dir under the state dir; we serve the
    # playlist + segments and iOS handles seeking natively.
    _hls_root = settings.state_dir / "hls"
    _hls_procs: dict[str, asyncio.subprocess.Process] = {}
    _HLS_FILE_RE = re.compile(r"^(init\.mp4|seg\d+\.m4s)$")

    def _hls_token(p: Path, venc: int = 0) -> str:
        return hashlib.sha256(f"{p}:{venc}".encode()).hexdigest()[:16]

    def _prune_hls(max_age: float = 2 * 3600) -> None:
        if not _hls_root.is_dir():
            return
        now = time.time()
        for d in _hls_root.iterdir():
            with contextlib.suppress(OSError):
                if d.is_dir() and now - d.stat().st_mtime > max_age:
                    shutil.rmtree(d, ignore_errors=True)

    @app.get("/api/media/hls")
    async def hls_playlist(path: str = Query(...), venc: int = Query(0)) -> Response:
        """Start (or reuse) an on-the-fly HLS transcode; return its playlist.

        Segment/init URIs are rewritten to absolute API paths so the playlist's
        query-string URL doesn't break relative resolution. The player reloads
        this URL to pick up new segments; ffmpeg races ahead of real time and
        appends EXT-X-ENDLIST when done, enabling full seeking. Video is
        stream-copied (iOS decodes H.264/HEVC); ``venc=1`` re-encodes it to H.264
        for codecs iOS can't decode (XviD/MPEG-4 ASP, MPEG-2, VC1, …) — the iOS
        side of B20/B22, hardware via Quick Sync when available.
        """
        p = _resolve_any(path)
        if not p.is_file():
            raise HTTPException(status_code=404, detail=f"Not found: {path}")
        token = _hls_token(p, venc)
        d = _hls_root / token
        playlist = d / "index.m3u8"
        hw = bool(venc) and _vaapi_h264_available()

        proc = _hls_procs.get(token)
        running = proc is not None and proc.returncode is None
        if not playlist.exists() and not running:
            _prune_hls()
            shutil.rmtree(d, ignore_errors=True)
            d.mkdir(parents=True, exist_ok=True)
            cmd = ["ffmpeg", "-nostdin"]
            if hw:
                cmd += ["-vaapi_device", _VAAPI_DEVICE]
            cmd += ["-i", str(p)]
            if venc:
                cmd += (["-vf", "format=nv12,hwupload", "-c:v", "h264_vaapi", "-qp", "24"]
                        if hw else ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"])
            else:
                cmd += ["-c:v", "copy"]
            cmd += [
                "-c:a", "aac", "-b:a", "192k", "-ac", "2",
                "-f", "hls", "-hls_time", "6", "-hls_list_size", "0",
                "-hls_playlist_type", "event", "-hls_flags", "independent_segments",
                "-hls_segment_type", "fmp4", "-hls_fmp4_init_filename", "init.mp4",
                "-hls_segment_filename", str(d / "seg%05d.m4s"),
                str(playlist),
            ]
            _hls_procs[token] = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )

        for _ in range(150):  # wait up to ~15s for the playlist + first segment
            if playlist.exists() and any(d.glob("seg*.m4s")):
                break
            await asyncio.sleep(0.1)
        if not playlist.exists():
            raise HTTPException(status_code=504, detail="HLS transcode did not start in time.")

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
        f = _hls_root / token / name
        for _ in range(50):  # a segment may be requested just before ffmpeg flushes it
            if f.is_file():
                break
            await asyncio.sleep(0.1)
        if not f.is_file():
            raise HTTPException(status_code=404, detail="Segment not ready")
        return FileResponse(f, media_type="video/mp4")

    # --- clipping & jobs -------------------------------------------------------

    @app.post("/api/clip")
    def create_clip(req: ClipRequest) -> dict:
        video = _resolve_any(req.path)
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
                end = media.probe_duration(video)
                if end is None:
                    raise HTTPException(
                        status_code=400,
                        detail="Could not determine file duration for a whole-file clip.",
                    )
            rng = ClipRange(start=max(0.0, req.start - req.before), end=end + req.after)
            label = f"{req.kind} clip {format_timestamp(req.start)}–{format_timestamp(end)}"
        elif req.query:
            try:
                # Use the cache: extraction is slow and the same cues were very
                # likely already extracted when the user searched in the UI.
                cues = sub_cache.resolve(video, track=req.track)
            except (ValueError, FileNotFoundError, RuntimeError) as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            matches = engine_search(req.query, cues, limit=req.match_index + 1)
            if req.match_index >= len(matches):
                raise HTTPException(status_code=404, detail="Match index out of range.")
            m = matches[req.match_index]
            cue_text = m.text
            rng = compute_range(m, before=req.before, after=req.after)
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
                codecs = probe_audio_streams(video)
                if req.audio_tracks:
                    codecs = [codecs[i] for i in req.audio_tracks if i < len(codecs)]
            ext = output_extension(req.kind, lossless=req.lossless, audio_codecs=codecs)

        # Output path from the user template (B9). The default reproduces the
        # Phase 2 layout: {source}/{timestamp}_{cue}_{title}. "/" makes
        # subfolders; absent tokens (e.g. {cue} on non-search clips) are dropped.
        # Split-channel clips get a per-channel suffix appended by the splitter,
        # so they pass the extensionless base.
        ctx = _clip_template_context(video, rng, cue_text)
        template = (req.template or "").strip() or DEFAULT_CLIP_TEMPLATE
        rel = _render_clip_template(template, ctx) or ctx["timestamp"] or "clip"
        clips_root = settings.clips_dir.resolve()
        out_path = (clips_root / (rel if req.split_channels else f"{rel}.{ext}")).resolve()
        if out_path != clips_root and not out_path.is_relative_to(clips_root):
            raise HTTPException(
                status_code=400, detail="Naming template resolves outside the clips directory."
            )
        out_path.parent.mkdir(parents=True, exist_ok=True)

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
        # Hardware-encode a re-encoded video clip on the iGPU (Quick Sync via
        # VAAPI) when available — the same path the browser preview uses. Lossless
        # cuts are stream copies (no encode); only re-encodes (Exact / --no-lossless,
        # gif) hit the encoder. cut_clip falls back to libx264 if it fails.
        _hw_encode = (not req.lossless) and req.kind == "video" and _vaapi_h264_available()

        def do_cut() -> list[Path]:
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
            return [cut_clip(
                _video, _rng, kind=_kind, lossless=_lossless, out=_out_path,
                audio_indices=_audio_indices, embed_cues=_embed_cues,
                audio_codec=_audio_codec, default_sub_track=_default_sub_track,
                video_encoder="h264_vaapi" if _hw_encode else "libx264",
                vaapi_device=_VAAPI_DEVICE if _hw_encode else None,
            )]

        job = jobs.submit(do_cut, label=label)
        return {"job_id": job.id, "status": job.status.value}

    @app.get("/api/jobs")
    def list_jobs() -> dict:
        return {"jobs": [j.to_dict() for j in jobs.list_recent()]}

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str) -> dict:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found.")
        return job.to_dict()

    @app.get("/api/jobs/{job_id}/download/{filename}")
    def download_clip(job_id: str, filename: str) -> FileResponse:
        job = jobs.get(job_id)
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

    @app.get("/api/bookmarks")
    def list_bookmarks(path: str | None = None) -> dict:
        if path:
            _resolve_any(path)  # path-safety check
            bms = bookmarks.list_for_path(path)
        else:
            bms = bookmarks.list_all()
        return {"bookmarks": [b.to_dict() for b in bms]}

    @app.post("/api/bookmarks")
    def create_bookmark(req: BookmarkCreate) -> dict:
        _resolve_any(req.path)  # path-safety check
        if not req.label:
            req.label = f"{format_timestamp(req.start)} – {format_timestamp(req.end)}"
        bm = bookmarks.add(
            req.path, req.label, req.start, req.end,
            before=req.before, after=req.after,
            sub_track=req.sub_track, audio_track=req.audio_track,
            channel_subset=req.channel_subset, cue=req.cue,
        )
        return bm.to_dict()

    @app.patch("/api/bookmarks/{bookmark_id}")
    def update_bookmark(bookmark_id: str, req: BookmarkUpdate) -> dict:
        bm = bookmarks.update(
            bookmark_id, label=req.label, before=req.before, after=req.after,
        )
        if bm is None:
            raise HTTPException(status_code=404, detail="Bookmark not found.")
        return bm.to_dict()

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
    def list_clips(folder: str | None = None) -> dict:
        """Browse the clips directory. With folder=, list clips inside that subfolder."""
        base = settings.clips_dir
        if not base.is_dir():
            return {"clips": [], "folders": []}
        if folder:
            target = (base / folder).resolve()
            if not target.is_relative_to(base.resolve()):
                raise HTTPException(status_code=403, detail="Path outside clips dir.")
            if not target.is_dir():
                raise HTTPException(status_code=404, detail="Folder not found.")
        else:
            target = base

        def _has_clip(d: Path) -> bool:
            try:
                return any(
                    f.is_file() and f.suffix.lower() in _CLIP_EXTS for f in d.iterdir()
                )
            except OSError:
                return False

        # Only show subfolders that actually contain clips — empty folders left
        # behind after deleting their clips shouldn't clutter the library.
        folders = sorted(
            d.name for d in target.iterdir() if d.is_dir() and _has_clip(d)
        )
        # When a front proxy serves the clips dir directly (QC_CLIPS_URL_PREFIX,
        # e.g. "/clips"), point downloads there so large files bypass uvicorn.
        dl_base = settings.clips_url_prefix or "/api/clips/download"
        clips = sorted(
            [
                {
                    "name": f.name,
                    "path": str(f.resolve()),
                    "size": f.stat().st_size,
                    "folder": folder or "",
                    "download_url": f"{dl_base}/{quote(((folder + '/') if folder else '') + f.name)}",
                    "stream_url": f"/api/clips/stream/{quote(((folder + '/') if folder else '') + f.name)}",
                }
                for f in target.iterdir()
                if f.is_file() and f.suffix.lower() in _CLIP_EXTS
            ],
            key=lambda c: c["name"],
        )
        return {"folder": folder, "folders": folders, "clips": clips}

    @app.get("/api/clips/download/{clip_path:path}")
    def download_saved_clip(clip_path: str) -> FileResponse:
        """Download a clip from the clips library."""
        target = (settings.clips_dir / clip_path).resolve()
        if not target.is_relative_to(settings.clips_dir.resolve()):
            raise HTTPException(status_code=403, detail="Path outside clips dir.")
        if not target.is_file():
            raise HTTPException(status_code=404, detail="Clip not found.")
        mime = _BROWSER_MIME.get(target.suffix.lower(), "application/octet-stream")
        return FileResponse(target, media_type=mime, filename=target.name)

    @app.get("/api/clips/stream/{clip_path:path}")
    def stream_saved_clip(clip_path: str) -> FileResponse:
        """Stream a clip for in-browser playback (no Content-Disposition download)."""
        target = (settings.clips_dir / clip_path).resolve()
        if not target.is_relative_to(settings.clips_dir.resolve()):
            raise HTTPException(status_code=403, detail="Path outside clips dir.")
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
