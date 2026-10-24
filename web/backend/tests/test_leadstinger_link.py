"""Send to LeadStinger by link (M14, LeadStinger's D-076): handles, the link, the endpoints."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qsl

import pytest
from fastapi.testclient import TestClient

from quipclipper_web import lslink
from quipclipper_web.app import create_app
from quipclipper_web.config import Settings

SERVER = "https://quip.example"


# --- handles ------------------------------------------------------------------------------


def test_handles_sit_either_side_of_the_line():
    assert lslink.handle_cut(100.0, 103.5, 2.0, 2.0, 600.0) == lslink.Cut(98.0, 105.5, 2.0, 5.5)


def test_handles_are_clamped_at_the_start_of_the_file():
    # 0.5 s in: the cut starts at 0, so the lead is only 0.5 s and the line still sits on it.
    assert lslink.handle_cut(0.5, 2.0, 2.0, 2.0, 600.0) == lslink.Cut(0.0, 4.0, 0.5, 2.0)
    assert lslink.handle_cut(0.0, 1.0, 2.0, 2.0, 600.0) == lslink.Cut(0.0, 3.0, 0.0, 1.0)


def test_handles_are_clamped_at_the_end_of_the_file():
    assert lslink.handle_cut(597.0, 599.0, 2.0, 2.0, 600.0) == lslink.Cut(595.0, 600.0, 2.0, 4.0)
    # A line running past the end is cut at the end too.
    assert lslink.handle_cut(598.0, 603.0, 2.0, 2.0, 600.0) == lslink.Cut(596.0, 600.0, 2.0, 4.0)


def test_a_line_in_a_file_shorter_than_its_handles():
    assert lslink.handle_cut(1.0, 2.0, 5.0, 5.0, 3.0) == lslink.Cut(0.0, 3.0, 1.0, 2.0)


def test_before_and_after_differ():
    assert lslink.handle_cut(100.0, 103.5, 1.0, 3.0, 600.0) == lslink.Cut(99.0, 106.5, 1.0, 4.5)
    # No padding: no handles, the trim is the whole clip.
    assert lslink.handle_cut(100.0, 103.5, 0.0, 0.0, 600.0) == lslink.Cut(100.0, 103.5, 0.0, 3.5)


def test_unknown_duration_leaves_the_tail_to_ffmpeg():
    assert lslink.handle_cut(10.0, 12.0, 2.0, 2.0, None) == lslink.Cut(8.0, 14.0, 2.0, 4.0)
    assert lslink.handle_cut(10.0, 12.0, 2.0, 2.0, 0.0) == lslink.Cut(8.0, 14.0, 2.0, 4.0)


def test_no_handles_and_rounding():
    assert lslink.handle_cut(10.0, 12.0, 0.0, 0.0, 600.0) == lslink.Cut(10.0, 12.0, 0.0, 2.0)
    cut = lslink.handle_cut(2002.4, 2004.0001, 2.0, 2.0, None)
    assert (cut.start, cut.end, cut.line_start, cut.line_end) == (2000.4, 2006.0, 2.0, 3.6)


def test_an_empty_line_or_one_past_the_end_is_refused():
    with pytest.raises(ValueError):
        lslink.handle_cut(5.0, 5.0, 2.0, 2.0, 600.0)
    with pytest.raises(ValueError):
        lslink.handle_cut(6.0, 5.0, 2.0, 2.0, None)
    with pytest.raises(ValueError, match="after the end of the file"):
        lslink.handle_cut(700.0, 701.0, 2.0, 2.0, 600.0)


# --- the link -----------------------------------------------------------------------------


def _params(link: str) -> list[tuple[str, str]]:
    assert link.startswith("leadstinger://quip?")
    return parse_qsl(link.split("?", 1)[1], keep_blank_values=True, strict_parsing=True)


def test_link_matches_leadstingers_documented_example():
    link = lslink.build_link(SERVER, "Heat/Heat - attached.mkv", title="Don't get attached",
                             start=2, end=5.4, set_name="Movie Lines")
    assert link == ("leadstinger://quip?server=https%3A%2F%2Fquip.example"
                    "&clip=Heat%2FHeat%20-%20attached.mkv&title=Don't%20get%20attached"
                    "&start=2&end=5.4&set=Movie%20Lines")


def test_link_encodes_like_encode_uri_component():
    link = lslink.build_link(SERVER, "a+b/c&d=e#f?.flac",
                             title="Plus + amp & hash # pct % quote \" é 日本", start=0, end=1)
    query = link.split("?", 1)[1]
    # Only the parameter separators are raw; a literal + is %2B (LeadStinger reads + as space).
    assert query.count("&") == 4 and query.count("=") == 5
    assert "+" not in query and "#" not in query and " " not in query
    assert "%2B" in query and "%C3%A9" in query and "%E6%97%A5" in query
    p = dict(_params(link))
    assert p["clip"] == "a+b/c&d=e#f?.flac"
    assert p["title"] == "Plus + amp & hash # pct % quote \" é 日本"


def test_link_parameter_order_and_omitted_optionals():
    link = lslink.build_link(SERVER + "/", "x.flac")
    assert _params(link) == [("server", SERVER), ("clip", "x.flac")]
    link = lslink.build_link(SERVER, "x.flac", title="  ", set_name="\n", start=None, end=3)
    assert _params(link) == [("server", SERVER), ("clip", "x.flac"), ("end", "3")]


def test_link_numbers_are_plain_decimals():
    assert lslink.seconds(2, "start") == "2"
    assert lslink.seconds(5.4, "end") == "5.4"
    assert lslink.seconds(1.23456, "end") == "1.235"
    assert lslink.seconds(0.00001, "start") == "0"      # never 1e-05
    assert lslink.seconds(-0.0, "start") == "0"
    assert lslink.seconds(86400, "end") == "86400"
    for bad in (-1, 86400.5, float("nan"), float("inf")):
        with pytest.raises(lslink.LinkError):
            lslink.seconds(bad, "start")
    with pytest.raises(lslink.LinkError, match="after the start"):
        lslink.build_link(SERVER, "x.flac", start=3, end=3)


def test_titles_and_set_names_become_one_line_within_limits():
    p = dict(_params(lslink.build_link(SERVER, "x.flac", title="Resistance\nis\tfutile.\r\n",
                                       set_name="  Borg   lines ")))
    assert p["title"] == "Resistance is futile." and p["set"] == "Borg lines"
    long = dict(_params(lslink.build_link(SERVER, "x.flac", title="w " * 300, set_name="s" * 150)))
    assert len(long["title"]) == 200 and long["title"].endswith("…")
    assert len(long["set"]) == 100


@pytest.mark.parametrize("server", [
    "https://quip.example", "http://quip.example:8896", "https://example.org/quip/",
    "HTTPS://Quip.Example",
])
def test_good_servers(server):
    assert lslink.normalize_server(server) == server.rstrip("/")


@pytest.mark.parametrize("server", [
    "", "quip.example", "ftp://quip.example", "https://", "https://u:p@quip.example",
    "https://quip.example?x=1", "https://quip.example/#top", "https://quip.example:99999",
])
def test_bad_servers(server):
    with pytest.raises(lslink.LinkError):
        lslink.normalize_server(server)


@pytest.mark.parametrize("clip", ["", "/abs.flac", "a//b.flac", "a/../b.flac", "./a.flac",
                                  "a\\b.flac", "a\nb.flac", "x" * 1025])
def test_bad_clip_paths(clip):
    with pytest.raises(lslink.LinkError):
        lslink.check_clip(clip)


def test_clip_path_is_relative_to_the_clips_library(tmp_path):
    clips = tmp_path / "clips"
    (clips / "LeadStinger").mkdir(parents=True)
    f = clips / "LeadStinger" / "a b.flac"
    f.write_bytes(b"")
    assert lslink.clip_path(f, clips) == "LeadStinger/a b.flac"
    with pytest.raises(lslink.LinkError):
        lslink.clip_path(tmp_path / "elsewhere.flac", clips)
    with pytest.raises(lslink.LinkError):
        lslink.clip_path(clips, clips)


# --- the endpoints ------------------------------------------------------------------------


def _client(tmp_path: Path, public_url: str | None = SERVER, sub: str = ""):
    if sub:
        tmp_path = tmp_path / sub
        tmp_path.mkdir()
    media = tmp_path / "media"
    media.mkdir()
    clips = tmp_path / "clips"
    clips.mkdir()
    env = {"QC_MEDIA_ROOTS": str(media), "QC_CLIPS_DIR": str(clips),
           "QC_STATE_DIR": str(tmp_path / "state")}
    if public_url is not None:
        env["QC_PUBLIC_URL"] = public_url
    client = TestClient(create_app(Settings.from_env(env)))
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
    out.write_bytes(b"fLaC")
    return out


def test_config_reports_the_public_url(tmp_path):
    client, _, _ = _client(tmp_path)
    assert client.get("/api/config").json()["public_url"] == SERVER
    client, _, _ = _client(tmp_path, public_url=None, sub="off")
    assert client.get("/api/config").json()["public_url"] is None


def test_quip_cuts_flac_with_handles_and_returns_the_link(tmp_path):
    client, media, clips = _client(tmp_path, public_url="https://quip.example/")
    ep = media / "TNG S03E26.mkv"
    ep.write_bytes(b"")
    with patch("quipclipper_web.app.cut_clip", side_effect=_fake_cut) as cut, \
            patch("quipclipper_web.app.media.probe_duration", return_value=2005.0):
        resp = client.post("/api/leadstinger/quip", json={
            "path": str(ep), "start": 2002.4, "end": 2004.0,
            "title": "Resistance is\nfutile.", "set": "Borg"})
        assert resp.status_code == 200, resp.text
        job = _wait(client, resp.json()["job_id"])
    assert job["status"] == "done", job
    kw = cut.call_args.kwargs
    assert kw["kind"] == "audio" and kw["audio_codec"] == "flac"
    rng = cut.call_args.args[1]
    assert (round(rng.start, 3), round(rng.end, 3)) == (2000.4, 2005.0)  # tail clamped at EOF
    result = job["result"]
    assert result["clip"].startswith("LeadStinger/TNG S03E26_")
    assert result["clip"].endswith(".flac") and (clips / result["clip"]).is_file()
    assert (result["start"], result["end"]) == (2.0, 3.6)
    p = dict(_params(result["link"]))
    assert p == {"server": SERVER, "clip": result["clip"], "title": "Resistance is futile.",
                 "start": "2", "end": "3.6", "set": "Borg"}
    # LeadStinger downloads it from the clips library, with its token.
    dl = client.get("/api/clips/download/" + result["clip"])
    assert dl.status_code == 200 and dl.content == b"fLaC"


def test_quip_near_the_start_of_the_file(tmp_path):
    client, media, _ = _client(tmp_path)
    ep = media / "a.mkv"
    ep.write_bytes(b"")
    with patch("quipclipper_web.app.cut_clip", side_effect=_fake_cut) as cut, \
            patch("quipclipper_web.app.media.probe_duration", return_value=None):
        job = _wait(client, client.post("/api/leadstinger/quip", json={
            "path": str(ep), "start": 0.75, "end": 2.0, "before": 3, "after": 3}).json()["job_id"])
    assert job["status"] == "done", job
    rng = cut.call_args.args[1]
    assert (rng.start, rng.end) == (0.0, 5.0)
    p = dict(_params(job["result"]["link"]))
    assert (p["start"], p["end"]) == ("0.75", "2")
    assert "title" not in p and "set" not in p


def test_quip_needs_an_address_for_the_server(tmp_path):
    # No QC_PUBLIC_URL: the link names the address the request came in on (test_pairing.py);
    # one it can't use is a 503 that says to set QC_PUBLIC_URL.
    client, media, _ = _client(tmp_path, public_url=None)
    resp = client.post("/api/leadstinger/quip", json={"path": str(media / "a.mkv"),
                                                      "start": 1, "end": 2},
                       headers={"host": "not a host"})
    assert resp.status_code == 503 and "QC_PUBLIC_URL" in resp.json()["detail"]
    client, media, _ = _client(tmp_path, public_url="quip.example", sub="bad")
    resp = client.post("/api/leadstinger/quip", json={"path": str(media / "a.mkv"),
                                                      "start": 1, "end": 2})
    assert resp.status_code == 503 and "QC_PUBLIC_URL" in resp.json()["detail"]


def test_quip_refuses_bad_requests(tmp_path):
    client, media, _ = _client(tmp_path)
    ep = media / "a.mkv"
    ep.write_bytes(b"")
    with patch("quipclipper_web.app.media.probe_duration", return_value=60.0):
        assert client.post("/api/leadstinger/quip", json={
            "path": str(ep), "start": 5, "end": 4}).status_code == 400
        assert client.post("/api/leadstinger/quip", json={
            "path": str(ep), "start": 70, "end": 71}).status_code == 400
        assert client.post("/api/leadstinger/quip", json={
            "path": str(ep), "start": 1, "end": 2, "before": 61}).status_code == 422
        assert client.post("/api/leadstinger/quip", json={
            "path": str(media / "missing.mkv"), "start": 1, "end": 2}).status_code == 404
        assert client.post("/api/leadstinger/quip", json={
            "path": "/etc/passwd", "start": 1, "end": 2}).status_code == 403


def test_link_for_a_clips_library_file(tmp_path):
    client, _, clips = _client(tmp_path)
    (clips / "Heat").mkdir()
    (clips / "Heat" / "Heat - attached.mkv").write_bytes(b"")
    resp = client.get("/api/leadstinger/link", params={
        "clip": "Heat/Heat - attached.mkv", "title": "Don't get attached", "set": "Movie Lines"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "clip": "Heat/Heat - attached.mkv",
        "link": "leadstinger://quip?server=https%3A%2F%2Fquip.example"
                "&clip=Heat%2FHeat%20-%20attached.mkv&title=Don't%20get%20attached"
                "&set=Movie%20Lines"}
    # Optional trims pass through.
    p = dict(_params(client.get("/api/leadstinger/link", params={
        "clip": "Heat/Heat - attached.mkv", "start": 1.5, "end": 3}).json()["link"]))
    assert (p["start"], p["end"]) == ("1.5", "3")


def test_link_refuses_files_outside_or_missing(tmp_path):
    client, _, clips = _client(tmp_path)
    (tmp_path / "secret.flac").write_bytes(b"")
    assert client.get("/api/leadstinger/link", params={"clip": "../secret.flac"}).status_code == 403
    assert client.get("/api/leadstinger/link", params={"clip": "nope.flac"}).status_code == 404
    (clips / "a.flac").write_bytes(b"")
    assert client.get("/api/leadstinger/link", params={
        "clip": "a.flac", "start": 3, "end": 2}).status_code == 400
    off, _, offclips = _client(tmp_path, public_url=None, sub="off")
    (offclips / "a.flac").write_bytes(b"")
    assert off.get("/api/leadstinger/link", params={"clip": "a.flac"},
                   headers={"host": "not a host"}).status_code == 503
    # Without QC_PUBLIC_URL the link names the address the request reached the app at.
    assert off.get("/api/leadstinger/link", params={"clip": "a.flac"}).json()["link"].startswith(
        "leadstinger://quip?server=http%3A%2F%2Ftestserver&")


# --- the CLI side -------------------------------------------------------------------------


def test_cli_quip_body_params_match_the_request_model():
    from quipclipper import commands as reg
    from quipclipper_web.app import LeadStingerQuipRequest

    cmd = reg.BY_ID["leadstinger.quip"]
    assert {p.name for p in cmd.params if p.where == reg.BODY} == \
        set(LeadStingerQuipRequest.model_fields)


def test_cli_link_query_params_match_the_route():
    from quipclipper import commands as reg

    cmd = reg.BY_ID["leadstinger.link"]
    assert [p.name for p in cmd.params if p.where == reg.QUERY] == \
        ["clip", "title", "start", "end", "set"]


def test_cli_opens_the_link_unless_told_not_to():
    from quipclipper import cli

    result = {"link": "leadstinger://quip?server=x&clip=y", "clip": "y"}
    with patch.object(cli, "_open_link", return_value=True) as opener:
        assert cli._deliver_link({"open": True}, result) == {**result, "opened": True}
        opener.assert_called_once_with(result["link"])
    with patch.object(cli, "_open_link") as opener:
        assert cli._deliver_link({"open": False}, result)["opened"] is False
        opener.assert_not_called()
    with pytest.raises(cli.CliError):
        cli._deliver_link({"open": True}, {})


def test_cli_quip_posts_the_line_and_delivers_the_jobs_link():
    from quipclipper import cli

    calls = []

    class Resp:
        def __init__(self, body):
            self._body = body

        def json(self):
            return self._body

    def fake_call(_c, method, path, query=None, body=None, stream=False):
        calls.append((method, path, query, body))
        return Resp({"job_id": "j1", "status": "queued"})

    link = "leadstinger://quip?server=s&clip=c"
    with patch.object(cli, "call", side_effect=fake_call), \
            patch.object(cli, "wait_job", return_value={"result": {"link": link}}), \
            patch.object(cli, "_open_link", return_value=False):
        out = cli._client_leadstinger_quip(object(), {
            "path": "/m/a.mkv", "start": 1.0, "end": 2.5, "title": "Hi", "before": 2.0,
            "after": 0.5, "set": None, "open": True})
    assert calls == [("POST", "/api/leadstinger/quip", [],
                      {"path": "/m/a.mkv", "start": 1.0, "end": 2.5, "title": "Hi",
                       "before": 2.0, "after": 0.5})]
    assert out == {"link": link, "opened": False}
