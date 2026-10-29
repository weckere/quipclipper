"""The search live regions are plain aria-live sections (Orca's spam filter spares those,
not role="status"; see DESIGN_NOTES "Live regions")."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
pytestmark = pytest.mark.skipif(not (FRONTEND / "index.html").exists(),
                                reason="web/frontend is not part of this source tree")


def _tag(id_: str) -> str:
    m = re.search(rf'<[^>]*\bid="{id_}"[^>]*>', (FRONTEND / "index.html").read_text())
    assert m, id_
    return m.group(0)


def test_search_live_regions_are_aria_live_divs():
    for id_ in ("announcer", "dialogue-search-status"):
        tag = _tag(id_)
        assert tag.startswith("<div"), tag
        assert 'aria-live="polite"' in tag and 'aria-atomic="true"' in tag, tag
        assert 'role="status"' not in tag, tag
