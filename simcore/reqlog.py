from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from datetime import datetime, timezone

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

log = logging.getLogger("sim")


class RequestLog:
    """Bounded in-memory ring of recent requests, exposed under /_sim/requests as evidence."""

    def __init__(self, size: int):
        self.items: deque = deque(maxlen=size)
        self.lock = threading.Lock()
        self.counter = 0

    def add(self, item: dict):
        with self.lock:
            self.counter += 1
            item["seq"] = self.counter
            self.items.append(item)

    def recent(self, n: int = 50, path_prefix: str | None = None):
        with self.lock:
            data = list(self.items)
        if path_prefix:
            data = [d for d in data if d["path"].startswith(path_prefix)]
        return data[-n:]


MAX_BODY_CAPTURE = 4096


def _redact_headers(h) -> dict:
    out = {}
    for k, v in h.items():
        kl = k.lower()
        if kl == "authorization":
            out[k] = v.split(" ")[0] + " ***" if " " in v else "***"
        elif kl == "x-sim-admin-token":
            out[k] = "***"
        else:
            out[k] = v
    return out


class RequestLogMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, reqlog: RequestLog):
        super().__init__(app)
        self.reqlog = reqlog

    async def dispatch(self, request: Request, call_next):
        started = time.perf_counter()
        body = b""
        if not request.url.path.startswith("/_sim/"):
            body = await request.body()
        response = await call_next(request)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        if request.url.path.startswith("/_sim/"):
            return response
        # capture response body (contract endpoints only, small bodies)
        resp_body = b""
        async for chunk in response.body_iterator:  # type: ignore[attr-defined]
            resp_body += chunk
        from starlette.responses import Response

        new_response = Response(
            content=resp_body,
            status_code=response.status_code,
            headers=dict(response.headers),
            media_type=response.media_type,
        )
        entry = {
            "time": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "client": request.client.host if request.client else None,
            "method": request.method,
            "path": request.url.path,
            "query": str(request.url.query),
            "headers": _redact_headers(request.headers),
            "request_body": body[:MAX_BODY_CAPTURE].decode("utf-8", "replace"),
            "status": response.status_code,
            "response_body": resp_body[:MAX_BODY_CAPTURE].decode("utf-8", "replace"),
            "elapsed_ms": elapsed_ms,
        }
        self.reqlog.add(entry)
        log.info(json.dumps({k: entry[k] for k in ("time", "client", "method", "path", "status", "elapsed_ms")}, ensure_ascii=False))
        return new_response
