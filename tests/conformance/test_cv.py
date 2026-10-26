"""IF-CV-001 — expectations from 接口定义说明书四、CV部分-1013 §24.2-24.4.

Two directions are verified: (a) the simulated CV producer emits conformant messages and honours the
response semantics; (b) the verification sink answers like the contract's receiver (200/401/422, idempotency).
"""
import copy
import re
import time

import httpx

ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
ALL_TYPES = [
    "ENTER", "LEAVE", "WALL_FLIP", "PERSON_COUNT", "SPARK", "TRANSFER_VEHICLE_EMPTY", "EMPTY_TRANSFER_VEHICLE_ENTER",
    "EMPTY_TRANSFER_VEHICLE_LEAVE", "LOADED_TRANSFER_VEHICLE_ENTER", "LOADED_TRANSFER_VEHICLE_LEAVE",
    "SIDEWALL_FINISHED_PRODUCT_ENTER_TRANSFER_VEHICLE", "PROFILE_LIFTED_FROM_TRANSFER_VEHICLE", "SOUNDPROOF_COVER_MOVE_COMPLETED",
]


def A(creds):
    return {"X-Sim-Admin-Token": creds["admin"]}


def sample_event():
    """Request example 24.4.1 (verbatim field values)."""
    return {
        "spec_version": "2.0",
        "algorithm_version": "1.0.0",
        "event_id": "40444779484610560",
        "event_definition": "PERSON_COUNT-ROI_20_01_02",
        "event_type": "PERSON_COUNT",
        "operation": "创建",
        "timestamp": "2026-08-12T00:02:06.000Z",
        "sent_at": "2026-08-12T05:06:13.713Z",
        "camera_id": "CAM-20",
        "roi": {"id": "ROI_20_01_02", "area_name": "侧墙附件组焊台位04人数统计"},
        "event_data": {"count": 1, "targets": [{"cid": 0, "confidence": 0.9132, "bbox": {"x": 432.77, "y": 95.04, "w": 30.08, "h": 62.91}, "center": {"x": 447.81, "y": 126.50}, "evidence_ref": {"file_id": "frame-001"}}]},
        "evidence": {"type": "SINGLE_IMAGE", "files": [{"file_id": "frame-001", "uri": "oss://cv-evidence/2026/08/12/CAM-20/40444779484610560/frame_001.jpg", "media_type": "image/jpeg", "captured_at": "2026-08-12T00:02:06.000Z", "width": 1920, "height": 1080, "sha256": "28f93d7e1b6f0a4c9e832d75417cab680f23e9164a5d70bc2981ef364d52a7c0"}]},
    }


def check_common(ev):
    assert ev["spec_version"] == "2.0" and ev["algorithm_version"] == "1.0.0"
    assert re.match(r"^\d{1,19}$", ev["event_id"]) and int(ev["event_id"]) < 2**63
    assert ev["event_definition"] == f"{ev['event_type']}-{ev['roi']['id']}"
    assert ev["operation"] == "创建"
    assert ISO.match(ev["timestamp"]) and ISO.match(ev["sent_at"])
    assert re.match(r"^CAM-\d{2}$", ev["camera_id"])
    assert isinstance(ev["roi"]["id"], str) and isinstance(ev["roi"]["area_name"], str)
    assert isinstance(ev["event_data"]["targets"], list)
    f = ev["evidence"]["files"]
    assert ev["evidence"]["type"] == "SINGLE_IMAGE" and len(f) == 1
    assert f[0]["media_type"] == "image/jpeg" and f[0]["width"] > 0 and f[0]["height"] > 0 and re.match(r"^[0-9a-f]{64}$", f[0]["sha256"])
    for t in ev["event_data"]["targets"]:
        assert isinstance(t["cid"], int) and 0 <= t["confidence"] <= 1
        assert set(t["bbox"]) == {"x", "y", "w", "h"} and set(t["center"]) == {"x", "y"}
        assert t["evidence_ref"]["file_id"] == f[0]["file_id"]


