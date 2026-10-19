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
