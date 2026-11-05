"""The YouTube backfill buttons (subtitles and, M6, metadata): the folder card shows both
for any folder with files, even after the library home hid them, and the item view offers
Fetch metadata off /api/items' ``metadata_missing``. These read ``app.js`` and
``index.html`` (no JS runtime in the tests)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
pytestmark = pytest.mark.skipif(not (FRONTEND / "app.js").exists(),
                                reason="web/frontend is not part of this source tree")


def _function(name: str) -> str:
    m = re.search(rf"^(?:async )?function {name}\(.*?^}}", (FRONTEND / "app.js").read_text(),
                  re.M | re.S)
    assert m, f"no function {name} in app.js"
    return m.group(0)


def test_folder_status_shows_both_backfill_buttons():
    """libIndexHome hides them; renderIndexStatus (every folder) must show them again."""
    assert '"fetch-folder-meta-btn"' in _function("libIndexHome")
    status = _function("renderIndexStatus")
    assert '["fetch-folder-subs-btn", "fetch-folder-meta-btn"]' in status
    assert "data.total === 0" in status


def test_folder_fetch_uses_both_endpoints_and_one_cancel():
    fetch = _function("startFolderFetch")
    assert "fetchFolderJob" in fetch and '$("fetch-folder-cancel")' in fetch
    js = (FRONTEND / "app.js").read_text()
    assert '"/api/search/folder/fetch-subs"' in js
    assert '"/api/search/folder/fetch-metadata"' in js


def test_item_offers_fetch_metadata_when_sidecars_are_missing():
    html = (FRONTEND / "index.html").read_text()
    assert 'id="ss-fetch-meta" data-action="item.fetch-metadata"' in html
    assert 'id="fetch-folder-meta-btn" data-action="index.fetch-metadata"' in html
    js = (FRONTEND / "app.js").read_text()
    assert "info.metadata_missing" in js and "wireFetchMeta(path, info.metadata_missing)" in js
    assert "/api/media/fetch-metadata?path=" in _function("wireFetchMeta")
