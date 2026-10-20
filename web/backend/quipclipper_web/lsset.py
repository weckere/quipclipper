"""LeadStinger set files (``.lsset``): hand clips straight to the soundboard (M14).

A set file is a zip of a LeadStinger starter-pack folder: ``manifest.json`` at the top,
then one folder per set holding its clips. LeadStinger imports it with File ▸ Import
Set…, a drop on its window, or ``leadstinger-cli set.import``. The manifest fields are
LeadStinger's own (``starter/README.md`` and ``src/starter.rs`` in that repo); every
field is optional there, so this writes only what a clip needs.

Each clip is cut with *handles* (a few seconds either side of the line) and the tile's
``trim_start`` / ``trim_end`` sit on the line itself, so the cut points can still be
moved in LeadStinger. Nothing here touches ffmpeg or the filesystem beyond the zip.
"""

from __future__ import annotations

import json
import re
import zipfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path

FORMAT = "leadstinger-starter"
VERSION = 1
EXTENSION = ".lsset"

_UNSAFE = re.compile(r'[\x00-\x1f/\\:*?"<>|]+')


def safe_name(text: str, *, limit: int = 60, fallback: str = "clip") -> str:
    """A file or folder name from free text: no path separators or reserved characters,
    no leading dots, collapsed spaces, at most ``limit`` characters."""
    name = _UNSAFE.sub(" ", text or "")
    name = re.sub(r"\s+", " ", name).strip().lstrip(".").strip()
    name = name[:limit].rstrip(" .")
    return name or fallback


@dataclass(frozen=True)
class Trim:
    """Where the line sits inside a clip cut with handles."""

    start: float          # seconds into the clip file
    end: float | None     # seconds into the clip file; None = to the end


def trim_for(start: float | None, end: float | None, before: float) -> Trim:
    """The trims for a clip cut from ``start - before`` (to ``end`` plus any padding).

    The cut is clamped at 0, so a line near the start of the source has a shorter lead.
    No ``start`` (a whole clips-library file, cut without padding) means no trim."""
    if start is None:
        return Trim(0.0, None)
    lead = min(max(before, 0.0), max(start, 0.0))
    if end is None:
        return Trim(round(lead, 3), None)
    return Trim(round(lead, 3), round(lead + max(end - start, 0.0), 3))


def auto_set_name(source_titles: list[str], today: date | None = None) -> str:
    """A set name without asking: the movie or show when every clip comes from it,
    else ``quipclipper <date>``."""
    titles = {t.strip() for t in source_titles if t and t.strip()}
    if len(titles) == 1:
        return titles.pop()
    return f"quipclipper {(today or date.today()).isoformat()}"


def tile_entry(file: str, *, title: str, trim: Trim, notes: str = "") -> dict:
    """One manifest tile. ``file`` is relative to the pack folder (``<Set>/<name>.flac``)."""
    tile: dict = {"file": file, "title": title}
    if trim.start:
        tile["trim_start"] = trim.start
    if trim.end is not None:
        tile["trim_end"] = trim.end
    if notes:
        tile["notes"] = notes
    return tile


def build_manifest(set_name: str, tiles: list[dict], *, app: str = "quipclipper") -> dict:
    return {"format": FORMAT, "version": VERSION, "app": app,
            "sets": [{"name": set_name, "tiles": tiles}]}


def write_lsset(out: Path, manifest: dict, files: list[tuple[str, Path]]) -> Path:
    """Zip ``manifest.json`` and ``files`` (``(name inside the zip, source path)``) to ``out``.

    Audio is already compressed (FLAC), so entries are stored, not deflated. The zip is
    written next to ``out`` and renamed into place, so a reader never sees half a file."""
    tmp = out.with_name(out.name + ".part")
    try:
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_STORED) as zf:
            zf.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))
            for arcname, src in files:
                zf.write(src, arcname)
        tmp.replace(out)
    finally:
        tmp.unlink(missing_ok=True)
    return out


def unique_path(path: Path) -> Path:
    """``path``, or ``<stem> 2<suffix>``, ``<stem> 3<suffix>``… if it exists."""
    if not path.exists():
        return path
    n = 2
    while True:
        candidate = path.with_name(f"{path.stem} {n}{path.suffix}")
        if not candidate.exists():
            return candidate
        n += 1
