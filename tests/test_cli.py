import pytest
import typer

import quipclipper.cli as cli
from quipclipper.models import Cue, Match


def mk(i: int) -> Match:
    cue = Cue(index=i, start=float(i), end=float(i) + 1, text=f"line{i}")
    return Match(score=100.0, cues=(cue,), text=f"line{i}")


def test_parse_tracks_none():
    assert cli._parse_tracks(None) is None
    assert cli._parse_tracks("") is None


def test_parse_tracks_list():
    assert cli._parse_tracks("0,2") == [0, 2]
    assert cli._parse_tracks(" 1 , 3 ") == [1, 3]


def test_parse_tracks_invalid():
    with pytest.raises(typer.BadParameter):
        cli._parse_tracks("a,b")


def test_select_single_candidate_autoselects(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("should not prompt for a single candidate")

    monkeypatch.setattr(cli.typer, "prompt", boom)
    cands = [mk(0)]
    assert cli._select_matches(cands) == cands


def test_select_parses_indices(monkeypatch):
    cands = [mk(0), mk(1), mk(2), mk(3)]
    monkeypatch.setattr(cli.typer, "prompt", lambda *a, **k: "0,2")
    assert cli._select_matches(cands) == [cands[0], cands[2]]


def test_select_all(monkeypatch):
    cands = [mk(0), mk(1), mk(2)]
    monkeypatch.setattr(cli.typer, "prompt", lambda *a, **k: "all")
    assert cli._select_matches(cands) == cands


def test_select_dedupes_preserving_order(monkeypatch):
    cands = [mk(0), mk(1), mk(2)]
    monkeypatch.setattr(cli.typer, "prompt", lambda *a, **k: "2,1,2,1")
    assert cli._select_matches(cands) == [cands[2], cands[1]]


def test_select_out_of_range_rejected(monkeypatch):
    cands = [mk(0), mk(1)]
    monkeypatch.setattr(cli.typer, "prompt", lambda *a, **k: "5")
    with pytest.raises(typer.BadParameter):
        cli._select_matches(cands)


def test_select_empty_rejected(monkeypatch):
    cands = [mk(0), mk(1)]
    monkeypatch.setattr(cli.typer, "prompt", lambda *a, **k: " , ")
    with pytest.raises(typer.BadParameter):
        cli._select_matches(cands)
