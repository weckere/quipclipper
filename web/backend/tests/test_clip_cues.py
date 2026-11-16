"""Clips carry their subtitles as timed cues (D-109): the lines a cut holds, in the clip's
own seconds, in its metadata sidecar and in each tile of a LeadStinger set file."""

from __future__ import annotations

import json
import time
import zipfile
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from quipclipper.models import Cue
from quipclipper.subtitles import ResolvedSubtitles, SubtitleTrack
from quipclipper_web import clipmeta, lsset
from quipclipper_web.app import create_app
from quipclipper_web.config import Settings

PROBE = {"codec": "flac", "channels": 2, "sample_rate": 48000, "video_codec": None,
         "duration": 7.5}

# Around a line at 100–103 s: one that starts in the lead, the line, one that runs past
# the tail, one that ends exactly where a 98 s cut starts, and one well after.
SRT = """1
00:01:30,000 --> 00:01:38,000
Ends at the cut.

2
00:01:39,000 --> 00:01:40,500
Before.

3
00:01:40,000 --> 00:01:43,000
Make it so.

4
00:01:44,000 --> 00:01:46,000
Later.

5
00:01:50,000 --> 00:01:52,000
Outside.
"""

# The cut 98–104.5 s (line 100–103, 2 s before, 1.5 s after) in the clip's own seconds.
EXPECTED = [
    {"start": 1.0, "end": 2.5, "text": "Before."},
    {"start": 2.0, "end": 5.0, "text": "Make it so."},
    {"start": 6.0, "end": 6.5, "text": "Later."},
]


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
    for _ in range(400):
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


def _source(media: Path, subs: bool = True) -> Path:
    src = media / "Show S01E02.mkv"
    src.write_bytes(b"")
    if subs:
        (media / "Show S01E02.en.srt").write_text(SRT)
    return src


def _no_tracks():
    # The empty test file has no embedded tracks; don't ask ffprobe.
    return patch("quipclipper_web.app.list_embedded_tracks", return_value=[])


# --- the window -------------------------------------------------------------------------


def _cue(i, s, e, text, source=None):
    return Cue(i, s, e, text, source=source)


def test_window_keeps_overlapping_lines_shifted_and_clamped_in_order():
    cues = [
        _cue(3, 104.0, 106.0, "Later."),
        _cue(1, 90.0, 98.0, "Ends at the cut."),       # touches the start: out
        _cue(2, 99.0, 100.5, "Before."),
        _cue(4, 104.5, 105.0, "Starts at the end."),   # touches the end: out
        _cue(5, 100.0, 103.0, "Make it so."),
        _cue(6, 101.0, 101.5, "   "),                  # nothing to say: out
    ]
    assert clipmeta.window_cues(cues, 98.0, 104.5) == EXPECTED


def test_window_clamps_a_line_longer_than_the_cut_and_marks_machine_lines():
    cues = [_cue(1, 5.0, 50.0, "A long caption."), _cue(2, 11.0, 12.0, "heard", "machine")]
    assert clipmeta.window_cues(cues, 10.0, 12.5) == [
        {"start": 0.0, "end": 2.5, "text": "A long caption."},
        {"start": 1.0, "end": 2.0, "text": "heard", "machine": True},
    ]


def test_window_of_no_lines_is_empty_and_no_cue_has_words():
    assert clipmeta.window_cues([], 0.0, 5.0) == []
    out = clipmeta.window_cues([_cue(1, 1.0, 2.0, "x")], 0.0, 5.0)
    assert "words" not in out[0]


def test_clamp_to_a_shorter_file_drops_what_no_longer_fits():
    assert clipmeta.clamp_cues(EXPECTED, 6.2) == [
        {"start": 1.0, "end": 2.5, "text": "Before."},
        {"start": 2.0, "end": 5.0, "text": "Make it so."},
        {"start": 6.0, "end": 6.2, "text": "Later."},
    ]
    assert clipmeta.clamp_cues(EXPECTED, 1.5) == [{"start": 1.0, "end": 1.5, "text": "Before."}]
    assert clipmeta.clamp_cues(EXPECTED, None) == EXPECTED


