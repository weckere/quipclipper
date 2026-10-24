"""The Script tab's help is a "?" button, not four lines of text (M20).

Hover or keyboard focus shows the text as a tooltip; a click or Enter keeps it open as a
popover until Esc (which returns focus to the button), another click or a click elsewhere.
The text is the button's description, so a screen reader reads the keyboard hints.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
pytestmark = pytest.mark.skipif(not (FRONTEND / "app.js").exists(),
                                reason="web/frontend is not part of this source tree")


def _html() -> str:
    return (FRONTEND / "index.html").read_text()


def test_the_help_is_a_named_button_described_by_its_text():
    html = _html()
    btn = re.search(r'<button type="button" id="script-help-btn"[^>]*>\?</button>', html).group(0)
    for attr in ('aria-label="Script help"', 'aria-describedby="script-help-text"',
                 'aria-expanded="false"', 'aria-controls="script-help-text"'):
        assert attr in btn
    tip = re.search(r'<span id="script-help-text"[^>]*>(.*?)</span>\n', html, re.S)
    assert 'role="tooltip"' in tip.group(0)
    text = tip.group(1)
    for words in ("Click a line to seek", "Start of video", "<kbd>S</kbd>", "label", "Matches"):
        assert words in text
    # The four lines are gone from the row: only the button and Follow playback are in it.
    bar = html[html.index('<div class="script-bar">'):html.index("</div>", html.index('<div class="script-bar">'))]
    assert 'class="script-hint"' not in bar and 'id="marks-hint" class="tip-wrap script-help"' in bar


def test_it_opens_on_click_and_esc_closes_it_back_to_the_button():
    js = (FRONTEND / "app.js").read_text()
    m = re.search(r"^function setScriptHelp\(.*?^}", js, re.M | re.S)
    assert m and 'classList.toggle("tip-open", open)' in m.group(0) and "aria-expanded" in m.group(0)
    assert 'if (e.key === "Escape" && setScriptHelp(false, true)) $("script-help-btn").focus();' in js
    assert 'if (!e.target.closest("#marks-hint")) setScriptHelp(false)' in js
    css = (FRONTEND / "styles.css").read_text()
    assert ".tip-wrap.tip-open:not(.tip-off) .tip { visibility: visible; opacity: 1;" in css
    assert ".help-btn:focus-visible { outline: 2px solid var(--accent)" in css


def test_the_manual_and_changelog_say_so():
    root = FRONTEND.parents[1]
    assert "**?**" in (root / "docs" / "MANUAL.md").read_text()
    log = (root / "CHANGELOG.md").read_text()
    m20 = log[log.index("### UI follow-ups (M20)"):]
    assert "Script help" in m20[:m20.index("\n### ")]
