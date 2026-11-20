"""Signed media links (M27, D-133): a link a player such as VLC opens with no header.

A paired app (scope ``media.link``) asks ``GET /api/media/link?path=…`` and gets
``/api/media/s/<token>/<file name>``: the raw file, Range requests and all, for
:data:`LIFETIME` seconds. VLC can't send ``X-API-Key``, so the proof is in the URL.

The token is ``<payload>.<sig>``. ``payload`` is base64url compact JSON::

    {"v": 1, "k": "file" | "subs", "p": <resolved absolute path>, "e": <expiry, unix s>,
     "c": <issuing client id, "" for a person>,
     "t": <embedded track> | "x": <sidecar path>, "f": "srt" | "vtt"}   # subs only

``sig`` is HMAC-SHA256 over :data:`CONTEXT` + payload with a 32-byte key kept in
``state_dir/media-link.key`` (0600, made on first use; deleting it kills every link).
:meth:`MediaLinks.verify` checks, on every request: the signature (constant time), the
expiry, and that the issuing client is still paired with ``media.link`` or full access.
The app re-checks the path is inside the media roots. Every failure is the same None, so
the route answers one 404 for a bad, expired, revoked or unknown link.

Tokens never land in the access log in full: :class:`RedactLinks` rewrites the path in
uvicorn's access log records; the bundled nginx serves the route with ``access_log off``.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import tempfile
import time
from pathlib import Path
from typing import Callable
from urllib.parse import quote

from . import pairing

KEY_FILE = "media-link.key"
LIFETIME = 6 * 3600           # s: long enough to watch a film through, pausing
CONTEXT = b"quipclipper.media-link/1\n"
SCOPE = "media.link"
ROUTE_PREFIX = "/api/media/s/"
SUB_FORMATS = ("srt", "vtt")

_B64 = re.compile(r"^[A-Za-z0-9_-]+$")
TOKEN_RE = r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"
_KEY_HEX = re.compile(r"^[0-9a-f]{64}$")
# The token in a logged request line: everything between the prefix and the next "/".
_LOGGED = re.compile(re.escape(ROUTE_PREFIX) + r"[^/?\s]+")
REDACTED = ROUTE_PREFIX + "<redacted>"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def load_key(state_dir: Path, create: bool = True) -> bytes | None:
    """The signing key in ``state_dir/media-link.key``; made (0600) when missing and
    ``create``. Two processes starting together agree: the first to link its file wins.
    None when it can't be read or made (no links are issued or accepted then)."""
    path = Path(state_dir) / KEY_FILE
    for _ in range(3):
        try:
            found = path.read_text(encoding="ascii").strip()
        except FileNotFoundError:
            found = None
        except (OSError, UnicodeDecodeError):
            return None
        if found is not None:
            return bytes.fromhex(found) if _KEY_HEX.match(found) else None
        if not create:
            return None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{KEY_FILE}.", suffix=".tmp")
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w", encoding="ascii") as fh:
                    fh.write(secrets.token_hex(32) + "\n")
                os.link(tmp, path)        # fails if another process got there first
            except FileExistsError:
                continue
            finally:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)
        except OSError:
            return None
    return None


def link_name(name: str) -> str:
    """A file name as the decorative last segment of a link (VLC shows it)."""
    return quote(name, safe="") or "media"


def _same_language(tag: str | None, want: str) -> bool:
    """``eng`` matches ``en`` and ``en-US``; an untagged track matches nothing."""
    tag, want = (tag or "").lower(), want.lower()
    return bool(tag) and tag != "und" and (tag.startswith(want) or want.startswith(tag))


