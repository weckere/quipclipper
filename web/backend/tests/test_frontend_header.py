"""The workspace header's Jellyfin line under the title (found retaking the 0.2.0 screenshots).

A movie in a "Title (Year)" folder got its title twice: the heading (the folder's name),
then Jellyfin's "Title (Year)" under it. There's no JS runtime in the tests, so these read
``app.js`` for the comparison that hides the repeat and leaves episodes alone.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
pytestmark = pytest.mark.skipif(not (FRONTEND / "app.js").exists(),
                                reason="web/frontend is not part of this source tree")


def _function(name: str) -> str:
    js = (FRONTEND / "app.js").read_text()
    m = re.search(rf"^(?:async )?function {name}\(.*?^}}", js, re.M | re.S)
    assert m, f"no function {name} in app.js"
    return m.group(0)


def test_titles_compare_on_letters_and_digits_only():
    body = _function("sameTitle")
    assert 'normalize("NFKD")' in body and "toLowerCase()" in body
    assert r"[^\p{L}\p{N}]" in body, "punctuation and spaces must not make two titles differ"


def test_a_movies_line_never_repeats_the_heading():
    body = _function("showItemArtwork")
    movie = body[body.index('jf.type === "Movie"'):]
    assert '$("item-name").textContent' in movie, "the line must be checked against the heading"
    assert "sameTitle(heading, text)" in movie, "a Title (Year) folder hides the line"
    assert "sameTitle(heading, jf.title)" in movie, "a bare Title folder keeps only the year"
    assert "line.hidden = !shown" in body


def test_the_heading_is_set_before_the_line_is_filled():
    body = _function("openItem")
    assert body.index('$("item-name").textContent = cleanItemName') < \
        body.index("showItemArtwork(info.jellyfin")
