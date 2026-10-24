"""Pairing (``quipclipper.pair/1``): an app such as LeadStinger asks for access, a person
approves it in the web UI or the CLI, and the app gets a client token of its own.

Nobody copies a secret. The app sends only a name and a host name::

    POST /api/pair {"name": "LeadStinger", "host": "laptop"}
      -> {"id", "code": "7F3K", "poll_secret", "expires_in": 600}

shows the code, and polls ``GET /api/pair/{id}`` with ``X-Pair-Secret``. The web UI shows
"LeadStinger on laptop wants access, code 7F3K. Allow / Deny"; on Allow the next poll
hands the app its token, once. A request carries no authority until a person approves
it, so a stranger can at most add a pending row: at most :data:`MAX_PENDING` at once, at
most :data:`RATE_PER_MINUTE` a minute from one address, each gone after
:data:`EXPIRES_IN` seconds.

Client tokens (``qcc_…``) are stored only as their SHA-256, in ``state_dir/clients.json``
(0600, written atomically), and revoked one at a time. Nothing here touches the network.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import tempfile
import threading
import time
import unicodedata
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"  # no 0/O, 1/I/L
CODE_LENGTH = 4
EXPIRES_IN = 600          # s a request waits for a person
MAX_PENDING = 5           # pending requests at once, from everyone
RATE_PER_MINUTE = 10      # new requests a minute from one client address
# A denied or expired request still answers its poller for this long, then it is gone.
# An approved one waits at least this long for its poll to collect the token.
LINGER = 60
TOKEN_PREFIX = "qcc_"
NAME_MAX = 60
HOST_MAX = 100
SERVER_NAME_MAX = 63
LAST_USED_EVERY = 60      # s: last_used is written at most this often per client

CLIENTS_FILE = "clients.json"
SERVER_ID_FILE = "server-id"
ADVERTISE_FILE = "advertise.json"

_HEX32 = re.compile(r"^[0-9a-f]{32}$")


def iso(t: float) -> str:
    """UTC ISO 8601 with ``Z``, to the second."""
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def token_sha256(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8", "surrogatepass")).hexdigest()


def is_client_token(value: str | None) -> bool:
    return bool(value) and value.startswith(TOKEN_PREFIX)


def check_label(value: str, what: str, lo: int, hi: int) -> str:
    """A name or host name as the web UI will show it: ``lo``..``hi`` characters and
    nothing a person could misread. Control characters, invisible format characters
    (bidi overrides, zero-width joiners) and line breaks are refused, so "LeadStinger on
    laptop" always reads as what it is. Raises ValueError."""
    if not isinstance(value, str):
        raise ValueError(f"{what} must be text")
    if not lo <= len(value) <= hi:
        raise ValueError(f"{what} must be {lo} to {hi} characters")
    if lo and not value.strip():
        raise ValueError(f"{what} must not be blank")
    if any(unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp") for ch in value):
        raise ValueError(f"{what} must not hold control or invisible characters")
    return value


# --- files in the state dir --------------------------------------------------------------


def write_atomic(path: Path, text: str, mode: int = 0o600) -> None:
    """Write ``text`` to ``path`` through a temporary file in the same folder, so a reader
    never sees half a file; the file has ``mode`` from the start."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def server_id(state_dir: Path) -> str:
    """This server's id: 32 hex characters made once and kept in ``state_dir/server-id``.

    The app and the advertiser (another process, sharing the state dir) may both start
    on an empty state dir; whichever links its file in first wins and the other reads it.
    An unreadable state dir gives an id for this run only."""
    path = Path(state_dir) / SERVER_ID_FILE
    for _ in range(3):
        try:
            found = path.read_text(encoding="ascii").strip()
        except FileNotFoundError:
            found = None
        except (OSError, UnicodeDecodeError):
            found = ""
        if found and _HEX32.match(found):
            return found
        new = uuid.uuid4().hex
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".server-id.", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="ascii") as fh:
                fh.write(new + "\n")
            try:
                if found is None:
                    os.link(tmp, path)        # fails if another process got there first
                else:
                    os.replace(tmp, path)     # a damaged file: replace it
                    tmp = None
            except FileExistsError:
                continue
            finally:
                if tmp is not None:
                    with contextlib.suppress(OSError):
                        os.unlink(tmp)
            return new
        except OSError:
            return new
    return new


def server_name(env_name: str | None, state_dir: Path | None = None) -> str:
    """The name LeadStinger shows for this server: ``QC_SERVER_NAME``; else the name the
    advertiser saw (``advertise.json``: in Docker it runs on the host's network, where the
    host name is the machine's, not the container's); else this host's name without a
    ``.local`` suffix. At most 63 characters."""
    for candidate in (env_name, _advertised(state_dir).get("name") if state_dir else None,
                      socket.gethostname()):
        name = (candidate or "").strip()
        if name.lower().endswith(".local"):
            name = name[:-6]
        name = "".join(ch for ch in name if unicodedata.category(ch)[0] != "C")
        if name:
            return name[:SERVER_NAME_MAX]
    return "quipclipper"


def _advertised(state_dir: Path | None) -> dict:
    if state_dir is None:
        return {}
    try:
        data = json.loads((Path(state_dir) / ADVERTISE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def advertised_url(state_dir: Path) -> str | None:
    """The URL the advertiser announces (``state_dir/advertise.json``), if it runs."""
    url = _advertised(state_dir).get("url")
    return url if isinstance(url, str) and url else None


# --- client tokens ----------------------------------------------------------------------


class ClientStore:
    """Paired apps, in ``state_dir/clients.json``: ``{"clients": [{id, name, host,
    token_sha256, created, last_used}]}``. The token itself is never stored; it is handed
    to the app once, by the poll that collects an approval."""

    def __init__(self, state_dir: Path, clock: Callable[[], float] = time.time):
        self.path = Path(state_dir) / CLIENTS_FILE
        self.clock = clock
        self._lock = threading.RLock()
        self._clients: list[dict] = []
        self._by_hash: dict[str, dict] = {}
        self._stamp: tuple[int, int] | None = None   # (mtime_ns, size) of what we read
        self._seen: dict[str, float] = {}            # id -> last time last_used was written

    # The file is the truth: another process (an admin editing it, a restore) may change
    # it, so a changed mtime or size reloads it before each use.
    def _load(self) -> None:
        try:
            st = self.path.stat()
        except FileNotFoundError:
            self._clients, self._by_hash, self._stamp = [], {}, None
            return
        except OSError:
            return
        stamp = (st.st_mtime_ns, st.st_size)
        if stamp == self._stamp:
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            rows = data.get("clients") if isinstance(data, dict) else None
        except (OSError, ValueError):
            rows = None
        if not isinstance(rows, list):
            rows = []   # an unreadable file: nobody is paired (fail closed)
        clean = [r for r in rows if isinstance(r, dict) and isinstance(r.get("id"), str)
                 and isinstance(r.get("token_sha256"), str)]
        self._clients = clean
        self._by_hash = {r["token_sha256"]: r for r in clean}
        self._stamp = stamp

    def _save(self) -> None:
        write_atomic(self.path, json.dumps({"clients": self._clients}, indent=2) + "\n")
        try:
            st = self.path.stat()
            self._stamp = (st.st_mtime_ns, st.st_size)
        except OSError:
            self._stamp = None

    @staticmethod
    def public(row: dict) -> dict:
        return {k: row.get(k) for k in ("id", "name", "host", "created", "last_used")}

    def add(self, name: str, host: str) -> tuple[dict, str]:
        """A new client and its token (the only copy of it)."""
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        row = {"id": secrets.token_urlsafe(9), "name": name, "host": host,
               "token_sha256": token_sha256(token), "created": iso(self.clock()),
               "last_used": None}
        with self._lock:
            self._load()
            self._clients.append(row)
            self._by_hash[row["token_sha256"]] = row
            self._save()
        return self.public(row), token

    def authenticate(self, token: str) -> dict | None:
        """The client ``token`` belongs to, or None. Notes its use (``last_used``, written
        at most once a minute)."""
        if not is_client_token(token):
            return None
        digest = token_sha256(token)
        with self._lock:
            self._load()
            row = self._by_hash.get(digest)
            # The dict lookup found it by hash; compare again in constant time anyway.
            if row is None or not hmac.compare_digest(row["token_sha256"], digest):
                return None
            now = self.clock()
            if now - self._seen.get(row["id"], float("-inf")) >= LAST_USED_EVERY:
                self._seen[row["id"]] = now
                row["last_used"] = iso(now)
                try:
                    self._save()
                except OSError:
                    pass   # a read-only state dir still authenticates
            return self.public(row)

    def list(self) -> list[dict]:
        with self._lock:
            self._load()
            return [self.public(r) for r in self._clients]

    def remove(self, client_id: str) -> bool:
        with self._lock:
            self._load()
            keep = [r for r in self._clients if r["id"] != client_id]
            if len(keep) == len(self._clients):
                return False
            self._clients = keep
            self._by_hash = {r["token_sha256"]: r for r in keep}
            self._seen.pop(client_id, None)
            self._save()
            return True


# --- pair requests --------------------------------------------------------------------------


class PairError(Exception):
    """A request the endpoint answers with ``status`` (429 for the caps)."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass
class _Request:
    id: str
    name: str
    host: str
    code: str
    address: str
    poll_secret: str
    created: float
    expires_at: float
    status: str = "pending"          # pending | approved | denied | expired
    settled_at: float | None = None
    token: str | None = field(default=None, repr=False)
    client_id: str | None = None

    def public(self) -> dict:
        return {"id": self.id, "name": self.name, "host": self.host, "code": self.code,
                "address": self.address, "created": iso(self.created),
                "expires_at": iso(self.expires_at)}


class Pairing:
    """Pending pair requests, in memory (a restart forgets them; the app asks again)."""

    def __init__(self, clients: ClientStore, clock: Callable[[], float] = time.time):
        self.clients = clients
        self.clock = clock
        self._lock = threading.Lock()
        self._requests: dict[str, _Request] = {}
        self._recent: dict[str, deque[float]] = {}   # address -> times of its new requests

    def _tidy(self, now: float) -> None:
        """Expire what has waited too long and forget what has lingered long enough. An
        approval nobody collected takes its client with it: no one holds that token."""
        for rid, r in list(self._requests.items()):
            if r.status == "pending" and now >= r.expires_at:
                r.status, r.settled_at = "expired", r.expires_at
            if r.status == "approved":
                if now >= max(r.expires_at, (r.settled_at or now) + LINGER):
                    del self._requests[rid]
                    if r.client_id:
                        self.clients.remove(r.client_id)
            elif r.status in ("denied", "expired") and now >= (r.settled_at or now) + LINGER:
                del self._requests[rid]
        for addr, times in list(self._recent.items()):
            while times and now - times[0] >= 60:
                times.popleft()
            if not times:
                del self._recent[addr]

    def _code(self) -> str:
        taken = {r.code for r in self._requests.values() if r.status == "pending"}
        while True:
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
            if code not in taken:
                return code

    def start(self, name: str, host: str, address: str) -> dict:
        """A new pending request. Raises PairError(429) past either cap."""
        now = self.clock()
        with self._lock:
            self._tidy(now)
            times = self._recent.setdefault(address, deque())
            if len(times) >= RATE_PER_MINUTE:
                raise PairError(429, "Too many pairing requests from this address; try "
                                     "again in a minute.")
            if sum(r.status == "pending" for r in self._requests.values()) >= MAX_PENDING:
                raise PairError(429, f"{MAX_PENDING} pairing requests are already waiting; "
                                     "approve or deny them in QuipClipper, or wait for them "
                                     "to expire.")
            times.append(now)
            r = _Request(id=secrets.token_urlsafe(16), name=name, host=host, code=self._code(),
                         address=address, poll_secret=secrets.token_urlsafe(32),
                         created=now, expires_at=now + EXPIRES_IN)
            self._requests[r.id] = r
            return {"id": r.id, "code": r.code, "poll_secret": r.poll_secret,
                    "expires_in": EXPIRES_IN}

    def poll(self, request_id: str, secret: str | None) -> dict | None:
        """What the app learns about its request, or None (unknown id, or a wrong or
        missing secret: the same answer). An approval's token is handed out once; the
        request is gone after that."""
        now = self.clock()
        with self._lock:
            self._tidy(now)
            r = self._requests.get(request_id)
            if r is None or not secret:
                return None
            try:
                ok = hmac.compare_digest(secret, r.poll_secret)
            except TypeError:   # a non-ASCII header value can never match
                ok = False
            if not ok:
                return None
            if r.status == "approved":
                del self._requests[r.id]
                return {"status": "approved", "token": r.token, "client_id": r.client_id}
            return {"status": r.status}

    def pending(self) -> list[dict]:
        now = self.clock()
        with self._lock:
            self._tidy(now)
            rows = [r for r in self._requests.values() if r.status == "pending"]
            return [r.public() for r in sorted(rows, key=lambda r: r.created)]

    def approve(self, request_id: str) -> dict | None:
        """Approve a pending request: its app becomes a client. None if it isn't pending."""
        now = self.clock()
        with self._lock:
            self._tidy(now)
            r = self._requests.get(request_id)
            if r is None or r.status != "pending":
                return None
            client, token = self.clients.add(r.name, r.host)
            r.status, r.settled_at, r.token, r.client_id = "approved", now, token, client["id"]
            return {"status": "approved", "client_id": client["id"]}

    def deny(self, request_id: str) -> dict | None:
        now = self.clock()
        with self._lock:
            self._tidy(now)
            r = self._requests.get(request_id)
            if r is None or r.status != "pending":
                return None
            r.status, r.settled_at = "denied", now
            return {"status": "denied"}
