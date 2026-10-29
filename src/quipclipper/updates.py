"""Check for updates (M24): is a newer quipclipper release out on GitHub?

A check asks GitHub's API for the latest release of weckere/quipclipper and compares its
version with this build's. The request carries a ``User-Agent: quipclipper/<version>``
and nothing else about this install; proxies in the environment (``https_proxy``) are
honoured. Nothing here updates anything: a check says what is out and how to update this
kind of install (Docker, NixOS, Nix, pip).

The build says who it is through the environment: ``QC_BUILD_COMMIT`` (a build from
``dev`` carries its commit; a release image carries none) and ``QC_INSTALL`` (docker,
nixos, nix; anything else is pip). Only the release script changes the version, so a dev
build's version is the release it is based on: "dev build <sha>, ahead of X.Y.Z", and an
update is available only when a release newer than that is out. The NixOS module always
passes its flake's commit (the flake can't tell a release tag from a dev commit), so a check
also asks which commit the base release's tag is, once per version: a build on exactly that
commit is the release itself.

Only the standard library is used, so the CLI keeps its small dependency set.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import math
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

REPO = "weckere/quipclipper"
LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_URL = f"https://github.com/{REPO}/releases"
TIMEOUT = 10.0
DAY = 86400.0
# The daily check's first look comes this long after the server starts (not at once: a
# restart loop must not become a request loop), and it wakes this often to see whether
# a check is due or the setting changed.
FIRST_DELAY = 600.0
WAKE = 3600.0
MAX_REPLY = 2 * 1024 * 1024
# A stored time past this (the year 3000) is damage, not a time; and a rate-limit reset more
# than a day ahead is never GitHub's (it resets hourly), so it can't hold checks off for good.
MAX_TIME = 32_503_680_000.0
MAX_RETRY_WAIT = 86400.0

INSTALLS = ("docker", "nixos", "nix", "pip")
_COMMIT = re.compile(r"[0-9a-f]{7,40}(-dirty)?")


def parse_version(text: str | None) -> tuple[int, ...] | None:
    """``v0.5.1`` or ``0.5.1`` → ``(0, 5, 1)``; None for anything else (a pre-release tag)."""
    m = re.fullmatch(r"v?(\d+(?:\.\d+)*)", (text or "").strip())
    return tuple(int(x) for x in m.group(1).split(".")) if m else None


def compare(a: str, b: str) -> int | None:
    """-1, 0 or 1 as version ``a`` is older than, the same as or newer than ``b``; None
    when either isn't a plain version."""
    va, vb = parse_version(a), parse_version(b)
    if va is None or vb is None:
        return None
    n = max(len(va), len(vb))
    va, vb = va + (0,) * (n - len(va)), vb + (0,) * (n - len(vb))
    return (va > vb) - (va < vb)


@dataclass(frozen=True)
class Build:
    """This build: its version, its commit (dev builds only) and how it was installed."""

    version: str
    commit: str = ""
    install: str = "pip"

    @classmethod
    def from_env(cls, version: str, env: Mapping[str, str] | None = None) -> "Build":
        e = os.environ if env is None else env
        commit = (e.get("QC_BUILD_COMMIT") or "").strip().lower()
        install = (e.get("QC_INSTALL") or "").strip().lower()
        return cls(version=version,
                   commit=commit if _COMMIT.fullmatch(commit) else "",
                   install=install if install in INSTALLS else "pip")

    @property
    def dev(self) -> bool:
        return bool(self.commit)

    @property
    def short(self) -> str:
        """The commit as people read it: 7 characters, and ``-dirty`` when it was."""
        if not self.commit:
            return ""
        return self.commit[:7] + ("-dirty" if self.commit.endswith("-dirty") else "")

    @property
    def label(self) -> str:
        """``0.5.1``, or ``dev build 2da1479, ahead of 0.5.1``."""
        return f"dev build {self.short}, ahead of {self.version}" if self.dev else self.version

    def describe(self) -> dict[str, Any]:
        return {"version": self.version, "commit": self.commit or None,
                "short": self.short or None, "dev": self.dev, "install": self.install,
                "label": self.label}


