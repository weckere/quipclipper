"""Deep links into the web app (M18): the hash routes app.js reads, built on the server.

The frontend routes on the URL's fragment, so a link is the public base URL plus
``#/<view>?<params>``. The browser builds the same links itself (``routeHash`` in
app.js); this is the copy for everything that isn't a browser: the ``link`` field on
search hits, bookmarks and clips, and ``item.link`` for the CLI and Hermes. Keep the
two encoders in step: values are percent-encoded like JavaScript's
``encodeURIComponent``, except ``/`` and ``:``, which a fragment may hold as they are
(so a path stays readable), and times are seconds with at most three decimals.
"""

from __future__ import annotations

import math
from urllib.parse import quote

# encodeURIComponent leaves A-Z a-z 0-9 - _ . ~ ! * ' ( ) alone; quote() keeps the
# letters, digits and -_.~ by itself. Add the rest, plus / and :.
_SAFE = "/:!*'()"


def fmt_time(t: float) -> str:
    """Seconds as a link writes them: 12.5, 90, 3.042 (JS: ``String(+t.toFixed(3))``)."""
    s = f"{float(t):.3f}".rstrip("0").rstrip(".")
    return s or "0"


def route(view: str, params: dict[str, object] | None = None) -> str:
    """``#/<view>?k=v&...``, skipping empty values, in the order given."""
    pairs = [f"{k}={quote(str(v), safe=_SAFE)}"
             for k, v in (params or {}).items() if v is not None and v != ""]
    return f"#/{view}" + (f"?{'&'.join(pairs)}" if pairs else "")


def item_route(path: str, at: float | None = None,
               sel: tuple[float, float] | None = None, track: int | None = None) -> str:
    """The workspace route: open ``path``, seek to ``at`` and select ``sel`` (start, end)."""
    if at is None and sel is not None:
        at = sel[0]
    return route("item", {
        "path": path,
        "t": fmt_time(at) if at is not None else None,
        "sel": f"{fmt_time(sel[0])}-{fmt_time(sel[1])}" if sel is not None else None,
        "track": track,
    })


def link(base: str, fragment: str) -> str:
    """The absolute link: ``base`` (QC_PUBLIC_URL, no trailing slash) plus ``/`` and the route."""
    return f"{base.rstrip('/')}/{fragment}"


def parse_sel(sel: str) -> tuple[float, float]:
    """``"A-B"`` (seconds) -> (A, B). Raises ValueError unless 0 <= A <= B."""
    a, sep, b = sel.partition("-")
    if not sep:
        raise ValueError(f"sel must be START-END in seconds, e.g. 61.2-64.8 (got {sel!r})")
    start, end = float(a), float(b)
    if not (math.isfinite(start) and math.isfinite(end)) or start < 0 or end < start:
        raise ValueError(f"sel must have 0 <= START <= END (got {sel!r})")
    return start, end
