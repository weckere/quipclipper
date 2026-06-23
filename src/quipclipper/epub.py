"""Read EPUB3 Media Overlays (e.g. Storyteller audiobooks) as clippable cues.

An EPUB3 with media overlays syncs text to narration: each content document is
linked (via the OPF ``media-overlay`` attribute) to a SMIL overlay whose ``<par>``
elements tie a text fragment — an element ``id`` in the XHTML — to an audio clip
(``<audio src clipBegin clipEnd>``). The audio itself is embedded in the EPUB zip.

We surface that as quipclipper cues — one per ``<par>`` — each carrying the source
audio member and its in-file time span, so a matched line can be clipped straight
out of the embedded audio without unpacking the whole book. The data model is
deliberately *per-cue* (not per-chapter): consecutive chapters often share one
audio file as sub-ranges, and a chapter can span two files, but a single cue
always maps cleanly to one ``(audio member, start, end)``.

Naming/placement of synced EPUBs varies (``<Title> (readaloud).epub`` from newer
Storyteller, ``aligned/<Title>.epub`` from older), so :func:`is_media_overlay_epub`
detects them by the manifest (a SMIL item / ``media-overlay`` link), never by name.
"""

from __future__ import annotations

import posixpath
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

from quipclipper.models import Cue

SMIL_MEDIA_TYPE = "application/smil+xml"
_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}


@dataclass(frozen=True)
class MOCue:
    """A media-overlay cue: narration text plus its time span *within one embedded
    audio member*. ``index`` is the cue's position in reading order across the book."""

    index: int
    start: float  # seconds, relative to `audio`
    end: float
    text: str
    audio: str  # zip member name, e.g. "OEBPS/Audio/00001-00001.mp4"

    def as_cue(self) -> Cue:
        return Cue(self.index, self.start, self.end, self.text)


@dataclass(frozen=True)
class EpubChapter:
    """A content document with narration. ``first_cue``/``n_cues`` index into
    :attr:`EpubBook.cues` (the flat reading-order list)."""

    index: int
    title: str | None
    href: str  # zip member of the content document
    first_cue: int
    n_cues: int


@dataclass(frozen=True)
class EpubBook:
    path: Path
    title: str | None
    author: str | None
    cues: list[MOCue]  # flat, in reading order
    chapters: list[EpubChapter]

    def chapter_cues(self, ch: EpubChapter) -> list[MOCue]:
        return self.cues[ch.first_cue : ch.first_cue + ch.n_cues]


# --- low-level helpers -------------------------------------------------------

def _localname(tag: str) -> str:
    """Strip an XML namespace: '{...}item' -> 'item'."""
    return tag.rsplit("}", 1)[-1].lower()


def _resolve(base_dir: str, href: str) -> str:
    """Resolve a relative href (dropping any #fragment) to a zip member name."""
    href = href.split("#", 1)[0]
    joined = posixpath.join(base_dir, href) if base_dir else href
    return posixpath.normpath(joined)


def _smil_clock(value: str | None) -> float | None:
    """Parse a SMIL clock value to seconds.

    Handles the timecount forms (``12.5s``, ``500ms``, ``1.5min``, ``2h``) and the
    clock forms (``0:00:01.646``, ``01:02.5``). Storyteller emits ``1.646s``."""
    if not value:
        return None
    v = value.strip()
    try:
        if v.endswith("ms"):
            return float(v[:-2]) / 1000.0
        if v.endswith("min"):
            return float(v[:-3]) * 60.0
        if v.endswith("h"):
            return float(v[:-1]) * 3600.0
        if v.endswith("s"):
            return float(v[:-1])
        if ":" in v:
            sec = 0.0
            for part in v.split(":"):
                sec = sec * 60.0 + float(part)
            return sec
        return float(v)
    except ValueError:
        return None


