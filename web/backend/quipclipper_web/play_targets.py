"""Play targets (M27): a paired app that plays lines it is sent, such as a TV remote app
that opens them in VLC.

A target is a paired client with the ``play.receive`` scope (or full access). It long-polls
``GET /api/play/receive``; the poll is its registration, so it is listed while a poll is
open and for :data:`LISTEN_GRACE` seconds after one ends. A sender (``play.send``, a
full-access token, the web UI or the CLI) lists the targets and sends one a line: the
request waits here until the target's poll collects it, at most :data:`DELIVER_WAIT`
seconds, and is then dropped, never delivered late. The target reports what it did
(``POST /api/play/receive/{id}``) and the sender is told, if within :data:`RESULT_WAIT`.

A target learns only ``{id, path, at, subtitles, track, from}``: no link and no token. It
fetches its own signed link with its own ``media.link`` scope, so a sender can never make
it open anything its own token couldn't.

Everything is in memory: a restart forgets the targets, and their next poll lists them
again. The registry has no threads of its own. Its methods are called from the event loop
(the play routes are async, so a waiting poll holds no threadpool worker), except
:meth:`PlayTargets.forget`, which the sync revoke route calls from a worker thread. The
clock is injectable, and every wait takes its timeout as a parameter, so tests never sleep.
"""

from __future__ import annotations

import asyncio
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

WAIT_MAX = 25.0        # s: the longest a poll waits (well under the proxy's read timeout)
LISTEN_GRACE = 45.0    # s: a target is listed this long after its last poll ended
DELIVER_WAIT = 10.0    # s: a send waits this long for the target's poll to collect it
RESULT_WAIT = 10.0     # s: then this long for the target to say what happened
REQUEST_TTL = 15.0     # s: a queued request is never handed out after this (a backstop)
SEND_RATE = 20         # sends a minute from one sender
PLAYER_MAX = 20        # characters in a target's player label ("VLC")
DETAIL_MAX = 300       # characters in a target's report
SENT_KEEP = 8          # delivered requests per target a report is still taken for


