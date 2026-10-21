"""LeadStinger set files (M14): the manifest, the trims and POST /api/leadstinger/set."""

from __future__ import annotations

import json
import time
import zipfile
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from quipclipper_web import lsset
from quipclipper_web.app import create_app
from quipclipper_web.config import Settings


# --- the pure parts ----------------------------------------------------------------------


def test_safe_name_strips_paths_and_reserved_characters():
    assert lsset.safe_name("Resistance is futile.") == "Resistance is futile"
    assert lsset.safe_name("../etc/passwd") == "etc passwd"
    assert lsset.safe_name('a:b*c?"d<e>f|g\\h') == "a b c d e f g h"
    assert lsset.safe_name("   ") == "clip"
    assert lsset.safe_name("x" * 100, limit=10) == "x" * 10


def test_trim_sits_on_the_line_inside_the_handles():
    assert lsset.trim_for(100.0, 103.5, 2.0) == lsset.Trim(2.0, 5.5)
    # A line 0.5 s into the source: the cut starts at 0, so the lead is only 0.5 s.
    assert lsset.trim_for(0.5, 2.0, 2.0) == lsset.Trim(0.5, 2.0)
    assert lsset.trim_for(10.0, 12.0, 0.0) == lsset.Trim(0.0, 2.0)
    # A whole clips-library file: no trims.
    assert lsset.trim_for(None, None, 2.0) == lsset.Trim(0.0, None)


def test_tile_entry_writes_only_what_is_set():
    t = lsset.tile_entry("Borg/01 x.flac", title="x", trim=lsset.Trim(0.0, None))
    assert t == {"file": "Borg/01 x.flac", "title": "x"}
    t = lsset.tile_entry("Borg/01 x.flac", title="x", trim=lsset.Trim(2.0, 4.5), notes="n")
    assert t == {"file": "Borg/01 x.flac", "title": "x", "trim_start": 2.0, "trim_end": 4.5,
                 "notes": "n"}


def test_write_lsset_puts_the_manifest_on_top_and_a_folder_per_set(tmp_path):
    clip = tmp_path / "a.flac"
    clip.write_bytes(b"fLaC")
    manifest = lsset.build_manifest("Borg", [lsset.tile_entry(
        "Borg/01 a.flac", title="a", trim=lsset.Trim(2.0, 3.0))])
    out = lsset.write_lsset(tmp_path / "Borg.lsset", manifest, [("Borg/01 a.flac", clip)])
    with zipfile.ZipFile(out) as zf:
        assert zf.namelist() == ["manifest.json", "Borg/01 a.flac"]
        m = json.loads(zf.read("manifest.json"))
    assert m["format"] == "leadstinger-starter" and m["version"] == 1
    assert m["sets"][0]["name"] == "Borg"
    assert not (tmp_path / "Borg.lsset.part").exists()


def test_unique_path_numbers_a_taken_name(tmp_path):
    p = tmp_path / "Borg.lsset"
    assert lsset.unique_path(p) == p
    p.write_bytes(b"")
    assert lsset.unique_path(p) == tmp_path / "Borg 2.lsset"


# --- the endpoint ------------------------------------------------------------------------


def _client(tmp_path: Path):
    media = tmp_path / "media"
    media.mkdir()
    clips = tmp_path / "clips"
    clips.mkdir()
    client = TestClient(create_app(Settings.from_env({
        "QC_MEDIA_ROOTS": str(media), "QC_CLIPS_DIR": str(clips),
        "QC_STATE_DIR": str(tmp_path / "state"),
    })))
    client.headers["X-Quipclipper"] = "1"
    return client, media, clips


def _wait(client: TestClient, job_id: str) -> dict:
    for _ in range(100):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "failed", "cancelled"):
            return job
        time.sleep(0.05)
    return job


def _fake_cut(*_args, **kwargs):
    out = Path(kwargs["out"])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"fLaC" + out.name.encode())
    return out


def _episode(media: Path, show: str, name: str) -> Path:
    ep = media / show / "Season 3" / name
    ep.parent.mkdir(parents=True, exist_ok=True)
    ep.write_bytes(b"")
    return ep