def _find_opf_path(z: zipfile.ZipFile) -> str:
    """The OPF package-document path, from META-INF/container.xml."""
    root = ET.fromstring(z.read("META-INF/container.xml"))
    for el in root.iter():
        if _localname(el.tag) == "rootfile" and el.get("full-path"):
            return el.get("full-path")
    raise ValueError("EPUB container.xml declares no rootfile full-path")


@dataclass
class _Manifest:
    items: dict[str, dict]  # item id -> {href, media_type, media_overlay}
    spine: list[str]  # ordered item idrefs
    opf_dir: str
    title: str | None
    author: str | None


def _parse_opf(z: zipfile.ZipFile, opf_path: str) -> _Manifest:
    root = ET.fromstring(z.read(opf_path))
    items: dict[str, dict] = {}
    spine: list[str] = []
    title = author = None
    for el in root.iter():
        ln = _localname(el.tag)
        if ln == "item":
            iid = el.get("id")
            if iid:
                items[iid] = {
                    "href": el.get("href"),
                    "media_type": el.get("media-type"),
                    "media_overlay": el.get("media-overlay"),
                }
        elif ln == "itemref":
            if el.get("idref"):
                spine.append(el.get("idref"))
        elif ln == "title" and title is None:
            title = (el.text or "").strip() or None
        elif ln == "creator" and author is None:
            author = (el.text or "").strip() or None
    return _Manifest(items, spine, posixpath.dirname(opf_path), title, author)


def _parse_xhtml(z: zipfile.ZipFile, member: str) -> tuple[dict[str, str], str | None]:
    """Return ({element id -> cleaned text}, first-heading text) for a content doc."""
    try:
        root = ET.fromstring(z.read(member))
    except (ET.ParseError, KeyError):
        return {}, None
    idtext: dict[str, str] = {}
    heading = None
    for el in root.iter():
        eid = el.get("id")
        if eid:
            text = " ".join("".join(el.itertext()).split())
            if text:
                idtext[eid] = text
        if heading is None and _localname(el.tag) in _HEADING_TAGS:
            htext = " ".join("".join(el.itertext()).split())
            if htext:
                heading = htext
    return idtext, heading


_NCX_MEDIA_TYPE = "application/x-dtbncx+xml"


def _parse_toc(z: zipfile.ZipFile, man: _Manifest) -> dict[str, str]:
    """Map content-document member -> chapter title from the NCX table of contents.

    The NCX is the most reliable title source (real chapter names, not the first
    heading). Returns {} if there's no NCX or it won't parse."""
    ncx_href = next(
        (it["href"] for it in man.items.values()
         if it.get("media_type") == _NCX_MEDIA_TYPE and it.get("href")),
        None,
    )
    if not ncx_href:
        return {}
    ncx_member = _resolve(man.opf_dir, ncx_href)
    try:
        root = ET.fromstring(z.read(ncx_member))
    except (ET.ParseError, KeyError):
        return {}
    ncx_dir = posixpath.dirname(ncx_member)
    titles: dict[str, str] = {}
    for np in root.iter():
        if _localname(np.tag) != "navpoint":
            continue
        label = src = None
        for child in np:  # direct children only — don't pull nested navPoints' labels
            ln = _localname(child.tag)
            if ln == "navlabel":
                label = " ".join("".join(child.itertext()).split()) or None
            elif ln == "content":
                src = child.get("src")
        if src and label:
            titles.setdefault(_resolve(ncx_dir, src), label)
    return titles