# ---------------------------------------------------------------- sink (receiver semantics)
def test_sink_accepts_sample_and_is_idempotent(cv_url, creds):
    httpx.post(f"{cv_url}/_sim/cv/sink/clear", headers=A(creds))
    ev = sample_event()
    r = httpx.post(f"{cv_url}/api/v1/cv-events", json=ev, auth=creds["cv_sink"])
    assert r.status_code == 200 and r.json() == {"returnStatus": "S", "returnCode": 200, "returnMsg": "successfully"}  # 24.4.2
    r2 = httpx.post(f"{cv_url}/api/v1/cv-events", json=ev, auth=creds["cv_sink"])  # retry with same event_id
    assert r2.status_code == 200 and r2.json()["returnStatus"] == "S"
    rows = httpx.get(f"{cv_url}/_sim/cv/sink/events", headers=A(creds)).json()
    rec = [x for x in rows if x["key"].startswith(ev["event_id"])][0]
    assert rec["duplicates"] == 1 and rec["conflicts"] == 0  # 幂等：不产生重复业务数据


def test_sink_auth_and_422(cv_url, creds):
    r = httpx.post(f"{cv_url}/api/v1/cv-events", json=sample_event(), auth=("bad", "bad"))
    assert r.status_code == 401  # 24.3.2
    ev = sample_event()
    del ev["event_id"]
    r = httpx.post(f"{cv_url}/api/v1/cv-events", json=ev, auth=creds["cv_sink"])
    assert r.status_code == 422  # 24.3.2 / 24.4.3
    d = r.json()["detail"]
    assert any(x["loc"] == ["body", "event_id"] and x["type"] == "missing" and x["msg"] == "Field required" for x in d)
    bad = sample_event()
    bad["event_data"]["wall_state"] = "vertical"  # not allowed for PERSON_COUNT
    bad["camera_id"] = "CAM-1"
    r = httpx.post(f"{cv_url}/api/v1/cv-events", json=bad, auth=creds["cv_sink"])
    assert r.status_code == 422
    locs = [tuple(x["loc"]) for x in r.json()["detail"]]
    assert ("body", "event_data", "wall_state") in locs and ("body", "camera_id") in locs


def test_sink_wall_flip_sample(cv_url, creds):
    ev = sample_event()
    ev.update(event_id="40444779492999168", event_definition="WALL_FLIP-ROI_20_02_01", event_type="WALL_FLIP", roi={"id": "ROI_20_02_01", "area_name": "侧墙附件组焊台位03"})
    ev["event_data"] = {"wall_state": "horizontal", "state_sequence": ["horizontal", "vertical", "horizontal"], "targets": [{"cid": 1, "confidence": 0.2624, "bbox": {"x": 458.36, "y": 62.98, "w": 557.95, "h": 433.40}, "center": {"x": 737.33, "y": 279.68}, "evidence_ref": {"file_id": "frame-001"}}]}
    r = httpx.post(f"{cv_url}/api/v1/cv-events", json=ev, auth=creds["cv_sink"])
    assert r.status_code == 200 and r.json()["returnCode"] == 200


