"""Clip output naming: path templates and their tokens.

Shared by the web app and the CLI, so a clip named from a template comes out the
same whichever one cut it. A template is a relative path with ``{token}``
placeholders; ``/`` makes subfolders.

Tokens: ``timestamp``, ``end``, ``duration``, ``cue``, ``title``, ``show``,
``folder``, ``source``, ``year``, ``season``, ``episode``, ``date``.

``title``, ``show``, ``season``, ``episode`` and ``year`` come from what a media server
(Jellyfin) knows about the file when the caller passes it as ``meta``; otherwise they
are parsed from the file's name and folders, as ``folder`` always is (D-032).
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from quipclipper.clip import _timestamp_slug

# The default reproduces the web app's Phase 2 layout: a subfolder per source file.
# It ends in {folder}, the name {title} gave before titles came from Jellyfin, so a
# default-named clip lands where it always did (D-032).
DEFAULT_CLIP_TEMPLATE = "{source}/{timestamp}_{cue}_{folder}"
# Earlier defaults, saved in settings and old browser tabs: read as today's default.
LEGACY_DEFAULT_TEMPLATES = ("{source}/{timestamp}_{cue}_{title}",)

TEMPLATE_TOKENS = (
    "timestamp", "end", "duration", "cue", "title", "show", "folder", "source",
    "year", "season", "episode", "date",
)

_SEASON_RE = re.compile(r"(?i)^(season\s*\d+|specials|extras)$")
_YEAR_RE = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
_SXXEXX_RE = re.compile(r"(?i)s(\d{1,2})e(\d{1,2})")
_TOKEN_RE = re.compile(r"\{(\w+)\}")


def clean_title(path: Path) -> str:
    """A clean display/clip title: the containing folder's name, stepping up past
    a ``Season N``/``Specials`` folder to the series name. Falls back to the stem.
    Mirrors the frontend's ``cleanItemName``."""
    parent = path.parent
    name = parent.name
    if _SEASON_RE.match(name) and parent.parent != parent:
        name = parent.parent.name
    return name or path.stem


def slugify(text: str) -> str:
    """Filesystem-safe slug: keep word chars/spaces/apostrophes/hyphens, collapse
    whitespace, join with underscores. Strips ``/`` and ``.`` so token values
    can't inject path separators or traversal."""
    text = re.sub(r"[^\w\s'\-]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text.replace(" ", "_")


def resolve_template(template: str | None) -> str:
    """The template to render: blank or an earlier default means today's default."""
    t = (template or "").strip()
    return DEFAULT_CLIP_TEMPLATE if not t or t in LEGACY_DEFAULT_TEMPLATES else t


def _number(value) -> str:
    return f"{value:02d}" if isinstance(value, int) and not isinstance(value, bool) else ""


def clip_template_context(video: Path, rng, cue_text: str,
                          meta: dict | None = None) -> dict[str, str]:
    """Resolve the placeholder values for a clip. Media-derived tokens (cue,
    title, show, folder, source) are slugified; the rest are already filesystem-safe.

    ``meta`` is what a media server knows about the file: ``title`` (the episode or
    movie title), ``show`` (the series), ``season``, ``episode`` and ``year`` (ints).
    Each one it has replaces the value parsed from the name; the rest stay parsed."""
    stem = video.stem
    year_m = _YEAR_RE.search(stem)
    se_m = _SXXEXX_RE.search(stem)
    folder = slugify(clean_title(video))
    meta = meta or {}
    ctx = {
        "timestamp": _timestamp_slug(rng.start),
        "end": _timestamp_slug(rng.end),
        "duration": str(int(round(rng.end - rng.start))),
        "cue": slugify(cue_text[:80]) if cue_text else "",
        "title": folder,
        "show": folder,
        "folder": folder,
        "source": stem,
        "year": year_m.group(0) if year_m else "",
        "season": f"{int(se_m.group(1)):02d}" if se_m else "",
        "episode": f"{int(se_m.group(2)):02d}" if se_m else "",
        "date": date.today().isoformat(),
    }
    for key in ("title", "show"):
        if isinstance(meta.get(key), str) and slugify(meta[key]):
            ctx[key] = slugify(meta[key][:120])
    for key in ("season", "episode"):
        if _number(meta.get(key)):
            ctx[key] = _number(meta[key])
    if isinstance(meta.get("year"), int) and 1800 < meta["year"] < 2200:
        ctx["year"] = str(meta["year"])
    return ctx


def render_clip_template(template: str, ctx: dict[str, str]) -> str:
    """Render a clip path template into a safe relative path *without* extension.

    Unknown/absent tokens become empty. Per path segment, runs of separators
    left by dropped tokens are collapsed and edges trimmed; empty, ``.`` and
    ``..`` segments are dropped (callers must still check the result stays
    inside their output folder). ``/`` yields subfolders."""
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
