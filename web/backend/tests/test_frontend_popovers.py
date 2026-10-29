"""Popovers, Connected apps and the pairing banner (found reviewing M19 to M24).

- Play on… stayed open beside Settings or Jobs: their buttons stop their own click, so the
  page's close-on-click never ran.
- Settings opened from the footer's "how to update" sent focus to the gear on Esc.
- Connected apps showed "No apps connected yet." beside "Couldn't list…", and kept that error
  after a later listing worked.
- A pair poll sent before Allow/Deny that came back after it brought the answered row back.

There's no JS runtime in the tests, so these read ``app.js``.
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


@pytest.mark.parametrize("name", ["toggleSettings", "toggleJobs"])
def test_opening_settings_or_jobs_closes_play_on(name):
    body = _function(name)
    opened = body[body.index("if (show && !was)"):]
    assert "closePlayOn()" in opened


def test_settings_opened_from_the_footer_returns_focus_there():
    assert "back.focus()" in _function("setPopover")
    toggle = _function("toggleSettings")
    # Looked up at close: the footer is drawn again while Settings is open.
    assert "settingsOpener && settingsOpener()" in toggle
    assert '!opener.closest("[hidden]")' in toggle
    assert "setPopover(pop, $(\"settings-btn\"), show, restore, back)" in toggle
    assert "settingsOpener = null" in toggle
    assert "settingsOpener = from" in _function("openUpdates")
    js = (FRONTEND / "app.js").read_text()
    assert 'openUpdates(() => $("update-note").querySelector(".update-how"))' in js


def test_a_failed_listing_of_connected_apps_says_only_that_and_clears_later():
    body = _function("refreshClients")
    failed = body[body.index("catch (err)"):body.index("return;", body.index("catch (err)"))]
    assert '$("apps-empty").hidden = true' in failed
    after = body[body.index("if (gen !== clientsLoad) return;"):]
    assert 'if (clientsFailed) $("apps-status").textContent = ""' in after


def test_a_pair_poll_older_than_an_answer_is_dropped():
    poll = _function("refreshPairs")
    assert "const gen = ++pairsLoad;" in poll
    assert poll.count("if (gen !== pairsLoad) return;") == 2  # success and failure alike
    answer = _function("answerPair")
    assert answer.index("pairsLoad++") < answer.index("renderPairs()")
