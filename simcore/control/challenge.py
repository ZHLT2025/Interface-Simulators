"""Two-phase confirmation gate (Owner: 所有改状态的人工操作都必须「确认 / 取消」).

    POST /_ctl/<op>/request   -> {challenge_id, expires_at, summary}
    POST /_ctl/<op>/confirm   -> the operation is performed
    POST /_ctl/<op>/cancel    -> the challenge is discarded, nothing changes

A challenge is
  * single-use      — the second confirm is refused (409 E_CHALLENGE_USED);
  * short-lived     — 60 s by default (409 E_CHALLENGE_EXPIRED);
  * op-bound        — confirming op X with a challenge for op Y is refused;
  * state-bound     — it carries the state fingerprint captured when it was issued, so a
                      challenge created before the round moved on can never be replayed to
                      pass a B stop point (409 E_CHALLENGE_STALE). This closes the
                      「旧、重复或错阶段确认不能放行下一停点」 requirement at the gate level, in
                      addition to the stop-point identity check in the operation itself.
Cancelling or letting a challenge expire changes nothing.
"""
from __future__ import annotations

import secrets
import threading
import time

E_CHALLENGE_UNKNOWN = "E_CHALLENGE_UNKNOWN"
E_CHALLENGE_EXPIRED = "E_CHALLENGE_EXPIRED"
E_CHALLENGE_USED = "E_CHALLENGE_USED"
E_CHALLENGE_OP_MISMATCH = "E_CHALLENGE_OP_MISMATCH"
E_CHALLENGE_STALE = "E_CHALLENGE_STALE"


class ChallengeError(Exception):
    def __init__(self, code: str, detail: dict | None = None):
        super().__init__(code)
        self.code = code
        self.detail = detail or {}


class Challenge:
    __slots__ = ("id", "op", "params", "fingerprint", "created", "expires", "used", "summary")

    def __init__(self, op: str, params: dict, fingerprint: str, ttl: float, summary: dict):
        self.id = "chl_" + secrets.token_urlsafe(18)
        self.op = op
        self.params = params
        self.fingerprint = fingerprint
        self.created = time.time()
        self.expires = self.created + ttl
        self.used = False
        self.summary = summary


class ChallengeStore:
    def __init__(self, ttl: float, now=time.time):
        self.ttl = ttl
        self._now = now
        self._items: dict[str, Challenge] = {}
        self._lock = threading.Lock()

    def issue(self, op: str, params: dict, fingerprint: str, summary: dict) -> Challenge:
        ch = Challenge(op, params, fingerprint, self.ttl, summary)
        ch.created = self._now()
        ch.expires = ch.created + self.ttl
        with self._lock:
            self._purge_locked()
            self._items[ch.id] = ch
        return ch

    def take(self, challenge_id: str, op: str, fingerprint: str) -> Challenge:
        """Validate and consume. Raises ChallengeError; the challenge is burned on success only."""
        with self._lock:
            ch = self._items.get(challenge_id or "")
            if ch is None:
                raise ChallengeError(E_CHALLENGE_UNKNOWN)
            if ch.op != op:
                raise ChallengeError(E_CHALLENGE_OP_MISMATCH, {"issued_for": ch.op, "confirmed_as": op})
            if ch.used:
                raise ChallengeError(E_CHALLENGE_USED, {"op": ch.op})
            if self._now() > ch.expires:
                ch.used = True
                raise ChallengeError(E_CHALLENGE_EXPIRED, {"op": ch.op, "expired_at": ch.expires})
            if ch.fingerprint != fingerprint:
                raise ChallengeError(E_CHALLENGE_STALE, {"issued_for_state": ch.fingerprint, "current_state": fingerprint})
            ch.used = True
            return ch

    def cancel(self, challenge_id: str, op: str | None = None) -> Challenge:
        with self._lock:
            ch = self._items.pop(challenge_id or "", None)
            if ch is None:
                raise ChallengeError(E_CHALLENGE_UNKNOWN)
            if op is not None and ch.op != op:
                self._items[ch.id] = ch
                raise ChallengeError(E_CHALLENGE_OP_MISMATCH, {"issued_for": ch.op, "cancelled_as": op})
            return ch

    def pending(self) -> list[dict]:
        now = self._now()
        with self._lock:
            return [
                {"challenge_id": c.id, "op": c.op, "expires_in_s": round(c.expires - now, 1), "used": c.used}
                for c in self._items.values()
                if not c.used and c.expires > now
            ]

    def _purge_locked(self):
        now = self._now()
        for k in [k for k, v in self._items.items() if v.expires < now - 600]:
            self._items.pop(k, None)
