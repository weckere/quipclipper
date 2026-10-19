"""Unit tests for SubtitleCache keying, dual-keying, and dedup.

These stub out the engine's resolve_subtitles so no ffmpeg/media is needed.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from quipclipper.models import Cue
from quipclipper.subtitles import ResolvedSubtitles
from quipclipper_web import sub_cache as sub_cache_mod
from quipclipper_web.sub_cache import SubtitleCache

VIDEO = Path("/fake/movie.mkv")  # never stat'd; _source_mtime falls back to 0.0


@pytest.fixture
def cache(tmp_path):
    return SubtitleCache(tmp_path)


def _stub_resolver(monkeypatch, *, chosen_track=2, counter=None, delay=0.0):
    """Patch resolve_subtitles to return deterministic cues.

    Mimics auto-selection: when called with track=None, reports `chosen_track`
    as the resolved track. The cue text encodes the track that was requested.
    """
    def fake(subs=None, video=None, track=None, langs=None):
        if counter is not None:
            counter["n"] += 1
        if delay:
            time.sleep(delay)
        resolved_track = chosen_track if track is None else track
        return ResolvedSubtitles(
            cues=[Cue(index=0, start=0.0, end=1.0, text=f"track={resolved_track}")],
            path=None,
            track=resolved_track,
        )
    monkeypatch.setattr(sub_cache_mod, "resolve_subtitles", fake)


def test_track_is_part_of_key(cache, monkeypatch):
    _stub_resolver(monkeypatch)
    a = cache.resolve(VIDEO, track=0)
    b = cache.resolve(VIDEO, track=1)
    assert a[0].text == "track=0"
    assert b[0].text == "track=1"


def test_cache_hit_skips_extraction(cache, monkeypatch):
    counter = {"n": 0}
    _stub_resolver(monkeypatch, counter=counter)
    cache.resolve(VIDEO, track=0)
    cache.resolve(VIDEO, track=0)
    assert counter["n"] == 1


def test_auto_resolve_dual_keys_concrete_track(cache, monkeypatch):
    """Resolving track=None (as pre-indexing does) must also warm the concrete
    track key the item view later requests — without re-extracting."""
    counter = {"n": 0}
    _stub_resolver(monkeypatch, chosen_track=2, counter=counter)

    # Simulate pre-indexing: auto-select.
    cache.resolve(VIDEO, track=None)
    assert counter["n"] == 1

    # The concrete key is now warm.
    assert cache.is_cached(VIDEO, track=2)

    # Item view requests the explicit best track -> cache hit, no extraction.
    cues = cache.resolve(VIDEO, track=2)
    assert cues[0].text == "track=2"
    assert counter["n"] == 1


def test_inflight_lock_dedupes_concurrent_cold(cache, monkeypatch):
    counter = {"n": 0}
    _stub_resolver(monkeypatch, counter=counter, delay=0.2)

    results: list[str] = []
    threads = [
        threading.Thread(target=lambda: results.append(cache.resolve(VIDEO, track=7)[0].text))
        for _ in range(5)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(r == "track=7" for r in results)
    assert counter["n"] == 1  # five concurrent callers, one extraction


def test_clear_removes_all_entries_for_video(cache, monkeypatch):
    _stub_resolver(monkeypatch, chosen_track=2)
    cache.resolve(VIDEO, track=None)  # writes auto + :2
    cache.resolve(VIDEO, track=5)     # writes :5
    removed = cache.clear(VIDEO)
    assert removed >= 3
    assert not cache.is_cached(VIDEO, track=2)
    assert not cache.is_cached(VIDEO, track=5)


def test_inflight_lock_pruned_after_resolve(cache, monkeypatch):
    """The per-key lock map must not grow with every key ever extracted — R14."""
    _stub_resolver(monkeypatch)
    cache.resolve(VIDEO, track=0)
    cache.resolve(VIDEO, track=1)
    assert cache._inflight == {}


def test_write_survives_mkstemp_failure(cache, monkeypatch):
    """C8: if tempfile.mkstemp raises, the OSError handler must not NameError on
    an unbound `tmp` — the extraction result is still returned, just uncached."""
    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(sub_cache_mod.tempfile, "mkstemp", boom)
    cp = cache._cache_path(VIDEO, track=0)
    # Must not raise (no NameError); simply leaves nothing on disk.
    cache._write(cp, VIDEO, [Cue(index=0, start=0.0, end=1.0, text="hi")])
    assert not cp.exists()


def test_stale_mtime_entries_swept_on_fresh_write(tmp_path, monkeypatch):
    """R5: writing a fresh entry for a path removes entries keyed on a superseded
    source mtime, so subtitle edits don't leave old JSON behind forever."""
    import os as _os
    import time as _time

    video = tmp_path / "movie.mkv"
    video.write_bytes(b"")
    (tmp_path / "movie.srt").write_text("x")  # sidecar = the subtitle source

    cache = SubtitleCache(tmp_path / "state")
    _stub_resolver(monkeypatch, chosen_track=0)

    cache.resolve(video, track=0)
    first = list((tmp_path / "state" / "sub_cache").glob("*.json"))
    assert len(first) >= 1

    # "Edit" the subtitles: bump the sidecar mtime so the cache key changes.
    future = _time.time() + 100
    _os.utime(tmp_path / "movie.srt", (future, future))

    cache.resolve(video, track=0)
    after = list((tmp_path / "state" / "sub_cache").glob("*.json"))
    # The stale (old-mtime) entry is gone; only the fresh key remains.
    assert len(after) == 1
    assert after[0] not in first


