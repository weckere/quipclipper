"""FastAPI application factory.

Phase 0 exposes just enough to prove the wiring end to end: a health check
(including whether the media tools are present) and a sanitized view of the
effective configuration. Later phases add the library, search, clip-job, and
bookmark routes described in ``docs/WEBAPP_PLAN.md``.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response

from quipclipper.subtitles import resolve_subtitles
from quipclipper_web import __version__, library, media
from quipclipper_web.config import Settings

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


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    app = FastAPI(title="quipclipper-web", version=__version__)
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

    return app


# Module-level app for `uvicorn quipclipper_web.app:app`.
app = create_app()
