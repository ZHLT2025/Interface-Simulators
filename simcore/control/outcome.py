"""Classification of one actual technical result (IS-07, V1.1 §4, acceptance F09–F11).

Three outcomes only:

  ACCEPTED        the receiver's complete response says it accepted the message
                  (HTTP 200 + returnStatus=S + returnCode=200 per IF-CV-001 24.3.1).
                  This is a *technical* result; it is not a business test verdict.

  KNOWN_REJECTED  the narrow, conjunctive 已知类型拒绝 case. Every one of these must hold:
                    1. HTTP status is exactly 422;
                    2. a complete, untruncated, parseable response body;
                    3. the body is the formal contract shape {"detail": [ ... ]} and *nothing else*,
                       with every entry pointing at ["body", "event_type"] and no other field;
                    4. the event_id belongs to the 原 10 条 identity list derived from the plan;
                    5. the event still carries its original event_type / business values
                       (event_id, event_type, timestamp, camera_id, roi.id, operation);
                    6. the request went to the pre-configured approved target.
                  The round continues, the result is NOT counted as accepted.

  UNKNOWN         everything else: another 422, any other 4xx, 401, 5xx, a timeout, a dropped
                  connection, an unparseable or incomplete body, an ambiguous 200. The result is
                  latched, the round stops advancing and the control layer never blindly resends.

A bare HTTP status code is never sufficient on its own (V1.1 §4 末句).
"""
from __future__ import annotations

import json

from .config import SOUNDPROOF_EVENT_TYPE

ACCEPTED = "ACCEPTED"
KNOWN_REJECTED = "KNOWN_REJECTED"
UNKNOWN = "UNKNOWN"

# reason codes
R_RECEIVER_ACCEPTED = "RECEIVER_ACCEPTED"
R_ENUM_ONLY = "EVENT_TYPE_ENUM_NOT_ACCEPTED"
R_TRANSPORT = "TRANSPORT_ERROR"
R_TIMEOUT = "TIMEOUT"
R_AUTH = "AUTH_FAILED"
R_SERVER = "SERVER_ERROR"
R_OTHER_4XX = "UNEXPECTED_4XX"
R_OTHER_STATUS = "UNEXPECTED_STATUS"
R_AMBIGUOUS_200 = "AMBIGUOUS_200_BODY"
R_BODY_UNPARSEABLE = "RESPONSE_BODY_UNPARSEABLE"
R_BODY_INCOMPLETE = "RESPONSE_INCOMPLETE"
R_NOT_CONTRACT_SHAPE = "NOT_CONTRACT_REJECTION_SHAPE"
R_NOT_ENUM_ONLY = "REJECTION_NOT_ONLY_EVENT_TYPE_ENUM"
R_NOT_KNOWN_IDENTITY = "EVENT_NOT_IN_KNOWN_TEN"
R_NOT_KNOWN_TYPE = "EVENT_TYPE_NOT_THE_KNOWN_TYPE"
R_BUSINESS_CHANGED = "BUSINESS_VALUE_CHANGED"
R_TARGET_NOT_APPROVED = "TARGET_NOT_APPROVED"

BUSINESS_FIELDS = ("event_id", "event_type", "timestamp", "camera_id", "operation")


def business_identity(event: dict) -> dict:
    roi = event.get("roi") if isinstance(event.get("roi"), dict) else {}
    out = {k: event.get(k) for k in BUSINESS_FIELDS}
    out["roi_id"] = roi.get("id")
    return out


def plan_identity(step: dict) -> dict:
    return {
        "event_id": step.get("event_id"),
        "event_type": step.get("event_type"),
        "timestamp": step.get("timestamp"),
        "camera_id": step.get("camera_id"),
        "operation": step.get("operation", "创建"),
        "roi_id": step.get("roi_id"),
    }


def business_value_diff(event: dict, step: dict) -> dict:
    """Fields of the actually sent event that differ from the frozen plan original."""
    sent, orig = business_identity(event), plan_identity(step)
    return {k: {"plan": orig[k], "sent": sent[k]} for k in orig if orig[k] is not None and sent[k] != orig[k]}


