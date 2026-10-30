"""Local stand-in for the CV receiving target — used by the isolated control tests.

It is NOT the platform and it is NOT the project's own verification sink: it exists purely so the
manual single-round controller can be exercised against every response shape the frozen design has
to distinguish (accept / contract enum rejection / another 422 / 5xx / 401 / timeout / unparseable)
without a single request leaving the test container. It binds to 127.0.0.1 on an ephemeral port, so
the suite runs under `docker run --network none`.
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SOUNDPROOF = "SOUNDPROOF_COVER_MOVE_COMPLETED"

ACCEPT_BODY = {"returnStatus": "S", "returnCode": 200, "returnMsg": "successfully"}
CONTRACT_ENUM_422 = {"detail": [{"loc": ["body", "event_type"], "msg": "not an enumerated event_type"}]}
OTHER_422 = {"detail": [{"loc": ["body", "timestamp"], "msg": "invalid datetime format"}]}
MIXED_422 = {"detail": [{"loc": ["body", "event_type"], "msg": "not an enumerated event_type"}, {"loc": ["body", "roi", "id"], "msg": "unknown roi"}]}
EXTRA_KEY_422 = {"detail": [{"loc": ["body", "event_type"], "msg": "not an enumerated event_type"}], "message": "validation failed"}


class Replica:
    """Server state: `mode` picks the /api/v1/cv-events behaviour, `readyz_mode` picks /readyz."""

    def __init__(self, mode: str = "accept_all", readyz_mode: str = "ready", timeout_sleep: float = 2.0):
        self.mode = mode
        self.readyz_mode = readyz_mode
        self.timeout_sleep = timeout_sleep
        self.received: list[dict] = []
        self.readyz_calls = 0
        self.lock = threading.Lock()
        replica = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):  # keep the pytest output readable
                pass

            def _send(self, status: int, body, content_type="application/json"):
                raw = (json.dumps(body, ensure_ascii=False) if not isinstance(body, (str, bytes)) else body)
                if isinstance(raw, str):
                    raw = raw.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("X-Replica-Mode", replica.mode)
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                if self.path.rstrip("/") == "/readyz":
                    with replica.lock:
                        replica.readyz_calls += 1
                    m = replica.readyz_mode
                    if m == "down":
                        return self._send(503, {"detail": "unavailable"})
                    if m == "not_json":
                        return self._send(200, "ready", content_type="text/plain")
                    body = {"ready": True, "artifact": "yqyd-backend", "version": "0.6.0-test", "data_domain": "TEST_ISOLATED", "not_ready_because": []}
                    if m == "wrong_domain":
                        body["data_domain"] = "PRODUCTION"
                    elif m == "wrong_artifact":
                        body["artifact"] = "stm-backend"
                    elif m == "not_ready":
                        body["ready"] = False
                        body["not_ready_because"] = ["database"]
                    elif m == "nested_domain":
                        body["data_domain"] = {"expected": "TEST_ISOLATED", "observed": {"data_domain": "TEST_ISOLATED"}}
                    return self._send(200, body)
                return self._send(404, {"detail": "not found"})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    event = json.loads(raw)
                except Exception:
                    event = {"_unparseable": raw[:200].decode("utf-8", "replace")}
                with replica.lock:
                    replica.received.append(event)
                mode = replica.mode
                is_known = isinstance(event, dict) and event.get("event_type") == SOUNDPROOF
                if mode == "timeout":
                    time.sleep(replica.timeout_sleep)
                    return self._send(200, ACCEPT_BODY)
                if mode == "accept_all":
                    return self._send(200, ACCEPT_BODY)
                if mode == "enum_reject_known":
                    return self._send(422, CONTRACT_ENUM_422) if is_known else self._send(200, ACCEPT_BODY)
                if mode == "enum_reject_all":
                    return self._send(422, CONTRACT_ENUM_422)
                if mode == "other_422":
                    return self._send(422, OTHER_422)
                if mode == "mixed_422":
                    return self._send(422, MIXED_422)
                if mode == "mixed_422_known":
                    return self._send(422, MIXED_422) if is_known else self._send(200, ACCEPT_BODY)
                if mode == "extra_key_422":
                    return self._send(422, EXTRA_KEY_422)
                if mode == "extra_key_422_known":
                    return self._send(422, EXTRA_KEY_422) if is_known else self._send(200, ACCEPT_BODY)
                if mode == "other_422_known":
                    return self._send(422, OTHER_422) if is_known else self._send(200, ACCEPT_BODY)
                if mode == "error_500":
                    return self._send(500, {"detail": "simulated receiver failure"})
                if mode == "auth_401":
                    return self._send(401, {"detail": "authentication failed"})
                if mode == "bad_request_400":
                    return self._send(400, CONTRACT_ENUM_422)
                if mode == "html_200":
                    return self._send(200, "<html>ok</html>", content_type="text/html")
                if mode == "ambiguous_200":
                    return self._send(200, {"status": "ok"})
                return self._send(418, {"detail": f"unknown replica mode {mode!r}"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True, name="replica")
        self.thread.start()

    @property
    def port(self) -> int:
        return self.server.server_address[1]

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def events_url(self) -> str:
        return f"{self.base}/api/v1/cv-events"

    @property
    def readyz_url(self) -> str:
        return f"{self.base}/readyz"

    def reset(self):
        with self.lock:
            self.received.clear()
            self.readyz_calls = 0

    def close(self):
        self.server.shutdown()
        self.server.server_close()
