"""The narrow 已知类型拒绝 criteria and the UNKNOWN latch (V1.1 §4 / F09–F11).

Every assertion below is conjunctive on purpose: removing any one condition must turn the result
into UNKNOWN, never into "continue".
"""
import json

from simcore.control.outcome import (
    ACCEPTED,
    KNOWN_REJECTED,
    UNKNOWN,
    business_value_diff,
    classify,
    is_contract_enum_only_rejection,
)

TARGET = "http://127.0.0.1:9/api/v1/cv-events"
KNOWN_ID = "40426882302566085"
SOUND = "SOUNDPROOF_COVER_MOVE_COMPLETED"

STEP = {
    "event_id": KNOWN_ID,
    "event_type": SOUND,
    "timestamp": "2026-03-02T08:39:00.000Z",
    "camera_id": "CAM-09",
    "roi_id": "ROI_09_03_01",
    "operation": "创建",
}
EVENT = {
    "event_id": KNOWN_ID,
    "event_type": SOUND,
    "timestamp": "2026-03-02T08:39:00.000Z",
    "camera_id": "CAM-09",
    "roi": {"id": "ROI_09_03_01", "area_name": "加工02"},
    "operation": "创建",
}


def rec(**kw):
    base = {
        "target": TARGET,
        "http_status": 200,
        "response_body": json.dumps({"returnStatus": "S", "returnCode": 200, "returnMsg": "successfully"}),
        "response_complete": True,
        "body_truncated": False,
        "transport_error": None,
    }
    base.update(kw)
    return base


def cl(record, event=None, step=None, known=(KNOWN_ID,)):
    return classify(record, event=event or EVENT, step=step or STEP, known_event_ids=known, approved_target=TARGET)


# ------------------------------------------------------------------ accept
def test_accept_requires_the_full_contract_body():
    assert cl(rec())["outcome"] == ACCEPTED
    for body in ('{"returnStatus":"E","returnCode":500}', '{"status":"ok"}', "<html>ok</html>", "", "null"):
        out = cl(rec(response_body=body))
        assert out["outcome"] == UNKNOWN, body
        assert out["reason"] in ("AMBIGUOUS_200_BODY", "RESPONSE_BODY_UNPARSEABLE")


# ------------------------------------------------------------------ known rejection
ENUM_422 = json.dumps({"detail": [{"loc": ["body", "event_type"], "msg": "not an enumerated event_type"}]})


def test_known_rejection_passes_only_with_every_condition_met():
    out = cl(rec(http_status=422, response_body=ENUM_422))
    assert out["outcome"] == KNOWN_REJECTED
    assert out["reason"] == "EVENT_TYPE_ENUM_NOT_ACCEPTED"


def test_known_rejection_is_not_counted_as_accepted():
    assert KNOWN_REJECTED != ACCEPTED


def test_not_one_of_the_original_ten_is_unknown():
    other = dict(EVENT, event_id="40400000000000001")
    step = dict(STEP, event_id="40400000000000001")
    out = cl(rec(http_status=422, response_body=ENUM_422), event=other, step=step)
    assert (out["outcome"], out["reason"]) == (UNKNOWN, "EVENT_NOT_IN_KNOWN_TEN")


def test_another_event_type_is_unknown_even_with_an_enum_error():
    ev = dict(EVENT, event_type="ENTER")
    st = dict(STEP, event_type="ENTER")
    out = cl(rec(http_status=422, response_body=ENUM_422), event=ev, step=st)
    assert (out["outcome"], out["reason"]) == (UNKNOWN, "EVENT_TYPE_NOT_THE_KNOWN_TYPE")


