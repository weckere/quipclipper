"""Check for updates (M24): versions, the build's identity, the GitHub call and the checker.

No test touches the network: GitHub is a fake opener or a fake fetch, and time is a counter.
"""

from __future__ import annotations

import email.message
import io
import json
import urllib.error

import pytest
from quipclipper import updates as upd

SHA = "2da1479c0ffee0123456789abcdef0123456789a"


def _release(tag="v0.6.0"):
    return {"version": tag.lstrip("v"), "tag": tag,
            "url": f"https://github.com/weckere/quipclipper/releases/tag/{tag}",
            "name": f"quipclipper {tag}", "published_at": "2026-11-01T10:00:00Z"}


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def _checker(tmp_path, version="0.5.1", commit="", install="pip", fetch=None,
             tag_commit=None, **kw):
    build = upd.Build(version=version, commit=commit, install=install)
    return upd.UpdateChecker(tmp_path / "update.json", build,
                             fetch=fetch or (lambda v: _release()),
                             tag_commit=tag_commit or (lambda v, tag: None), **kw)


# --- versions -------------------------------------------------------------------------------


def test_parse_and_compare_versions():
    assert upd.parse_version("v0.5.1") == (0, 5, 1)
    assert upd.parse_version("0.10.0") == (0, 10, 0)
    assert upd.parse_version("v1.0.0-rc1") is None
    assert upd.parse_version(None) is None
    assert upd.compare("0.10.0", "0.9.9") == 1  # numbers, not strings
    assert upd.compare("0.5.1", "0.5.1") == 0
    assert upd.compare("0.5", "0.5.0") == 0
    assert upd.compare("0.5.0", "0.5.1") == -1
    assert upd.compare("nonsense", "0.5.1") is None


# --- the build ------------------------------------------------------------------------------


def test_a_release_build_reads_as_its_version():
    b = upd.Build.from_env("0.5.1", {})
    assert (b.dev, b.label, b.install) == (False, "0.5.1", "pip")
    assert b.describe()["commit"] is None


def test_a_dev_build_names_its_commit_and_its_base_release():
    b = upd.Build.from_env("0.5.1", {"QC_BUILD_COMMIT": SHA.upper(), "QC_INSTALL": "NixOS"})
    assert b.dev and b.commit == SHA and b.install == "nixos"
    assert b.label == "dev build 2da1479, ahead of 0.5.1"
    dirty = upd.Build.from_env("0.5.1", {"QC_BUILD_COMMIT": SHA + "-dirty"})
    assert dirty.short == "2da1479-dirty"


@pytest.mark.parametrize("commit", ["", "main", "<script>", "abc", "zzzzzzz"])
def test_a_commit_that_is_not_one_is_ignored(commit):
    b = upd.Build.from_env("0.5.1", {"QC_BUILD_COMMIT": commit, "QC_INSTALL": "flatpak"})
    assert (b.dev, b.install) == (False, "pip")


@pytest.mark.parametrize("install,needle", [
    ("docker", "docker compose pull"),
    ("nixos", "nix flake update quipclipper"),
    ("nix", "nix profile upgrade quipclipper"),
    ("pip", 'pip install --upgrade "git+https://github.com/weckere/quipclipper@v0.6.0"'),
])
def test_how_to_update_each_install(install, needle):
    how = upd.how_to_update(install, "v0.6.0")
    assert how["install"] == install
    assert needle in how["commands"][0]


def test_a_pip_web_server_upgrades_both_packages():
    cmds = upd.how_to_update("pip", "v0.6.0", web=True)["commands"]
    assert cmds[1].endswith('@v0.6.0#subdirectory=web/backend"')
    assert "@main" in upd.how_to_update("pip")["commands"][0]  # no tag known: main


# --- the GitHub call ------------------------------------------------------------------------


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _opener(payload=None, error=None, seen=None):
    def open_(req, timeout):
        if seen is not None:
            seen.append((req, timeout))
        if error is not None:
            raise error
        return FakeResponse(json.dumps(payload).encode())
    return open_


def _http_error(code, **headers):
    msg = email.message.Message()
    for k, v in headers.items():
        msg[k.replace("_", "-")] = v
    return urllib.error.HTTPError(upd.LATEST_URL, code, "x", msg, None)


