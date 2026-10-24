"""CLI parity: every web-app action must have a CLI command.

These tests compare the command registry (``quipclipper.commands``) with the
running app's routes, the request models and the frontend's API calls, so a new
route, a new request field or a new ``/api/`` call in ``app.js`` fails here until
the CLI covers it (or the action is listed as exempt, with a reason). The
round-trip tests then drive the real CLI against the app in-process.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from quipclipper import cli
from quipclipper import commands as reg
from quipclipper.client import Response
from quipclipper_web.app import (
    BookmarkCreate,
    BookmarkUpdate,
    ClipRequest,
    LeadStingerQuipRequest,
    TranscribeRequest,
    FolderTranscribeRequest,
    YouTubeAdd,
    create_app,
)
from quipclipper_web.config import Settings

FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "app.js"
# The Nix build packages web/backend alone, so the frontend isn't next to the tests there.
needs_frontend = pytest.mark.skipif(not FRONTEND.exists(),
                                    reason="web/frontend is not part of this source tree")
TOKEN = "parity-token"

# Request-model fields the CLI deliberately doesn't expose, and why.
IGNORED_FIELDS: dict[str, str] = {}


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    for var in ("QC_URL", "QC_API_TOKEN", "QC_API_TOKEN_FILE"):
        monkeypatch.delenv(var, raising=False)


def _app(tmp_path: Path):
    media = tmp_path / "media"
    media.mkdir(exist_ok=True)
    clips = tmp_path / "clips"
    clips.mkdir(exist_ok=True)
    return create_app(Settings.from_env({
        "QC_MEDIA_ROOTS": str(media), "QC_CLIPS_DIR": str(clips),
        "QC_STATE_DIR": str(tmp_path / "state"), "QC_API_TOKEN": TOKEN,
    })), media, clips


def _api_routes(app) -> set[tuple[str, str]]:
    out = set()
    for r in app.routes:
        path = getattr(r, "path", "")
        if not path.startswith("/api"):
            continue
        for m in getattr(r, "methods", ()) or ():
            if m in ("HEAD", "OPTIONS"):
                continue
            out.add(reg.route_key(m, path))
    return out


def _registry_routes() -> dict[tuple[str, str], reg.Command]:
    return {reg.route_key(c.method, c.path): c for c in reg.COMMANDS if c.method}


# --- 1. routes ----------------------------------------------------------------------


def test_every_api_route_has_a_command_or_an_exemption(tmp_path):
    app, _, _ = _app(tmp_path)
    covered = set(_registry_routes()) | {reg.route_key(*k) for k in reg.EXEMPT_ROUTES}
    missing = sorted(_api_routes(app) - covered)
    assert not missing, (
        f"API routes with no CLI command: {missing}. Add a Command to "
        "src/quipclipper/commands.py (or an EXEMPT_ROUTES entry with a reason).")


def test_every_command_endpoint_exists(tmp_path):
    app, _, _ = _app(tmp_path)
    routes = _api_routes(app)
    stale = sorted(k for k in _registry_routes() if k not in routes)
    stale += sorted(reg.route_key(*k) for k in reg.EXEMPT_ROUTES
                    if reg.route_key(*k) not in routes)
    assert not stale, f"registry names routes the app doesn't have: {stale}"


# --- 2. parameters --------------------------------------------------------------------


def _wire_names(cmd: reg.Command, where: str) -> set[str]:
    return {p.name for p in cmd.params if p.where == where}


@pytest.mark.parametrize("cmd_id,model", [
    ("clip.create", ClipRequest),
    ("bookmark.add", BookmarkCreate),
    ("bookmark.update", BookmarkUpdate),
    ("youtube.add", YouTubeAdd),
    ("leadstinger.quip", LeadStingerQuipRequest),
    ("item.transcribe", TranscribeRequest),
    ("index.transcribe", FolderTranscribeRequest),
])
def test_body_params_match_request_models(cmd_id, model):
    cmd = reg.BY_ID[cmd_id]
    fields = set(model.model_fields) - set(IGNORED_FIELDS)
    assert _wire_names(cmd, reg.BODY) == fields


def test_query_and_path_params_match_routes(tmp_path):
    app, _, _ = _app(tmp_path)
    by_route = _registry_routes()
    problems = []
    for r in app.routes:
        if not getattr(r, "path", "").startswith("/api") or not hasattr(r, "dependant"):
            continue
        for m in r.methods:
            cmd = by_route.get(reg.route_key(m, r.path))
            if cmd is None:
                continue
            q = {p.alias for p in r.dependant.query_params}  # the wire name (?as=)
            pp = {p.name for p in r.dependant.path_params}
            if _wire_names(cmd, reg.QUERY) != q:
                problems.append((cmd.id, "query", sorted(q), sorted(_wire_names(cmd, reg.QUERY))))
            if _wire_names(cmd, reg.PATH) != pp:
                problems.append((cmd.id, "path", sorted(pp), sorted(_wire_names(cmd, reg.PATH))))
    assert not problems, problems


# --- 3. the frontend ------------------------------------------------------------------


def _frontend_api_calls(source: str) -> set[str]:
    """Normalised ``/api/...`` paths the frontend references: template
    expressions become ``{}``, query strings are dropped."""
    found = set()
    for literal in re.findall(r"[`\"'](/api/[^`\"'\s]*)", source):
        # An expression glued to a path segment (``/api/bookmarks${qs}``) is a
        # query suffix built at runtime; one after a "/" is a path parameter.
        literal = re.sub(r"(?<!/)\$\{[^}]*\}.*$", "", literal)
        path = re.sub(r"\$\{[^}]*\}", "{}", literal).split("?", 1)[0].rstrip("/")
        found.add(path)
    return found


def _template_regex(path: str) -> re.Pattern:
    return re.compile("^" + re.sub(r"\\\{\w+\\\}", r"[^/]+", re.escape(path)) + "$")


def test_frontend_api_calls_normaliser():
    src = ('getJSON(`/api/jobs/${id}`); apiFetch("/api/bookmarks", {}); '
           'x=`/api/items?path=${p}`; y=`/api/clips${qs}`; z=`/api/search${q}&query=${t}`')
    assert _frontend_api_calls(src) == {"/api/jobs/{}", "/api/bookmarks", "/api/items",
                                        "/api/clips", "/api/search"}


@needs_frontend
def test_every_frontend_api_call_has_a_command_or_an_exemption():
    paths = [c.path for c in reg.COMMANDS if c.path] + [k[1] for k in reg.EXEMPT_ROUTES]
    patterns = [_template_regex(p) for p in paths]
    missing = []
    for call in sorted(_frontend_api_calls(FRONTEND.read_text())):
        probe = call.replace("{}", "x")
        if not any(p.match(probe) or p.match(call) for p in patterns):
            # A prefix the frontend extends at runtime (e.g. download links).
            if not any(path.startswith(call + "/") for path in paths):
                missing.append(call)
    assert not missing, f"frontend API calls with no CLI command: {missing}"


@needs_frontend
def test_every_frontend_setting_maps_to_a_cli_setting_or_an_exemption():
    keys = set(re.findall(r"""localStorage\.(?:setItem|getItem)\(\s*["'`](\w+)""",
                          FRONTEND.read_text()))
    keys |= set(re.findall(r"""const \w+_KEY\s*=\s*["'](\w+)["']""", FRONTEND.read_text()))
    unmapped = sorted(k for k in keys if k not in reg.UI_SETTINGS
                      and k not in reg.EXEMPT_UI_SETTINGS)
    assert not unmapped, f"web UI preferences with no CLI setting: {unmapped}"
    for key in reg.UI_SETTINGS.values():
        assert key in reg.SETTINGS