def test_changed_business_values_are_unknown():
    moved = dict(EVENT, timestamp="2026-03-02T08:40:00.000Z")
    out = cl(rec(http_status=422, response_body=ENUM_422), event=moved)
    assert (out["outcome"], out["reason"]) == (UNKNOWN, "BUSINESS_VALUE_CHANGED")
    assert "timestamp" in out["detail"]["diff"]
    assert business_value_diff(dict(EVENT, camera_id="CAM-01"), STEP)["camera_id"]["plan"] == "CAM-09"
    assert business_value_diff({**EVENT, "roi": {"id": "ROI_OTHER"}}, STEP)["roi_id"]["sent"] == "ROI_OTHER"


def test_a_422_about_another_field_never_continues():
    body = json.dumps({"detail": [{"loc": ["body", "timestamp"], "msg": "bad"}]})
    out = cl(rec(http_status=422, response_body=body))
    assert (out["outcome"], out["reason"]) == (UNKNOWN, "REJECTION_NOT_ONLY_EVENT_TYPE_ENUM")


def test_a_422_mixing_event_type_with_another_field_never_continues():
    body = json.dumps({"detail": [{"loc": ["body", "event_type"], "msg": "x"}, {"loc": ["body", "roi", "id"], "msg": "y"}]})
    out = cl(rec(http_status=422, response_body=body))
    assert (out["outcome"], out["reason"]) == (UNKNOWN, "REJECTION_NOT_ONLY_EVENT_TYPE_ENUM")


def test_a_non_contract_422_body_never_continues():
    for body in [
        json.dumps({"detail": [{"loc": ["body", "event_type"]}], "message": "extra"}),
        json.dumps({"errors": [{"loc": ["body", "event_type"]}]}),
        json.dumps({"detail": "event_type invalid"}),
        json.dumps({"detail": []}),
        "not json at all",
    ]:
        out = cl(rec(http_status=422, response_body=body))
        assert out["outcome"] == UNKNOWN, body
        assert out["reason"] in ("NOT_CONTRACT_REJECTION_SHAPE", "RESPONSE_BODY_UNPARSEABLE")


def test_an_incomplete_response_never_continues():
    out = cl(rec(http_status=422, response_body=ENUM_422, response_complete=False, body_truncated=True))
    assert (out["outcome"], out["reason"]) == (UNKNOWN, "RESPONSE_INCOMPLETE")


def test_a_non_approved_target_never_continues():
    out = cl(rec(http_status=422, response_body=ENUM_422, target="http://127.0.0.1:9/elsewhere"))
    assert (out["outcome"], out["reason"]) == (UNKNOWN, "TARGET_NOT_APPROVED")


def test_status_code_alone_is_never_enough():
    """Same narrow body, but on 400 / 409 / 500 — the HTTP status is not interchangeable."""
    for status, reason in ((400, "UNEXPECTED_4XX"), (409, "UNEXPECTED_4XX"), (500, "SERVER_ERROR"), (503, "SERVER_ERROR")):
        out = cl(rec(http_status=status, response_body=ENUM_422))
        assert (out["outcome"], out["reason"]) == (UNKNOWN, reason), status


# ------------------------------------------------------------------ unknown
def test_auth_timeout_and_transport_failures_are_unknown():
    assert cl(rec(http_status=401, response_body="{}"))["reason"] == "AUTH_FAILED"
    assert cl(rec(http_status=403, response_body="{}"))["reason"] == "AUTH_FAILED"
    t = cl(rec(http_status=None, response_body=None, response_complete=False, transport_error="ReadTimeout: x", transport_kind="timeout"))
    assert (t["outcome"], t["reason"]) == (UNKNOWN, "TIMEOUT")
    n = cl(rec(http_status=None, response_body=None, response_complete=False, transport_error="ConnectError: x", transport_kind="http_error"))
    assert (n["outcome"], n["reason"]) == (UNKNOWN, "TRANSPORT_ERROR")


def test_contract_shape_helper():
    assert is_contract_enum_only_rejection(ENUM_422) == (True, None)
    assert is_contract_enum_only_rejection(None)[0] is False
    assert is_contract_enum_only_rejection('{"detail":[{"loc":["body","event_type"],"msg":"a"},{"loc":["body","event_type"],"msg":"b"}]}')[0] is True