def test_fetch_latest_sends_only_the_request():
    seen = []
    got = upd.fetch_latest("0.5.1", opener=_opener(
        {"tag_name": "v0.6.0", "name": "quipclipper 0.6.0", "html_url": "https://evil.example/",
         "published_at": "2026-11-01T10:00:00Z", "body": "notes"}, seen=seen))
    assert got == {"version": "0.6.0", "tag": "v0.6.0",
                   "url": "https://github.com/weckere/quipclipper/releases/tag/v0.6.0",
                   "name": "quipclipper 0.6.0", "published_at": "2026-11-01T10:00:00Z"}
    (req, timeout), = seen
    assert req.full_url == upd.LATEST_URL and req.get_method() == "GET" and req.data is None
    assert req.get_header("User-agent") == "quipclipper/0.5.1"
    assert set(dict(req.header_items())) == {"User-agent", "Accept", "X-github-api-version"}
    assert timeout == upd.TIMEOUT


@pytest.mark.parametrize("error,kind,retry_at", [
    (_http_error(403, X_RateLimit_Remaining="0", X_RateLimit_Reset="1800003600"),
     "rate_limited", 1800003600.0),
    (_http_error(429, Retry_After="120"), "rate_limited", 1_800_000_120.0),
    (_http_error(404), "no_release", None),
    (_http_error(502), "http", None),
    (urllib.error.URLError("Name or service not known"), "unreachable", None),
    (TimeoutError("timed out"), "unreachable", None),
])
def test_fetch_latest_failures(error, kind, retry_at):
    with pytest.raises(upd.CheckError) as exc:
        upd.fetch_latest("0.5.1", opener=_opener(error=error), now=Clock())
    assert (exc.value.kind, exc.value.retry_at) == (kind, retry_at)


@pytest.mark.parametrize("payload", [{"tag_name": "nightly"}, ["not", "a", "release"], {}])
def test_fetch_latest_rejects_a_reply_with_no_version(payload):
    with pytest.raises(upd.CheckError):
        upd.fetch_latest("0.5.1", opener=_opener(payload))


# --- the checker ----------------------------------------------------------------------------


@pytest.mark.parametrize("latest,state", [
    ("v0.6.0", "update_available"), ("v0.5.1", "up_to_date"), ("v0.5.0", "ahead")])
def test_newer_same_or_older_release(tmp_path, latest, state):
    s = _checker(tmp_path, fetch=lambda v: _release(latest)).check()
    assert s["state"] == state
    assert s["update_available"] is (state == "update_available")
    assert s["latest"]["version"] == latest.lstrip("v")
    assert s["error"] is None and s["checked_at"]


def test_a_dev_build_is_up_to_date_until_a_newer_release_than_its_base(tmp_path):
    c = _checker(tmp_path, commit=SHA, install="nixos", fetch=lambda v: _release("v0.5.1"))
    s = c.check()
    assert s["current"]["label"] == "dev build 2da1479, ahead of 0.5.1"
    assert s["state"] == "up_to_date" and not s["update_available"]
    assert s["how_to_update"]["install"] == "nixos"
    c._fetch = lambda v: _release("v0.6.0")
    assert c.check()["update_available"] is True


def test_a_build_on_exactly_its_release_commit_is_that_release(tmp_path):
    """A Nix build always carries its commit: on the release's own commit it is the release."""
    asked = []

    def tag_commit(version, tag):
        asked.append(tag)
        return SHA

    c = _checker(tmp_path, commit=SHA, install="nixos", tag_commit=tag_commit,
                 fetch=lambda v: _release("v0.5.1"))
    assert c.status()["current"]["label"] == "dev build 2da1479, ahead of 0.5.1"  # not asked yet
    s = c.check()
    assert (s["current"]["dev"], s["current"]["label"], s["current"]["commit"]) == (
        False, "0.5.1", SHA)
    c.check()
    assert asked == ["v0.5.1"]  # once per version: tags don't move
    # A later dev commit on the same base is a dev build again (it asks nothing new).
    later = _checker(tmp_path, commit="1234567" + SHA[7:], tag_commit=tag_commit)
    assert later.check()["current"]["label"] == "dev build 1234567, ahead of 0.5.1"
    assert asked == ["v0.5.1"]