def test_set_job_cuts_flac_with_each_items_padding_and_writes_the_lsset(tmp_path):
    client, media, clips = _client(tmp_path)
    ep = _episode(media, "Star Trek - The Next Generation", "TNG S03E26.mkv")
    with patch("quipclipper_web.app.cut_clip", side_effect=_fake_cut) as cut:
        resp = client.post("/api/leadstinger/set", json={
            "name": "Borg", "items": [
                {"path": str(ep), "start": 2002.4, "end": 2004.0,
                 "title": "Resistance is futile.", "before": 2, "after": 2},
                {"path": str(ep), "start": 1.0, "end": 2.5, "title": "First line",
                 "before": 2, "after": 0.5},
            ]})
        assert resp.status_code == 200, resp.text
        job = _wait(client, resp.json()["job_id"])
    assert job["status"] == "done", job
    assert job["files"][0]["name"] == "Borg.lsset"
    calls = [c.kwargs for c in cut.call_args_list]
    assert all(k["audio_codec"] == "flac" and k["kind"] == "audio" for k in calls)
    rng = cut.call_args_list[0].args[1]
    assert (round(rng.start, 3), round(rng.end, 3)) == (2000.4, 2006.0)   # its padding
    rng2 = cut.call_args_list[1].args[1]
    assert (round(rng2.start, 3), round(rng2.end, 3)) == (0.0, 3.0)
    out = clips / "LeadStinger" / "Borg.lsset"
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
        m = json.loads(zf.read("manifest.json"))
    assert names == ["manifest.json", "Borg/01 Resistance is futile.flac", "Borg/02 First line.flac"]
    t1, t2 = m["sets"][0]["tiles"]
    assert (t1["trim_start"], t1["trim_end"]) == (2.0, 3.6)
    assert t1["title"] == "Resistance is futile."
    assert t1["notes"] == "TNG S03E26 · 00:33:22.400 · quipclipper"
    assert (t2["trim_start"], t2["trim_end"]) == (1.0, 2.5)                 # clamped lead
    # The staging folder is gone.
    assert not any((tmp_path / "state" / "lsset-staging").iterdir())


def test_no_padding_means_no_handles_and_the_set_is_named_after_the_show(tmp_path):
    client, media, clips = _client(tmp_path)
    ep = _episode(media, "Star Trek - The Next Generation", "TNG S03E26.mkv")
    with patch("quipclipper_web.app.cut_clip", side_effect=_fake_cut) as cut:
        job = _wait(client, client.post("/api/leadstinger/set", json={
            "items": [{"path": str(ep), "start": 10.0, "end": 12.5, "title": "x"}]}).json()["job_id"])
    assert job["status"] == "done", job
    rng = cut.call_args.args[1]
    assert (rng.start, rng.end) == (10.0, 12.5)
    assert job["files"][0]["name"] == "Star Trek - The Next Generation.lsset"
    with zipfile.ZipFile(clips / "LeadStinger" / "Star Trek - The Next Generation.lsset") as zf:
        tile = json.loads(zf.read("manifest.json"))["sets"][0]["tiles"][0]
    assert "trim_start" not in tile and tile["trim_end"] == 2.5


def test_a_batch_from_several_sources_is_named_quipclipper_and_the_date(tmp_path):
    client, media, clips = _client(tmp_path)
    a = _episode(media, "Show A", "a.mkv")
    b = _episode(media, "Show B", "b.mkv")
    with patch("quipclipper_web.app.cut_clip", side_effect=_fake_cut):
        job = _wait(client, client.post("/api/leadstinger/set", json={"items": [
            {"path": str(a), "start": 1, "end": 2}, {"path": str(b), "start": 1, "end": 2},
        ]}).json()["job_id"])
    assert job["status"] == "done", job
    assert job["files"][0]["name"].startswith("quipclipper 20")


def test_auto_set_name():
    from datetime import date

    assert lsset.auto_set_name(["Star Trek", "Star Trek"]) == "Star Trek"
    assert lsset.auto_set_name(["A", "B"], today=date(2026, 10, 20)) == "quipclipper 2026-10-20"
    assert lsset.auto_set_name([], today=date(2026, 10, 20)) == "quipclipper 2026-10-20"


def test_a_second_set_with_the_same_name_is_numbered(tmp_path):
    client, media, clips = _client(tmp_path)
    ep = media / "a.mkv"
    ep.write_bytes(b"")
    body = {"name": "Borg", "items": [{"path": str(ep), "start": 5, "end": 6}]}
    with patch("quipclipper_web.app.cut_clip", side_effect=_fake_cut):
        for _ in range(2):
            assert _wait(client, client.post("/api/leadstinger/set", json=body).json()["job_id"])[
                "status"] == "done"
    names = sorted(p.name for p in (clips / "LeadStinger").iterdir())
    # Each set has its hidden metadata sidecar beside it (M18).
    assert names == [".Borg 2.lsset.qc.json", ".Borg.lsset.qc.json", "Borg 2.lsset",
                     "Borg.lsset"]


