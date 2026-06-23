"""EPUB3 media-overlay reader (Storyteller-style synced audiobooks)."""

import zipfile

import pytest

from quipclipper.epub import (
    _smil_clock,
    extract_audio_member,
    is_media_overlay_epub,
    read_epub,
    to_cues,
)
from quipclipper.search import search

CONTAINER = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf"
    media-type="application/oebps-package+xml"/></rootfiles>
</container>"""

OPF = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>The Test Book</dc:title>
    <dc:creator>Ada Lovelace</dc:creator>
    <meta property="media:duration">0:00:08.000</meta>
  </metadata>
  <manifest>
    <item id="cover" href="cover.xhtml" media-type="application/xhtml+xml"/>
    <item id="ch1" href="ch1.xhtml" media-type="application/xhtml+xml" media-overlay="ch1_mo"/>
    <item id="ch2" href="ch2.xhtml" media-type="application/xhtml+xml" media-overlay="ch2_mo"/>
    <item id="ch1_mo" href="ch1.smil" media-type="application/smil+xml"/>
    <item id="ch2_mo" href="ch2.smil" media-type="application/smil+xml"/>
    <item id="aud1" href="audio/part1.mp4" media-type="audio/mp4"/>
    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
  </manifest>
  <spine>
    <itemref idref="cover"/>
    <itemref idref="ch1"/>
    <itemref idref="ch2"/>
  </spine>
</package>"""

COVER = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><body><p>Cover, no narration.</p></body></html>"""

CH1 = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><body>
<h1 id="h">Chapter One</h1>
<p><span id="s0">You will become one with the Borg.</span>
   <span id="s1">Resistance is futile.</span></p>
</body></html>"""

CH2 = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><body>
<h2 id="h">Chapter Two</h2>
<p><span id="s0">The second chapter shares an audio file.</span></p>
</body></html>"""

# ch1 uses part1.mp4 from 0s; ch2 *shares* part1.mp4 starting at 5s (the Ishmael
# split_003/004 pattern: consecutive chapters as sub-ranges of one audio file).
CH1_SMIL = """<?xml version="1.0"?>
<smil xmlns="http://www.w3.org/ns/SMIL" xmlns:epub="http://www.idpf.org/2007/ops" version="3.0">
 <body><seq id="s" epub:textref="ch1.xhtml" epub:type="chapter">
  <par id="p0"><text src="ch1.xhtml#s0"/><audio src="audio/part1.mp4" clipBegin="0.000s" clipEnd="3.500s"/></par>
  <par id="p1"><text src="ch1.xhtml#s1"/><audio src="audio/part1.mp4" clipBegin="3.500s" clipEnd="5.000s"/></par>
 </seq></body>
</smil>"""

CH2_SMIL = """<?xml version="1.0"?>
<smil xmlns="http://www.w3.org/ns/SMIL" xmlns:epub="http://www.idpf.org/2007/ops" version="3.0">
 <body><seq id="s" epub:textref="ch2.xhtml" epub:type="chapter">
  <par id="p0"><text src="ch2.xhtml#s0"/><audio src="audio/part1.mp4" clipBegin="5.000s" clipEnd="8.000s"/></par>
 </seq></body>
</smil>"""

# NCX names only ch1 ("Opening"); ch2 is absent, so it falls back to its heading.
NCX = """<?xml version="1.0"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
 <navMap>
  <navPoint id="n1"><navLabel><text>Opening</text></navLabel><content src="ch1.xhtml"/></navPoint>
 </navMap>