# --- 4. round trips through the real CLI -------------------------------------------------


class AppTransport:
    """The CLI's Transport, answered by the app in-process."""

    def __init__(self, client: TestClient):
        self.client = client
        self.calls: list[tuple[str, str]] = []

    def request(self, method, path, query, body, stream=False):
        self.calls.append((method, path))
        r = self.client.request(method, path, params=query, json=body,
                                headers={"X-API-Key": TOKEN})
        headers = {k.lower(): v for k, v in r.headers.items()}
        if stream:
            return Response(r.status_code, headers, lines=iter(r.text.splitlines()))
        return Response(r.status_code, headers, r.content)


SRT = "1\n00:00:01,000 --> 00:00:03,000\nI'll be back.\n\n2\n00:00:05,000 --> 00:00:07,000\nHasta la vista, baby.\n"


def _run(transport, argv, capsys) -> tuple[int, str]:
    code = cli.main(["--json", *argv], transport=transport)
    out = capsys.readouterr().out.strip()
    return code, out


def _last_json(out: str):
    return json.loads(out.splitlines()[-1])


@pytest.fixture()
def served(tmp_path):
    app, media, clips = _app(tmp_path)
    video = media / "movie.mkv"
    video.write_bytes(b"")
    (media / "movie.srt").write_text(SRT, encoding="utf-8")
    transport = AppTransport(TestClient(app))

    def fake_cut(src, rng, **kw):
        out = Path(kw["out"])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"clip bytes")
        return out

    with patch("quipclipper_web.app.cut_clip", side_effect=fake_cut):
        yield transport, media, clips, video