def is_contract_enum_only_rejection(body_text: str | None) -> tuple[bool, str | None]:
    """True only for the formal contract rejection shape that refers to the event_type enum and
    nothing else. Returns (ok, reason_code_when_not_ok)."""
    if body_text is None:
        return False, R_BODY_INCOMPLETE
    try:
        body = json.loads(body_text)
    except Exception:
        return False, R_BODY_UNPARSEABLE
    if not isinstance(body, dict):
        return False, R_NOT_CONTRACT_SHAPE
    if set(body.keys()) != {"detail"}:
        return False, R_NOT_CONTRACT_SHAPE
    detail = body.get("detail")
    if not isinstance(detail, list) or not detail:
        return False, R_NOT_CONTRACT_SHAPE
    for item in detail:
        if not isinstance(item, dict):
            return False, R_NOT_CONTRACT_SHAPE
        loc = item.get("loc")
        if not isinstance(loc, list):
            return False, R_NOT_CONTRACT_SHAPE
        if [str(x) for x in loc] != ["body", "event_type"]:
            return False, R_NOT_ENUM_ONLY
    return True, None


def classify(record: dict, *, event: dict, step: dict, known_event_ids, approved_target: str) -> dict:
    """`record` is one complete attempt record produced by cv.pusher.Pusher.send_once()."""
    status = record.get("http_status")
    out = {
        "outcome": UNKNOWN,
        "reason": R_OTHER_STATUS,
        "http_status": status,
        "target": record.get("target"),
        "event_id": event.get("event_id"),
        "event_type": event.get("event_type"),
    }

    if record.get("target") != approved_target:
        # 获准目标 is a precondition of every continue-rule, including the known rejection
        out.update(outcome=UNKNOWN, reason=R_TARGET_NOT_APPROVED, detail={"approved": approved_target, "used": record.get("target")})
        return out
    if record.get("transport_error"):
        kind = R_TIMEOUT if record.get("transport_kind") == "timeout" else R_TRANSPORT
        out.update(outcome=UNKNOWN, reason=kind, detail={"error": record.get("transport_error")})
        return out
    if not record.get("response_complete", False):
        out.update(outcome=UNKNOWN, reason=R_BODY_INCOMPLETE, detail={"body_bytes": record.get("body_bytes"), "truncated": record.get("body_truncated")})
        return out

    body_text = record.get("response_body")
    if status == 200:
        try:
            body = json.loads(body_text)
        except Exception:
            out.update(outcome=UNKNOWN, reason=R_BODY_UNPARSEABLE)
            return out
        if isinstance(body, dict) and body.get("returnStatus") == "S" and body.get("returnCode") == 200:
            out.update(outcome=ACCEPTED, reason=R_RECEIVER_ACCEPTED)
            return out
        out.update(outcome=UNKNOWN, reason=R_AMBIGUOUS_200, detail={"body": body_text[:500] if body_text else None})
        return out
    if status == 401 or status == 403:
        out.update(outcome=UNKNOWN, reason=R_AUTH)
        return out
    if status is not None and 500 <= status <= 599:
        out.update(outcome=UNKNOWN, reason=R_SERVER)
        return out
    if status != 422:
        out.update(outcome=UNKNOWN, reason=R_OTHER_4XX if status is not None and 400 <= status <= 499 else R_OTHER_STATUS)
        return out

    # ---- status == 422: the only gate through which 已知类型拒绝 can pass
    if event.get("event_id") not in set(known_event_ids or ()):
        out.update(outcome=UNKNOWN, reason=R_NOT_KNOWN_IDENTITY)
        return out
    if event.get("event_type") != SOUNDPROOF_EVENT_TYPE:
        out.update(outcome=UNKNOWN, reason=R_NOT_KNOWN_TYPE)
        return out
    diff = business_value_diff(event, step)
    if diff:
        out.update(outcome=UNKNOWN, reason=R_BUSINESS_CHANGED, detail={"diff": diff})
        return out
    ok, why = is_contract_enum_only_rejection(body_text)
    if not ok:
        out.update(outcome=UNKNOWN, reason=why)
        return out
    out.update(outcome=KNOWN_REJECTED, reason=R_ENUM_ONLY)
    return out
