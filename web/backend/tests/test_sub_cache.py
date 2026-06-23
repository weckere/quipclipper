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
