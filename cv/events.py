"""IF-CV-001 event model: builds and validates 机器视觉检测事件 payloads per 接口定义说明书四、CV部分-1013.md."""
from __future__ import annotations

import hashlib
import io
import re
from datetime import datetime, timezone

from simcore.timeutil import iso_utc_ms, parse_iso_utc_ms

SPEC_VERSION = "2.0"
ALGORITHM_VERSION = "1.0.0"
OPERATIONS = ("创建", "修订", "撤销")
EVENT_TYPES = (
    "ENTER",
    "LEAVE",
    "WALL_FLIP",
    "PERSON_COUNT",
    "SPARK",
    "TRANSFER_VEHICLE_EMPTY",
    "EMPTY_TRANSFER_VEHICLE_ENTER",
    "EMPTY_TRANSFER_VEHICLE_LEAVE",
    "LOADED_TRANSFER_VEHICLE_ENTER",
    "LOADED_TRANSFER_VEHICLE_LEAVE",
    "SIDEWALL_FINISHED_PRODUCT_ENTER_TRANSFER_VEHICLE",
    "PROFILE_LIFTED_FROM_TRANSFER_VEHICLE",
    "SOUNDPROOF_COVER_MOVE_COMPLETED",
)
WALL_STATES = ("horizontal", "vertical")
STATE_SEQ_VALUES = ("horizontal", "vertical", "none")
CAMERA_RE = re.compile(r"^CAM-\d{2}$")
EVENT_ID_RE = re.compile(r"^[0-9]{1,19}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ValidationError(Exception):
    """Carries a FastAPI-style detail list: [{type, loc, msg}]."""

    def __init__(self, errors: list[dict]):
        super().__init__("validation failed")
        self.errors = errors


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def validate_event(ev) -> list[dict]:
    """Return a list of errors (empty == valid). Mirrors the contract's field table (24.2.1 / 24.2.2)."""
    errs: list[dict] = []

    def missing(loc):
        errs.append({"type": "missing", "loc": ["body", *loc], "msg": "Field required"})

    def bad(loc, msg, typ="value_error"):
        errs.append({"type": typ, "loc": ["body", *loc], "msg": msg})

    if not isinstance(ev, dict):
        return [{"type": "dict_type", "loc": ["body"], "msg": "Input should be a valid dictionary"}]
    for k in ("spec_version", "algorithm_version", "event_id", "event_definition", "event_type", "operation", "timestamp", "sent_at", "camera_id"):
        if k not in ev:
            missing([k])
        elif not isinstance(ev[k], str):
            bad([k], "Input should be a valid string", "string_type")
    if isinstance(ev.get("spec_version"), str) and ev["spec_version"] != SPEC_VERSION:
        bad(["spec_version"], f"must be {SPEC_VERSION!r}")
    if isinstance(ev.get("algorithm_version"), str) and ev["algorithm_version"] != ALGORITHM_VERSION:
        bad(["algorithm_version"], f"must be {ALGORITHM_VERSION!r}")
    if isinstance(ev.get("event_id"), str) and not EVENT_ID_RE.match(ev["event_id"]):
        bad(["event_id"], "must be the decimal string of a 63-bit Snowflake ID")
    et = ev.get("event_type")
    if isinstance(et, str) and et not in EVENT_TYPES:
        bad(["event_type"], f"unknown event_type {et!r}")
    if isinstance(ev.get("operation"), str) and ev["operation"] not in OPERATIONS:
        bad(["operation"], "must be one of 创建/修订/撤销")
    for k in ("timestamp", "sent_at"):
        if isinstance(ev.get(k), str):
            try:
                parse_iso_utc_ms(ev[k])
            except ValueError:
                bad([k], "must be ISO 8601 UTC with milliseconds: YYYY-MM-DDTHH:mm:ss.SSSZ")
    if isinstance(ev.get("camera_id"), str) and not CAMERA_RE.match(ev["camera_id"]):
        bad(["camera_id"], "must match CAM-NN")
    roi = ev.get("roi")
    if roi is None:
        missing(["roi"])
    elif not isinstance(roi, dict):
        bad(["roi"], "Input should be a valid dictionary", "dict_type")
    else:
        for k in ("id", "area_name"):
            if not isinstance(roi.get(k), str) or not roi.get(k):
                missing(["roi", k]) if k not in roi else bad(["roi", k], "Input should be a non-empty string")
        if isinstance(et, str) and isinstance(roi.get("id"), str) and isinstance(ev.get("event_definition"), str):
            if ev["event_definition"] != f"{et}-{roi['id']}":
                bad(["event_definition"], "must equal {event_type}-{roi.id}")
    ed = ev.get("event_data")
    file_ids: set[str] = set()
    evd = ev.get("evidence")
    if evd is None:
        missing(["evidence"])
    elif not isinstance(evd, dict):
        bad(["evidence"], "Input should be a valid dictionary", "dict_type")
    else:
        if evd.get("type") != "SINGLE_IMAGE":
            bad(["evidence", "type"], "must be 'SINGLE_IMAGE'")
        files = evd.get("files")
        if not isinstance(files, list):
            missing(["evidence", "files"])
        else:
            if len(files) != 1:
                bad(["evidence", "files"], "single-image mode requires exactly 1 file")
            for i, f in enumerate(files):
                loc = ["evidence", "files", i]
                if not isinstance(f, dict):
                    bad(loc, "Input should be a valid dictionary", "dict_type")
                    continue
                for k in ("file_id", "uri", "media_type", "captured_at", "sha256"):
                    if not isinstance(f.get(k), str) or not f.get(k):
                        missing(loc + [k])
                if isinstance(f.get("file_id"), str):
                    if f["file_id"] in file_ids:
                        bad(loc + ["file_id"], "file_id must be unique within the message")
                    file_ids.add(f["file_id"])
                if isinstance(f.get("captured_at"), str):
                    try:
                        parse_iso_utc_ms(f["captured_at"])
                    except ValueError:
                        bad(loc + ["captured_at"], "must be ISO 8601 UTC with milliseconds")
                for k in ("width", "height"):
                    if not (isinstance(f.get(k), int) and not isinstance(f.get(k), bool) and f.get(k) > 0):
                        bad(loc + [k], "must be a positive integer", "int_parsing")
                if isinstance(f.get("sha256"), str) and not SHA256_RE.match(f["sha256"]):
                    bad(loc + ["sha256"], "must be 64 lowercase hex chars")
    if ed is None:
        missing(["event_data"])
    elif not isinstance(ed, dict):
        bad(["event_data"], "Input should be a valid dictionary", "dict_type")
    else:
        targets = ed.get("targets")
        if targets is None:
            missing(["event_data", "targets"])
        elif not isinstance(targets, list):
            bad(["event_data", "targets"], "Input should be a valid list", "list_type")
        else:
            for i, t in enumerate(targets):
                loc = ["event_data", "targets", i]
                if not isinstance(t, dict):
                    bad(loc, "Input should be a valid dictionary", "dict_type")
                    continue
                if not (isinstance(t.get("cid"), int) and not isinstance(t.get("cid"), bool)):
                    bad(loc + ["cid"], "must be an integer (Number)", "int_parsing")
                if not _num(t.get("confidence")):
                    bad(loc + ["confidence"], "must be a number")
                bbox, center, ref = t.get("bbox"), t.get("center"), t.get("evidence_ref")
                if not isinstance(bbox, dict) or not all(_num(bbox.get(k)) for k in ("x", "y", "w", "h")):
                    bad(loc + ["bbox"], "must contain numeric x, y, w, h")
                if not isinstance(center, dict) or not all(_num(center.get(k)) for k in ("x", "y")):
                    bad(loc + ["center"], "must contain numeric x, y")
                if not isinstance(ref, dict) or not isinstance(ref.get("file_id"), str):
                    bad(loc + ["evidence_ref", "file_id"], "required")
                elif file_ids and ref["file_id"] not in file_ids:
                    bad(loc + ["evidence_ref", "file_id"], "must reference evidence.files[].file_id")
        if et == "PERSON_COUNT":
            if not (isinstance(ed.get("count"), int) and not isinstance(ed.get("count"), bool)):
                missing(["event_data", "count"]) if "count" not in ed else bad(["event_data", "count"], "must be an integer")
            elif isinstance(targets, list) and ed["count"] != len(targets):
                bad(["event_data", "count"], "must equal the number of person targets")
        elif "count" in ed:
            bad(["event_data", "count"], "only allowed for PERSON_COUNT")
        if et == "WALL_FLIP":
            if ed.get("wall_state") not in WALL_STATES:
                missing(["event_data", "wall_state"]) if "wall_state" not in ed else bad(["event_data", "wall_state"], "must be horizontal|vertical")
            seq = ed.get("state_sequence")
            if not isinstance(seq, list) or not all(s in STATE_SEQ_VALUES for s in seq):
                missing(["event_data", "state_sequence"]) if "state_sequence" not in ed else bad(["event_data", "state_sequence"], "elements must be horizontal|vertical|none")
        else:
            for k in ("wall_state", "state_sequence"):
                if k in ed:
                    bad(["event_data", k], "only allowed for WALL_FLIP")
    return errs


# ---------------------------------------------------------------- evidence image
def render_evidence_jpeg(text: str, width: int = 1920, height: int = 1080) -> bytes:
    """Generate a real JPEG frame (so sha256 refers to actual bytes). Deterministic for a given text."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (width, height), (24, 28, 36))
    d = ImageDraw.Draw(img)
    d.rectangle([40, 40, width - 40, height - 40], outline=(90, 200, 255), width=6)
    y = 120
    for line in text.split("\n"):
        d.text((80, y), line, fill=(235, 235, 235))
        y += 40
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=70)
    return buf.getvalue()


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------- builder
def build_event(
    *,
    event_id: str,
    event_type: str,
    camera_id: str,
    roi_id: str,
    roi_name: str,
    timestamp: datetime | None,
    targets: list[dict] | None,
    count: int | None,
    wall_state: str | None,
    state_sequence: list[str] | None,
    evidence_uri: str,
    image_bytes: bytes,
    operation: str = "创建",
    width: int = 1920,
    height: int = 1080,
) -> dict:
    ts = timestamp or datetime.now(timezone.utc)
    ts_s = iso_utc_ms(ts)
    file_id = "frame-001"
    if targets is None:
        targets = []
    for t in targets:
        t.setdefault("evidence_ref", {"file_id": file_id})
    event_data: dict = {}
    if event_type == "PERSON_COUNT":
        event_data["count"] = len(targets) if count is None else count
    if event_type == "WALL_FLIP":
        event_data["wall_state"] = wall_state or "horizontal"
        event_data["state_sequence"] = state_sequence or ["horizontal", "vertical", "horizontal"]
    event_data["targets"] = targets
    return {
        "spec_version": SPEC_VERSION,
        "algorithm_version": ALGORITHM_VERSION,
        "event_id": event_id,
        "event_definition": f"{event_type}-{roi_id}",
        "event_type": event_type,
        "operation": operation,
        "timestamp": ts_s,
        "sent_at": iso_utc_ms(),
        "camera_id": camera_id,
        "roi": {"id": roi_id, "area_name": roi_name},
        "event_data": event_data,
        "evidence": {
            "type": "SINGLE_IMAGE",
            "files": [
                {
                    "file_id": file_id,
                    "uri": evidence_uri,
                    "media_type": "image/jpeg",
                    "captured_at": ts_s,
                    "width": width,
                    "height": height,
                    "sha256": sha256_hex(image_bytes),
                }
            ],
        },
    }
