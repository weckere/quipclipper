"""Send to LeadStinger on a search result row (M26): one line goes as a tile from the results.

Every row of the dialogue search results carries a Send to LeadStinger button beside Play on
Jellyfin. It does what ticking that line alone and Clip selected with the toggle on does: the
line's range with the workspace's Before/After padding as the tile's handles goes by link
(``leadstinger.quip``) when the server can name its address, else as a one-tile set file to
download (``leadstinger.set``), and the row's status slot says how it went. There's no JS
runtime in the tests, so these read ``app.js``.
"""

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


def test_every_hit_row_has_the_button_right_after_play_on_jellyfin():
    src = _function("dialogueSearch")
    row = src[src.index('class="hit-actions"'):]
    btn = re.search(r"<button[^>]*class=\"mark-icon-btn hit-ls\"[^>]*>", row)
    assert btn, "no Send to LeadStinger button in the hit row"
    assert 'data-action="leadstinger.quip leadstinger.set"' in btn.group(0)
    assert 'title="${HIT_LS_TITLE}"' in btn.group(0)
    assert 'aria-label="Send the line at ${hit.start_ts} to LeadStinger"' in btn.group(0)
    element = row[btn.start():row.index("</button>", btn.start())]
    assert 'iconSvg("leadstinger")' in element  # the cart every Send to LeadStinger control draws
    # Beside Play on Jellyfin: after it (when it is offered), before the row's status slot.
    assert row.index("hit-play-on") < row.index("hit-ls") < row.index("hit-status")
    # Not conditional on Jellyfin: the Play on button's ternary closes before it.
    assert '</button>` : "") +\n' in row[:row.index("hit-ls")]
    # Its click never opens the row.
    assert "e.stopPropagation(); sendHitToLeadStinger(hit, e.currentTarget, statusEl)" in src


def test_the_title_says_what_the_padding_becomes():
    js = _js()
    m = re.search(r'const HIT_LS_TITLE = "(.*?)" \+\n  "(.*?)";', js)
    assert m
    title = m.group(1) + m.group(2)
    assert title.startswith("Send to LeadStinger: ")
    assert "Before/After padding" in title and "handles" in title


def test_one_line_goes_the_way_the_batch_bar_sends_a_single_ticked_hit():
    fn = _function("sendHitToLeadStinger")
    # The line with the workspace's padding, titled by its text (the tile's title).
    assert "path: hit.path, start: hit.start, end: hit.end, title: hit.text" in fn
    assert "before: clipPadBefore(), after: clipPadAfter()" in fn
    # By link when the server can name its address, else a one-tile set file.
    assert "if (appConfig.link_server) await sendLeadStingerLink(item, statusEl);" in fn
    assert "else await makeLeadStingerSet([item], statusEl);" in fn
    # The batch bar takes the same two paths.
    batch = _function("runLeadStingerBatch")
    assert "await sendLeadStingerLink(items[0], status);" in batch
    assert "await makeLeadStingerSet(items, status);" in batch


def test_the_link_path_posts_the_quip_with_its_padding():
    fn = _function("sendLeadStingerLink")
    assert 'postJSON("/api/leadstinger/quip", {' in fn
    assert "before: it.before || 0, after: it.after || 0," in fn
    assert "launchLeadStinger(j.result.link, status, what);" in fn


def test_the_button_is_busy_while_it_sends_and_comes_back_either_way():
    fn = _function("sendHitToLeadStinger")
    assert "if (btn.disabled) return;" in fn
    assert "btn.disabled = true;" in fn and 'btn.setAttribute("aria-busy", "true");' in fn
    assert "} finally {" in fn
    assert fn.index("} finally {") < fn.index("btn.disabled = false;") < fn.index('btn.removeAttribute("aria-busy");')


def test_feedback_lands_in_the_rows_status_slot_and_the_live_region():
    fn = _function("sendHitToLeadStinger")
    assert 'announce(appConfig.link_server ? "Sending to LeadStinger" : "Making a LeadStinger set");' in fn
    launch = _function("launchLeadStinger")
    assert "Sent ${escapeHtml(what)} to LeadStinger" in launch and 'class="ls-again"' in launch
    link = _function("sendLeadStingerLink")
    assert "status.textContent = `Send to LeadStinger failed: ${err.message}`;" in link
    assert 'status.textContent = `Send to LeadStinger failed: ${j.error || "no link"}`;' in link
    setfn = _function("makeLeadStingerSet")
    assert "status.textContent = `LeadStinger set failed: ${err.message}`;" in setfn
    assert "status.textContent = `LeadStinger set failed: ${j.error || j.status}`;" in setfn


def test_the_set_file_fallback_downloads_or_opens_the_set():
    fn = _function("makeLeadStingerSet")
    assert 'postJSON("/api/leadstinger/set", { items: items.map(({ clip, ...it }) => it) })' in fn
    assert "launchLeadStinger(link, status, lsSetWhat(file));" in fn
    assert 'status.querySelector("a").click();' in fn
    assert "resolve();" in fn  # settles, so the row's button comes back


def test_the_rows_other_actions_are_untouched():
    src = _function("dialogueSearch")
    row = src[src.index('class="hit-actions"'):]
    for cls in ("hit-listen", "hit-bookmark", "hit-clip", "hit-link", "hit-play-on"):
        assert row.count(f'class="mark-icon-btn {cls}"') + row.count(f'make-clip-btn {cls}"') == 1, cls
    assert row.index("hit-listen") < row.index("hit-bookmark") < row.index("hit-clip") \
        < row.index("hit-link") < row.index("hit-play-on")
