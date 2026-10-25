import json

import pytest

from quipclipper import commands as reg
from quipclipper.client import CliError, Context, Response, build_request, execute


class FakeTransport:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def request(self, method, path, query, body, stream=False):
        self.calls.append((method, path, query, body))
        status, data = self.replies.pop(0)
        return Response(status, {"content-type": "application/json"},
                        json.dumps(data).encode())


def ctx(replies, **kw):
    return Context(server="x", transport=FakeTransport(replies), err=lambda s: None, **kw)


def test_index_file_sends_its_paths_in_the_body():
    # PA-5: hundreds of paths in a query string meet HTTP 414 behind a proxy.
    m, path, q, body = build_request(reg.BY_ID["index.file"],
                                     {"path": ["/tv/a.mkv", "/tv/b;c.srt"], "force": True})
    assert (m, path, q) == ("POST", "/api/index/file", [])
    assert body == {"path": ["/tv/a.mkv", "/tv/b;c.srt"], "force": True}


def test_build_request_query_path_and_body():
    m, path, q, body = build_request(reg.BY_ID["search.folder"],
                                     {"query": "hi", "path": ["/a,b", "/c"], "limit": 3})
    assert (m, path, body) == ("GET", "/api/search/folder", None)
    assert ("path", "/a,b") in q and ("path", "/c") in q and ("limit", "3") in q
    m, path, q, body = build_request(reg.BY_ID["clip.create"], {
        "query": "x", "path": "/m.mkv", "audio_tracks": "0,2", "split_groups": "front,lfe",
        "lossless": False, "wait": True, "out": "ignored"})
    assert body["audio_tracks"] == [0, 2] and body["split_groups"] == ["front", "lfe"]
    assert body["lossless"] is False and "wait" not in body and "out" not in body
    m, path, q, body = build_request(reg.BY_ID["clips.download"], {"clip_path": "a b/c.mkv"})
    assert path == "/api/clips/download/a%20b/c.mkv"


def test_negative_audio_track_is_a_usage_error():
    with pytest.raises(CliError) as exc:
        build_request(reg.BY_ID["clip.create"], {"path": "/m", "audio_tracks": "-1"})
    assert exc.value.exit_code == 2


def test_job_is_followed_until_done():
    c = ctx([(200, {"job_id": "j1", "status": "queued"}),
             (200, {"id": "j1", "status": "running"}),
             (200, {"id": "j1", "status": "done", "files": []})])
    job = execute(c, reg.BY_ID["youtube.add"], {"url": "u", "wait": True})
    assert job["status"] == "done"


def test_failed_job_raises():
    c = ctx([(200, {"job_id": "j1"}), (200, {"id": "j1", "status": "failed", "error": "boom"})])
    with pytest.raises(CliError, match="boom"):
        execute(c, reg.BY_ID["youtube.add"], {"url": "u"})


@pytest.mark.parametrize("status,exit_code", [(401, 4), (403, 4), (422, 2), (404, 1), (500, 1)])
def test_http_errors_map_to_exit_codes(status, exit_code):
    # 403 here is the auth/CSRF gate's bare "Forbidden" (a route's own 403 is tested below).
    c = ctx([(status, {"detail": "Forbidden" if status == 403 else "nope"})])
    with pytest.raises(CliError) as exc:
        execute(c, reg.BY_ID["library.roots"], {})
    assert exc.value.exit_code == exit_code


def test_safe_refuses_destructive_commands_before_any_request():
    c = ctx([], safe=True)
    with pytest.raises(CliError) as exc:
        execute(c, reg.BY_ID["bookmark.clear"], {})
    assert exc.value.exit_code == 5 and c.transport.calls == []


def test_a_403_with_a_message_is_not_reported_as_a_token_problem():
    with pytest.raises(CliError) as exc:
        execute(ctx([(403, {"detail": "Path is outside the configured media roots: /x"})]),
                reg.BY_ID["library.browse"], {"path": "/x"})
    assert exc.value.code == "forbidden" and "QC_API_TOKEN" not in str(exc.value)
    with pytest.raises(CliError) as exc:
        execute(ctx([(403, {"detail": "Forbidden"})]), reg.BY_ID["library.browse"], {"path": "/x"})
    assert exc.value.code == "auth"


# --- review fixes (M18 review) ----------------------------------------------------------


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_a_redirect_is_an_error_not_a_success(status):
    c = ctx([(status, {"detail": "Moved"})])
    with pytest.raises(CliError, match="redirected") as exc:
        execute(c, reg.BY_ID["bookmark.clear"], {})
    assert exc.value.exit_code == 3


def test_the_transport_never_follows_a_redirect():
    """A followed redirect would carry the API key to whatever host Location names."""
    import http.server
    import threading

    from quipclipper.client import UrllibTransport

    hits = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            hits.append(self.path)
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:9/elsewhere")
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        resp = UrllibTransport(f"http://127.0.0.1:{srv.server_port}", "tok").request(
            "GET", "/api/health", [], None)
    finally:
        srv.shutdown()
    assert resp.status == 302 and hits == ["/api/health"]


def test_a_url_without_a_scheme_is_a_usage_error():
    from quipclipper.client import UrllibTransport

    with pytest.raises(CliError, match="Not a server URL") as exc:
        UrllibTransport("quip.example").request("GET", "/api/health", [], None)
    assert exc.value.exit_code == 2


