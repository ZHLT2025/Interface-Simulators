"""IF-CV-001 sender: POSTs events to the platform receiver with Basic Auth and the retry semantics of 24.3.2.

  200 + returnStatus=S + returnCode=200  -> success, never resend
  401                                    -> record auth error, do NOT retry with same credentials
  422                                    -> record field errors, do NOT retry
  500 / no HTTP response                 -> keep event_id, retry with backoff
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from datetime import datetime, timezone

import httpx

from simcore.timeutil import iso_utc_ms

log = logging.getLogger("cv.pusher")


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

    def send(self, event: dict, target_url: str | None = None, auth: tuple | None = None) -> dict:
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
                with self.lock:
                    self.stats["retries"] += 1
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

    def status(self) -> dict:
        with self.lock:
            return {"target_url": self.target_url, "auth": bool(self.username), "tls_verify": self.tls_verify, "max_retries": self.max_retries, "stats": dict(self.stats), "recent": list(self.recent)[-10:]}
