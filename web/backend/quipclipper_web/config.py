"""Runtime settings, read from the environment.

A single :class:`Settings` object drives the app. Docker sets these via
environment variables; the NixOS module sets the same fields through its
options, so the two deployments behave identically. Keeping config in plain
``os.environ`` (rather than a settings framework) keeps the dependency surface
small for Phase 0.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from quipclipper.transcribe import WhisperConfig


def _roots(raw: str | None) -> list[Path]:
    if not raw:
        return []
    return [Path(p).expanduser() for p in raw.split(os.pathsep) if p.strip()]


def _path_map(raw: str | None) -> tuple[tuple[str, str], ...]:
    """``QC_JELLYFIN_PATH_MAP`` / ``QC_PATH_MAP``: ``<their path>=<local path>`` pairs,
    separated like ``QC_MEDIA_ROOTS`` (``:``) or by ``;``, e.g.
    ``/movies=/srv/movies:/shows=/srv/shows``."""
    pairs = []
    for part in (raw or "").replace(";", os.pathsep).split(os.pathsep):
        src, sep, dst = part.partition("=")
        if sep and src.strip() and dst.strip():
            pairs.append((src.strip().rstrip("/") or "/", dst.strip().rstrip("/") or "/"))
    return tuple(pairs)


def _switch(raw: str | None, default: bool) -> bool:
    """An on/off variable: 1, true, yes or on is on; 0, false, no or off is off (any case);
    unset, empty or anything else is ``default``."""
    v = (raw or "").strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return default


def _secret(e, name: str) -> str:
    """A secret from ``<name>`` or from the file ``<name>_FILE`` names (the NixOS module
    passes it as a systemd credential). An unreadable file is a missing secret, not a
    failed start: the feature it gates reports itself off."""
    if e.get(name):
        return e[name].strip()
    path = e.get(name + "_FILE")
    if not path:
        return ""
    try:
        return Path(path).expanduser().read_text(encoding="utf-8").strip()
    except OSError:
        return ""


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
    proxy_secret: str
    api_tokens: frozenset[str]
    youtube_dir: Path | None
    public_url: str
    jellyfin_url: str = ""
    jellyfin_api_key: str = field(default="", repr=False)
    jellyfin_path_map: tuple[tuple[str, str], ...] = ()
    # Re-index hooks (M20): paths other apps report -> the same files here, and tokens
    # that may only queue re-indexes (POST /api/index/file).
    path_map: tuple[tuple[str, str], ...] = ()
    hook_tokens: frozenset[str] = field(default=frozenset(), repr=False)
    # Seconds between the timestamp-only checks for subtitles changed since indexing
    # (quipclipper_web.index_changes); 0 = never.
    index_check_interval: float = 3600.0
    # Machine transcription (M21): whisper.cpp and its models, from QC_WHISPER_* (the
    # NixOS module's transcribe.* options). No model = the feature reports itself off.
    whisper: WhisperConfig = field(default_factory=WhisperConfig)
    # Pairing (quipclipper.pair/1): apps such as LeadStinger ask for access and a person
    # approves them; QC_PAIRING=0 turns every /api/pair* route into 404.
    pairing: bool = True
    # The name apps show for this server (QC_SERVER_NAME); empty = the host name.
    server_name: str = ""
    # LAN advertising (QC_ADVERTISE=1, the `quipclipper-web advertise` process): the port
    # browsers use (QC_ADVERTISE_PORT), which may differ from QC_PORT behind nginx.
    advertise: bool = False
    advertise_port: int | None = None

    @property
    def jellyfin_enabled(self) -> bool:
        return bool(self.jellyfin_url and self.jellyfin_api_key)

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
            # SECURITY (defence in depth): a shared secret the front proxy (nginx)
            # injects on every request it forwards. When set, the backend rejects
            # any request that lacks it (except /api/health), so reaching the
            # backend port directly — bypassing nginx and its basic-auth gate, e.g.
            # another container on the same Docker network — gets 403. Empty (the
            # default) disables the check; the loopback bind is then the only
            # boundary. See web/nginx/50-quip-proxy.sh for the nginx side.
            proxy_secret=e.get("QC_PROXY_SECRET", ""),
            # API tokens for programmatic clients (agents/scripts). Comma-separated
            # so several can be valid at once (rotation). A client presents a token
            # as X-API-Key, Authorization: Bearer, or the HTTP Basic password; the
            # backend then treats the request as an authenticated machine client
            # and skips the browser CSRF-header requirement. Empty = disabled.
            api_tokens=frozenset(
                t.strip() for t in e.get("QC_API_TOKEN", "").split(",") if t.strip()
            ),
            # Where downloaded YouTube videos are written. Unset = they stay
            # private in the state dir (state/youtube/<id>/video.mp4). Set it to
            # a real, browsable folder — ideally under a media root — so a
            # download lands as "<Title> [<id>].mp4" (with a .vtt sidecar) and is
            # a first-class library item, visible to other tools too.
            youtube_dir=(
                Path(e["QC_YOUTUBE_DIR"]).expanduser() if e.get("QC_YOUTUBE_DIR") else None
            ),
            # This server's public base URL, as other machines reach it (e.g.
            # https://quip.example, or with a path prefix). "Send to LeadStinger"
            # puts it in the leadstinger://quip link so LeadStinger knows where to
            # download the clip from. It comes from here, never from the request's
            # Host header, which a proxy or a crafted request could change. Empty =
            # links are off (the button says how to turn them on).
            public_url=e.get("QC_PUBLIC_URL", "").strip().rstrip("/"),
            # Jellyfin, for "Now playing" only (M15, D-025): which sessions are playing
            # and where their playheads are. The server's own API key (QC_JELLYFIN_API_KEY,
            # or a file named by QC_JELLYFIN_API_KEY_FILE), not a user sign-in. Jellyfin
            # reports its own (container) paths, so QC_JELLYFIN_PATH_MAP maps them onto
            # the media roots. No URL or no key = the feature is off.
            jellyfin_url=e.get("QC_JELLYFIN_URL", "").strip().rstrip("/"),
            jellyfin_api_key=_secret(e, "QC_JELLYFIN_API_KEY"),
            jellyfin_path_map=_path_map(e.get("QC_JELLYFIN_PATH_MAP")),
            # Re-index hooks (M20): Bazarr, Sonarr and Radarr tell the server a file's
            # subtitles changed (POST /api/index/file). They usually run in containers
            # and report their own mount points, so QC_PATH_MAP maps those prefixes onto
            # the paths here (same format as QC_JELLYFIN_PATH_MAP). QC_HOOK_TOKEN (or a
            # file named by QC_HOOK_TOKEN_FILE; comma-separated for rotation) is a
            # low-privilege token: it may only queue re-indexes, nothing else.
            path_map=_path_map(e.get("QC_PATH_MAP")),
            # How often (seconds) a background pass looks for files whose subtitles
            # changed since indexing: listings and stats only, at idle priority, and it
            # never re-indexes (the library page offers a button). 0 = never.
            index_check_interval=float(e.get("QC_INDEX_CHECK_INTERVAL", "3600") or 0),
            hook_tokens=frozenset(
                t.strip() for t in _secret(e, "QC_HOOK_TOKEN").split(",") if t.strip()
            ),
            whisper=WhisperConfig.from_env(e),
            pairing=_switch(e.get("QC_PAIRING"), default=True),
            server_name=e.get("QC_SERVER_NAME", "").strip(),
            advertise=_switch(e.get("QC_ADVERTISE"), default=False),
            advertise_port=int(e["QC_ADVERTISE_PORT"]) if e.get("QC_ADVERTISE_PORT") else None,
        )