# ---------------------------------------------------------------- producer
def test_producer_emits_all_event_types(cv_url, creds):
    httpx.post(f"{cv_url}/_sim/cv/sink/clear", headers=A(creds))
    ids = set()
    for et in ALL_TYPES:
        spec = {"event_type": et, "camera_id": "CAM-20", "roi_id": "ROI_20_02_01"}
        if et in ("TRANSFER_VEHICLE_EMPTY", "EMPTY_TRANSFER_VEHICLE_ENTER"):
            spec["targets"] = []  # 没有可用目标详情时为空数组
        r = httpx.post(f"{cv_url}/_sim/cv/emit", json=spec, headers=A(creds), timeout=30)
        assert r.status_code == 200, r.text
        ev, dl = r.json()["event"], r.json()["delivery"]
        check_common(ev)
        assert ev["event_type"] == et
        ed = ev["event_data"]
        if et == "PERSON_COUNT":
            assert ed["count"] == len(ed["targets"]) and "wall_state" not in ed
        elif et == "WALL_FLIP":
            assert ed["wall_state"] in ("horizontal", "vertical") and all(s in ("horizontal", "vertical", "none") for s in ed["state_sequence"]) and "count" not in ed
        else:
            assert set(ed) == {"targets"}  # 不属于当前事件的字段不再出现
        assert dl["outcome"] == "success" and dl["attempts"][-1]["status"] == 200
        ids.add(ev["event_id"])
    assert len(ids) == len(ALL_TYPES)  # unique Snowflake ids
    rows = httpx.get(f"{cv_url}/_sim/cv/sink/events?n=100", headers=A(creds)).json()
    assert {r["event"]["event_type"] for r in rows} == set(ALL_TYPES)


def test_producer_evidence_sha256_matches_served_image(cv_url, creds):
    r = httpx.post(f"{cv_url}/_sim/cv/emit", json={"event_type": "SPARK", "camera_id": "CAM-21", "roi_id": "ROI_21_01_01"}, headers=A(creds), timeout=30)
    ev = r.json()["event"]
    img = httpx.get(f"{cv_url}/_sim/cv/evidence/{ev['event_id']}/frame_001.jpg")
    assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg" and img.content[:2] == b"\xff\xd8"
    import hashlib

    assert hashlib.sha256(img.content).hexdigest() == ev["evidence"]["files"][0]["sha256"]


def test_producer_retries_on_500_with_same_event_id(cv_url, creds):
    httpx.post(f"{cv_url}/_sim/cv/sink/fail-mode", json={"mode": "500"}, headers=A(creds))
    try:
        r = httpx.post(f"{cv_url}/_sim/cv/emit", json={"event_type": "ENTER", "camera_id": "CAM-20", "roi_id": "ROI_20_02_01"}, headers=A(creds), timeout=60)
    finally:
        httpx.post(f"{cv_url}/_sim/cv/sink/fail-mode", json={"mode": None}, headers=A(creds))
    dl = r.json()["delivery"]
    assert dl["outcome"] == "server_error" and len(dl["attempts"]) == 4  # 1 + CV_MAX_RETRIES(3)
    assert all(a["status"] == 500 for a in dl["attempts"])
    assert len({a["at"] for a in dl["attempts"]}) == 4  # sent_at refreshed per attempt, event_id reused


def test_producer_does_not_retry_401_or_422(cv_url, creds):
    r = httpx.post(f"{cv_url}/_sim/cv/emit-raw", json={"payload": {"spec_version": "2.0"}}, headers=A(creds), timeout=30)
    dl = r.json()["delivery"]
    assert dl["outcome"] == "rejected_422" and len(dl["attempts"]) == 1
    r = httpx.post(f"{cv_url}/_sim/cv/emit", json={"event_type": "LEAVE", "camera_id": "CAM-20", "roi_id": "ROI_20_02_01", "target_user": "wrong", "target_pass": "creds"}, headers=A(creds), timeout=30)
    dl = r.json()["delivery"]
    assert dl["outcome"] == "auth_failed" and len(dl["attempts"]) == 1


def test_producer_scenario_runs(cv_url, creds):
    httpx.post(f"{cv_url}/_sim/cv/sink/clear", headers=A(creds))
    r = httpx.post(f"{cv_url}/_sim/cv/scenario/run", json={"name": "default"}, headers=A(creds))
    assert r.status_code == 200 and r.json()["steps"] == 6
    deadline = time.time() + 40
    while time.time() < deadline:
        st = httpx.get(f"{cv_url}/_sim/cv/scenario/status", headers=A(creds)).json()
        if not st["running"]:
            break
        time.sleep(1)
    assert not st["running"] and len(st["results"]) == 6 and all(x.get("outcome") == "success" for x in st["results"])