def test_round_trip_browse_search_clip_download(served, capsys, tmp_path):
    transport, media, clips, video = served
    code, out = _run(transport, ["ls", str(media)], capsys)
    assert code == 0 and any(e["name"] == "movie.mkv" for e in _last_json(out)["entries"])

    code, out = _run(transport, ["search", "be back", "--video", str(video)], capsys)
    assert code == 0 and _last_json(out)["matches"][0]["text"] == "I'll be back."

    dest = tmp_path / "downloads"
    code, out = _run(transport, ["clip", "be back", "-v", str(video), "-t", "audio",
                                 "--backend", "ffmpeg", "--out-dir", str(dest)], capsys)
    assert code == 0, out
    job = _last_json(out)
    assert job["status"] == "done" and job["saved"]
    assert Path(job["saved"][0]).read_bytes() == b"clip bytes"

    code, out = _run(transport, ["clips.list"], capsys)
    assert code == 0
    assert _last_json(out)["folders"] == ["movie"]


def test_round_trip_clip_sends_the_cli_lossless_default(served, capsys):
    transport, media, clips, video = served
    sent = []
    original = transport.request

    def spy(method, path, query, body, stream=False):
        if path == "/api/clip":
            sent.append(body)
        return original(method, path, query, body, stream)

    transport.request = spy
    _run(transport, ["clip", "be back", "-v", str(video), "--backend", "ffmpeg"], capsys)
    assert sent[0]["kind"] == "video" and sent[0]["lossless"] is False


def test_round_trip_bookmarks(served, capsys):
    transport, media, clips, video = served
    code, out = _run(transport, ["bookmark.add", str(video), "--start", "1", "--end", "3",
                                 "--label", "back"], capsys)
    assert code == 0
    bid = _last_json(out)["id"]
    code, out = _run(transport, ["bookmark.update", bid, "--before", "1.5"], capsys)
    assert code == 0 and _last_json(out)["before"] == 1.5
    code, out = _run(transport, ["bookmark.list", "--path", str(video)], capsys)
    assert [b["id"] for b in _last_json(out)["bookmarks"]] == [bid]
    # Destructive commands are refused with --safe …
    code, out = _run(transport, ["--safe", "bookmark.delete", bid], capsys)
    assert code == 5
    # … and run without it.
    code, out = _run(transport, ["bookmark.delete", bid], capsys)
    assert code == 0
    code, out = _run(transport, ["bookmark.list"], capsys)
    assert _last_json(out)["bookmarks"] == []


def test_round_trip_batch_from_folder_search(served, capsys, monkeypatch):
    transport, media, clips, video = served
    code, out = _run(transport, ["index.build", str(media)], capsys)  # folder search: indexed only
    assert code == 0
    code, out = _run(transport, ["search.folder", "baby", "--path", str(media)], capsys)
    assert code == 0
    hits = out.splitlines()[-1]
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(hits))
    code, out = _run(transport, ["clip.batch", "--kind", "audio", "--backend", "ffmpeg"],
                     capsys)
    assert code == 0, out
    result = _last_json(out)
    assert len(result["jobs"]) == 1 and result["jobs"][0]["status"] == "done"

    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(hits))
    code, out = _run(transport, ["bookmark.batch"], capsys)
    assert code == 0 and len(_last_json(out)["bookmarks"]) == 1


def test_round_trip_script_and_index(served, capsys):
    transport, media, clips, video = served
    code, out = _run(transport, ["item.script", str(video)], capsys)
    assert code == 0 and [c["text"] for c in _last_json(out)][:2] == [
        "I'll be back.", "Hasta la vista, baby."]
    code, out = _run(transport, ["index.status", str(media)], capsys)
    assert code == 0 and "total" in _last_json(out)


def test_server_only_command_without_a_server_exits_3(capsys):
    assert cli.main(["--json", "ls"]) == 3
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "no_server"


def test_local_only_flag_is_refused_in_server_mode(served, capsys):
    transport, media, clips, video = served
    code, out = _run(transport, ["clip", "be back", "-v", str(video), "--pick"], capsys)
    assert code == 2


def test_every_server_command_is_reachable_by_id_and_shortcut():
    group = __import__("typer").main.get_command(cli.app)
    names = set(group.list_commands(None))
    for c in reg.COMMANDS:
        assert c.id in names
        if c.shortcut:
            assert c.shortcut in names


