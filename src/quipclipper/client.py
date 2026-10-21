"""Drive a quipclipper web server over its HTTP API.

The executor is generic: a registry ``Command`` plus a dict of parameter values
becomes one HTTP request. Only the standard library is used, so the CLI keeps
its small dependency set.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Optional, Protocol

from quipclipper.commands import BODY, PATH, QUERY, SETTINGS, Command

# Exit codes (also documented in docs/CLI.md).
EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_NO_SERVER, EXIT_AUTH, EXIT_REFUSED = 0, 1, 2, 3, 4, 5


class CliError(Exception):
    """An error with a stable code and an exit status."""

    def __init__(self, message: str, *, code: str = "failed", exit_code: int = EXIT_FAILED):
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code


# --- settings ------------------------------------------------------------------------


def settings_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "quipclipper" / "cli.json"


def load_settings() -> dict[str, str]:
    try:
        data = json.loads(settings_path().read_text())
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if k in SETTINGS and isinstance(v, str)}


def save_settings(values: dict[str, str]) -> None:
    p = settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(values, indent=2, sort_keys=True) + "\n")


# --- transport ----------------------------------------------------------------------------


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes = b""
    lines: Optional[Iterator[str]] = None  # set for streamed responses

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "")

    def json(self) -> Any:
        return json.loads(self.body or b"null")


class Transport(Protocol):
    def request(self, method: str, path: str, query: list[tuple[str, str]],
                body: Optional[dict], stream: bool = False) -> Response: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: a followed one would carry the token to wherever
    Location points, and an unfollowed one must not pass for success."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


class UrllibTransport:
    """HTTP transport for a real server.

    Every request carries ``X-Quipclipper: 1`` (the CSRF header a browser sends),
    so a server with no API token configured still accepts writes. With a token
    it adds ``X-API-Key`` for the backend and ``Authorization: Basic api:<token>``
    for the nginx password gate in front of it. Redirects are errors (see
    :class:`_NoRedirect`). ``token_error`` is a token file that couldn't be read: it
    fails the first request, so commands that never call the server still work.
    """

    def __init__(self, base_url: str, token: Optional[str] = None, timeout: float = 600.0,
                 token_error: Optional["CliError"] = None):
        self.base = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.token_error = token_error

    def request(self, method, path, query, body, stream=False):
        if self.token_error is not None:
            raise self.token_error
        url = self.base + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        headers = {"X-Quipclipper": "1", "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["X-API-Key"] = self.token
            basic = base64.b64encode(f"api:{self.token}".encode()).decode()
            headers["Authorization"] = f"Basic {basic}"
        try:
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
        except ValueError as exc:  # no scheme, or not a URL at all
            raise CliError(f"Not a server URL: {self.base} ({exc}); use e.g. "
                           "https://quip.example", code="usage", exit_code=EXIT_USAGE) from None
        try:
            resp = _OPENER.open(req, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            return Response(exc.code, {k.lower(): v for k, v in exc.headers.items()},
                            exc.read())
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise CliError(f"Can't reach {self.base}: {reason}", code="no_server",
                           exit_code=EXIT_NO_SERVER) from None
        headers_out = {k.lower(): v for k, v in resp.headers.items()}
        if stream:
            def gen() -> Iterator[str]:
                with resp:
                    for raw in resp:
                        yield raw.decode("utf-8", "replace").rstrip("\r\n")
            return Response(resp.status, headers_out, lines=gen())
        with resp:
            return Response(resp.status, headers_out, resp.read())


def read_token(settings: dict[str, str]) -> Optional[str]:
    token = os.environ.get("QC_API_TOKEN")
    if token:
        return token.split(",")[0].strip()
    token_file = os.environ.get("QC_API_TOKEN_FILE") or settings.get("token_file")
    if token_file:
        try:
            return Path(token_file).expanduser().read_text().strip() or None
        except OSError as exc:
            raise CliError(f"Can't read the API token file {token_file}: {exc}",
                           code="usage", exit_code=EXIT_USAGE) from None
    return None


@dataclass
class Context:
    """How this invocation talks to quipclipper."""

    server: Optional[str] = None
    transport: Optional[Transport] = None
    json: bool = False
    safe: bool = False
    settings: dict[str, str] = field(default_factory=dict)
    err: Callable[[str], None] = lambda s: print(s, file=sys.stderr)

    @property
    def remote(self) -> bool:
        return self.transport is not None


def make_context(*, server: Optional[str], local: bool, json_out: bool, safe: bool,
                 transport: Optional[Transport] = None) -> Context:
    """Pick the mode: ``--local`` > ``--server`` > ``QC_URL`` > settings > local."""
    settings = load_settings()
    ctx = Context(json=json_out, safe=safe, settings=settings)
    if local:
        return ctx
    if transport is not None:
        ctx.transport = transport
        ctx.server = server or "test"
        return ctx
    url = server or os.environ.get("QC_URL") or settings.get("server")
    if url:
        ctx.server = url
        try:
            ctx.transport = UrllibTransport(url, read_token(settings))
        except CliError as exc:
            ctx.transport = UrllibTransport(url, None, token_error=exc)
    return ctx


# --- execution -----------------------------------------------------------------------------


def _wire(value: Any, ptype: str, name: str = "") -> Any:
    """Convert a CLI value to its wire form. Lists may arrive as "0,2" (never
    split for ``path``: a folder name may contain a comma)."""
    if value is None:
        return None
    if ptype.startswith("list["):
        items: list = []
        for v in (value if isinstance(value, (list, tuple)) else [value]):
            if isinstance(v, str) and ptype == "list[int]":
                items += [x for x in v.replace(" ", "").split(",") if x != ""]
            elif isinstance(v, str) and ptype == "list[str]" and name != "path":
                items += [x.strip() for x in v.split(",") if x.strip()]
            else:
                items.append(v)
        if ptype == "list[int]":
            try:
                ints = [int(x) for x in items]
            except ValueError:
                raise CliError(f"expected comma-separated integers, got {value!r}",
                               code="usage", exit_code=EXIT_USAGE) from None
            if any(i < 0 for i in ints):
                raise CliError("indices must be 0 or greater", code="usage",
                               exit_code=EXIT_USAGE)
            return ints or None
        return items or None
    return value


def build_request(cmd: Command, values: dict[str, Any]):
    """Turn a command and its values into (method, path, query, body)."""
    path = cmd.path or ""
    query: list[tuple[str, str]] = []
    body: dict[str, Any] = {}
    for p in cmd.params:
        v = _wire(values.get(p.name, p.default), p.type, p.name)
        if p.where == PATH:
            if v is None:
                if p.required:
                    raise CliError(f"{p.name} is required", code="usage",
                                   exit_code=EXIT_USAGE)
                path = path.replace("/{" + p.name + "}", "")
                continue
            path = path.replace("{" + p.name + "}",
                                urllib.parse.quote(str(v), safe="/" if p.name == "clip_path" else ""))
        elif p.where == QUERY:
            if v is None:
                if p.required:
                    raise CliError(f"--{p.name.replace('_', '-')} is required",
                                   code="usage", exit_code=EXIT_USAGE)
                continue
            for item in (v if isinstance(v, list) else [v]):
                query.append((p.name, str(item).lower() if isinstance(item, bool) else str(item)))
        elif p.where == BODY:
            if v is None:
                if p.required:
                    raise CliError(f"--{p.name.replace('_', '-')} is required",
                                   code="usage", exit_code=EXIT_USAGE)
                continue
            body[p.name] = v
    has_body = any(p.where == BODY for p in cmd.params)
    return cmd.method, path, query, (body if has_body else None)


# Requests whose 404 can mean "that path isn't on the server": the hint about --local
# helps there, not for a job, a bookmark or "no such match".
_PATH_ROUTES = ("/api/items", "/api/search", "/api/library", "/api/media")


def _error_from(resp: Response, path: str = "") -> CliError:
    if 300 <= resp.status < 400:
        where = resp.headers.get("location") or "another URL"
        return CliError(f"The server redirected to {where} (HTTP {resp.status}); use that "
                        "address as --server / QC_URL.", code="redirect",
                        exit_code=EXIT_NO_SERVER)
    try:
        detail = resp.json().get("detail")
    except Exception:  # noqa: BLE001 — a non-JSON error page
        detail = None
    if isinstance(detail, list):  # FastAPI validation errors
        detail = "; ".join(
            f"{'.'.join(str(x) for x in d.get('loc', [])[1:])}: {d.get('msg')}" for d in detail
        )
    message = detail or resp.body.decode("utf-8", "replace")[:300] or f"HTTP {resp.status}"
    # The auth/CSRF gate answers a bare "Forbidden"; a route's own 403 (a path outside
    # the media roots) carries a real message and is not about the token.
    if resp.status == 401 or (resp.status == 403 and message.strip().strip('"') == "Forbidden"):
        return CliError(f"{message} (check QC_API_TOKEN / the token file)", code="auth",
                        exit_code=EXIT_AUTH)
    if resp.status == 403:
        return CliError(message, code="forbidden", exit_code=EXIT_USAGE)
    if resp.status == 422 or resp.status == 400:
        return CliError(message, code="bad_request", exit_code=EXIT_USAGE)
    if resp.status == 404:
        if path.startswith(_PATH_ROUTES) or path == "/api/clip":
            message += (" (paths are resolved on the server; use --local for a file on "
                        "this machine)")
        return CliError(message, code="not_found")
    return CliError(message, code=f"http_{resp.status}")


def call(ctx: Context, method: str, path: str, query=None, body=None, stream=False) -> Response:
    if not ctx.remote:
        raise CliError("This command needs a quipclipper server: pass --server URL or "
                       "set QC_URL.", code="no_server", exit_code=EXIT_NO_SERVER)
    resp = ctx.transport.request(method, path, query or [], body, stream=stream)
    if resp.status >= 300:
        raise _error_from(resp, path)
    return resp


def execute(ctx: Context, cmd: Command, values: dict[str, Any]) -> Any:
    """Run a server command. Returns the parsed reply (or a summary for files/lines)."""
    if cmd.destructive and ctx.safe:
        raise CliError(f"{cmd.id} changes or deletes data; refused with --safe.",
                       code="refused", exit_code=EXIT_REFUSED)
    method, path, query, body = build_request(cmd, values)
    if cmd.response == "lines":
        resp = call(ctx, method, path, query, body, stream=True)
        last = None
        for line in resp.lines or []:
            last = line
            if not ctx.json:
                print(line, flush=True)
        return {"last": last}
    if cmd.response == "file":
        if cmd.id == "job.download" and not values.get("filename"):
            job = call(ctx, "GET", f"/api/jobs/{urllib.parse.quote(str(values['job_id']))}").json()
            return {"files": download_job_files(ctx, job, values.get("out_dir") or ".")}
        resp = call(ctx, method, path, query, body)
        name = _filename(resp, path)
        out = Path(values.get("out_dir") or ".").expanduser() / name
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(resp.body)
        except OSError as exc:
            raise CliError(f"Can't save {out}: {exc.strerror or exc}", code="io") from None
        return {"saved": str(out), "size": len(resp.body)}
    resp = call(ctx, method, path, query, body)
    if "json" not in resp.content_type:
        return resp.body.decode("utf-8", "replace")
    data = resp.json()
    if cmd.response == "job" and isinstance(data, dict) and data.get("job_id") \
            and values.get("wait", True):
        out_dir = values.get("out_dir") or ctx.settings.get("out_dir")
        return wait_job(ctx, data["job_id"], out_dir=out_dir)
    return data


def _filename(resp: Response, path: str) -> str:
    """The name to save a download as: the server's Content-Disposition, else the URL's
    last segment. Only ever a bare name, so a reply can't write outside --out-dir."""
    cd = resp.headers.get("content-disposition", "")
    name = ""
    for part in cd.split(";"):
        part = part.strip()
        if part.lower().startswith("filename*="):
            name = urllib.parse.unquote(part.split("''", 1)[-1])
            break
        if part.lower().startswith("filename="):
            name = part.split("=", 1)[1].strip('"')
    if not name:
        name = urllib.parse.unquote(path.rstrip("/").rsplit("/", 1)[-1])
    name = Path(name.replace("\\", "/")).name
    return name if name not in ("", ".", "..") else "download"


