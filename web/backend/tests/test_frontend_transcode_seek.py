"""Seeks past a growing HLS transcode (M20): a file whose audio the browser can't play plays
through an HLS transcode that segments it from the start into a playlist that grows until
ffmpeg finishes. The element clamps a seek past the playlist's end to that end, so such a
seek is held (``seekPlayer`` → ``holdSeek``) and made once the playlist reaches it. Under
hls.js the element's duration is the probed one from the start, so the native seek bar spans
the whole file, and a pick on it past the edge is held the same way.

There's no JS runtime in the tests, so these read ``app.js``: every seek on an HLS source
goes through ``seekPlayer``, and the hold lets go when the playlist is finished.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[2]
FRONTEND = WEB / "frontend"
pytestmark = pytest.mark.skipif(not (FRONTEND / "app.js").exists(),
                                reason="web/frontend is not part of this source tree")


def _js() -> str:
    return (FRONTEND / "app.js").read_text()


def _function(name: str, src: str | None = None, indent: str = "") -> str:
    m = re.search(rf"^{indent}(?:async )?function {name}\(.*?^{indent}}}", src or _js(), re.M | re.S)
    assert m, f"no function {name} in app.js"
    return m.group(0)


def _listener(event: str) -> str:
    m = re.search(rf'^\$\("player"\)\.addEventListener\("{event}", \(\) => \{{\n.*?^\}}\);',
                  _js(), re.M | re.S)
    assert m, f"no top-level {event} listener on the player in app.js"
    return m.group(0)


def _open_item() -> str:
    return _function("openItem")


def test_a_seek_past_the_edge_of_a_growing_playlist_is_held():
    edge = _function("beyondTranscodeEdge")
    assert "growing && t > edge" in edge
    seek = _function("seekPlayer")
    hold = seek.index("holdSeek(t, play)")
    assert seek.index("isHlsSource(player)") < hold < seek.index("player.currentTime = t")
    assert "beyondTranscodeEdge(t, transcodeEdge(player), transcodeGrowing(player))" in seek


def test_the_hold_lets_go_once_the_playlist_reaches_it_or_ends():
    hold = _function("holdSeek")
    assert "player.pause()" in hold
    assert "setInterval(tick" in hold
    # Each tick re-checks against the playlist as it is now, then makes the seek.
    assert "const edge = transcodeEdge(player);" in hold
    assert "if (!beyondTranscodeEdge(t, edge, transcodeGrowing(player)))" in hold
    assert "seekPlayer(t, held.play)" in hold  # a pick on the bar may learn to play later
    # Another item opened meanwhile drops it.
    assert "currentItem !== held.item" in hold
    # hls.js knows when the playlist has EXT-X-ENDLIST ("live" false): nothing is held then.
    growing = _function("transcodeGrowing")
    assert "hls.latestLevelDetails.live" in growing


def test_safaris_own_player_counts_a_finished_playlist_by_its_length():
    growing = _function("transcodeGrowing")
    assert "currentDuration - HLS_SEGMENT_SLACK" in growing
    slack = int(re.search(r"const HLS_SEGMENT_SLACK = (\d+);", _js()).group(1))
    app = (WEB / "backend" / "quipclipper_web" / "app.py").read_text()
    hls_time = int(re.search(r'"-hls_time", "(\d+)"', app).group(1))
    assert slack >= 2 * hls_time  # a segment can run to the next keyframe, past -hls_time


def test_every_seek_on_an_hls_source_goes_through_seek_player():
    seek_to = _function("seekTo")
    assert "isHlsSource(player)" in seek_to and "seekPlayer(seconds, true)" in seek_to
    # The re-seek once metadata arrives (a stream still attaching) is held as well.
    assert re.search(r'"loadedmetadata", \(\) => \{\n\s+if \(currentItem !== item\) return;\n'
                     r'\s+seekPlayer\(seconds, true\);', seek_to)
    item = _open_item()
    start_hls = _function("startHls", item, indent="  ")
    assert "if (at) seekPlayer(at, autoplay)" in start_hls  # a link's t=, a reload's position
    seek_abs = _function("seekAbs", item, indent="  ")
    assert "seekPlayer(t, autoplay)" in seek_abs  # the transport and the range loop
    assert "() => seekPlayer(seekTarget, false)" in item  # Safari's own HLS (iOS)
    # Nothing in the item sets the HLS element's clock past seekPlayer.
    assert not re.search(r"player\.currentTime = (at|seekTarget|seconds)\b", start_hls + seek_abs)


def test_a_new_source_or_play_drops_a_held_seek():
    assert "cancelHeldSeek();" in _function("destroyHls")
    play = _listener("play")
    assert play.rstrip().endswith("cancelHeldSeek();\n});")
    cancel = _function("cancelHeldSeek")
    assert "clearInterval(heldSeek.timer)" in cancel and "heldSeek = null" in cancel


def test_the_seek_bar_spans_the_probed_duration_under_hls_js():
    start_hls = _function("startHls", _open_item(), indent="  ")
    # hls.js keeps an override over the growing playlist's duration (MediaSource duration).
    assert "overrides: { duration: probedDuration }" in start_hls
    assert "probedDuration > 0" in start_hls  # an unprobed file attaches as before
    attach = start_hls.index("inst.attachMedia(")
    assert start_hls.index("inst.loadSource(url)") < attach


def test_the_edge_comes_from_the_playlist_under_hls_js():
    # With the duration overridden the element's seekable range is the whole file; only the
    # playlist knows how far the transcode got. Safari's own player still seeks to the edge.
    edge = _function("transcodeEdge")
    assert "hls.latestLevelDetails.edge" in edge
    assert "return seekableEnd(player);" in edge
    for name in ("seekPlayer", "holdSeek"):
        assert "seekableEnd(" not in _function(name)


def test_a_pick_on_the_seek_bar_past_the_edge_is_held_and_the_element_goes_back():
    seeking = _listener("seeking")
    assert "if (!hls) return;" in seeking  # the pipe and Safari's own player clamp themselves
    assert "beyondTranscodeEdge(t, edge, transcodeGrowing(player))" in seeking
    hold = seeking.index("holdSeek(t, play)")
    play = seeking.index("const play = !player.paused;")
    assert play < seeking.index("cancelHeldSeek();", play) < hold
    # A seek already held at that point (hls.js moving to a link's t=) stays as it is.
    assert "Math.abs(heldSeek.t - t) > 0.5" in seeking
    # The element waits where it was, never past the edge, so hls.js has data to sit on.
    assert "heldSeek.back = Math.min(lastPlayhead, edge);" in seeking
    assert seeking.rstrip().endswith("player.currentTime = heldSeek.back;\n});")
    timeupdate = _listener("timeupdate")
    assert "if (!player.seeking) lastPlayhead = player.currentTime;" in timeupdate


def test_dragging_back_inside_drops_a_bar_hold_but_not_the_element_going_back():
    seeking = _listener("seeking")
    inside = seeking[:seeking.index("holdSeek(t, play)")]
    assert "heldSeek.fromBar && Math.abs(t - heldSeek.back) > 0.01) cancelHeldSeek();" in inside


def test_the_seek_bars_own_play_on_release_keeps_the_hold():
    # Chrome's bar pauses while dragged and plays on release: that play is the pick's.
    seeking = _listener("seeking")
    assert "heldSeek.fromBar = performance.now() + BAR_RELEASE_MS;" in seeking
    play = _listener("play")
    keep = play.index("heldSeek.play = true;")
    assert play.index("heldSeek.fromBar && performance.now() < heldSeek.fromBar") < keep
    assert play.index('$("player").pause();') > keep
    assert play.index("return;", keep) < play.rindex("cancelHeldSeek();")
    ms = int(re.search(r"const BAR_RELEASE_MS = (\d+);", _js()).group(1))
    assert 200 <= ms <= 2000  # well after a release (tens of ms), well before a deliberate play
