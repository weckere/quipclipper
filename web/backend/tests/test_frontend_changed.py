"""Subtitles changed since indexing (M20): the rows' "changed" chip, the banner a folder
view and search results show with its Re-index them button, and the library home's
"Index out of date: N files" badge. These read the frontend (no JS runtime in the tests)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
pytestmark = pytest.mark.skipif(not (FRONTEND / "app.js").exists(),
                                reason="web/frontend is not part of this source tree")


def _js() -> str:
    return (FRONTEND / "app.js").read_text()


def _html() -> str:
    return (FRONTEND / "index.html").read_text()


def _function(name: str) -> str:
    m = re.search(rf"^(?:async )?function {name}\(.*?^}}", _js(), re.M | re.S)
    assert m, f"no function {name} in app.js"
    return m.group(0)


def _tag(id_: str) -> str:
    m = re.search(rf'<[^>]*\bid="{id_}"[^>]*>', _html())
    assert m, id_
    return m.group(0)


def test_changed_rows_get_a_chip_with_a_name_and_hover_text():
    chip = _function("indexChipHtml")
    branch = chip[chip.index('state === "changed"'):chip.index('state === "no_subs"')]
    assert '"changed"' in branch and "subtitles changed since indexing" in branch
    assert "idx-changed" in branch and "re-indexed" in branch
    css = (FRONTEND / "styles.css").read_text()
    # The amber tint and text pair the contrast test checks.
    assert re.search(r"\.badge\.idx-changed \{[^}]*var\(--accent-soft\)[^}]*var\(--accent-text\)", css)


def test_folder_views_and_search_results_show_the_banner():
    assert "showChangedBanner(data.changed_files, data.changed)" in _function("renderIndexStatus")
    search = _function("dialogueSearch")
    assert "hideChangedBanner()" in search
    assert "showChangedBanner(data.changed_files, data.changed)" in search
    assert "hideChangedBanner()" in _function("browse")
    show = _function("showChangedBanner")
    assert "new or changed subtitles since indexing" in show
    assert 'role="status"' in _tag("changed-banner-text")
    # The skipped count in the search status line says how many changed files it left out.
    assert "changed since indexing" in _function("searchSkipped")


def test_the_banner_button_queues_exactly_those_files():
    btn = _tag("changed-banner-btn")
    assert 'data-action="index.file"' in btn and "title=" in btn and ">Re-index them<" in _html()
    js = _js()
    handler = js[js.index('$("changed-banner-btn").addEventListener'):]
    handler = handler[:handler.index("\n});") + 4]
    assert 'postJSON("/api/index/file", { paths })' in handler
    assert "changedPaths.slice()" in handler and "followReindex(" in handler


def test_the_home_badge_and_its_button():
    badge = _tag("changes-badge")
    assert 'data-action="index.changes"' in badge and 'role="status"' in badge
    assert 'data-action="index.reindex-changed"' in _tag("changes-reindex-btn")
    refresh = _function("refreshChanges")
    assert '"/api/index/changes"' in refresh and "Index out of date: " in refresh
    assert "refreshChanges()" in _function("libIndexHome")
    js = _js()
    handler = js[js.index('$("changes-reindex-btn").addEventListener'):]
    assert 'postJSON("/api/index/changes/reindex", {})' in handler[:1500]


def test_nothing_reindexes_without_a_click():
    """Only the two buttons' handlers post a re-index; no timer or render path does."""
    js = _js()
    assert js.count('"/api/index/file", {') == 1
    assert js.count('"/api/index/changes/reindex"') == 1
    for fn in ("refreshChanges", "showChangedBanner", "renderIndexStatus", "followReindex"):
        assert "postJSON" not in _function(fn), fn
