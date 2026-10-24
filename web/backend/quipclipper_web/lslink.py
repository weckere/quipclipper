"""``leadstinger://quip`` links: send one clip to LeadStinger (M14, LeadStinger's D-076).

LeadStinger's "Clips from QuipClipper" (its ``docs/QUIPCLIPPER.md``, code ``src/quip.rs``)
opens

    leadstinger://quip?server=<base URL>&clip=<clip path>&title=<text>&start=<s>&end=<s>&set=<name>

and downloads ``<server>/api/clips/download/<clip>`` itself, with the API token the user
connected for that server (``leadstinger-cli quip.connect``). A link never carries a
token. ``start`` and ``end`` are where the line sits inside the clip, so the tile's trim
is the line and the handles either side stay for fine-tuning.

LeadStinger refuses the whole link over one bad value (an unknown parameter, a number it
can't read, a title over 200 characters), so every value is checked or tidied here before
the link is built. Nothing here touches the filesystem or the network.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import quote, urlsplit

PREFIX = "leadstinger://quip?"
PAIR_PREFIX = "leadstinger://pair?"
MAX_SECONDS = 86400.0   # LeadStinger reads start/end from 0 to 86400
TITLE_MAX = 200
SET_MAX = 100
CLIP_MAX_BYTES = 1024

# encodeURIComponent's unreserved set: LeadStinger decodes as a browser encodes, and reads
# a "+" as a space, so a literal "+" must go out as %2B (quote() does that).
_SAFE = "-_.!~*'()"
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class LinkError(ValueError):
    """A value LeadStinger would refuse."""


@dataclass(frozen=True)
class Cut:
    """A line cut with handles: what to cut from the source, and where the line sits in it."""

    start: float        # cut from here in the source (s)
    end: float          # to here (s)
    line_start: float   # the line's start inside the cut clip (s): the tile's trim start
    line_end: float     # the line's end inside the cut clip (s): the tile's trim end


def handle_cut(start: float, end: float, before: float, after: float,
               duration: float | None = None) -> Cut:
    """Cut ``[start - before, end + after]``, clamped to the source's bounds.

    ``before``/``after`` are the clip's padding, kept as handle audio around the tile's
    trim (D-026); 0 means none. A line near the start of the file gets a shorter lead,
    one near the end a shorter tail; the line itself is kept whole. ``duration``
    (seconds, when known) also clamps a line that runs past the end of the file. Raises
    ValueError for an empty line or one that starts after the end."""
    start = max(float(start), 0.0)
    end = float(end)
    if duration is not None and duration > 0:
        if start >= duration:
            raise ValueError(f"the line starts at {start:g} s, after the end of the file "
                             f"({duration:g} s)")
        end = min(end, duration)
    if not end > start:
        raise ValueError(f"the line's end ({end:g} s) must be after its start ({start:g} s)")
    cut_start = round(max(0.0, start - max(float(before), 0.0)), 3)
    cut_end = end + max(float(after), 0.0)
    if duration is not None and duration > 0:
        cut_end = min(cut_end, duration)
    return Cut(cut_start, round(cut_end, 3),
               round(start - cut_start, 3), round(end - cut_start, 3))


def normalize_server(url: str) -> str:
    """This quipclipper's public base URL as LeadStinger wants it: http(s), a host, an
    optional path prefix, no user, password, query or fragment, no trailing slash."""
    raw = (url or "").strip()
    try:
        parts = urlsplit(raw)
        parts.port  # noqa: B018 - raises ValueError for a bad port
    except ValueError as exc:
        raise LinkError(f"{raw!r} isn't a URL ({exc})") from None
    if parts.scheme.lower() not in ("http", "https"):
        raise LinkError(f"{raw!r} must be an http:// or https:// URL")
    if not parts.hostname:
        raise LinkError(f"{raw!r} has no host")
    if parts.username is not None or parts.password is not None:
        raise LinkError(f"{raw!r} must not hold a user name or password")
    if parts.query or parts.fragment or raw.endswith(("?", "#")):
        raise LinkError(f"{raw!r} must have no ?query or #fragment")
    return raw.rstrip("/")


# A Host / X-Forwarded-Host value: a DNS name or an IP literal, and an optional port.
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_HOST = re.compile(rf"^(?:{_LABEL}(?:\.{_LABEL})*\.?|\[[0-9A-Fa-f:.]+\])(?::[0-9]{{1,5}})?$")
# A path prefix (X-Forwarded-Prefix, or the ASGI root_path): /segments, nothing else.
_PATH_PREFIX = re.compile(r"^(?:/[A-Za-z0-9._~!$&'()*+,;=:@%-]+)*/?$")


def _first(value: str | None) -> str:
    """The first of a comma-separated forwarded value (the hop nearest the browser)."""
    return (value or "").split(",", 1)[0].strip()


def request_base(headers: Mapping[str, str], scheme: str, root_path: str = "") -> str | None:
    """The base URL the browser (or CLI) used to reach this app, path prefix included, or
    None when the request doesn't say well enough.

    A front proxy's ``X-Forwarded-Proto``, ``X-Forwarded-Host`` and ``X-Forwarded-Prefix``
    win (a TLS proxy in front of nginx, a prefix it strips); else the ``Host`` header and
    the connection's scheme. The bundled nginx passes ``Host`` with its port
    (``$http_host``) and leaves the ``X-Forwarded-*`` headers to a proxy in front of it.
    A client can claim any of these, so the result only ever goes back to that client,
    in a link that LeadStinger asks about before it trusts the server."""
    proto = _first(headers.get("x-forwarded-proto")).lower() or (scheme or "").lower()
    host = _first(headers.get("x-forwarded-host")) or (headers.get("host") or "").strip()
    prefix = _first(headers.get("x-forwarded-prefix")) or root_path or ""
    if proto not in ("http", "https") or not _HOST.match(host) or not _PATH_PREFIX.match(prefix):
        return None
    try:
        return normalize_server(f"{proto}://{host}{prefix.rstrip('/')}")
    except LinkError:
        return None


def link_server(public_url: str, advertised: str | None, base: str | None) -> str | None:
    """The ``server`` a link names: ``QC_PUBLIC_URL`` (a bad one raises LinkError), else the
    URL the advertiser announces, else the base URL of this request. None: none is known."""
    if public_url:
        return normalize_server(public_url)
    for candidate in (advertised, base):
        if candidate:
            try:
                return normalize_server(candidate)
            except LinkError:
                continue
    return None


def pair_link(server: str) -> str:
    """``leadstinger://pair?server=<url>``: LeadStinger asks whether to connect to this
    server, then pairs with it (``/api/pair``)."""
    return PAIR_PREFIX + "server=" + quote(normalize_server(server), safe=_SAFE)


def clip_path(path: Path, clips_dir: Path) -> str:
    """``path`` as ``/api/clips/download/{path}`` expects it: relative to the clips folder,
    ``/``-separated. Raises LinkError for a file outside it."""
    target, root = Path(path).resolve(), Path(clips_dir).resolve()
    if not target.is_relative_to(root) or target == root:
        raise LinkError(f"{path} is not in the clips library")
    return check_clip(target.relative_to(root).as_posix())


def check_clip(clip: str) -> str:
    """A clip path LeadStinger accepts: relative, no empty, ``.`` or ``..`` parts, no
    backslash or control characters, at most 1024 bytes."""
    if not clip or len(clip.encode()) > CLIP_MAX_BYTES:
        raise LinkError(f"the clip path must be 1 to {CLIP_MAX_BYTES} bytes")
    if "\\" in clip or _CONTROL.search(clip):
        raise LinkError(f"the clip path {clip!r} has a backslash or a control character")
    if any(part in ("", ".", "..") for part in clip.split("/")):
        raise LinkError(f"the clip path {clip!r} must be relative, with no empty, . or .. parts")
    return clip


def one_line(text: str | None, limit: int) -> str:
    """``text`` on one line (whitespace and control characters collapsed to single
    spaces), cut to ``limit`` characters with an ellipsis. Empty when nothing is left."""
    flat = " ".join(_CONTROL.sub(" ", text or "").split())
    if len(flat) > limit:
        flat = flat[: limit - 1].rstrip() + "…"
    return flat


def seconds(value: float, what: str) -> str:
    """A time as LeadStinger reads it: 0 to 86400, plain decimals, at most 3 places."""
    v = float(value) + 0.0   # + 0.0 turns -0.0 into 0.0
    if not math.isfinite(v) or v < 0 or v > MAX_SECONDS:
        raise LinkError(f"{what} must be 0 to {MAX_SECONDS:g} seconds (got {value!r})")
    return f"{v:.3f}".rstrip("0").rstrip(".")


def build_link(server: str, clip: str, *, title: str | None = None,
               start: float | None = None, end: float | None = None,
               set_name: str | None = None) -> str:
    """The ``leadstinger://quip?…`` link for one clip. Every value is percent-encoded the
    way ``encodeURIComponent`` does; empty optional values are left out."""
    params = [("server", normalize_server(server)), ("clip", check_clip(clip))]
    if t := one_line(title, TITLE_MAX):
        params.append(("title", t))
    s = seconds(start, "start") if start is not None else None
    e = seconds(end, "end") if end is not None else None
    if s is not None and e is not None and not float(e) > float(s):
        raise LinkError(f"the end ({e} s) must be after the start ({s} s)")
    if s is not None:
        params.append(("start", s))
    if e is not None:
        params.append(("end", e))
    if n := one_line(set_name, SET_MAX):
        params.append(("set", n))
    return PREFIX + "&".join(f"{k}={quote(v, safe=_SAFE)}" for k, v in params)