def test_a_tile_carries_cues_only_when_given():
    trim = lsset.Trim(1.0, 2.0)
    assert "cues" not in lsset.tile_entry("S/a.flac", title="a", trim=trim)
    assert lsset.tile_entry("S/a.flac", title="a", trim=trim, cues=[])["cues"] == []
    assert lsset.tile_entry("S/a.flac", title="a", trim=trim, cues=EXPECTED)["cues"] == EXPECTED


# --- every writer -----------------------------------------------------------------------


def test_a_clip_job_writes_the_lines_inside_the_cut_and_where_they_came_from(tmp_path):
    client, media, clips = _client(tmp_path)
    src = _source(media)
    with patch("quipclipper_web.app.cut_clip", side_effect=_fake_cut), \
            patch("quipclipper_web.app.probe_audio_streams", return_value=["flac"]), \
            patch.object(clipmeta, "probe", return_value=PROBE):
        job = _wait(client, client.post("/api/clip", json={
            "path": str(src), "start": 100.0, "end": 103.0, "before": 2, "after": 1.5,
            "kind": "audio", "audio_format": "flac", "cue_text": "Make it so.",
        }).json()["job_id"])
    assert job["status"] == "done", job
    (out,) = clips.rglob("*.flac")
    meta = clipmeta.read(out)
    assert meta["cue"] == "Make it so." and meta["version"] == 1
    assert meta["trim"] == {"start": 2.0, "end": 5.0}
    assert meta["cues"] == EXPECTED
    assert meta["cues_source"] == {"kind": "sidecar", "lang": "en",
                                   "path": "Show S01E02.en.srt"}
    # GET /api/clips hands the sidecar on as it is.
    listed = client.get("/api/clips", params={"recursive": True}).json()["clips"]
    assert listed[0]["meta"]["cues"] == EXPECTED


def test_a_search_query_clip_uses_the_same_lines(tmp_path):
    client, media, clips = _client(tmp_path)
    src = _source(media)
    with patch("quipclipper_web.app.cut_clip", side_effect=_fake_cut), \
            patch("quipclipper_web.app.probe_audio_streams", return_value=["flac"]), \
            patch.object(clipmeta, "probe", return_value=PROBE):
        job = _wait(client, client.post("/api/clip", json={
            "path": str(src), "query": "make it so", "before": 2, "after": 1.5,
            "kind": "audio", "audio_format": "flac",
        }).json()["job_id"])
    assert job["status"] == "done", job
    (out,) = clips.rglob("*.flac")
    assert clipmeta.read(out)["cues"] == EXPECTED


def test_a_clip_without_subtitles_has_neither_field(tmp_path):
    client, media, clips = _client(tmp_path)
    src = _source(media, subs=False)
    with patch("quipclipper_web.app.cut_clip", side_effect=_fake_cut), \
            patch("quipclipper_web.app.probe_audio_streams", return_value=["flac"]), \
            patch.object(clipmeta, "probe", return_value=PROBE), _no_tracks():
        job = _wait(client, client.post("/api/clip", json={
            "path": str(src), "start": 100.0, "end": 103.0, "kind": "audio",
            "audio_format": "flac"}).json()["job_id"])
    assert job["status"] == "done", job
    (out,) = clips.rglob("*.flac")
    meta = clipmeta.read(out)
    assert "cues" not in meta and "cues_source" not in meta


