"""IF-CV-001 sender: POSTs events to the platform receiver with Basic Auth and the retry semantics of 24.3.2.

  200 + returnStatus=S + returnCode=200  -> success, never resend
  401                                    -> record auth error, do NOT retry with same credentials
  422                                    -> record field errors, do NOT retry
  500 / no HTTP response                 -> keep event_id, retry with backoff

`send()` is the original ad-hoc/scenario path (truncating record, optional retries).

`send_once()` (IS-07) is what the manual single-round controller uses: exactly one attempt, never a
retry, and the *complete* actual response is kept — status code, response headers, the full body
(up to CV_RESPONSE_CAPTURE_BYTES, with an explicit truncation flag that makes the result
"incomplete" rather than silently shortened), elapsed time, the target identifier and the attempt
instant. Classification is deliberately NOT done here: simcore.control.outcome decides, so that a
bare status code can never be enough to continue a round.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from datetime import datetime, timezone

import httpx

from simcore.config import env_int
from simcore.timeutil import iso_utc_ms

log = logging.getLogger("cv.pusher")

#: full-body capture limit for send_once(); a longer body is flagged and treated as incomplete
RESPONSE_CAPTURE_BYTES = env_int("CV_RESPONSE_CAPTURE_BYTES", 65536)
#: response headers that are never written to the technical log
REDACT_HEADERS = ("set-cookie", "authorization", "proxy-authorization")


class Pusher:
    def __init__(self, *, target_url, username, password, timeout_s, max_retries, backoff_s, tls_verify, accept_legacy_ok):
        self.target_url = target_url
        self.username, self.password = username, password
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.backoff_s = backoff_s
        self.tls_verify = tls_verify
        self.accept_legacy_ok = accept_legacy_ok
        self.lock = threading.Lock()
        self.stats = {"sent": 0, "success": 0, "auth_failed": 0, "rejected_422": 0, "failed": 0, "retries": 0}
        self.recent = deque(maxlen=50)

    def _auth(self):
        return (self.username, self.password) if self.username else None

    def classify(self, status: int, text: str) -> str:
        if status == 200:
            try:
                body = json.loads(text)
            except Exception:
                return "ambiguous"
            if isinstance(body, dict) and body.get("returnStatus") == "S" and body.get("returnCode") == 200:
                return "success"
            if self.accept_legacy_ok and isinstance(body, dict) and body.get("status") == "ok":
                return "success_legacy"
            return "ambiguous"
        if status == 401:
            return "auth_failed"
        if status == 422:
            return "rejected_422"
        if status >= 500:
            return "server_error"
        return "unexpected"

    def send(self, event: dict, target_url: str | None = None, auth: tuple | None = None, stop_event=None) -> dict:
        """stop_event (threading.Event): when set, pending retries/backoff are abandoned (scenario stop)."""
        url = target_url or self.target_url
        auth = auth or self._auth()
        attempts = []
        outcome = "failed"
        t0 = time.monotonic()
        for attempt in range(1, self.max_retries + 2):
            # 24.2.1: sent_at = 报文发送时间, refreshed per attempt; event_id/content are reused (24.2.2.1-7)
            if isinstance(event, dict):
                event["sent_at"] = iso_utc_ms()
            rec = {"attempt": attempt, "at": event["sent_at"]}
            try:
                with httpx.Client(timeout=self.timeout_s, verify=self.tls_verify) as c:
                    r = c.post(url, json=event, auth=auth, headers={"Content-Type": "application/json"})
                rec.update({"status": r.status_code, "body": r.text[:1000]})
                kind = self.classify(r.status_code, r.text)
            except httpx.HTTPError as e:
                rec.update({"status": None, "error": f"{type(e).__name__}: {e}"})
                kind = "network_error"
            rec["result"] = kind
            attempts.append(rec)
            if kind in ("success", "success_legacy"):
                outcome = kind
                break
            if kind in ("auth_failed", "rejected_422", "ambiguous", "unexpected"):
                outcome = kind
                break
            if attempt <= self.max_retries:
                if stop_event is not None and stop_event.is_set():
                    outcome = "aborted"
                    break
                with self.lock:
                    self.stats["retries"] += 1
                if stop_event is not None:
                    if stop_event.wait(self.backoff_s * attempt):
                        outcome = "aborted"
                        break
                else:
                    time.sleep(self.backoff_s * attempt)
            else:
                outcome = kind
        with self.lock:
            self.stats["sent"] += 1
            key = {"success": "success", "success_legacy": "success", "auth_failed": "auth_failed", "rejected_422": "rejected_422"}.get(outcome, "failed")
            self.stats[key] += 1
        summary = {
            "event_id": event.get("event_id"),
            "event_type": event.get("event_type"),
            "event_definition": event.get("event_definition"),
            "target": url,
            "outcome": outcome,
            "attempts": attempts,
            "elapsed_ms": round((time.monotonic() - t0) * 1000, 1),
            "finished_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        }
        self.recent.append(summary)
        log.info(json.dumps({k: summary[k] for k in ("event_id", "event_type", "outcome", "elapsed_ms")}, ensure_ascii=False))
        return summary

    def send_once(self, event: dict, target_url: str | None = None, auth: tuple | None = None) -> dict:
        """One attempt, no retry, complete response capture. Never classifies, never resends."""
        url = target_url or self.target_url
        auth = auth if auth is not None else self._auth()
        if isinstance(event, dict):
            event["sent_at"] = iso_utc_ms()  # 24.2.1: 报文发送时间
        rec = {
            "target": url,
            "event_id": event.get("event_id") if isinstance(event, dict) else None,
            "event_type": event.get("event_type") if isinstance(event, dict) else None,
            "sent_at": event.get("sent_at") if isinstance(event, dict) else None,
            "attempt_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "attempts": 1,
            "max_retries_used": 0,
            "auth": bool(auth),
            "http_status": None,
            "response_reason": None,
            "response_headers": None,
            "response_body": None,
            "body_bytes": None,
            "body_truncated": False,
            "response_complete": False,
            "transport_error": None,
            "transport_kind": None,
        }
        t0 = time.monotonic()
        try:
            with httpx.Client(timeout=self.timeout_s, verify=self.tls_verify) as c:
                r = c.post(url, json=event, auth=auth, headers={"Content-Type": "application/json"})
            body = r.text
            raw = len(body.encode("utf-8", "replace"))
            truncated = raw > RESPONSE_CAPTURE_BYTES
            rec.update(
                http_status=r.status_code,
                response_reason=getattr(r, "reason_phrase", None),
                response_headers={k: ("***" if k.lower() in REDACT_HEADERS else v) for k, v in r.headers.items()},
                response_body=body if not truncated else body[:RESPONSE_CAPTURE_BYTES],
                body_bytes=raw,
                body_truncated=truncated,
                response_complete=not truncated,
            )
        except httpx.TimeoutException as e:
            rec.update(transport_error=f"{type(e).__name__}: {e}", transport_kind="timeout")
        except httpx.HTTPError as e:
            rec.update(transport_error=f"{type(e).__name__}: {e}", transport_kind="http_error")
        except Exception as e:  # socket/TLS/anything else: the real result is unknown
            rec.update(transport_error=f"{type(e).__name__}: {e}", transport_kind="other")
        rec["elapsed_ms"] = round((time.monotonic() - t0) * 1000, 1)
        with self.lock:
            self.stats["sent"] += 1
            self.stats.setdefault("single_attempt", 0)
            self.stats["single_attempt"] += 1
        self.recent.append({k: rec[k] for k in ("event_id", "event_type", "target", "http_status", "transport_error", "elapsed_ms", "attempt_at")})
        log.info(json.dumps({k: rec[k] for k in ("event_id", "event_type", "http_status", "transport_kind", "elapsed_ms")}, ensure_ascii=False))
        return rec

    def status(self) -> dict:
        with self.lock:
            return {"target_url": self.target_url, "auth": bool(self.username), "tls_verify": self.tls_verify, "max_retries": self.max_retries, "stats": dict(self.stats), "recent": list(self.recent)[-10:]}
