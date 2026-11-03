"""Listen (M26): a search result's audio plays in place, through one shared element.

Every row of the dialogue search results carries a Listen button (``item.listen``): it plays
the line's range, widened by the workspace's Before/After padding, as an audio-only excerpt
from ``/api/media/audio`` through the one ``<audio id="listen-audio">``, so a second row
stops the first, pressing the playing row's button stops it, a new search or opening the
workspace stops it, and a failure is said in the row and the live region. There's no JS
runtime in the tests, so these read ``app.js`` and ``index.html``.
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


def test_one_shared_audio_element_never_shown():
    html = (FRONTEND / "index.html").read_text()
    tags = re.findall(r"<audio[^>]*>", html)
    assert len(tags) == 1 and 'id="listen-audio"' in tags[0]
    assert "hidden" in tags[0] and 'preload="none"' in tags[0]


def test_every_hit_row_has_a_listen_button_before_its_other_actions():
    src = _function("dialogueSearch")
    row = src[src.index('class="hit-actions"'):]
    btn = re.search(r"<button[^>]*class=\"mark-icon-btn hit-listen\"[^>]*>", row)
    assert btn, "no Listen button in the hit row"
    assert 'data-action="item.listen"' in btn.group(0)
    assert 'title="${LISTEN_TITLE}"' in btn.group(0)
    assert 'aria-label="Listen to the line at ${hit.start_ts}"' in btn.group(0)
    assert row.index("hit-listen") < row.index("hit-bookmark")
    assert "listenHit(hit, e.currentTarget, statusEl)" in src
    assert 'e.stopPropagation(); listenHit' in src  # the row's own click (open) stays separate


def test_the_excerpt_is_the_line_plus_the_clip_bars_padding():
    url = _function("listenUrl")
    assert "hit.start - clipPadBefore()" in url and "hit.end + clipPadAfter()" in url
    assert "Math.max(0, " in url  # never before the start of the file
    assert "/api/media/audio?path=" in url and "&start=" in url and "&end=" in url


def test_starting_a_row_stops_the_one_playing_and_the_same_row_toggles():
    fn = _function("listenHit")
    toggle = fn.index("if (listening && listening.btn === btn) { stopListening(); return; }")
    assert toggle < fn.index("stopListening();", toggle + 1) < fn.index("audio.src = url")
    assert "audio.play()" in fn and 'err.name !== "AbortError"' in fn


def test_stopping_releases_the_stream_and_resets_the_button():
    fn = _function("stopListening")
    assert "audio.pause()" in fn
    assert 'audio.removeAttribute("src")' in fn and "audio.load()" in fn
    assert 'setListenState(listening.btn, "idle")' in fn
    assert "listening = null" in fn


def test_button_states_carry_names_for_mouse_and_screen_reader():
    fn = _function("setListenState")
    assert 'btn.setAttribute("aria-pressed", String(state !== "idle"))' in fn
    assert 'btn.setAttribute("aria-busy", String(state === "loading"))' in fn
    assert "btn.title = " in fn and "Stop listening" in fn
    assert "Listen to the line at ${btn.dataset.at}" in fn
    assert "Stop the line at ${btn.dataset.at}" in fn


def test_a_new_search_or_the_workspace_stops_the_preview():
    search = _function("dialogueSearch")
    assert search.index("stopListening()") < search.index('$("search-batch").hidden = true')
    assert "stopListening()" in _function("openItem")


def test_a_failure_is_said_in_the_row_and_the_live_region():
    js = _js()
    assert '$("listen-audio").addEventListener("ended", () => stopListening())' in js
    assert '$("listen-audio").addEventListener("error", () => { if (listening) listenFailed(); })' in js
    assert '$("listen-audio").addEventListener("playing"' in js
    fail = _function("listenFailed")
    assert "Listen failed: " in fail
    assert 'class="job-failed"' in fail and "announce(msg)" in fail
    assert fail.index("announce(msg)") < fail.index("stopListening()")