def _parse_smil(z: zipfile.ZipFile, member: str) -> list[tuple[str | None, str, float, float]]:
    """Return [(text_src, audio_member, start, end)] for each ``<par>`` with audio."""
    smil_dir = posixpath.dirname(member)
    try:
        root = ET.fromstring(z.read(member))
    except (ET.ParseError, KeyError):
        return []
    pars: list[tuple[str | None, str, float, float]] = []
    for par in root.iter():
        if _localname(par.tag) != "par":
            continue
        text_src = audio_src = begin = end = None
        for child in par.iter():
            ln = _localname(child.tag)
            if ln == "text":
                text_src = child.get("src")
            elif ln == "audio":
                audio_src = child.get("src")
                begin, end = child.get("clipBegin"), child.get("clipEnd")
        if not audio_src:
            continue  # a text-only par (no narration) — skip
        start = _smil_clock(begin) or 0.0
        stop = _smil_clock(end)
        if stop is None or stop < start:
            stop = start
        pars.append((text_src, _resolve(smil_dir, audio_src), start, stop))
    return pars


# --- public API --------------------------------------------------------------

def is_media_overlay_epub(path: str | Path) -> bool:
    """True if ``path`` is an EPUB whose manifest declares media overlays (a SMIL
    item or a ``media-overlay`` link) — i.e. a synced text+audio book."""
    try:
        with zipfile.ZipFile(path) as z:
            root = ET.fromstring(z.read(_find_opf_path(z)))
            for el in root.iter():
                if _localname(el.tag) == "item" and (
                    el.get("media-type") == SMIL_MEDIA_TYPE or el.get("media-overlay")
                ):
                    return True
    except (zipfile.BadZipFile, ET.ParseError, KeyError, ValueError, OSError):
        return False
    return False


def read_epub(path: str | Path) -> EpubBook:
    """Parse an EPUB3 media-overlay book into chapters and reading-order cues.

    Raises ValueError if it isn't a media-overlay EPUB or has no narrated cues."""
    path = Path(path)
    with zipfile.ZipFile(path) as z:
        man = _parse_opf(z, _find_opf_path(z))
        toc_titles = _parse_toc(z, man)
        idtext_cache: dict[str, dict[str, str]] = {}
        heading_cache: dict[str, str | None] = {}

        def content_info(member: str) -> tuple[dict[str, str], str | None]:
            if member not in idtext_cache:
                idtext_cache[member], heading_cache[member] = _parse_xhtml(z, member)
            return idtext_cache[member], heading_cache[member]

        cues: list[MOCue] = []
        chapters: list[EpubChapter] = []
        for idref in man.spine:
            item = man.items.get(idref)
            if not item or not item.get("media_overlay"):
                continue  # no narration for this doc (cover/nav/front matter)
            smil_item = man.items.get(item["media_overlay"])
            if not smil_item or not smil_item.get("href"):
                continue
            content_member = _resolve(man.opf_dir, item["href"])
            smil_member = _resolve(man.opf_dir, smil_item["href"])
            idtext, heading = content_info(content_member)
            first = len(cues)
            for text_src, audio_member, start, stop in _parse_smil(z, smil_member):
                text = ""
                if text_src:
                    frag = text_src.partition("#")[2]
                    text = idtext.get(frag, "")
                if not text:
                    continue
                cues.append(MOCue(len(cues), start, stop, text, audio_member))
            if len(cues) > first:
                title = toc_titles.get(content_member) or heading
                chapters.append(
                    EpubChapter(len(chapters), title, content_member, first, len(cues) - first)
                )

    if not cues:
        raise ValueError(f"No media-overlay cues found in {path.name} (not a synced EPUB?).")
    return EpubBook(path, man.title, man.author, cues, chapters)


def to_cues(mocues: list[MOCue]) -> list[Cue]:
    """Plain Cues (text + timing) for the search engine; map a Match back to a
    MOCue by index to recover its audio member."""
    return [c.as_cue() for c in mocues]


def extract_audio_member(epub_path: str | Path, member: str, out_path: str | Path) -> Path:
    """Copy one embedded audio member out of the EPUB zip to ``out_path`` (so
    ffmpeg can read it), returning the path."""
    out_path = Path(out_path)
    with zipfile.ZipFile(epub_path) as z, z.open(member) as src, open(out_path, "wb") as dst:
        shutil.copyfileobj(src, dst)
    return out_path
