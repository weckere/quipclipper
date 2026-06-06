"""FastAPI application factory.

Phase 0 exposes just enough to prove the wiring end to end: a health check
(including whether the media tools are present) and a sanitized view of the
effective configuration. Later phases add the library, search, clip-job, and
bookmark routes described in ``docs/WEBAPP_PLAN.md``.
"""

from __future__ import annotations

import shutil

from fastapi import FastAPI

from quipclipper_web import __version__
from quipclipper_web.config import Settings


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

    return app


# Module-level app for `uvicorn quipclipper_web.app:app`.
app = create_app()