TERMINAL = ("done", "failed", "cancelled")


def wait_job(ctx: Context, job_id: str, *, out_dir: Optional[str] = None,
             poll: float = 1.0) -> dict:
    """Poll a job until it finishes; optionally download its files.

    Ctrl-C cancels the job on the server (a job you started and stopped
    watching would otherwise keep running unseen)."""
    path = f"/api/jobs/{urllib.parse.quote(str(job_id))}"
    shown = None
    try:
        while True:
            job = call(ctx, "GET", path).json()
            status = job.get("status")
            line = f"{job.get('label') or job_id}: {status}" + (
                f" — {job['progress']}" if job.get("progress") else "")
            if line != shown and not ctx.json:
                ctx.err(line)
                shown = line
            if status in TERMINAL:
                break
            time.sleep(poll)
    except KeyboardInterrupt:
        ctx.err(f"Cancelling job {job_id}…")
        try:
            call(ctx, "DELETE", path)
        except CliError as exc:
            # A running clip cut can't stop (409): it finishes on the server.
            raise CliError(f"Interrupted; the job keeps running on the server ({exc}).",
                           code="interrupted") from None
        raise CliError("Interrupted; job cancelled.", code="interrupted") from None
    if status == "failed":
        raise CliError(job.get("error") or "job failed", code="job_failed")
    if status == "cancelled":
        raise CliError("job was cancelled", code="job_cancelled")
    if out_dir and job.get("files"):
        job["saved"] = download_job_files(ctx, job, out_dir)
    return job


def download_job_files(ctx: Context, job: dict, out_dir: str) -> list[str]:
    saved = []
    dest = Path(out_dir).expanduser()
    dest.mkdir(parents=True, exist_ok=True)
    for f in job.get("files") or []:
        name = f["name"] if isinstance(f, dict) else str(f)
        resp = call(ctx, "GET", f"/api/jobs/{urllib.parse.quote(str(job['id']))}/download/"
                                f"{urllib.parse.quote(name)}")
        target = dest / Path(name).name
        target.write_bytes(resp.body)
        saved.append(str(target))
    return saved