def how_to_update(install: str, tag: str | None = None, web: bool = False,
                  dev: bool = False) -> dict[str, Any]:
    """How to update an install of this kind to release ``tag`` (None: the latest).
    ``web``: a quipclipper-web server, whose pip install has a second package; ``dev``: a
    build from a checkout (its images were built here, not pulled)."""
    ref = tag or "main"  # main is the latest release
    if install == "docker" and dev:
        return {"install": "docker",
                "summary": "This server runs from images built from a checkout: update the "
                           "checkout and rebuild them (or switch the compose file to the "
                           "released images and pull those).",
                "commands": ["git pull",
                             "QC_BUILD_COMMIT=$(git rev-parse HEAD) docker compose up -d --build"]}
    if install == "docker":
        return {"install": "docker",
                "summary": "This server runs from the Docker images: pull the new ones and "
                           "recreate the containers. If your compose file pins a version "
                           f"tag, change it to {ref.lstrip('v')} first.",
                "commands": ["docker compose pull", "docker compose up -d"]}
    if install == "nixos":
        return {"install": "nixos",
                "summary": "This is a NixOS install: update the quipclipper flake input, "
                           "then rebuild.",
                "commands": ["nix flake update quipclipper",
                             "sudo nixos-rebuild switch --flake .#<host>"]}
    if install == "nix":
        return {"install": "nix",
                "summary": "This runs from the quipclipper flake: upgrade the profile that "
                           "installed it, or update the flake input that provides it.",
                "commands": ["nix profile upgrade quipclipper"]}
    git = f"git+https://github.com/{REPO}@{ref}"
    commands = [f'pip install --upgrade "{git}"']
    if web:
        commands.append(f'pip install --upgrade "{git}#subdirectory=web/backend"')
    return {"install": "pip", "summary": "This was installed with pip: upgrade it to the "
                                         "release.", "commands": commands}