def test_round_trip_item_link(tmp_path, capsys):
    """item.link (the Copy link button's twin) prints the deep link, built from QC_PUBLIC_URL."""
    media = tmp_path / "media"
    media.mkdir()
    video = media / "movie.mkv"
    video.write_bytes(b"")
    app = create_app(Settings.from_env({
        "QC_MEDIA_ROOTS": str(media), "QC_CLIPS_DIR": str(tmp_path / "clips"),
        "QC_STATE_DIR": str(tmp_path / "state"), "QC_API_TOKEN": TOKEN,
        "QC_PUBLIC_URL": "https://quip.example/"}))
    transport = AppTransport(TestClient(app))
    code, out = _run(transport, ["item.link", str(video), "--at", "5", "--sel", "5-7.25"], capsys)
    assert code == 0, out
    assert _last_json(out)["link"] == f"https://quip.example/#/item?path={video}&t=5&sel=5-7.25"
    # Without --json it prints the link alone, ready to paste.
    assert cli.main(["item.link", str(video), "--sel", "1-2"], transport=transport) == 0
    assert capsys.readouterr().out.strip() == f"https://quip.example/#/item?path={video}&t=1&sel=1-2"


def test_round_trip_index_file(served, capsys):
    """The re-index hook's command (M20): queue a subtitle's video, then read the queue."""
    transport, media, clips, video = served
    code, out = _run(transport, ["index.file", str(media / "movie.srt")], capsys)
    assert code == 0, out
    res = _last_json(out)
    assert res["queued"] == 1 and res["files"][0]["media"] == str(video)
    for _ in range(100):
        code, out = _run(transport, ["index.file-status"], capsys)
        st = _last_json(out)
        if st["recent"] and not st["pending"] and st["job_id"] is None:
            break
        __import__("time").sleep(0.05)
    assert [r["status"] for r in st["recent"]] == ["indexed"]
    # Nothing it can use: the command fails.
    code, out = _run(transport, ["index.file", "/etc/passwd"], capsys)
    assert code != 0
    cli.main(["index.file", str(video), "--force"], transport=transport)
    assert "queued" in capsys.readouterr().out


def test_round_trip_index_changes(served, capsys):
    """index.changes / index.reindex-changed (M20): the out-of-date badge from the CLI."""
    transport, media, clips, video = served
    code, out = _run(transport, ["index.changes"], capsys)
    assert code == 0 and _last_json(out)["changed"] == 0
    code, out = _run(transport, ["index.reindex-changed"], capsys)
    assert code == 0 and _last_json(out)["queued"] == 0
    assert cli.main(["index.changes"], transport=transport) == 0
    assert "0 files with subtitles changed since indexing" in capsys.readouterr().out


def test_round_trip_pairing_and_connected_apps(served, capsys):
    """pair.list / pair.approve / pair.deny and clients.list / clients.revoke: the pairing
    banner and Settings ▸ Connected apps from the CLI (with the server's own token)."""
    transport, media, clips, video = served
    web = transport.client
    ask = lambda: web.post("/api/pair", json={"name": "LeadStinger", "host": "laptop"},
                           headers={"X-Quipclipper": "1"}).json()
    first, second = ask(), ask()
    code, out = _run(transport, ["pair.list"], capsys)
    assert code == 0
    assert [r["code"] for r in _last_json(out)["requests"]] == [first["code"], second["code"]]
    # Approving and denying change things: refused with --safe.
    code, out = _run(transport, ["--safe", "pair.approve", first["id"]], capsys)
    assert code == 5
    code, out = _run(transport, ["pair.approve", first["id"]], capsys)
    assert code == 0 and _last_json(out)["status"] == "approved"
    client_id = _last_json(out)["client_id"]
    code, out = _run(transport, ["pair.deny", second["id"]], capsys)
    assert code == 0 and _last_json(out) == {"status": "denied"}
    code, out = _run(transport, ["pair.deny", second["id"]], capsys)
    assert code != 0   # answered already: 404
    token = web.get(f"/api/pair/{first['id']}",
                    headers={"X-Pair-Secret": first["poll_secret"]}).json()["token"]

    code, out = _run(transport, ["clients.list"], capsys)
    assert code == 0 and [c["id"] for c in _last_json(out)["clients"]] == [client_id]
    assert cli.main(["clients.list"], transport=transport) == 0
    assert f"{client_id}  LeadStinger on laptop" in capsys.readouterr().out
    code, out = _run(transport, ["clients.revoke", client_id], capsys)
    assert code == 0 and _last_json(out) == {"removed": True}
    assert web.get("/api/clips", headers={"X-API-Key": token}).status_code == 401


def test_round_trip_leadstinger_connect(served, capsys):
    """leadstinger.connect: the Connect LeadStinger button's leadstinger://pair link."""
    transport, media, clips, video = served
    code, out = _run(transport, ["leadstinger.connect", "--no-open"], capsys)
    assert code == 0, out
    result = _last_json(out)
    assert result["server"] == "http://testserver" and result["pairing"] is True
    assert result["link"] == "leadstinger://pair?server=http%3A%2F%2Ftestserver"
    assert result["opened"] is False
    assert cli.main(["leadstinger.connect", "--no-open"], transport=transport) == 0
    assert capsys.readouterr().out.strip() == result["link"]
