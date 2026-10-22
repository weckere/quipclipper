"""Library rows show the dialogue index (M12 leftover): an "indexed" or "no subs" chip from
library.browse's ``index_state``, with hover text and a name a screen reader reads, kept
current after the folder is indexed. These read ``app.js`` (no JS runtime in the tests)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
pytestmark = pytest.mark.skipif(not (FRONTEND / "app.js").exists(),
                                reason="web/frontend is not part of this source tree")


def _js() -> str:
    return (FRONTEND / "app.js").read_text()


def _function(name: str) -> str:
    m = re.search(rf"^(?:async )?function {name}\(.*?^}}", _js(), re.M | re.S)
    assert m, f"no function {name} in app.js"
    return m.group(0)


def test_rows_chip_the_index_state():
    assert "indexChipHtml(e.index_state)" in _function("renderEntries")
    chip = _function("indexChipHtml")
    for state in ('"indexed"', '"no_subs"'):
        assert state in chip
    # Styled like the other chips, hover text, the visible word hidden and a full name read.
    assert 'class="badge idx-chip" title=' in chip
    assert 'aria-hidden="true"' in chip and 'class="visually-hidden"' in chip


def test_indexing_refreshes_the_chips():
    js = _js()
    # The Index button and Re-index all both re-read the folder's index states.
    assert js.count("refreshIndexChips(") == 3  # the definition and the two handlers
    refresh = _function("refreshIndexChips")
    assert "/api/library/browse?path=" in refresh and "currentBrowsePath !== folder" in refresh