def test_a_tag_lookup_that_fails_leaves_a_dev_build(tmp_path):
    def down(version, tag):
        raise upd.CheckError("couldn't reach GitHub")

    s = _checker(tmp_path, commit=SHA, tag_commit=down).check()
    assert s["error"] is None and s["current"]["dev"] is True


def test_fetch_tag_commit():
    seen = []

    def open_(req, timeout):
        seen.append(req)
        return FakeResponse((SHA + "\n").encode())

    assert upd.fetch_tag_commit("0.5.1", "v0.5.1", opener=open_) == SHA
    assert seen[0].full_url.endswith("/repos/weckere/quipclipper/commits/v0.5.1")
    assert seen[0].get_header("Accept") == "application/vnd.github.sha"
    assert upd.fetch_tag_commit("0.5.1", "v9.9.9", opener=_opener(error=_http_error(404))) is None
    assert upd.fetch_tag_commit("0.5.1", "v0.5.1",
                                opener=lambda req, timeout: FakeResponse(b"<html>")) is None
    with pytest.raises(upd.CheckError):
        upd.fetch_tag_commit("0.5.1", "v0.5.1", opener=_opener(error=_http_error(500)))


def test_never_checked(tmp_path):
    s = _checker(tmp_path).status()
    assert (s["state"], s["latest"], s["checked_at"], s["error"]) == ("unknown", None, None, None)
    assert "@main" in s["how_to_update"]["commands"][0]
    assert not (tmp_path / "update.json").exists()  # reading writes nothing


def test_unreachable_keeps_the_last_good_answer(tmp_path):
    clock = Clock()
    c = _checker(tmp_path, now=clock)
    first = c.check()

    def down(version):
        raise upd.CheckError("couldn't reach GitHub (timed out)")

    c._fetch = down
    clock.t += 3600
    s = c.check()
    assert s["error"]["kind"] == "unreachable"
    assert s["latest"]["version"] == "0.6.0" and s["update_available"]
    assert s["checked_at"] == first["checked_at"] != s["attempted_at"]


def test_any_other_failure_is_an_error_too(tmp_path):
    def broken(version):
        raise RuntimeError("boom")

    s = _checker(tmp_path, fetch=broken).check()
    assert s["error"]["kind"] == "http" and "boom" in s["error"]["message"]


def test_rate_limited_asks_nothing_until_the_limit_resets(tmp_path):
    clock = Clock()
    calls = []

    def limited(version):
        calls.append(clock.t)
        raise upd.CheckError("rate limit", kind="rate_limited", retry_at=clock.t + 600)

    c = _checker(tmp_path, fetch=limited, now=clock)
    s = c.check()
    assert s["error"]["kind"] == "rate_limited" and s["error"]["retry_at"]
    clock.t += 300
    c.check()
    assert len(calls) == 1  # not yet
    clock.t += 301
    c._fetch = lambda v: _release()
    assert c.check()["error"] is None


def test_the_daily_setting_wins_over_the_default(tmp_path):
    c = _checker(tmp_path, default_daily=False)
    s = c.status()
    assert (s["daily"], s["daily_source"], s["next_check"]) == (False, "default", None)
    s = c.set_daily(True)
    assert (s["daily"], s["daily_source"]) == (True, "setting")
    # A new checker (a restart) with the other default keeps the setting.
    again = _checker(tmp_path, default_daily=True)
    again.set_daily(False)
    assert _checker(tmp_path, default_daily=True).status()["daily"] is False


def test_the_daily_check_is_due_once_a_day(tmp_path):
    clock = Clock()
    c = _checker(tmp_path, now=clock)
    c._started_at = clock.t  # as start() records it, without the thread
    assert not c.due()  # not straight after a start
    clock.t += upd.FIRST_DELAY
    assert c.due()
    c.check()
    assert not c.due()
    clock.t += upd.DAY - 1
    assert not c.due()
    clock.t += 1
    assert c.due()
    c.set_daily(False)
    assert not c.due() and c.status()["next_check"] is None


def test_a_damaged_state_file_reads_as_never_checked(tmp_path):
    (tmp_path / "update.json").write_text("{not json")
    assert _checker(tmp_path).status()["state"] == "unknown"
    assert _checker(tmp_path).check()["state"] == "update_available"


def test_the_thread_stops(tmp_path):
    c = _checker(tmp_path, default_daily=False)
    c.start()
    c.stop()
    c._thread.join(timeout=5)
    assert not c._thread.is_alive()