def test_a_whole_clips_library_file_is_cut_without_padding_or_trims(tmp_path):
    client, media, clips = _client(tmp_path)
    clip = clips / "movie" / "00-01-05_line.mka"
    clip.parent.mkdir()
    clip.write_bytes(b"")
    with patch("quipclipper_web.app.cut_clip", side_effect=_fake_cut) as cut, \
            patch("quipclipper_web.app.media.probe_duration", return_value=3.0):
        job = _wait(client, client.post("/api/leadstinger/set", json={
            "name": "x", "items": [{"path": str(clip), "before": 5, "after": 5}]}).json()["job_id"])
    assert job["status"] == "done", job
    rng = cut.call_args.args[1]
    assert (rng.start, rng.end) == (0.0, 3.0)
    with zipfile.ZipFile(clips / "LeadStinger" / "x.lsset") as zf:
        tile = json.loads(zf.read("manifest.json"))["sets"][0]["tiles"][0]
    assert "trim_start" not in tile and "trim_end" not in tile
    assert tile["title"] == "00-01-05_line"


def test_a_bad_item_fails_the_job_with_its_message_and_writes_nothing(tmp_path):
    client, media, clips = _client(tmp_path)
    resp = client.post("/api/leadstinger/set", json={
        "name": "x", "items": [{"path": str(media / "missing.mkv"), "start": 1, "end": 2}]})
    job = _wait(client, resp.json()["job_id"])
    assert job["status"] == "failed"
    assert "item 1" in job["error"] and "Not found" in job["error"]
    assert not (clips / "LeadStinger").exists() or not any((clips / "LeadStinger").iterdir())


def test_request_limits_are_enforced(tmp_path):
    client, media, _ = _client(tmp_path)
    assert client.post("/api/leadstinger/set", json={"items": []}).status_code == 422
    assert client.post("/api/leadstinger/set", json={
        "name": "", "items": [{"path": "/x"}]}).status_code == 422
    assert client.post("/api/leadstinger/set", json={
        "items": [{"path": "/x", "before": 61}]}).status_code == 422


# --- the CLI side ------------------------------------------------------------------------


def test_cli_body_params_match_the_request_model():
    from quipclipper import commands as reg
    from quipclipper_web.app import LeadStingerSetRequest

    cmd = reg.BY_ID["leadstinger.set"]
    body = {p.name for p in cmd.params if p.where == reg.BODY}
    # The items come from stdin rows, not flags.
    assert body == set(LeadStingerSetRequest.model_fields) - {"items"}


def test_cli_turns_hits_bookmarks_and_clips_into_items():
    from quipclipper.cli import _leadstinger_items

    rows = [
        {"path": "/m/a.mkv", "start": 1.0, "end": 2.0, "text": "Hit line"},
        {"path": "/m/b.mkv", "start": 3.0, "end": 4.0, "label": "Saved", "cue": "Cue"},
        {"name": "00-01_x.mka", "path": "/clips/a/00-01_x.mka", "size": 10},
        # A bookmark's default label is "<times> — <cue>": the tile gets the line alone.
        {"path": "/m/c.mkv", "start": 5.0, "end": 6.0, "label": "44:01 – 44:04 — Hi.", "cue": "Hi.",
         "before": 3, "after": 1.5},
    ]
    # --before/--after apply to every row; a bookmark's own padding wins (D-026).
    assert _leadstinger_items(rows, None, 1.0, 0.5) == [
        {"path": "/m/a.mkv", "title": "Hit line", "start": 1.0, "end": 2.0,
         "before": 1.0, "after": 0.5},
        {"path": "/m/b.mkv", "title": "Saved", "start": 3.0, "end": 4.0,
         "before": 1.0, "after": 0.5},
        {"path": "/clips/a/00-01_x.mka", "title": "00-01_x"},
        {"path": "/m/c.mkv", "title": "Hi.", "start": 5.0, "end": 6.0,
         "before": 3.0, "after": 1.5},
    ]


def test_cli_runs_leadstinger_cli_with_json_and_data_dir(tmp_path):
    from quipclipper.cli import _leadstinger

    fake = tmp_path / "leadstinger-cli"
    fake.write_text('#!/bin/sh\nprintf \'{"args": "%s"}\\n\' "$*"\n')
    fake.chmod(0o755)
    out = _leadstinger({"leadstinger_cli": str(fake), "data_dir": str(tmp_path / "lib")},
                       ["set.import", "x.lsset"])
    assert out == {"args": f"--json --data-dir {tmp_path / 'lib'} set.import x.lsset"}
