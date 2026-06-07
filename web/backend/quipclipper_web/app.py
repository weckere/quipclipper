"""FastAPI application factory.

Routes:
- Phase 0: health, config
- Phase 1: library browse, items, subtitles (WebVTT), media streaming
- Phase 2: dialogue search
- Phase 3: clip jobs (POST /api/clip, GET /api/jobs)
"""

from __future__ import annotations

import shutil
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from quipclipper.clip import (
    ClipRange,
    compute_range,
    cut_clip,
    output_extension,
    probe_audio_streams,
    split_audio_channels,
)
from quipclipper.models import Match, format_timestamp
from quipclipper.mkv import (
    cut_with_mkvmerge,
    is_matroska,
    mkvmerge_available,
)
from quipclipper.search import search as engine_search
from quipclipper.subtitles import resolve_subtitles
from quipclipper_web import __version__, library, media
from quipclipper_web.config import Settings
from quipclipper_web.jobs import JobRegistry

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


class ClipRequest(BaseModel):
    """JSON body for POST /api/clip — full CLI option parity."""

    path: str
    # Time range — either explicit start/end OR search-based (query + match_index).
    start: float | None = None
    end: float | None = None
    query: str | None = None
    match_index: int = 0
    track: int | None = None
    # Clip options
    kind: str = Field("video", pattern="^(audio|video|gif)$")
    lossless: bool = True
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


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    app = FastAPI(title="quipclipper-web", version=__version__)
    app.state.settings = settings
    jobs = JobRegistry(max_workers=settings.max_concurrent_jobs)

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
            "save_to_library": settings.save_to_library,
            "auth_required": settings.auth_required,
            "jellyfin_enabled": settings.jellyfin_url is not None,
            "max_concurrent_jobs": settings.max_concurrent_jobs,
        }

    # --- library browsing ----------------------------------------------------

    def _resolve(path: str) -> Path:
        """Resolve a client path within the media roots, mapping errors to HTTP."""
        try:
            return library.resolve_within_roots(path, settings.media_roots)
        except library.PathNotAllowed as exc:
            raise HTTPException(status_code=403, detail=str(exc))

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

    # --- file inspection -----------------------------------------------------

    @app.get("/api/items")
    def item(path: str = Query(...)) -> dict:
        p = _resolve(path)
        if not p.exists():
            raise HTTPException(status_code=404, detail=f"Not found: {path}")
        if not p.is_file():
            raise HTTPException(status_code=400, detail=f"Not a file: {path}")
        try:
            return media.item_info(p)
        except RuntimeError as exc:  # ffprobe failure
            raise HTTPException(status_code=500, detail=str(exc))

    @app.get("/api/items/subtitles")
    def subtitles(path: str = Query(...), track: int | None = None) -> Response:
        p = _resolve(path)
        try:
            resolved = resolve_subtitles(subs=None, video=p, track=track)
        except ValueError as exc:  # multiple tracks, none auto-selectable
            raise HTTPException(status_code=409, detail=str(exc))
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc))
        return Response(content=media.cues_to_vtt(resolved.cues), media_type="text/vtt")

    # --- dialogue search -------------------------------------------------------

    @app.get("/api/search")
    def search_dialogue(
        path: str = Query(...),
        query: str = Query(..., min_length=1),
        track: int | None = None,
        limit: int = Query(10, ge=1, le=100),
        min_score: float = Query(60.0, ge=0, le=100),
        max_span: int = Query(3, ge=1, le=10),
    ) -> dict:
        p = _resolve(path)
        try:
            resolved = resolve_subtitles(subs=None, video=p, track=track)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc))

        matches = engine_search(
            query,
            resolved.cues,
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

    # --- media streaming -------------------------------------------------------

    @app.get("/api/media")
    def stream(path: str = Query(...)) -> FileResponse:
        """Serve a source file with HTTP range support (best-effort preview).

        Starlette's FileResponse handles Range requests (206), so the browser can
        seek. Whether it actually decodes depends on the codec/container.
        """
        p = _resolve(path)
        if not p.is_file():
            raise HTTPException(status_code=404, detail=f"Not found: {path}")
        mime = _BROWSER_MIME.get(p.suffix.lower(), "application/octet-stream")
        return FileResponse(p, media_type=mime)

    # --- clipping & jobs -------------------------------------------------------

    @app.post("/api/clip")
    def create_clip(req: ClipRequest) -> dict:
        video = _resolve(req.path)
        if not video.is_file():
            raise HTTPException(status_code=404, detail=f"Not found: {req.path}")

        # Determine the clip range: explicit start/end or search-based.
        if req.start is not None and req.end is not None:
            rng = ClipRange(start=max(0.0, req.start - req.before), end=req.end + req.after)
            label = f"{req.kind} clip {format_timestamp(req.start)}–{format_timestamp(req.end)}"
        elif req.query:
            try:
                resolved = resolve_subtitles(subs=None, video=video, track=req.track)
            except (ValueError, FileNotFoundError, RuntimeError) as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            matches = engine_search(req.query, resolved.cues, limit=req.match_index + 1)
            if req.match_index >= len(matches):
                raise HTTPException(status_code=404, detail="Match index out of range.")
            m = matches[req.match_index]
            rng = compute_range(m, before=req.before, after=req.after)
            label = f"{req.kind} clip: \"{req.query}\" → {m.text[:60]}"
        else:
            raise HTTPException(
                status_code=400,
                detail="Provide either start/end or query to define the clip range.",
            )

        # Validation
        if req.split_channels and req.kind != "audio":
            raise HTTPException(status_code=400, detail="split_channels only applies to audio.")

        # Decide backend (mirrors cli.py logic).
        mkv_capable = req.lossless and req.kind in ("audio", "video") and not req.split_channels
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

        do_remux = use_mkvmerge and req.remux_first and not is_matroska(video)

        # Resolve subtitles for embedding (if applicable).
        embed_cues = None
        embed_subs_path = None
        if req.embed_subs and req.kind == "video" and req.lossless:
            try:
                sub_resolved = resolve_subtitles(subs=None, video=video, track=req.track)
                if use_mkvmerge and sub_resolved.path:
                    embed_subs_path = sub_resolved.path
                elif sub_resolved.path:
                    embed_cues = sub_resolved.cues
            except Exception:
                pass  # non-fatal: skip subtitle embedding

        # Build the output path inside clips_dir.
        clips_dir = settings.clips_dir
        clips_dir.mkdir(parents=True, exist_ok=True)

        # Determine extension.
        if use_mkvmerge:
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

        from quipclipper.clip import _timestamp_slug
        out_name = f"{video.stem}_{_timestamp_slug(rng.start)}.{ext}"
        out_path = clips_dir / out_name

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
        _out_path = out_path

        def do_cut() -> list[Path]:
            if _split_channels:
                return split_audio_channels(
                    _video, _rng,
                    audio_index=(_audio_indices[0] if _audio_indices else 0),
                    fmt=_split_format, include_lfe=_include_lfe, out=_out_path,
                )
            if _use_mkvmerge:
                return [cut_with_mkvmerge(
                    _video, _rng, kind=_kind, out=_out_path,
                    audio_indices=_audio_indices, keep_subs=True,
                    keep_chapters=_chapters, embed_subs=_embed_subs_path,
                    remux_first=_do_remux,
                )]
            return [cut_clip(
                _video, _rng, kind=_kind, lossless=_lossless, out=_out_path,
                audio_indices=_audio_indices, embed_cues=_embed_cues,
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

    # Dev mode: serve the frontend when running outside nginx.
    _frontend = Path(__file__).resolve().parent.parent.parent / "frontend"
    if _frontend.is_dir():
        app.mount("/", StaticFiles(directory=_frontend, html=True), name="frontend")

    return app


# Module-level app for `uvicorn quipclipper_web.app:app`.
app = create_app()