def pick_subtitles(video: Path, language: str) -> dict | None:
    """The subtitles a link for ``video`` in ``language`` serves: the first non-forced
    text sidecar in that language (``{"x": path}``), else the first non-forced text track
    embedded in it (``{"t": index}``); full dialogue before SDH. None when there is none.
    Sidecars come first, as they do everywhere else (resolve_subtitles)."""
    from quipclipper import subtitles as subs

    sidecars = []
    for p in subs._sidecar_candidates(video):
        lang, forced, hi, _ = subs._sidecar_tags(video.stem, p)
        if not forced and _same_language(lang, language):
            sidecars.append((hi, p.name.lower(), p))
    if sidecars:
        return {"x": str(min(sidecars)[2])}
    try:
        tracks = subs.list_embedded_tracks(video)
    except RuntimeError:
        return None
    found = [(t.hearing_impaired, t.index) for t in tracks
             if not t.forced and subs.is_text_subtitle(t) and _same_language(t.language, language)]
    return {"t": min(found)[1]} if found else None


class MediaLinks:
    """Issue and check signed links. ``clock`` is injectable for tests."""

    def __init__(self, state_dir: Path, clients: pairing.ClientStore,
                 clock: Callable[[], float] = time.time, lifetime: int = LIFETIME):
        self.state_dir = Path(state_dir)
        self.clients = clients
        self.clock = clock
        self.lifetime = lifetime

    def _sign(self, key: bytes, payload: str) -> str:
        return _b64(hmac.new(key, CONTEXT + payload.encode("ascii"), hashlib.sha256).digest())

    def issue(self, fields: dict, client_id: str) -> tuple[str, int]:
        """A token for ``fields`` (``k``, ``p`` and the subtitle fields) issued by
        ``client_id`` ("" for a person), and its expiry. Raises OSError without a key."""
        key = load_key(self.state_dir)
        if key is None:
            raise OSError("The media link key in the state dir can't be read or made.")
        expires = int(self.clock()) + self.lifetime
        body = {"v": 1, **fields, "e": expires, "c": client_id}
        payload = _b64(json.dumps(body, separators=(",", ":"), ensure_ascii=False)
                       .encode("utf-8", "surrogatepass"))
        return f"{payload}.{self._sign(key, payload)}", expires

    def verify(self, token: str) -> dict | None:
        """The signed fields of a current link, or None: a bad signature, a damaged token,
        an expired link, or a client that was revoked or lost the scope."""
        payload, dot, sig = token.partition(".")
        if not dot or not _B64.match(payload) or not _B64.match(sig) or len(token) > 8192:
            return None
        key = load_key(self.state_dir, create=False)
        if key is None:
            return None
        if not hmac.compare_digest(self._sign(key, payload), sig):
            return None
        try:
            body = json.loads(_unb64(payload).decode("utf-8", "surrogatepass"))
        except (ValueError, UnicodeDecodeError):
            return None
        if (not isinstance(body, dict) or body.get("v") != 1
                or body.get("k") not in ("file", "subs")
                or not isinstance(body.get("p"), str)
                or not isinstance(body.get("e"), int) or isinstance(body.get("e"), bool)
                or not isinstance(body.get("c"), str)):
            return None
        if self.clock() >= body["e"]:
            return None
        if body["c"] and not self._client_may(body["c"]):
            return None
        return body

    def _client_may(self, client_id: str) -> bool:
        """Whether a link's issuer is still paired with media.link or full access."""
        row = self.clients.get(client_id)
        if row is None:
            return False
        scopes = row.get("scopes")
        return scopes is None or SCOPE in scopes


class RedactLinks(logging.Filter):
    """Rewrite a signed link's token in uvicorn's access log records to ``<redacted>``."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and any(isinstance(a, str) and ROUTE_PREFIX in a
                                           for a in args):
            record.args = tuple(_LOGGED.sub(REDACTED, a) if isinstance(a, str) else a
                                for a in args)
        if isinstance(record.msg, str) and ROUTE_PREFIX in record.msg:
            record.msg = _LOGGED.sub(REDACTED, record.msg)
        return True


def install_log_filter() -> None:
    """Put :class:`RedactLinks` on uvicorn's access logger, once."""
    log = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, RedactLinks) for f in log.filters):
        log.addFilter(RedactLinks())