class CheckError(Exception):
    """A check that got no answer: ``kind`` is unreachable, rate_limited, no_release or
    http; ``retry_at`` (epoch seconds) is when GitHub's rate limit resets."""

    def __init__(self, message: str, kind: str = "unreachable",
                 retry_at: float | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.retry_at = retry_at


def _header_float(headers: Any, name: str) -> float | None:
    try:
        value = float(headers.get(name))
    except (TypeError, ValueError, AttributeError):
        return None
    return value if math.isfinite(value) else None


def fetch_latest(version: str, url: str = LATEST_URL, timeout: float = TIMEOUT,
                 opener: Callable[..., Any] | None = None,
                 now: Callable[[], float] = time.time) -> dict[str, Any]:
    """The latest release: ``{version, tag, url, name, published_at}``. Raises CheckError."""
    req = urllib.request.Request(url, headers={
        "User-Agent": f"quipclipper/{version}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    open_ = opener or urllib.request.urlopen  # honours http(s)_proxy and no_proxy
    try:
        with open_(req, timeout=timeout) as resp:
            data = json.loads(resp.read(MAX_REPLY))
    except urllib.error.HTTPError as exc:
        limit_left = exc.headers.get("X-RateLimit-Remaining") if exc.headers else None
        retry_after = _header_float(exc.headers, "Retry-After")
        if exc.code == 429 or (exc.code == 403 and (limit_left == "0" or retry_after)):
            reset = _header_float(exc.headers, "X-RateLimit-Reset")
            if reset is None and retry_after is not None:
                reset = now() + retry_after
            raise CheckError("GitHub's rate limit for this address is used up",
                             kind="rate_limited", retry_at=reset) from None
        if exc.code == 404:
            raise CheckError("GitHub lists no release yet", kind="no_release") from None
        raise CheckError(f"GitHub answered {exc.code}", kind="http") from None
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as exc:
        reason = getattr(exc, "reason", None) or exc
        raise CheckError(f"couldn't reach GitHub ({reason})"[:300]) from None
    tag = data.get("tag_name") if isinstance(data, dict) else None
    if not isinstance(tag, str) or parse_version(tag) is None:
        raise CheckError("GitHub's reply names no release version", kind="http")
    name = data.get("name")
    published = data.get("published_at")
    return {
        "version": tag.strip().lstrip("v"),
        "tag": tag.strip(),
        # Built here, not taken from the reply: the page links to it.
        "url": f"{RELEASES_URL}/tag/{urllib.parse.quote(tag.strip())}",
        "name": name if isinstance(name, str) else None,
        "published_at": published if isinstance(published, str) else None,
    }


def fetch_tag_commit(version: str, tag: str, timeout: float = TIMEOUT,
                     opener: Callable[..., Any] | None = None) -> str | None:
    """The commit release ``tag`` points at (GitHub's plain-sha reply, 40 characters);
    None when GitHub has no such tag. Raises CheckError when it can't be asked."""
    url = f"https://api.github.com/repos/{REPO}/commits/{urllib.parse.quote(tag)}"
    req = urllib.request.Request(url, headers={
        "User-Agent": f"quipclipper/{version}",
        "Accept": "application/vnd.github.sha",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    open_ = opener or urllib.request.urlopen
    try:
        with open_(req, timeout=timeout) as resp:
            sha = resp.read(100).decode("ascii", "replace").strip().lower()
    except urllib.error.HTTPError as exc:
        if exc.code in (404, 422):
            return None
        raise CheckError(f"GitHub answered {exc.code}", kind="http") from None
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as exc:
        raise CheckError(f"couldn't reach GitHub ({getattr(exc, 'reason', None) or exc})"[:300]) from None
    return sha if re.fullmatch(r"[0-9a-f]{40}", sha) else None


def _when(value: Any) -> float | None:
    """A stored time: a number of epoch seconds between 1970 and 3000, else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and 0 <= value <= MAX_TIME else None


def _clean(data: Any) -> dict[str, Any]:
    """The state file's fields, each of the type it should be (others are left out): a
    hand-edited or damaged file reads as if those fields were never written."""
    if not isinstance(data, dict):
        return {}
    out: dict[str, Any] = {}
    if isinstance(data.get("daily"), bool):
        out["daily"] = data["daily"]
    for key in ("attempted_at", "checked_at"):
        if _when(data.get(key)) is not None:
            out[key] = _when(data[key])
    latest = data.get("latest")
    if (isinstance(latest, dict) and isinstance(latest.get("version"), str)
            and isinstance(latest.get("tag"), str) and parse_version(latest["tag"])):
        out["latest"] = latest
    error = data.get("error")
    if isinstance(error, dict):
        out["error"] = {"message": str(error.get("message") or "no answer")[:300],
                        "kind": str(error.get("kind") or "http"),
                        "retry_at": _when(error.get("retry_at"))}
    base = data.get("base")
    if isinstance(base, dict) and isinstance(base.get("version"), str) \
            and isinstance(base.get("commit"), str):
        out["base"] = {"version": base["version"], "commit": base["commit"]}
    return out


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


class UpdateChecker:
    """Checks for a newer release, remembers the last answer in ``path`` (JSON), and,
    while the daily check is on, checks once a day on a thread of its own.

    The daily check is on or off by ``default_daily`` (QC_UPDATE_CHECK) until someone
    sets it (Settings ▸ Updates, ``update.settings``); from then on that setting wins.
    A failed check keeps the last good answer and says why it failed; nothing here
    raises for GitHub being unreachable or rate-limited.
    """

    def __init__(self, path: Path, build: Build, default_daily: bool = True, *,
                 web: bool = False, fetch: Callable[[str], dict[str, Any]] | None = None,
                 tag_commit: Callable[[str, str], str | None] | None = None,
                 now: Callable[[], float] = time.time, interval: float = DAY,
                 first_delay: float = FIRST_DELAY) -> None:
        self.path = path
        self.build = build
        self.default_daily = default_daily
        self.web = web
        self._fetch = fetch or (lambda version: fetch_latest(version, now=now))
        self._tag_commit = tag_commit or (lambda version, tag: fetch_tag_commit(version, tag))
        self._now = now
        self.interval = interval
        self.first_delay = first_delay
        self._lock = threading.Lock()  # guards the state file
        self._checking = threading.Lock()  # one check at a time
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started_at: float | None = None
        # The last attempt and rate-limit reset, also kept here: with a state file that
        # can't be written, the daily check still waits a day, not a second.
        self._attempted_at: float | None = None
        self._retry_at: float | None = None

    # --- state --------------------------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        try:
            return _clean(json.loads(self.path.read_text()))
        except (OSError, ValueError):
            return {}

    def _save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n")
        os.replace(tmp, self.path)

    def _update(self, strict: bool = False, **changes: Any) -> dict[str, Any]:
        """Write ``changes`` into the state file and return the whole state. A file that
        can't be written raises only when ``strict``: a check still answers."""
        with self._lock:
            data = self._load()
            data.update(changes)
            try:
                self._save(data)
            except OSError:
                if strict:
                    raise
            return data

    def daily(self, data: dict[str, Any] | None = None) -> bool:
        setting = (self._load() if data is None else data).get("daily")
        return setting if isinstance(setting, bool) else self.default_daily

    def next_check(self, data: dict[str, Any] | None = None) -> float | None:
        """When the daily check looks next (epoch seconds); None while it is off."""
        data = self._load() if data is None else data
        if not self.daily(data):
            return None
        attempts = [t for t in (data.get("attempted_at"), self._attempted_at) if t is not None]
        due = max(attempts) + self.interval if attempts else 0.0
        if self._started_at is not None:
            due = max(due, self._started_at + self.first_delay)
        return max([due, *self._resets(data)])

    def _resets(self, data: dict[str, Any]) -> list[float]:
        """The rate-limit resets still to wait for: none more than a day ahead."""
        until = self._now() + MAX_RETRY_WAIT
        return [t for t in ((data.get("error") or {}).get("retry_at"), self._retry_at)
                if t is not None and t <= until]

    def current(self, data: dict[str, Any] | None = None) -> dict[str, Any]:
        """This build (``Build.describe``), and a build on exactly its base release's
        commit (as a check found it) read as that release."""
        data = self._load() if data is None else data
        out = self.build.describe()
        base = data.get("base") if isinstance(data.get("base"), dict) else {}
        # A short QC_BUILD_COMMIT (git rev-parse --short) names the tag's full sha too.
        if (self.build.dev and base.get("version") == self.build.version
                and str(base.get("commit") or "").startswith(self.build.commit)):
            out.update(dev=False, label=self.build.version)
        return out

    def status(self, data: dict[str, Any] | None = None) -> dict[str, Any]:
        """What the last check found, this build, the daily setting and how to update."""
        data = self._load() if data is None else data
        latest = data.get("latest") if isinstance(data.get("latest"), dict) else None
        order = compare(latest["version"], self.build.version) if latest else None
        state = ("unknown" if order is None else "update_available" if order > 0
                 else "up_to_date" if order == 0 else "ahead")
        error = data.get("error") if isinstance(data.get("error"), dict) else None
        if error:
            error = {"message": error.get("message"), "kind": error.get("kind"),
                     "retry_at": _iso(error.get("retry_at"))}
        setting = data.get("daily")
        return {
            "current": self.current(data),
            "latest": latest,
            "state": state,
            "update_available": state == "update_available",
            "checked_at": _iso(data.get("checked_at")),  # the last check that got an answer
            "attempted_at": _iso(data.get("attempted_at")),
            "error": error,  # the last check failed (the answer above is older)
            "daily": self.daily(data),
            "daily_source": "setting" if isinstance(setting, bool) else "default",
            "next_check": _iso(self.next_check(data)),
            "how_to_update": how_to_update(self.build.install,
                                           latest["tag"] if latest else None, web=self.web,
                                           dev=self.build.dev),
        }

    def set_daily(self, on: bool) -> dict[str, Any]:
        return self.status(self._update(strict=True, daily=bool(on)))

    # --- checking -----------------------------------------------------------------------

    def check(self) -> dict[str, Any]:
        """Ask GitHub now and return the status. While a check is running another returns
        the last answer at once (no second request, no thread held waiting); while
        GitHub's rate limit is used up, nothing is asked until it resets."""
        if not self._checking.acquire(blocking=False):
            return self.status()
        try:
            now = self._now()
            retry_at = max(self._resets(self._load()), default=None)
            if retry_at is not None and now < retry_at:
                return self.status()
            self._attempted_at = now
            try:
                latest = self._fetch(self.build.version)
            except CheckError as exc:
                self._retry_at = _when(exc.retry_at)
                return self.status(self._update(attempted_at=now, error={
                    "message": str(exc), "kind": exc.kind, "retry_at": self._retry_at}))
            except Exception as exc:  # noqa: BLE001 -- a check must never take the app down
                return self.status(self._update(attempted_at=now, error={
                    "message": f"the check failed ({exc})"[:300], "kind": "http",
                    "retry_at": None}))
            changes: dict[str, Any] = {"attempted_at": now, "checked_at": now,
                                       "latest": latest, "error": None}
            self._retry_at = None
            base = self._load().get("base") or {}
            if self.build.dev and base.get("version") != self.build.version:
                # Tags don't move: which commit is this build's base release, once it is
                # known (a tag not pushed yet is asked about again at the next check).
                with contextlib.suppress(Exception):
                    sha = self._tag_commit(self.build.version, f"v{self.build.version}")
                    if sha:
                        changes["base"] = {"version": self.build.version, "commit": sha}
            return self.status(self._update(**changes))
        finally:
            self._checking.release()

    def due(self) -> bool:
        nxt = self.next_check()
        return nxt is not None and self._now() >= nxt

    def start(self) -> None:
        """Start the daily check's thread (it does nothing while the setting is off)."""
        if self._thread is not None:
            return
        self._started_at = self._now()
        self._thread = threading.Thread(target=self._loop, name="qc-update-check",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        wait = WAKE
        with contextlib.suppress(Exception):
            wait = self._wait()
        while not self._stop.wait(wait):
            wait = WAKE  # whatever goes wrong below, the next look is a while away
            with contextlib.suppress(Exception):
                if self.due():
                    self.check()
                wait = self._wait()

    def _wait(self) -> float:
        nxt = self.next_check()
        if nxt is None:
            return WAKE  # off: look again in a while, in case someone turns it on
        return min(WAKE, max(1.0, nxt - self._now()))


def cache_path() -> Path:
    """Where the CLI keeps its own last check (``update.status`` without a server)."""
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "quipclipper" / "update.json"