def test_subtitles_with_no_line_in_the_cut_give_an_empty_list(tmp_path):
    client, media, clips = _client(tmp_path)
    src = _source(media)
    with patch("quipclipper_web.app.cut_clip", side_effect=_fake_cut), \
            patch("quipclipper_web.app.probe_audio_streams", return_value=["flac"]), \
            patch.object(clipmeta, "probe", return_value=PROBE):
        job = _wait(client, client.post("/api/clip", json={
            "path": str(src), "start": 10.0, "end": 12.0, "kind": "audio",
            "audio_format": "flac"}).json()["job_id"])
    assert job["status"] == "done", job
    (out,) = clips.rglob("*.flac")
    meta = clipmeta.read(out)
    assert meta["cues"] == [] and meta["cues_source"]["kind"] == "sidecar"


def test_each_file_of_a_channel_split_gets_the_cues_clamped_to_it(tmp_path):
    client, media, clips = _client(tmp_path)
    src = _source(media)

    def split(_video, _rng, *, out, **_kw):
        files = [Path(f"{out}_FL.wav"), Path(f"{out}_FR.wav")]
        for f in files:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(b"RIFF")
        return files

    durations = iter([7.5, 6.2])
    with patch("quipclipper_web.app.split_audio_channels", side_effect=split), \
            patch.object(clipmeta, "probe",
                         side_effect=lambda _p: {**PROBE, "duration": next(durations)}):
        job = _wait(client, client.post("/api/clip", json={
            "path": str(src), "start": 100.0, "end": 103.0, "before": 2, "after": 1.5,
            "kind": "audio", "split_channels": True, "split_format": "wav",
        }).json()["job_id"])
    assert job["status"] == "done", job
    fl, fr = sorted(clips.rglob("*.wav"))
    assert clipmeta.read(fl)["cues"] == EXPECTED and clipmeta.read(fl)["split"] is True
    assert clipmeta.read(fr)["cues"] == clipmeta.clamp_cues(EXPECTED, 6.2)
    assert clipmeta.read(fr)["cues_source"]["kind"] == "sidecar"


def test_send_to_leadstinger_writes_the_cues_in_the_clip_frame(tmp_path):
    client, media, clips = _client(tmp_path, QC_PUBLIC_URL="https://quip.example")
    src = _source(media)
    with patch("quipclipper_web.app.cut_clip", side_effect=_fake_cut), \
            patch("quipclipper_web.app.media.probe_duration", return_value=2000.0), \
            patch.object(clipmeta, "probe", return_value=PROBE):
        job = _wait(client, client.post("/api/leadstinger/quip", json={
            "path": str(src), "start": 100.0, "end": 103.0, "title": "Make it so.",
            "before": 2, "after": 1.5}).json()["job_id"])
    assert job["status"] == "done", job
    meta = clipmeta.read(clips / job["result"]["clip"])
    assert meta["trim"] == {"start": 2.0, "end": 5.0}
    assert meta["cues"] == EXPECTED


def test_send_to_leadstinger_uses_the_chosen_embedded_track(tmp_path):
    client, media, clips = _client(tmp_path, QC_PUBLIC_URL="https://quip.example")
    src = _source(media)  # a sidecar too: the chosen track wins over it
    lines = [_cue(1, 100.0, 103.0, "Mach es so.")]
    seen = []

    def resolve(*, subs, video, track, langs):
        seen.append(track)
        return ResolvedSubtitles(lines, None, track)

    tracks = [SubtitleTrack(0, "subrip", "eng", None), SubtitleTrack(2, "subrip", "ger", None)]
    with patch("quipclipper_web.app.cut_clip", side_effect=_fake_cut), \
            patch("quipclipper_web.app.media.probe_duration", return_value=2000.0), \
            patch("quipclipper_web.sub_cache.resolve_subtitles", side_effect=resolve), \
            patch("quipclipper_web.app.list_embedded_tracks", return_value=tracks), \
            patch.object(clipmeta, "probe", return_value=PROBE):
        job = _wait(client, client.post("/api/leadstinger/quip", json={
            "path": str(src), "start": 100.0, "end": 103.0, "before": 2, "after": 1.5,
            "track": 2}).json()["job_id"])
    assert job["status"] == "done", job
    assert seen == [2]
    meta = clipmeta.read(clips / job["result"]["clip"])
    assert meta["cues"] == [{"start": 2.0, "end": 5.0, "text": "Mach es so."}]
    assert meta["cues_source"] == {"kind": "embedded", "lang": "ger", "track": 2}


