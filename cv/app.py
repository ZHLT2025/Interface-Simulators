"""CV simulator — IF-CV-001 机器视觉检测事件推送.

The real CV system is the *caller* of this contract: it produces events and POSTs them to the
platform receiver. This simulator therefore has two parts:

  1. Producer (the simulated 中南大学机器视觉系统): /_sim/cv/emit, /_sim/cv/emit-raw, /_sim/cv/scenario/*
     build contract-conformant events (events.py) and push them (pusher.py) to CV_TARGET_URL.
  2. Verification sink (验证用接收端, NOT the platform): POST /api/v1/cv-events implements the
     receiver side of the contract (Basic Auth, 200/401/422 semantics, event_id idempotency) so the
     producer can be verified end-to-end without the platform, and so phase-3 payloads can be checked.

Authority: 接口定义说明书四、CV部分-1013.md.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from datetime import datetime, timezone

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, Response

from simcore.app_factory import SimContext
from simcore.auth import check_basic
from simcore.config import env, env_bool, env_float, env_int
from simcore.snowflake import Snowflake
from simcore.timeutil import parse_iso_utc_ms

from .events import EVENT_TYPES, build_event, render_evidence_jpeg, validate_event
from .pusher import Pusher

ctx = SimContext("cv", "CV simulator (IF-CV-001 producer + verification sink)")
app = ctx.app
S = ctx.settings

# producer configuration
TARGET_URL = env("CV_TARGET_URL", "http://127.0.0.1:8000/api/v1/cv-events")
TARGET_USER = env("CV_TARGET_BASIC_USER")
TARGET_PASS = env("CV_TARGET_BASIC_PASS")
EVIDENCE_URI_TEMPLATE = env("CV_EVIDENCE_URI_TEMPLATE", "oss://cv-evidence/{date}/{camera_id}/{event_id}/frame_001.jpg")
pusher = Pusher(
    target_url=TARGET_URL,
    username=TARGET_USER,
    password=TARGET_PASS,
    timeout_s=env_float("CV_HTTP_TIMEOUT_SECONDS", 10.0),
    max_retries=env_int("CV_MAX_RETRIES", 3),
    backoff_s=env_float("CV_RETRY_BACKOFF_SECONDS", 2.0),
    tls_verify=env_bool("CV_TLS_VERIFY", True),
    accept_legacy_ok=env_bool("CV_ACCEPT_LEGACY_STATUS_OK", False),  # 24.5-2 旧备注 {"status":"ok"} 待确认
)
snow = Snowflake(env_int("CV_SNOWFLAKE_WORKER_ID", 7))

# sink configuration (验证用接收端)
SINK_USER = env("CV_SINK_BASIC_USER")
SINK_PASS = env("CV_SINK_BASIC_PASS")
SINK_FAIL_MODE = {"mode": None}  # None | "500" — for testing the producer's retry path
sink_lock = threading.Lock()
sink_events: dict[str, dict] = {}  # event_id -> {hash, event, received_at, duplicates, conflicts}
sink_order: list[str] = []
evidence_store: dict[str, bytes] = {}
EVIDENCE_CAP = 200


def _admin_guard(request: Request):
    if S.admin_token is None:
        raise HTTPException(status_code=403, detail="admin endpoints disabled")
    if not hmac.compare_digest(request.headers.get("x-sim-admin-token", ""), S.admin_token):
        raise HTTPException(status_code=401, detail="invalid admin token")


def _catalogue() -> dict:
    return ctx.ds.load("catalogue")


def _roi_name(roi_id: str, fallback: str | None) -> str:
    for r in _catalogue().get("rois", []):
        if r.get("id") == roi_id:
            return r.get("area_name", roi_id)
    return fallback or roi_id


def _store_evidence(event_id: str, data: bytes):
    with sink_lock:
        evidence_store[event_id] = data
        while len(evidence_store) > EVIDENCE_CAP:
            evidence_store.pop(next(iter(evidence_store)))


def _auth_override(spec: dict):
    """Optional per-request credentials: spec.target_user / spec.target_pass (e.g. to exercise a receiver's 401 path)."""
    if spec.get("target_user") is not None:
        return (spec["target_user"], spec.get("target_pass") or "")
    return None


def make_event(spec: dict) -> dict:
    """spec: {event_type, camera_id, roi_id, roi_name?, timestamp?, targets?, count?, wall_state?, state_sequence?, operation?, event_id?,
             target_url?, target_user?, target_pass?}"""
    et = spec.get("event_type")
    if et not in EVENT_TYPES:
        raise HTTPException(status_code=400, detail=f"event_type must be one of {list(EVENT_TYPES)}")
    camera = spec.get("camera_id") or "CAM-20"
    roi_id = spec.get("roi_id") or "ROI_20_01_02"
    event_id = str(spec.get("event_id") or snow.next_str())
    ts = None
    if spec.get("timestamp"):
        try:
            ts = parse_iso_utc_ms(spec["timestamp"])
        except ValueError:
            raise HTTPException(status_code=400, detail="timestamp must be YYYY-MM-DDTHH:mm:ss.SSSZ")
    targets = spec.get("targets")
    if targets is None:
        targets = _catalogue().get("default_targets", {}).get(et, [])
        targets = json.loads(json.dumps(targets))
    now = ts or datetime.now(timezone.utc)
    image = render_evidence_jpeg(f"{et}\n{event_id}\n{camera} {roi_id}\n{now.isoformat()}")
    uri = EVIDENCE_URI_TEMPLATE.format(date=now.strftime("%Y/%m/%d"), camera_id=camera, event_id=event_id, public_base=S.public_base_url)
    ev = build_event(
        event_id=event_id,
        event_type=et,
        camera_id=camera,
        roi_id=roi_id,
        roi_name=spec.get("roi_name") or _roi_name(roi_id, None),
        timestamp=ts,
        targets=targets,
        count=spec.get("count"),
        wall_state=spec.get("wall_state"),
        state_sequence=spec.get("state_sequence"),
        evidence_uri=uri,
        image_bytes=image,
        operation=spec.get("operation", "创建"),
    )
    _store_evidence(event_id, image)
    errs = validate_event(ev)
    if errs:  # the producer must never emit a non-conformant message
        raise HTTPException(status_code=400, detail={"message": "built event failed contract validation", "errors": errs})
    return ev


# ======================================================================= producer admin API
@app.get("/_sim/cv/status")
def cv_status(request: Request):
    _admin_guard(request)
    return {"pusher": pusher.status(), "scenario": scenario.status(), "sink_events": len(sink_events)}


@app.post("/_sim/cv/emit")
def cv_emit(request: Request, spec: dict):
    """Build one contract-conformant event from a short spec and send it. Returns event + delivery record."""
    _admin_guard(request)
    ev = make_event(spec)
    result = pusher.send(ev, spec.get("target_url"), _auth_override(spec))
    return {"event": ev, "delivery": result}


@app.post("/_sim/cv/emit-raw")
def cv_emit_raw(request: Request, body: dict):
    """Send an arbitrary JSON body as-is (no validation) — for negative tests / phase-3 replay of recorded messages."""
    _admin_guard(request)
    payload = body.get("payload")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload (object) required")
    return {"delivery": pusher.send(payload, body.get("target_url"), _auth_override(body)), "validation": validate_event(payload)}


@app.post("/_sim/cv/build")
def cv_build(request: Request, spec: dict):
    """Build and validate an event without sending it."""
    _admin_guard(request)
    return make_event(spec)


@app.get("/_sim/cv/evidence/{event_id}/frame_001.jpg")
def cv_evidence(event_id: str):
    data = evidence_store.get(event_id)
    if data is None:
        raise HTTPException(status_code=404, detail="no evidence for this event_id")
    return Response(content=data, media_type="image/jpeg")


# ----------------------------------------------------------------------- scenario runner
class Scenario(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="cv-scenario")
        self.stop_event = threading.Event()
        self.steps: list[dict] = []
        self.loop = False
        self.pos = 0
        self.running = False
        self.results: list[dict] = []
        self.name = None

    def run(self):
        self.running = True
        started = time.monotonic()
        try:
            while not self.stop_event.is_set():
                if self.pos >= len(self.steps):
                    if not self.loop:
                        break
                    self.pos, started = 0, time.monotonic()
                step = self.steps[self.pos]
                delay = float(step.get("at", 0)) - (time.monotonic() - started)
                if delay > 0 and self.stop_event.wait(delay):
                    break
                try:
                    ev = make_event(step)
                    res = pusher.send(ev, step.get("target_url"), _auth_override(step))
                    self.results.append({"step": self.pos, "event_id": ev["event_id"], "outcome": res["outcome"]})
                except HTTPException as e:
                    self.results.append({"step": self.pos, "error": e.detail})
                self.pos += 1
        finally:
            self.running = False

    def status(self):
        return {"name": self.name, "running": self.running, "steps": len(self.steps), "position": self.pos, "loop": self.loop, "results": self.results[-20:]}


scenario = Scenario()


@app.post("/_sim/cv/scenario/run")
def scenario_run(request: Request, body: dict | None = None):
    """Run a scripted event sequence. body: {"name": "<key in scenarios.json>"} or {"steps": [...], "loop": false}."""
    global scenario
    _admin_guard(request)
    body = body or {}
    if scenario.running:
        raise HTTPException(status_code=409, detail="scenario already running; stop it first")
    if "steps" in body:
        steps, name = body["steps"], body.get("name", "inline")
    else:
        name = body.get("name", "default")
        scen = ctx.ds.load("scenarios", {"scenarios": {}}).get("scenarios", {}).get(name)
        if scen is None:
            raise HTTPException(status_code=404, detail=f"scenario {name!r} not found in dataset")
        steps = scen.get("steps", [])
    scenario = Scenario()
    scenario.steps, scenario.loop, scenario.name = steps, bool(body.get("loop", False)), name
    scenario.start()
    return {"started": True, "name": name, "steps": len(steps)}


@app.post("/_sim/cv/scenario/stop")
def scenario_stop(request: Request):
    _admin_guard(request)
    scenario.stop_event.set()
    return {"stopping": True, "status": scenario.status()}


@app.get("/_sim/cv/scenario/status")
def scenario_status(request: Request):
    _admin_guard(request)
    return scenario.status()


# ======================================================================= verification sink
def _sink_unauthorized(msg: str):
    return JSONResponse(status_code=401, content={"detail": msg}, headers={"WWW-Authenticate": 'Basic realm="CV sink"'})


@app.post("/api/v1/cv-events")
async def sink_receive(request: Request):
    """验证用接收端: implements the receiver side of IF-CV-001 (24.3.1 / 24.3.2)."""
    if not check_basic(request, SINK_USER, SINK_PASS):
        return _sink_unauthorized("authentication failed")
    if SINK_FAIL_MODE["mode"] == "500":
        return JSONResponse(status_code=500, content={"detail": "simulated receiver failure"})
    try:
        ev = await request.json()
    except Exception:
        return JSONResponse(status_code=422, content={"detail": [{"type": "json_invalid", "loc": ["body"], "msg": "JSON decode error"}]})
    errs = validate_event(ev)
    if errs:
        return JSONResponse(status_code=422, content={"detail": errs})
    digest = hashlib.sha256(json.dumps({k: v for k, v in ev.items() if k != "sent_at"}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    key = f"{ev['event_id']}:{ev['operation']}"
    with sink_lock:
        rec = sink_events.get(key)
        if rec is None:
            sink_events[key] = {"hash": digest, "event": ev, "received_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"), "duplicates": 0, "conflicts": 0}
            sink_order.append(key)
            if len(sink_order) > 2000:
                old = sink_order.pop(0)
                sink_events.pop(old, None)
        elif rec["hash"] == digest:
            rec["duplicates"] += 1  # same event_id + same content => idempotent, no new business data
        else:
            rec["conflicts"] += 1  # same event_id, different content => recorded as conflict (not silently overwritten)
    return {"returnStatus": "S", "returnCode": 200, "returnMsg": "successfully"}


@app.get("/_sim/cv/sink/events")
def sink_list(request: Request, n: int = 50):
    _admin_guard(request)
    with sink_lock:
        keys = sink_order[-n:]
        return [{"key": k, **{kk: vv for kk, vv in sink_events[k].items() if kk != "hash"}} for k in keys]


@app.post("/_sim/cv/sink/clear")
def sink_clear(request: Request):
    _admin_guard(request)
    with sink_lock:
        sink_events.clear()
        sink_order.clear()
    return {"cleared": True}


@app.post("/_sim/cv/sink/fail-mode")
def sink_fail_mode(request: Request, body: dict):
    """{"mode": "500"} makes the sink answer 500 (to exercise the producer's retry path); {"mode": null} restores."""
    _admin_guard(request)
    SINK_FAIL_MODE["mode"] = body.get("mode")
    return {"mode": SINK_FAIL_MODE["mode"]}


@app.get("/")
def index():
    return {
        "simulator": "CV",
        "role": "producer of IF-CV-001 events (机器视觉系统模拟) + verification sink",
        "producer_target": TARGET_URL,
        "producer_admin": "/_sim/cv/emit, /_sim/cv/emit-raw, /_sim/cv/build, /_sim/cv/scenario/{run,stop,status}, /_sim/cv/status",
        "verification_sink": "POST /api/v1/cv-events (HTTP Basic) — 验证用接收端，非平台正式接收服务",
        "event_types": list(EVENT_TYPES),
        "admin": "/_sim/health, /_sim/status, /_sim/requests, /_sim/reload, /_sim/dataset",
    }


ctx.mount_admin(extra_status=lambda: {"pusher": pusher.status(), "scenario": scenario.status(), "sink_events": len(sink_events)})
