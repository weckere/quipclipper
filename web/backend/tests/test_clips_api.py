"""The clips library API for LeadStinger (M18, D-033): metadata sidecars, the recursive,
searchable listing with its cursor and ETag, ?as=flac and the backfill."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from quipclipper import commands as reg
from quipclipper.client import build_request
from quipclipper_web import clipmeta
from quipclipper_web.app import create_app
from quipclipper_web.config import Settings

PROBE = {"codec": "flac", "channels": 6, "sample_rate": 48000, "video_codec": None,
         "duration": 7.5}


def _client(tmp_path: Path, **env):
    media = tmp_path / "media"
    media.mkdir()
    clips = tmp_path / "clips"
    clips.mkdir()
    client = TestClient(create_app(Settings.from_env({
        "QC_MEDIA_ROOTS": str(media), "QC_CLIPS_DIR": str(clips),
        "QC_STATE_DIR": str(tmp_path / "state"), **env,
    })))
    client.headers["X-Quipclipper"] = "1"
    return client, media, clips


def _wait(client: TestClient, job_id: str) -> dict:
    for _ in range(200):
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


def _clip(clips: Path, rel: str, data: bytes = b"x", mtime: float | None = None) -> Path:
    f = clips / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(data)
    if mtime is not None:
        os.utime(f, (mtime, mtime))
    return f


# --- sidecars at cut time --------------------------------------------------------------------


def test_a_clip_job_writes_a_hidden_sidecar_with_the_cut(tmp_path):
    client, media, clips = _client(tmp_path)
    src = media / "Show" / "Season 1" / "Show S01E02.mkv"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"")
    with patch("quipclipper_web.app.cut_clip", side_effect=_fake_cut), \
            patch("quipclipper_web.app.probe_audio_streams", return_value=["flac"]), \
            patch.object(clipmeta, "probe", return_value=PROBE):
        resp = client.post("/api/clip", json={
            "path": str(src), "start": 100.0, "end": 103.0, "before": 2, "after": 1.5,
            "kind": "audio", "audio_format": "flac", "cue_text": "Make it so.",
        })
        job = _wait(client, resp.json()["job_id"])
    assert job["status"] == "done", job
    (out,) = clips.rglob("*.flac")
    meta = clipmeta.read(out)
    assert meta["schema"] == clipmeta.SCHEMA and meta["version"] == clipmeta.VERSION
    assert meta["file"] == out.name
    assert meta["kind"] == "audio" and meta["origin"] == "clip"
    assert meta["source"] == {"path": str(src), "name": "Show", "url": None}
    assert meta["line"] == {"start": 100.0, "end": 103.0}
    assert meta["clip"] == {"start": 98.0, "end": 104.5}
    assert meta["padding"] == {"before": 2.0, "after": 1.5}
    assert meta["trim"] == {"start": 2.0, "end": 5.0}
    assert meta["cue"] == "Make it so."
    assert meta["channels"] == 6 and meta["codec"] == "flac" and meta["duration"] == 7.5
    assert meta["jellyfin"] is None and meta["created"].endswith("Z")
    # Hidden: a dot-file next to the clip, never listed.
    assert clipmeta.sidecar_path(out).name == f".{out.name}.qc.json"
    listed = client.get("/api/clips", params={"recursive": True}).json()["clips"]
    assert [c["name"] for c in listed] == [out.name]
    assert listed[0]["meta"]["cue"] == "Make it so."


def test_a_set_gets_a_set_sidecar_and_staging_cuts_get_none(tmp_path):
    client, media, clips = _client(tmp_path)
    ep = media / "a.mkv"
    ep.write_bytes(b"")
    body = {"name": "Borg", "items": [
        {"path": str(ep), "start": 5, "end": 6, "title": "Resistance", "before": 1}]}
    with patch("quipclipper_web.app.cut_clip", side_effect=_fake_cut):
        job = _wait(client, client.post("/api/leadstinger/set", json=body).json()["job_id"])
    assert job["status"] == "done", job
    meta = clipmeta.read(clips / "LeadStinger" / "Borg.lsset")
    assert meta["kind"] == "set" and meta["name"] == "Borg"
    item = meta["items"][0]
    assert item["title"] == "Resistance" and item["line"] == {"start": 5, "end": 6}
    assert item["trim_start"] == 1.0 and item["source"]["path"] == str(ep)
    assert not list((tmp_path / "state").rglob("*.qc.json"))
    entry = client.get("/api/clips", params={"folder": "LeadStinger"}).json()["clips"][0]
    assert entry["kind"] == "set" and entry["stream_url"] is None
    assert entry["flac_url"] is None and entry["title"] == "Borg"
    # A folder holding only sets is a folder of the library.
    assert client.get("/api/clips").json()["folders"] == ["LeadStinger"]


def test_send_to_leadstinger_records_the_real_line_and_handles(tmp_path):
    client, media, clips = _client(tmp_path, QC_PUBLIC_URL="https://quip.example")
    src = media / "a.mkv"
    src.write_bytes(b"")
    with patch("quipclipper_web.app.cut_clip", side_effect=_fake_cut), \
            patch("quipclipper_web.app.media.probe_duration", return_value=2005.0), \
            patch.object(clipmeta, "probe", return_value=PROBE):
        job = _wait(client, client.post("/api/leadstinger/quip", json={
            "path": str(src), "start": 2002.0, "end": 2004.0, "title": "Engage.",
            "before": 2, "after": 2, "set": "Bridge"}).json()["job_id"])
    assert job["status"] == "done", job
    meta = clipmeta.read(clips / job["result"]["clip"])
    assert meta["origin"] == "leadstinger.quip" and meta["set"] == "Bridge"
    assert meta["line"] == {"start": 2002.0, "end": 2004.0}
    # The tail stops at the end of the file (2005 s): 1 s of the 2 asked for.
    assert meta["padding"] == {"before": 2.0, "after": 1.0}
    assert meta["trim"] == {"start": 2.0, "end": 4.0}
    assert meta["cue"] == "Engage."


def test_a_failing_sidecar_never_fails_the_cut(tmp_path):
    client, media, clips = _client(tmp_path)
    src = media / "a.mkv"
    src.write_bytes(b"")
    with patch("quipclipper_web.app.cut_clip", side_effect=_fake_cut), \
            patch.object(clipmeta, "probe", side_effect=OSError("boom")):
        job = _wait(client, client.post("/api/clip", json={
            "path": str(src), "start": 1.0, "end": 2.0, "kind": "video",
            "lossless": False}).json()["job_id"])
    assert job["status"] == "done", job
    assert not list(clips.rglob("*.qc.json"))


def test_move_and_delete_take_the_sidecar_along(tmp_path):
    a = _clip(tmp_path, "a.flac")
    clipmeta.write(a, {"kind": "audio", "cue": "hi"})
    b = tmp_path / "sub" / "b.flac"
    b.parent.mkdir()
    clipmeta.move(a, b)
    assert not clipmeta.sidecar_path(a).exists()
    assert clipmeta.read(b)["file"] == "b.flac" and clipmeta.read(b)["cue"] == "hi"
    clipmeta.delete(b)
    assert not b.exists() and not clipmeta.sidecar_path(b).exists()


# --- the listing -----------------------------------------------------------------------------


def _library(clips: Path) -> None:
    now = time.time()
    _clip(clips, "Star_Trek/00-01-00_Make_it_so.flac", mtime=now - 300)
    _clip(clips, "Star_Trek/deeper/00-02-00_Engage.mkv", mtime=now - 200)
    _clip(clips, "Movie/00-03-00_Hello.gif", mtime=now - 100)
    _clip(clips, "LeadStinger/Bridge.lsset", mtime=now - 50)
    clipmeta.write(clips / "Movie/00-03-00_Hello.gif",
                   {"kind": "gif", "jellyfin": {"show": "Galaxy Quest", "title": None}})


def test_the_default_listing_keeps_its_old_shape(tmp_path):
    client, _, clips = _client(tmp_path)
    _library(clips)
    data = client.get("/api/clips", params={"folder": "Star_Trek"}).json()
    assert data["folder"] == "Star_Trek" and data["folders"] == ["deeper"]
    (c,) = data["clips"]
    for key in ("name", "path", "size", "folder", "download_url", "stream_url"):
        assert key in c
    assert c["folder"] == "Star_Trek" and c["clip"] == "Star_Trek/00-01-00_Make_it_so.flac"
    assert c["kind"] == "audio" and c["meta"] is None and c["title"] == "00-01-00 Make it so"
    assert c["flac_url"] == "/api/clips/download/Star_Trek/00-01-00_Make_it_so.flac?as=flac"
    assert data["contract"] == "quipclipper.clips/1"


def test_recursive_kind_query_and_sort(tmp_path):
    client, _, clips = _client(tmp_path)
    _library(clips)
    get = lambda **p: client.get("/api/clips", params=p).json()  # noqa: E731
    every = get(recursive=1)
    assert [c["clip"] for c in every["clips"]] == [
        "LeadStinger/Bridge.lsset", "Movie/00-03-00_Hello.gif",
        "Star_Trek/00-01-00_Make_it_so.flac", "Star_Trek/deeper/00-02-00_Engage.mkv"]
    assert every["count"] == 4
    assert [c["kind"] for c in get(recursive=1, kind="set")["clips"]] == ["set"]
    assert [c["name"] for c in get(recursive=1, kind="video")["clips"]] == [
        "00-02-00_Engage.mkv"]
    # q: every word, in the file name, the line, the title or the show (from the sidecar).
    assert [c["name"] for c in get(recursive=1, q="make SO")["clips"]] == [
        "00-01-00_Make_it_so.flac"]
    assert [c["name"] for c in get(recursive=1, q="galaxy quest")["clips"]] == [
        "00-03-00_Hello.gif"]
    assert get(recursive=1, q="make engage")["clips"] == []
    assert [c["name"] for c in get(folder="Star_Trek", recursive=1, q="engage")["clips"]] == [
        "00-02-00_Engage.mkv"]
    newest = [c["name"] for c in get(recursive=1, sort="newest")["clips"]]
    assert newest == ["Bridge.lsset", "00-03-00_Hello.gif", "00-02-00_Engage.mkv",
                      "00-01-00_Make_it_so.flac"]


def test_since_returns_only_what_was_added_after_the_cursor(tmp_path):
    client, _, clips = _client(tmp_path)
    _library(clips)
    first = client.get("/api/clips", params={"recursive": 1}).json()
    cursor = first["cursor"]
    assert cursor.startswith("v1.")
    again = client.get("/api/clips", params={"recursive": 1, "since": cursor}).json()
    assert again["clips"] == [] and again["cursor"] == cursor
    _clip(clips, "Star_Trek/00-09-00_New.flac", mtime=time.time() + 5)
    new = client.get("/api/clips", params={"recursive": 1, "since": cursor}).json()
    assert [c["name"] for c in new["clips"]] == ["00-09-00_New.flac"]
    assert clipmeta.parse_cursor(new["cursor"]) > clipmeta.parse_cursor(cursor)
    # A backfilled sidecar is not a new clip.
    clipmeta.write(clips / "Star_Trek/00-01-00_Make_it_so.flac", {"kind": "audio"})
    later = client.get("/api/clips", params={"recursive": 1, "since": new["cursor"]}).json()
    assert later["clips"] == []


@pytest.mark.parametrize("bad", ["yesterday", "v2.1", "v1.", "v1.-5", "1" * 50])
def test_a_bad_cursor_is_400(tmp_path, bad):
    client, _, clips = _client(tmp_path)
    resp = client.get("/api/clips", params={"since": bad})
    assert resp.status_code in (400, 422)


def test_etag_answers_304_until_something_changes(tmp_path):
    client, _, clips = _client(tmp_path)
    _library(clips)
    first = client.get("/api/clips", params={"recursive": 1})
    tag = first.headers["etag"]
    assert first.json()["etag"] == tag
    same = client.get("/api/clips", params={"recursive": 1}, headers={"If-None-Match": tag})
    assert same.status_code == 304 and same.headers["etag"] == tag
    # Weakly, as a compressing proxy (nginx gzip) hands it out, and in a list.
    for header in (f"W/{tag}", f'"other", {tag}', "*"):
        assert client.get("/api/clips", params={"recursive": 1},
                          headers={"If-None-Match": header}).status_code == 304
    assert client.get("/api/clips", params={"recursive": 1},
                      headers={"If-None-Match": '"nope"'}).status_code == 200
    # Other params are another listing.
    other = client.get("/api/clips", params={"recursive": 1, "kind": "set"},
                       headers={"If-None-Match": tag})
    assert other.status_code == 200
    # A new sidecar (metadata changed) is a change.
    clipmeta.write(clips / "Star_Trek/00-01-00_Make_it_so.flac", {"kind": "audio"})
    changed = client.get("/api/clips", params={"recursive": 1}, headers={"If-None-Match": tag})
    assert changed.status_code == 200 and changed.headers["etag"] != tag


def test_listing_params_cannot_leave_the_clips_folder(tmp_path):
    client, _, clips = _client(tmp_path)
    _library(clips)
    outside = tmp_path / "secret"
    outside.mkdir()
    (outside / "leak.flac").write_bytes(b"x")
    (tmp_path / "clips-evil").mkdir()
    for folder in ("..", "../secret", "../clips-evil", "/etc", "Star_Trek/../../secret"):
        resp = client.get("/api/clips", params={"folder": folder, "recursive": 1})
        assert resp.status_code in (403, 404), (folder, resp.status_code)
    # A symlink inside the library pointing out of it is not followed.
    (clips / "link").symlink_to(outside, target_is_directory=True)
    (clips / "leak.flac").symlink_to(outside / "leak.flac")
    names = [c["name"] for c in client.get("/api/clips", params={"recursive": 1}).json()["clips"]]
    assert "leak.flac" not in names
    for bad in ({"kind": "../x"}, {"sort": "path; rm"}):
        assert client.get("/api/clips", params=bad).status_code == 422
    # q is only text to match, never a path.
    assert client.get("/api/clips", params={"q": "../../secret", "recursive": 1}
                      ).json()["clips"] == []


def test_dot_folders_and_orphan_sidecars_stay_hidden(tmp_path):
    client, _, clips = _client(tmp_path)
    _clip(clips, ".trash/old.flac")
    _clip(clips, "A/.x.flac.qc.json", b"{}")
    _clip(clips, "A/._a.flac", b"apple")
    data = client.get("/api/clips", params={"recursive": 1}).json()
    assert data["clips"] == [] and data["folders"] == []


# --- ?as=flac --------------------------------------------------------------------------------


def _fake_ffmpeg(calls):
    def run(cmd, **_kw):
        if cmd[0] == "ffmpeg":
            calls.append(cmd)
            Path(cmd[-1]).write_bytes(b"fLaC converted")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        raise AssertionError(cmd)
    return run


def test_as_flac_converts_once_and_again_when_the_clip_changes(tmp_path):
    client, _, clips = _client(tmp_path)
    clip = _clip(clips, "Show/00-01-00_line.mka", b"matroska")
    calls: list = []
    with patch.object(clipmeta.subprocess, "run", side_effect=_fake_ffmpeg(calls)):
        r1 = client.get("/api/clips/download/Show/00-01-00_line.mka", params={"as": "flac"})
        r2 = client.get("/api/clips/download/Show/00-01-00_line.mka", params={"as": "flac"})
    assert r1.status_code == r2.status_code == 200
    assert r1.content == b"fLaC converted" and r1.headers["content-type"] == "audio/flac"
    assert 'filename="00-01-00_line.flac"' in r1.headers["content-disposition"]
    assert len(calls) == 1
    cmd = calls[0]
    assert cmd[cmd.index("-map") + 1] == "0:a:0" and cmd[cmd.index("-c:a") + 1] == "flac"
    assert "-ac" not in cmd  # every channel kept (D-024)
    cached = list((tmp_path / "state" / "clips-flac").rglob("*.flac"))
    assert len(cached) == 1
    # A re-cut (new mtime) converts again and drops the old copy, once it's an hour
    # old (a younger one may still be on its way to a client).
    os.utime(cached[0], (time.time() - 7200, time.time() - 7200))
    clip.write_bytes(b"matroska v2")
    os.utime(clip, (time.time() + 10, time.time() + 10))
    with patch.object(clipmeta.subprocess, "run", side_effect=_fake_ffmpeg(calls)):
        assert client.get("/api/clips/download/Show/00-01-00_line.mka",
                          params={"as": "flac"}).status_code == 200
    assert len(calls) == 2
    now = list((tmp_path / "state" / "clips-flac").rglob("*.flac"))
    assert len(now) == 1 and now != cached
    # Without ?as= the clip downloads as it is.
    assert client.get("/api/clips/download/Show/00-01-00_line.mka").content == b"matroska v2"


def test_as_flac_refuses_sets_and_gifs_and_passes_flac_through(tmp_path):
    client, _, clips = _client(tmp_path)
    _clip(clips, "LeadStinger/Bridge.lsset", b"PK")
    _clip(clips, "a.gif", b"GIF")
    _clip(clips, "a.flac", b"fLaC orig")
    with patch.object(clipmeta.subprocess, "run", side_effect=AssertionError("no ffmpeg")):
        assert client.get("/api/clips/download/LeadStinger/Bridge.lsset",
                          params={"as": "flac"}).status_code == 400
        assert client.get("/api/clips/download/a.gif", params={"as": "flac"}).status_code == 400
        r = client.get("/api/clips/download/a.flac", params={"as": "flac"})
    assert r.status_code == 200 and r.content == b"fLaC orig"
    assert client.get("/api/clips/download/LeadStinger/Bridge.lsset").content == b"PK"
    assert client.get("/api/clips/download/a.flac", params={"as": "mp3"}).status_code == 422


def test_as_flac_keeps_the_path_checks(tmp_path):
    client, _, clips = _client(tmp_path)
    (tmp_path / "secret.mka").write_bytes(b"x")
    with patch.object(clipmeta.subprocess, "run", side_effect=AssertionError("no ffmpeg")):
        for path in ("..%2Fsecret.mka", "%2E%2E/secret.mka", "../secret.mka"):
            resp = client.get(f"/api/clips/download/{path}", params={"as": "flac"})
            assert resp.status_code in (403, 404), (path, resp.status_code)


def test_as_flac_reports_a_clip_ffmpeg_cannot_convert(tmp_path):
    client, _, clips = _client(tmp_path)
    _clip(clips, "silent.mkv", b"x")

    def fail(cmd, **_kw):
        return subprocess.CompletedProcess(cmd, 1, "", "Stream map '0:a:0' matches no streams.")

    with patch.object(clipmeta.subprocess, "run", side_effect=fail):
        resp = client.get("/api/clips/download/silent.mkv", params={"as": "flac"})
    assert resp.status_code == 422 and "matches no streams" in resp.json()["detail"]
    assert not list((tmp_path / "state" / "clips-flac").rglob("*.part"))


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
                    reason="needs ffmpeg")
def test_as_flac_with_real_ffmpeg_keeps_every_channel(tmp_path):
    client, _, clips = _client(tmp_path)
    src = clips / "surround.mka"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=1", "-filter_complex",
                    "[0:a]pan=5.1|c0=c0|c1=c0|c2=c0|c3=c0|c4=c0|c5=c0", "-c:a", "ac3",
                    str(src)], check=True)
    resp = client.get("/api/clips/download/surround.mka", params={"as": "flac"})
    assert resp.status_code == 200 and resp.content[:4] == b"fLaC"
    out = tmp_path / "out.flac"
    out.write_bytes(resp.content)
    info = clipmeta.probe(out)
    assert info["codec"] == "flac" and info["channels"] == 6


# --- the backfill ----------------------------------------------------------------------------


def test_backfill_guesses_from_names_reattaches_moved_and_prunes_orphans(tmp_path):
    clips = tmp_path / "clips"
    a = _clip(clips, "Star Trek S03E26/00-33-20_Resistance_is_futile_Star_Trek.mka", b"aaa")
    ls = _clip(clips, "LeadStinger/Show_S01E02_00-01-05_Engage.flac", b"bb")
    with zipfile.ZipFile(clips / "LeadStinger" / "Borg.lsset", "w") as z:
        z.writestr("manifest.json", json.dumps({"sets": [{"name": "Borg", "tiles": [
            {"file": "Borg/01 x.flac", "title": "x", "trim_start": 2.0, "trim_end": 3.0}]}]}))
    moved = _clip(clips, "Elsewhere/kept.flac", b"cccc")
    # kept.flac's sidecar was left behind in its old folder (moved over SMB);
    # gone.flac was deleted and left its sidecar.
    (clips / "Old").mkdir()
    (clips / "Old" / ".kept.flac.qc.json").write_text(json.dumps(
        {"schema": clipmeta.SCHEMA, "version": 1, "file": "kept.flac", "size": 4,
         "cue": "the original line"}))
    (clips / "Old" / ".gone.flac.qc.json").write_text(json.dumps(
        {"schema": clipmeta.SCHEMA, "version": 1, "file": "gone.flac", "size": 1}))

    def by_stem(stem):
        if stem == "Star Trek S03E26":
            return "/media/tv/Star Trek/Season 3/Star Trek S03E26.mkv", {
                "id": "abc", "type": "Episode", "show": "Star Trek", "season": 3,
                "episode": 26, "title": "The Best of Both Worlds", "year": 1990, "art": {}}
        return None

    with patch.object(clipmeta, "probe", return_value=PROBE):
        stats = clipmeta.backfill(clips, jellyfin_by_stem=by_stem)
    assert stats == {"clips": 4, "written": 3, "kept": 0, "reattached": 1, "pruned": 1,
                     "jellyfin": 1, "errors": 0}
    m = clipmeta.read(a)
    assert m["backfilled"] is True and m["origin"] == "backfill"
    # The trailing {folder} of the default name is dropped once the source is known.
    assert m["clip"]["start"] == 2000.0 and m["cue"] == "Resistance is futile"
    assert m["source"]["path"] == "/media/tv/Star Trek/Season 3/Star Trek S03E26.mkv"
    assert m["source"]["name"] == "Star Trek"
    assert m["jellyfin"]["title"] == "The Best of Both Worlds" and m["channels"] == 6
    m = clipmeta.read(ls)
    assert m["source"]["stem"] == "Show_S01E02" and m["cue"] == "Engage"
    assert m["parsed"] == {"season": 1, "episode": 2, "year": None}
    s = clipmeta.read(clips / "LeadStinger" / "Borg.lsset")
    assert s["kind"] == "set" and s["name"] == "Borg" and s["items"][0]["trim_end"] == 3.0
    assert clipmeta.read(moved)["cue"] == "the original line"
    assert not list((clips / "Old").iterdir())
    # Again: nothing to do.
    with patch.object(clipmeta, "probe", side_effect=AssertionError("no probe")):
        again = clipmeta.backfill(clips)
    assert again["written"] == again["pruned"] == again["reattached"] == 0
    assert again["kept"] == 4


def test_backfill_runs_as_a_job_and_prunes_dead_flac_copies(tmp_path):
    client, _, clips = _client(tmp_path)
    _clip(clips, "a.mka", b"x")
    dead = tmp_path / "state" / "clips-flac" / ("0" * 32)
    dead.mkdir(parents=True)
    (dead / "1-1.flac").write_bytes(b"x")
    with patch.object(clipmeta, "probe", return_value=PROBE):
        job = _wait(client, client.post("/api/clips/backfill").json()["job_id"])
    assert job["status"] == "done", job
    assert job["result"]["written"] == 1 and job["result"]["flac_pruned"] == 1
    assert clipmeta.read(clips / "a.mka")["backfilled"] is True
    assert not dead.exists()


# --- the CLI ---------------------------------------------------------------------------------


def test_cli_sends_the_new_listing_and_download_params():
    method, path, query, _ = build_request(reg.BY_ID["clips.list"], {
        "folder": "Show", "recursive": True, "q": "make it so", "kind": "audio",
        "since": "v1.5", "sort": "newest"})
    assert (method, path) == ("GET", "/api/clips")
    assert dict(query) == {"folder": "Show", "recursive": "true", "q": "make it so",
                           "kind": "audio", "since": "v1.5", "sort": "newest"}
    method, path, query, _ = build_request(reg.BY_ID["clips.download"], {
        "clip_path": "Show/a b.mka", "as": "flac"})
    assert path == "/api/clips/download/Show/a%20b.mka" and query == [("as", "flac")]
    assert reg.BY_ID["clips.backfill"].path == "/api/clips/backfill"


def test_cli_flag_for_a_keyword_param():
    from typer.testing import CliRunner

    import quipclipper.cli as cli

    seen = {}

    def fake(_c, cmd, values):
        seen.update(values, id=cmd.id)
        return {"saved": "x", "size": 1}

    with patch.object(cli, "run_command", side_effect=fake):
        res = CliRunner().invoke(cli.app, ["clips.download", "a.mka", "--as", "flac"])
    assert res.exit_code == 0, res.output
    assert seen["id"] == "clips.download" and seen["as"] == "flac"
    assert seen["clip_path"] == "a.mka"
    import typer

    listing = typer.main.get_command(cli.app).commands["clips.list"]
    opts = {o for p in listing.params for o in getattr(p, "opts", [])}
    assert {"--recursive", "--query", "--kind", "--since", "--sort"} <= opts


# --- review fixes (M18 review) ----------------------------------------------------------


@pytest.mark.parametrize("ext", ["m4a", "ac3", "eac3", "opus", "dts", "thd", "mka", "wav"])
def test_every_extension_a_cut_writes_is_listed_and_keeps_its_sidecar(tmp_path, ext):
    """A lossless audio cut is codec-matched (an AAC line is .m4a): it must be listed,
    and the backfill must not take its sidecar for an orphan."""
    client, _, clips = _client(tmp_path)
    f = _clip(clips, f"LeadStinger/yt_00-01-02_hello.{ext}")
    clipmeta.write(f, {"kind": "audio", "origin": "clip"})
    listing = client.get("/api/clips", params={"recursive": 1}).json()
    assert [(c["name"], c["kind"]) for c in listing["clips"]] == [(f.name, "audio")]
    assert client.get("/api/clips").json()["folders"] == ["LeadStinger"]
    with patch.object(clipmeta, "probe", return_value=PROBE):
        stats = clipmeta.backfill(clips)
    assert stats["pruned"] == 0 and clipmeta.read(f) is not None


def test_a_clip_shows_up_only_once_it_and_its_sidecar_are_written(tmp_path):
    """A `since` poll during a cut must neither list the half-written file nor list it
    without metadata and move the cursor past it."""
    client, media, clips = _client(tmp_path)
    src = media / "a.mkv"
    src.write_bytes(b"")
    seen: dict = {}
    cursor = client.get("/api/clips", params={"recursive": 1}).json()["cursor"]

    def probe_and_peek(path):
        # The cut is done and the sidecar about to be written: a poll now sees nothing.
        seen["during"] = client.get("/api/clips", params={"recursive": 1}).json()
        return PROBE

    with patch("quipclipper_web.app.cut_clip", side_effect=_fake_cut), \
            patch("quipclipper_web.app.probe_audio_streams", return_value=["flac"]), \
            patch.object(clipmeta, "probe", side_effect=probe_and_peek):
        resp = client.post("/api/clip", json={
            "path": str(src), "start": 1.0, "end": 2.0, "kind": "audio",
            "audio_format": "flac", "cue_text": "Engage."})
        assert _wait(client, resp.json()["job_id"])["status"] == "done"
    assert seen["during"]["clips"] == []
    new = client.get("/api/clips", params={"recursive": 1, "since": cursor}).json()
    assert len(new["clips"]) == 1 and new["clips"][0]["meta"]["cue"] == "Engage."


def test_a_clip_published_after_a_newer_cursor_still_counts_as_new(tmp_path):
    client, _, clips = _client(tmp_path)
    late = _clip(clips, "a/late.flac", mtime=time.time() - 60)  # ffmpeg finished a minute ago
    with clipmeta.writing(late):
        _clip(clips, "a/other.flac")
        cursor = client.get("/api/clips", params={"recursive": 1}).json()["cursor"]
        clipmeta.publish([late])
    new = client.get("/api/clips", params={"recursive": 1, "since": cursor}).json()
    assert [c["name"] for c in new["clips"]] == ["late.flac"]


def test_nul_bytes_and_infinite_times_are_client_errors(tmp_path):
    client, media, _ = _client(tmp_path, QC_PUBLIC_URL="https://quip.example")
    for url in ("/api/clips/download/a%00b", "/api/clips/stream/a%00b",
                "/api/clips?folder=a%00b", "/api/leadstinger/link?clip=a%00",
                "/api/items/peaks?path=a%00b"):
        assert client.get(url).status_code == 400, url
    (media / "a.wav").write_bytes(b"")
    for url in (f"/api/items/snap?path={media}/a.wav&t=inf",
                f"/api/items/link?path={media}/a.wav&at=inf",
                f"/api/items/peaks?path={media}/a.wav&end=nan"):
        assert client.get(url).status_code == 422, url


def test_a_symlinked_folder_out_of_the_library_is_not_listed(tmp_path):
    client, _, clips = _client(tmp_path)
    outside = tmp_path / "outside"
    _clip(outside, "x.flac")
    (clips / "linkdir").symlink_to(outside, target_is_directory=True)
    _clip(clips, "real/y.flac")
    assert client.get("/api/clips").json()["folders"] == ["real"]


def test_etag_ignores_the_order_a_share_lists_files_in():
    rows = [("a", 1, 2, 3), ("b", 4, 5, 6)]
    assert clipmeta.etag(rows, ("x",)) == clipmeta.etag(rows[::-1], ("x",))


def test_flac_conversion_that_times_out_is_a_422_and_leaves_no_part_file(tmp_path):
    client, _, clips = _client(tmp_path)
    _clip(clips, "a.mka")
    with patch("quipclipper_web.clipmeta.subprocess.run",
               side_effect=subprocess.TimeoutExpired("ffmpeg", 900)):
        r = client.get("/api/clips/download/a.mka", params={"as": "flac"})
    assert r.status_code == 422 and "900" in r.json()["detail"]
    assert not list((tmp_path / "state" / "clips-flac").rglob("*.part"))


def test_flac_cache_keeps_under_its_limit(tmp_path):
    cache = clipmeta.FlacCache(tmp_path / "state", limit=10)
    clips = tmp_path / "clips"

    def fake_ffmpeg(cmd, **_kw):
        Path(cmd[-1]).write_bytes(b"f" * 6)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with patch("quipclipper_web.clipmeta.subprocess.run", side_effect=fake_ffmpeg):
        first = cache.get(_clip(clips, "a.mka"), "a.mka")
        time.sleep(0.01)
        second = cache.get(_clip(clips, "b.mka"), "b.mka")
    assert second.is_file() and not first.exists()