def test_a_set_tile_and_its_item_carry_the_cues(tmp_path):
    client, media, clips = _client(tmp_path)
    src = _source(media)
    with patch("quipclipper_web.app.cut_clip", side_effect=_fake_cut):
        job = _wait(client, client.post("/api/leadstinger/set", json={
            "name": "Bridge", "items": [
                {"path": str(src), "start": 100.0, "end": 103.0, "title": "Make it so.",
                 "before": 2, "after": 1.5},
            ]}).json()["job_id"])
    assert job["status"] == "done", job
    out = clips / "LeadStinger" / "Bridge.lsset"
    with zipfile.ZipFile(out) as zf:
        tile = json.loads(zf.read("manifest.json"))["sets"][0]["tiles"][0]
    assert tile["cues"] == EXPECTED
    assert (tile["trim_start"], tile["trim_end"]) == (2.0, 5.0)
    item = clipmeta.read(out)["items"][0]
    assert item["cues"] == EXPECTED and item["cues_source"]["kind"] == "sidecar"


def test_a_set_tile_without_subtitles_has_no_cues(tmp_path):
    client, media, clips = _client(tmp_path)
    src = _source(media, subs=False)
    with patch("quipclipper_web.app.cut_clip", side_effect=_fake_cut), _no_tracks():
        job = _wait(client, client.post("/api/leadstinger/set", json={
            "name": "Quiet", "items": [{"path": str(src), "start": 1.0, "end": 2.0}]
        }).json()["job_id"])
    assert job["status"] == "done", job
    with zipfile.ZipFile(clips / "LeadStinger" / "Quiet.lsset") as zf:
        tile = json.loads(zf.read("manifest.json"))["sets"][0]["tiles"][0]
    assert "cues" not in tile
    assert "cues" not in clipmeta.read(clips / "LeadStinger" / "Quiet.lsset")["items"][0]


def test_a_clips_library_file_in_a_set_keeps_its_own_cues(tmp_path):
    client, _media, clips = _client(tmp_path)
    clip = clips / "Show" / "00-01-38_Make_it_so.flac"
    clip.parent.mkdir()
    clip.write_bytes(b"fLaC")
    source = {"kind": "sidecar", "lang": "en", "path": "Show S01E02.en.srt"}
    clipmeta.write(clip, {"kind": "audio", "cues": EXPECTED, "cues_source": source})
    with patch("quipclipper_web.app.cut_clip", side_effect=_fake_cut), \
            patch("quipclipper_web.app.media.probe_duration", return_value=6.5):
        job = _wait(client, client.post("/api/leadstinger/set", json={
            "name": "Again", "items": [{"path": str(clip)}]}).json()["job_id"])
    assert job["status"] == "done", job
    with zipfile.ZipFile(clips / "LeadStinger" / "Again.lsset") as zf:
        tile = json.loads(zf.read("manifest.json"))["sets"][0]["tiles"][0]
    assert tile["cues"] == EXPECTED
    assert clipmeta.read(clips / "LeadStinger" / "Again.lsset")["items"][0][
        "cues_source"] == source


def test_a_backfilled_clip_gets_no_cues(tmp_path):
    clips = tmp_path / "clips"
    f = clips / "Show" / "Show_00-01-38_Make_it_so.flac"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"fLaC")
    with patch.object(clipmeta, "probe", return_value=PROBE):
        clipmeta.backfill(clips)
    meta = clipmeta.read(f)
    assert meta["backfilled"] is True and "cues" not in meta
