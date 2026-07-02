"""Runtime settings, read from the environment.

A single :class:`Settings` object drives the app. Docker sets these via
environment variables; the NixOS module sets the same fields through its
options, so the two deployments behave identically. Keeping config in plain
``os.environ`` (rather than a settings framework) keeps the dependency surface
small for Phase 0.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _roots(raw: str | None) -> list[Path]:
    if not raw:
        return []
    return [Path(p).expanduser() for p in raw.split(os.pathsep) if p.strip()]


@dataclass(frozen=True)
class Settings:
    """Resolved configuration for one running instance."""

    media_roots: list[Path]
    clips_dir: Path
    clips_url_prefix: str
    state_dir: Path
    listen_address: str
    listen_port: int
    max_concurrent_jobs: int
    auth_required: bool
    subtitle_langs: list[str]

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        e = os.environ if env is None else env
        return cls(
            media_roots=_roots(e.get("QC_MEDIA_ROOTS")),
            clips_dir=Path(e.get("QC_CLIPS_DIR", "./clips")).expanduser(),
            # URL prefix where a front proxy (nginx) serves the clips dir
            # directly. Empty = downloads go through the backend API.
            clips_url_prefix=e.get("QC_CLIPS_URL_PREFIX", "").rstrip("/"),
            state_dir=Path(e.get("QC_STATE_DIR", "./state")).expanduser(),
            # SECURITY: the backend has NO auth of its own — the nginx front is
            # the only gate (HTTP basic auth when QC_PASSWORD is set). The safe
            # default binds to loopback so the API is only reachable through that
            # proxy. Setting QC_BIND to a non-loopback address without a proxy in
            # front exposes every endpoint (media browse, clip, delete) unguarded
            # on the LAN. Keep it 127.0.0.1 unless nginx is doing the auth.
            listen_address=e.get("QC_BIND", "127.0.0.1"),
            listen_port=int(e.get("QC_PORT", "8000")),
            max_concurrent_jobs=int(e.get("QC_MAX_CONCURRENT_JOBS", "2")),
            # The gate itself is enforced at the nginx layer (HTTP basic auth
            # when QC_PASSWORD is set — see web/nginx/40-quip-auth.sh). The
            # backend only reports whether a password was configured.
            auth_required=bool(e.get("QC_PASSWORD")),
            # Ordered subtitle-language preference for auto-selection (B14),
            # comma-separated (e.g. "eng,spa"); empty falls back to English.
            subtitle_langs=[
                s.strip() for s in e.get("QC_SUBTITLE_LANGS", "").split(",") if s.strip()
            ] or ["en"],
        )