def test_unreadable_cache_entry_is_a_miss_not_an_error(cache, monkeypatch):
    """An entry the service can't read (e.g. owned by a stale UID after a host
    user migration) must fall through to fresh extraction, not raise
    PermissionError up into a 500 on every subtitle load."""
    import os as _os

    if _os.geteuid() == 0:
        pytest.skip("chmod 000 doesn't block root")

    counter = {"n": 0}
    _stub_resolver(monkeypatch, counter=counter)
    cache.resolve(VIDEO, track=0)
    assert counter["n"] == 1

    cp = cache._cache_path(VIDEO, track=0)
    cp.chmod(0o000)
    try:
        cues = cache.resolve(VIDEO, track=0)  # must not raise
    finally:
        cp.chmod(0o600)  # restore so tmp_path cleanup works everywhere
    assert cues[0].text == "track=0"
    assert counter["n"] == 2  # unreadable entry -> treated as a miss, re-extracted


def test_cache_roundtrips_speaker(cache):
    """Speaker attribution must survive the JSON disk cache — the first
    (in-memory) resolve had it; cached reloads must not silently drop it."""
    cues = [
        Cue(index=0, start=1.0, end=2.0, text="Hello", speaker="Chris"),
        Cue(index=1, start=2.0, end=3.0, text="Yo"),  # speaker None
    ]
    cp = cache._cache_path(VIDEO, track=0)
    cache._write(cp, VIDEO, cues)
    back = cache._read(cp)
    assert [c.speaker for c in back] == ["Chris", None]
    assert [c.text for c in back] == ["Hello", "Yo"]


# --- files with no usable subtitles ------------------------------------------------


class _Track:
    def __init__(self, codec):
        self.codec = codec


def _video(tmp_path, name="movie.mkv"):
    v = tmp_path / "media" / name
    v.parent.mkdir(parents=True, exist_ok=True)
    v.write_bytes(b"")
    return v


def test_no_subs_marker_set_and_retired_by_a_new_sidecar(tmp_path, monkeypatch):
    cache = SubtitleCache(tmp_path / "state")
    v = _video(tmp_path)
    monkeypatch.setattr(sub_cache_mod, "list_embedded_tracks", lambda video: [])
    assert not cache.has_no_subs(v)
    assert cache.mark_no_subs(v)
    assert cache.has_no_subs(v)
    # A sidecar appears: the key moves to its mtime, so the marker no longer applies,
    # and the file can't be marked again while the sidecar is there.
    v.with_suffix(".srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nhi\n")
    assert not cache.has_no_subs(v)
    assert not cache.mark_no_subs(v)


@pytest.mark.parametrize("tracks,marked", [
    ([], True),
    ([_Track("hdmv_pgs_subtitle"), _Track("dvd_subtitle")], True),   # image-only = none
    ([_Track("dvd_subtitle"), _Track("subrip")], False),            # one text track
])
def test_no_subs_marker_only_when_no_text_track(tmp_path, monkeypatch, tracks, marked):
    cache = SubtitleCache(tmp_path / "state")
    v = _video(tmp_path)
    monkeypatch.setattr(sub_cache_mod, "list_embedded_tracks", lambda video: tracks)
    assert cache.mark_no_subs(v) is marked
    assert cache.has_no_subs(v) is marked


def test_no_subs_marker_not_set_when_the_probe_fails(tmp_path, monkeypatch):
    cache = SubtitleCache(tmp_path / "state")
    v = _video(tmp_path)

    def boom(video):
        raise RuntimeError("ffprobe timed out")
    monkeypatch.setattr(sub_cache_mod, "list_embedded_tracks", boom)
    assert not cache.mark_no_subs(v)
    assert not cache.has_no_subs(v)


def test_clear_and_clear_under_remove_the_marker(tmp_path, monkeypatch):
    cache = SubtitleCache(tmp_path / "state")
    a, b = _video(tmp_path, "a.mkv"), _video(tmp_path, "b.mkv")
    monkeypatch.setattr(sub_cache_mod, "list_embedded_tracks", lambda video: [])
    cache.mark_no_subs(a)
    cache.mark_no_subs(b)
    assert cache.clear(a) == 1
    assert not cache.has_no_subs(a) and cache.has_no_subs(b)
    assert cache.clear_under(tmp_path / "media") == 1
    assert not cache.has_no_subs(b)
