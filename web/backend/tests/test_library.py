"""Path-safety and browse tests — the security boundary, kept media-free."""

from __future__ import annotations

from pathlib import Path

import pytest

from quipclipper_web import library
from quipclipper_web.library import PathNotAllowed, browse, resolve_within_roots


def _make_tree(root: Path) -> None:
    (root / "Movies").mkdir()
    (root / "Movies" / "film.mkv").write_bytes(b"")
    (root / "Movies" / "film.srt").write_text("ignored")
    (root / "Movies" / "notes.txt").write_text("not a video")
    (root / ".hidden").mkdir()


def test_resolve_accepts_paths_inside_a_root(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    resolved = resolve_within_roots(tmp_path / "Movies" / "film.mkv", [tmp_path])
    assert resolved == (tmp_path / "Movies" / "film.mkv").resolve()


def test_resolve_rejects_traversal(tmp_path: Path) -> None:
    root = tmp_path / "media"
    root.mkdir()
    with pytest.raises(PathNotAllowed):
        resolve_within_roots(root / ".." / "etc" / "passwd", [root])


def test_resolve_rejects_escaping_symlink(tmp_path: Path) -> None:
    root = tmp_path / "media"
    root.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("nope")
    link = root / "link"
    link.symlink_to(secret)
    with pytest.raises(PathNotAllowed):
        resolve_within_roots(link, [root])


def test_resolve_with_no_roots_denies(tmp_path: Path) -> None:
    with pytest.raises(PathNotAllowed):
        resolve_within_roots(tmp_path, [])


def test_browse_lists_roots_when_path_empty(tmp_path: Path) -> None:
    entries = browse(None, [tmp_path])
    assert len(entries) == 1
    assert entries[0].is_dir and entries[0].path == str(tmp_path.resolve())


def test_browse_dirs_then_videos_only(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    entries = browse(str(tmp_path), [tmp_path])
    names = [e.name for e in entries]
    assert names == ["Movies"]  # .hidden skipped, no videos at this level

    movies = browse(str(tmp_path / "Movies"), [tmp_path])
    by_name = {e.name: e for e in movies}
    assert set(by_name) == {"film.mkv"}  # notes.txt skipped (not a video)
    assert by_name["film.mkv"].is_video
    assert by_name["film.mkv"].has_sidecar  # film.srt sits next to it


def test_browse_lists_audio_only_with_a_transcript(tmp_path: Path) -> None:
    """Audio sources (podcasts/audiobooks) show up only when a sidecar transcript
    sits next to them — there's nothing to search or clip without one."""
    pod = tmp_path / "Podcast"
    pod.mkdir()
    (pod / "ep1.mp3").write_bytes(b"")
    (pod / "ep1.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nhi\n")
    (pod / "ep2.m4a").write_bytes(b"")  # no transcript → excluded
    (pod / "cover.jpg").write_bytes(b"")

    by_name = {e.name: e for e in browse(str(pod), [tmp_path])}
    assert set(by_name) == {"ep1.mp3"}  # ep2 (no .srt) and cover.jpg skipped
    entry = by_name["ep1.mp3"]
    assert entry.is_audio and not entry.is_video and entry.has_sidecar


def test_browse_rejects_outside_path(tmp_path: Path) -> None:
    root = tmp_path / "media"
    root.mkdir()
    with pytest.raises(PathNotAllowed):
        browse(str(tmp_path), [root])