class PlayError(Exception):
    """A send the route answers with ``status``."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


class Revoked(Exception):
    """The polling client was revoked while it waited."""


@dataclass
class _Request:
    id: str
    target: str
    play: dict                       # {path, at, subtitles, track, from}
    expires_at: float
    delivered: asyncio.Future = field(repr=False)
    result: asyncio.Future = field(repr=False)

    def public(self) -> dict:
        return {"id": self.id, **self.play}


@dataclass
class _Target:
    id: str
    name: str
    host: str
    player: str
    last_seen: float
    waiter: asyncio.Future | None = field(default=None, repr=False)   # the open poll's
    pending: _Request | None = None
    sent: dict[str, _Request] = field(default_factory=dict)   # delivered, awaiting a result

    def public(self) -> dict:
        return {"id": self.id, "name": self.name, "host": self.host, "player": self.player,
                "listening": True}


class PlayTargets:
    """The play targets and their pending requests, in memory."""

    def __init__(self, clock: Callable[[], float] = time.monotonic, *,
                 deliver_wait: float = DELIVER_WAIT, result_wait: float = RESULT_WAIT):
        self.clock = clock
        self.deliver_wait = deliver_wait
        self.result_wait = result_wait
        self._targets: dict[str, _Target] = {}
        self._recent: dict[str, deque[float]] = {}   # sender -> times of its sends
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()   # forget() comes from a worker thread

    def _listening(self, t: _Target, now: float) -> bool:
        return t.waiter is not None or now - t.last_seen < LISTEN_GRACE

    def _tidy(self, now: float) -> None:
        for tid, t in list(self._targets.items()):
            if not self._listening(t, now):
                self._drop(t, "isn't listening")
                del self._targets[tid]

    @staticmethod
    def _drop(t: _Target, why: str) -> None:
        """Fail what a target still holds: a queued request, and results still awaited."""
        if t.pending is not None and not t.pending.delivered.done():
            t.pending.delivered.set_exception(PlayError(409, f"{t.name} {why}."))
        t.pending = None
        for r in t.sent.values():
            if not r.result.done():
                r.result.set_result(None)
        t.sent.clear()

    # --- the target's side -------------------------------------------------------------

    async def receive(self, client: dict, player: str, wait: float) -> dict | None:
        """The target's poll: registers ``client`` as a target and answers the request
        waiting for it, now or within ``wait`` seconds; None when none came. A newer poll
        from the same client ends this one (with None). Raises :class:`Revoked` when the
        client is revoked meanwhile."""
        self._loop = asyncio.get_running_loop()
        wait = max(0.0, min(float(wait), WAIT_MAX))
        now = self.clock()
        with self._lock:
            self._tidy(now)
            t = self._targets.get(client["id"])
            if t is None:
                t = self._targets[client["id"]] = _Target(client["id"], client.get("name") or "",
                                                          client.get("host") or "", player, now)
            t.name, t.host, t.player, t.last_seen = (client.get("name") or "",
                                                     client.get("host") or "", player, now)
            if t.waiter is not None and not t.waiter.done():
                t.waiter.set_result(None)   # an older poll of this client ends empty
            got = self._take(t, now)
            if got is not None or wait <= 0:
                return got
            waiter = t.waiter = self._loop.create_future()
        try:
            await asyncio.wait_for(asyncio.shield(waiter), wait)
        except asyncio.TimeoutError:
            pass
        finally:
            with self._lock:
                if t.waiter is waiter:
                    t.waiter = None
                t.last_seen = self.clock()
        with self._lock:
            # A newer poll of this client is open: the request is that one's to take.
            if self._targets.get(t.id) is not t or t.waiter is not None:
                return None
            return self._take(t, self.clock())

    def _take(self, t: _Target, now: float) -> dict | None:
        """Hand the target its queued request, if it hasn't expired."""
        r = t.pending
        if r is None:
            return None
        t.pending = None
        if now >= r.expires_at or r.delivered.done():
            if not r.delivered.done():
                r.delivered.set_exception(PlayError(409, f"{t.name} isn't listening."))
            return None
        r.delivered.set_result(True)
        t.sent[r.id] = r
        while len(t.sent) > SENT_KEEP:   # reports nobody waits for any more
            t.sent.pop(next(iter(t.sent)))
        return r.public()

    def report(self, client_id: str, request_id: str, ok: bool, detail: str) -> bool:
        """The target says what it did with a request it collected. False when it holds no
        such request (unknown, another client's, or reported already)."""
        with self._lock:
            t = self._targets.get(client_id)
            r = t.sent.pop(request_id, None) if t is not None else None
            if r is None:
                return False
            if not r.result.done():
                r.result.set_result({"ok": bool(ok), "detail": detail})
            return True

    def forget(self, client_id: str) -> None:
        """The client was revoked: its poll ends (401) and it is no longer a target. Safe to
        call from any thread."""
        loop = self._loop
        if loop is None:
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._forget(client_id)
        else:
            loop.call_soon_threadsafe(self._forget, client_id)

    def _forget(self, client_id: str) -> None:
        with self._lock:
            t = self._targets.pop(client_id, None)
            if t is None:
                return
            if t.waiter is not None and not t.waiter.done():
                t.waiter.set_exception(Revoked())
            self._drop(t, "was disconnected")

    # --- the sender's side -------------------------------------------------------------

    def targets(self, alive: Callable[[str], bool]) -> list[dict]:
        """The listening targets whose client is still paired (``alive``), by name."""
        now = self.clock()
        with self._lock:
            self._tidy(now)
            found = [t for t in self._targets.values() if alive(t.id)]
            return [t.public() for t in sorted(found, key=lambda t: (t.name.lower(),
                                                                       t.host.lower(), t.id))]

    async def send(self, target_id: str, play: dict, sender: str,
                   alive: Callable[[str], bool]) -> dict:
        """Queue ``play`` for a target, wait for its poll to collect it and for its report.
        ``{"target", "delivered": True, "result": {"ok", "detail"} | None}``. Raises
        PlayError: 404 unknown target, 409 not collected in time (it is then dropped) or
        replaced by a newer send, 429 too many sends from ``sender``."""
        self._loop = asyncio.get_running_loop()
        now = self.clock()
        with self._lock:
            self._tidy(now)
            t = self._targets.get(target_id)
            if t is None or not alive(target_id):
                raise PlayError(404, "No such play target (it may have stopped listening).")
            times = self._recent.setdefault(sender, deque())
            while times and now - times[0] >= 60:
                times.popleft()
            if len(times) >= SEND_RATE:
                raise PlayError(429, "Too many play requests; try again in a minute.")
            times.append(now)
            if t.pending is not None and not t.pending.delivered.done():
                t.pending.delivered.set_exception(
                    PlayError(409, f"A newer play request replaced this one on {t.name}."))
            r = _Request(id=secrets.token_urlsafe(9), target=t.id, play=play,
                         expires_at=now + REQUEST_TTL,
                         delivered=self._loop.create_future(),
                         result=self._loop.create_future())
            t.pending = r
            if t.waiter is not None and not t.waiter.done():
                t.waiter.set_result(None)   # wake the open poll: it takes the request
            label = t.public()
        try:
            await asyncio.wait_for(asyncio.shield(r.delivered), self.deliver_wait)
        except asyncio.TimeoutError:
            with self._lock:
                if t.pending is r:
                    t.pending = None
            raise PlayError(409, f"{t.name} on {t.host} isn't listening.")
        except BaseException:
            with self._lock:   # the sender went away (cancelled): never deliver it late
                if t.pending is r:
                    t.pending = None
            raise
        try:
            result = await asyncio.wait_for(asyncio.shield(r.result), self.result_wait)
        except asyncio.TimeoutError:
            result = None
        return {"target": label, "delivered": True, "result": result}
