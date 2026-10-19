"""Clip output naming: path templates and their tokens.

Shared by the web app and the CLI, so a clip named from a template comes out the
same whichever one cut it. A template is a relative path with ``{token}``
placeholders; ``/`` makes subfolders.

Tokens: ``timestamp``, ``end``, ``duration``, ``cue``, ``title``, ``source``,
``year``, ``season``, ``episode``, ``date``.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from quipclipper.clip import _timestamp_slug

# The default reproduces the web app's Phase 2 layout: a subfolder per source file.
DEFAULT_CLIP_TEMPLATE = "{source}/{timestamp}_{cue}_{title}"

TEMPLATE_TOKENS = (
    "timestamp", "end", "duration", "cue", "title", "source",
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


def clip_template_context(video: Path, rng, cue_text: str) -> dict[str, str]:
    """Resolve the placeholder values for a clip. Media-derived tokens (cue,
    title, source) are slugified; the rest are already filesystem-safe."""
    stem = video.stem
    year_m = _YEAR_RE.search(stem)
    se_m = _SXXEXX_RE.search(stem)
    return {
        "timestamp": _timestamp_slug(rng.start),
        "end": _timestamp_slug(rng.end),
        "duration": str(int(round(rng.end - rng.start))),
        "cue": slugify(cue_text[:80]) if cue_text else "",
        "title": slugify(clean_title(video)),
        "source": stem,
        "year": year_m.group(0) if year_m else "",
        "season": f"{int(se_m.group(1)):02d}" if se_m else "",
        "episode": f"{int(se_m.group(2)):02d}" if se_m else "",
        "date": date.today().isoformat(),
    }


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