def test_an_unreadable_token_file_fails_only_commands_that_call_the_server(tmp_path, monkeypatch):
    from quipclipper.client import make_context

    monkeypatch.setenv("QC_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("QC_API_TOKEN_FILE", str(tmp_path / "missing"))
    c = make_context(server=None, local=False, json_out=False, safe=False)
    with pytest.raises(CliError, match="token file"):
        c.transport.request("GET", "/api/health", [], None)


def test_a_404_on_a_job_gets_no_local_hint():
    c = ctx([(404, {"detail": "Job not found."})])
    with pytest.raises(CliError) as exc:
        execute(c, reg.BY_ID["job.get"], {"job_id": "j1"})
    assert "--local" not in str(exc.value)
    c = ctx([(404, {"detail": "File not found"})])
    with pytest.raises(CliError, match="--local"):
        execute(c, reg.BY_ID["item.info"], {"path": "/m.mkv"})


@pytest.mark.parametrize("cd", ["attachment; filename*=utf-8''..%2F..%2Fx.txt",
                                'attachment; filename="/etc/x.txt"',
                                'attachment; filename="..\\\\x.txt"'])
def test_a_download_name_from_the_server_stays_in_out_dir(tmp_path, cd):
    from quipclipper.client import _filename

    resp = Response(200, {"content-disposition": cd})
    assert _filename(resp, "/api/clips/download/a.mp3") == "x.txt"
    assert _filename(Response(200, {"content-disposition": 'attachment; filename=".."'}),
                     "/api/clips/download/a.mp3") == "download"


def test_safe_refuses_commands_that_change_data():
    for cid in ("job.cancel", "bookmark.update", "clips.backfill", "settings.set"):
        assert reg.BY_ID[cid].destructive, cid
    c = ctx([], safe=True)
    with pytest.raises(CliError) as exc:
        execute(c, reg.BY_ID["job.cancel"], {"job_id": "j1"})
    assert exc.value.exit_code == 5


def test_server_search_with_no_matches_exits_1():
    from quipclipper.cli import main

    t = FakeTransport([(200, {"query": "x", "count": 0, "matches": []})])
    assert main(["--json", "search.folder", "x", "--path", "/m"], transport=t) == 1
    t = FakeTransport([(200, {"query": "x", "count": 0, "matches": []})])
    assert main(["search", "x", "-v", "/m.mkv"], transport=t) == 1


def test_server_clip_of_an_audio_file_copies_it_like_local_mode():
    from quipclipper.cli import main

    t = FakeTransport([(200, {"job_id": "j1", "status": "queued"})])
    assert main(["clip", "be back", "-v", "/podcasts/ep.m4a", "--no-wait"], transport=t) == 0
    body = t.calls[0][3]
    assert body["kind"] == "audio" and body["lossless"] is True


def test_server_clip_sends_its_search_tuning():
    # The match --index counts is found with the same tuning as the search that showed
    # it, so the server gets --min-score and --max-span (they were local-only).
    from quipclipper.cli import main

    t = FakeTransport([(200, {"job_id": "j1", "status": "queued"})])
    assert main(["clip", "x", "-v", "/m.mkv", "--min-score", "30", "--max-span", "5",
                 "--index", "2", "--no-wait"], transport=t) == 0
    body = t.calls[-1][3]
    assert (body["min_score"], body["max_span"], body["match_index"]) == (30.0, 5, 2)


def test_clip_nowplaying_reencodes_video_by_default():
    from quipclipper.cli import main

    t = FakeTransport([
        (200, {"sessions": [{"id": "s1", "device": "TV", "path": "/m.mkv", "position": 12}]}),
        (200, [{"start": 10.0, "end": 11.5, "text": "Hi."}]),
        (200, {"job_id": "j1", "status": "queued"}),
    ])
    assert main(["clip.nowplaying", "--no-wait"], transport=t) == 0
    assert t.calls[-1][3]["lossless"] is False


def test_ctrl_c_stops_a_whole_batch(monkeypatch):
    from quipclipper import cli

    rows = [{"path": "/m.mkv", "start": i, "end": i + 1} for i in range(3)]
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(json.dumps(rows)))
    waited = []

    def fake_wait(c, job_id, out_dir=None):
        waited.append(job_id)
        raise CliError("Interrupted; job cancelled.", code="interrupted")

    monkeypatch.setattr(cli, "wait_job", fake_wait)
    t = FakeTransport([(200, {"job_id": f"j{i}", "status": "queued"}) for i in range(3)])
    assert cli.main(["clip.batch"], transport=t) == 1
    assert waited == ["j0"]


# --- release review 2 (t-m20-review-2) --------------------------------------------------

def test_a_refused_reindex_says_why_for_each_path():
    # PA-3: `index.file /etc/passwd` said only "Nothing to re-index.".
    with pytest.raises(CliError) as exc:
        execute(ctx([(422, {"detail": "Nothing to re-index.", "job_id": None, "queued": 0,
                            "files": [{"path": "/etc/passwd", "status": "outside_roots"}]})]),
                reg.BY_ID["index.file"], {"path": ["/etc/passwd"]})
    assert "outside_roots: /etc/passwd" in str(exc.value) and exc.value.exit_code == 2


def test_ctrl_c_on_a_library_run_found_running_does_not_cancel_it(monkeypatch):
    # PA-4: `index.library --wait` waited on a run started elsewhere (the web UI), and
    # Ctrl-C then cancelled that run.
    calls = []

    def call(_ctx, method, path, *a, **k):
        calls.append((method, path))
        if method == "POST":
            return Response(200, {"content-type": "application/json"},
                            json.dumps({"job_id": "j1", "existing": True}).encode())
        raise KeyboardInterrupt

    monkeypatch.setattr("quipclipper.client.call", call)
    with pytest.raises(CliError, match="keeps running"):
        execute(ctx([]), reg.BY_ID["index.library"], {"wait": True})
    assert ("DELETE", "/api/jobs/j1") not in calls