</ncx>"""

AUDIO_BYTES = b"FAKE-M4A-AUDIO-PAYLOAD-" + b"\x00\x01\x02\x03" * 64


def _build_epub(path, *, with_overlays=True):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", CONTAINER)
        opf = OPF if with_overlays else OPF.replace('media-overlay="ch1_mo"', "").replace(
            'media-overlay="ch2_mo"', ""
        ).replace('media-type="application/smil+xml"', 'media-type="text/plain"')
        z.writestr("OEBPS/content.opf", opf)
        z.writestr("OEBPS/cover.xhtml", COVER)
        z.writestr("OEBPS/ch1.xhtml", CH1)
        z.writestr("OEBPS/ch2.xhtml", CH2)
        z.writestr("OEBPS/ch1.smil", CH1_SMIL)
        z.writestr("OEBPS/ch2.smil", CH2_SMIL)
        z.writestr("OEBPS/toc.ncx", NCX)
        z.writestr("OEBPS/audio/part1.mp4", AUDIO_BYTES)
    return path


@pytest.fixture
def epub(tmp_path):
    return _build_epub(tmp_path / "book.epub")


# --- detection ---------------------------------------------------------------

def test_is_media_overlay_epub_true(epub):
    assert is_media_overlay_epub(epub) is True


def test_is_media_overlay_epub_false_for_plain_epub(tmp_path):
    plain = _build_epub(tmp_path / "plain.epub", with_overlays=False)
    assert is_media_overlay_epub(plain) is False


def test_is_media_overlay_epub_false_for_non_epub(tmp_path):
    notzip = tmp_path / "x.epub"
    notzip.write_bytes(b"not a zip")
    assert is_media_overlay_epub(notzip) is False


# --- parsing -----------------------------------------------------------------

def test_read_epub_metadata_and_chapters(epub):
    book = read_epub(epub)
    assert book.title == "The Test Book"
    assert book.author == "Ada Lovelace"
    # The cover has no media-overlay -> skipped; two narrated chapters remain.
    # ch1's title comes from the NCX ("Opening"); ch2 isn't in the NCX, so it
    # falls back to its <h2> heading.
    assert [c.title for c in book.chapters] == ["Opening", "Chapter Two"]


def test_cues_carry_text_timing_and_audio_member(epub):
    book = read_epub(epub)
    assert [c.text for c in book.cues] == [
        "You will become one with the Borg.",
        "Resistance is futile.",
        "The second chapter shares an audio file.",
    ]
    assert [(c.start, c.end) for c in book.cues] == [(0.0, 3.5), (3.5, 5.0), (5.0, 8.0)]
    # All three cues resolve to the shared, normalized audio member.
    assert {c.audio for c in book.cues} == {"OEBPS/audio/part1.mp4"}
    # Indexes are contiguous reading-order positions.
    assert [c.index for c in book.cues] == [0, 1, 2]


def test_chapter_cues_slice(epub):
    book = read_epub(epub)
    ch2 = book.chapters[1]
    cues = book.chapter_cues(ch2)
    assert len(cues) == 1
    assert cues[0].start == 5.0  # shared file, non-zero offset for the 2nd chapter


def test_read_epub_rejects_non_overlay(tmp_path):
    plain = _build_epub(tmp_path / "plain.epub", with_overlays=False)
    with pytest.raises(ValueError):
        read_epub(plain)


# --- search → audio mapping (the clip path) ----------------------------------

def test_search_match_maps_back_to_audio(epub):
    book = read_epub(epub)
    matches = search("become one with the borg", to_cues(book.cues))
    assert matches
    mc = book.cues[matches[0].index]
    assert mc.audio == "OEBPS/audio/part1.mp4"
    assert (mc.start, mc.end) == (0.0, 3.5)


# --- audio extraction --------------------------------------------------------

def test_extract_audio_member_roundtrips_bytes(epub, tmp_path):
    out = extract_audio_member(epub, "OEBPS/audio/part1.mp4", tmp_path / "out.mp4")
    assert out.read_bytes() == AUDIO_BYTES


# --- SMIL clock parsing ------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("1.646s", 1.646),
    ("500ms", 0.5),
    ("1.5min", 90.0),
    ("2h", 7200.0),
    ("0:00:01.646", 1.646),
    ("01:02.5", 62.5),
    ("", None),
    ("garbage", None),
])
def test_smil_clock(value, expected):
    assert _smil_clock(value) == expected
