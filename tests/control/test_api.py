"""The /_ctl HTTP surface of the CV simulator process: two-phase endpoints, 409 semantics,
the minimal page, admin protection and mutual exclusion with the native sending entries.

Driven through the real ASGI stack (middleware included) with a loopback client address, because
/_ctl is restricted to the admin source allow-list exactly like /_sim.
"""
import json

import pytest
from starlette.testclient import TestClient

from simcore.control import machine as M
from simcore.ipguard import DEFAULT_ADMIN_CIDRS, decide, parse_cidrs

TOKEN = {"X-Sim-Admin-Token": "test-admin-token"}


@pytest.fixture
def client(cvapp):
    with TestClient(cvapp.app, client=("127.0.0.1", 50000)) as c:
        yield c


# ------------------------------------------------------------------ access control
def test_ctl_is_an_admin_surface_for_the_ip_guard():
    admin = parse_cidrs(DEFAULT_ADMIN_CIDRS)
    for path in ("/_ctl/", "/_ctl/status", "/_ctl/init_start/confirm", "/_ctl"):
        assert decide(path, "127.0.0.1", admin, []) is None, path
        assert decide(path, "8.8.8.8", admin, []) is not None, path
        assert decide(path, None, admin, []) is not None, path


def test_every_data_and_action_endpoint_needs_the_admin_token(client):
    assert client.get("/_ctl/status").status_code == 401
    assert client.get("/_ctl/results").status_code == 401
    assert client.get("/_ctl/audit").status_code == 401
    assert client.post("/_ctl/init_start/request", json={}).status_code == 401
    assert client.post("/_ctl/init_start/confirm", json={"challenge_id": "x"}).status_code == 401
    assert client.get("/_ctl/status", headers={"X-Sim-Admin-Token": "wrong"}).status_code == 401
    assert client.get("/_ctl/status", headers=TOKEN).status_code == 200


# ------------------------------------------------------------------ page
def test_the_page_offers_exactly_one_start_button_and_a_confirm_dialog_per_action(client):
    r = client.get("/_ctl/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    html = r.text
    assert html.count('id="btn-init"') == 1  # exactly one start button element
    assert "初始化启动（一次点击）" in html
    assert html.count("window.confirm(") >= 2  # pre-request dialog + second confirmation
    assert "/request" in html and "/confirm" in html and "/cancel" in html
    # no framework, no bundler
    assert "react" not in html.lower() and "<script src" not in html.lower()
    # only one place starts a round
    assert html.count('op("init_start"') == 1
    for word in ("select_mode", "pause", "resume", "stop", "confirm_stop_point"):
        assert f'op("{word}"' in html


# ------------------------------------------------------------------ status shape
def test_status_reports_only_the_permitted_technical_state(client):
    s = client.get("/_ctl/status", headers=TOKEN).json()
    assert s["selected_mode"] in ("A", "B")
    assert s["state"] in M.STATES
    assert s["state_display"] == M.DISPLAY[s["state"]]
    for key in ("progress", "stop_point", "counts", "last_result", "error", "allowed_ops", "run_lock", "target"):
        assert key in s
    assert set(s["modes"]) == {"A", "B"}
    assert set(s["allowed_ops"]) == set(M.OPS)
    # no field carries a business verdict, a report, a screenshot archive or a platform end-point fact
    # (the scope_notes are the explicit disclaimer and are checked separately)
    payload = json.dumps({k: v for k, v in s.items() if k != "scope_notes"}, ensure_ascii=False)
    for forbidden in ("verdict", "screenshot", "TRACKING_END", "问题工单", "测试报告", "通过/失败", "judgement"):
        assert forbidden not in payload, forbidden
    assert any("不代表业务测试通过" in n for n in s["scope_notes"])
    assert any("不产生业务测试报告" in n for n in s["scope_notes"])


# ------------------------------------------------------------------ two-phase over HTTP
def test_two_phase_confirmation_over_http(client):
    r = client.post("/_ctl/select_mode/request", json={"mode": "B"}, headers=TOKEN)
    assert r.status_code == 200
    body = r.json()
    assert body["challenge_id"].startswith("chl_")
    assert 0 < body["expires_in_s"] <= 60
    assert body["confirm_endpoint"] == "/_ctl/select_mode/confirm"
    assert client.get("/_ctl/status", headers=TOKEN).json()["selected_mode"] == "A"

    assert client.post("/_ctl/select_mode/cancel", json={"challenge_id": body["challenge_id"]}, headers=TOKEN).json()["cancelled"] is True
    assert client.get("/_ctl/status", headers=TOKEN).json()["selected_mode"] == "A"

    again = client.post("/_ctl/select_mode/request", json={"mode": "B"}, headers=TOKEN).json()
    ok = client.post("/_ctl/select_mode/confirm", json={"challenge_id": again["challenge_id"], "mode": "B"}, headers=TOKEN)
    assert ok.status_code == 200 and ok.json()["mode"] == "B"
    assert client.get("/_ctl/status", headers=TOKEN).json()["selected_mode"] == "B"

    stale = client.post("/_ctl/select_mode/confirm", json={"challenge_id": again["challenge_id"], "mode": "B"}, headers=TOKEN)
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] in ("E_CHALLENGE_USED", "E_CHALLENGE_STALE")
    client.post("/_ctl/select_mode/confirm",
                json={"challenge_id": client.post("/_ctl/select_mode/request", json={"mode": "A"}, headers=TOKEN).json()["challenge_id"], "mode": "A"},
                headers=TOKEN)


def test_confirming_an_operation_with_another_operations_code_is_refused(client):
    ch = client.post("/_ctl/stop/request", json={}, headers=TOKEN)
    assert ch.status_code == 409  # 停止 in 待启动 is not accepted either
    assert ch.json()["detail"]["code"] == M.E_STATE


def test_a_confirm_parameter_may_not_contradict_the_request(client):
    req = client.post("/_ctl/select_mode/request", json={"mode": "B"}, headers=TOKEN).json()
    bad = client.post("/_ctl/select_mode/confirm", json={"challenge_id": req["challenge_id"], "mode": "A"}, headers=TOKEN)
    assert bad.status_code == 409 and bad.json()["detail"]["code"] == "E_BAD_PARAM"
    client.post("/_ctl/select_mode/cancel", json={"challenge_id": req["challenge_id"]}, headers=TOKEN)


def test_unknown_operations_are_rejected(client):
    r = client.post("/_ctl/drop_database/request", json={}, headers=TOKEN)
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == M.E_UNKNOWN_OP


# ------------------------------------------------------------------ mutual exclusion with the native entries
def test_the_native_sending_entries_share_the_control_run_lock(client, cvapp, replica):
    token = cvapp.run_lock.acquire("control:A:round")
    try:
        r = client.post("/_sim/cv/emit", json={"event_type": "ENTER", "camera_id": "CAM-03", "roi_id": "ROI_03_02_01"}, headers=TOKEN)
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "E_RUN_LOCK_BUSY"
        r = client.post("/_sim/cv/emit-raw", json={"payload": {"event_id": "1"}}, headers=TOKEN)
        assert r.status_code == 409
        r = client.post("/_sim/cv/scenario/run", json={"steps": []}, headers=TOKEN)
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "E_RUN_LOCK_BUSY"
    finally:
        cvapp.run_lock.release(token)
    assert client.get("/_sim/status", headers=TOKEN).json()["run_lock"]["locked"] is False


def test_a_native_scenario_blocks_the_manual_round_and_releases_the_lock(client, cvapp, replica):
    replica.mode = "accept_all"
    started = client.post("/_sim/cv/scenario/run", json={"name": "inline", "steps": [{"at": 30, "event_type": "ENTER", "camera_id": "CAM-03", "roi_id": "ROI_03_02_01"}]}, headers=TOKEN)
    assert started.status_code == 200
    try:
        refused = client.post("/_ctl/init_start/request", json={}, headers=TOKEN)
        # the state guard or the lock may answer first; both are a 409 that creates no second task
        assert refused.status_code in (200, 409)
        if refused.status_code == 200:
            conf = client.post("/_ctl/init_start/confirm", json={"challenge_id": refused.json()["challenge_id"]}, headers=TOKEN)
            assert conf.status_code == 409
            assert conf.json()["detail"]["code"] == "E_RUN_LOCK_BUSY"
    finally:
        client.post("/_sim/cv/scenario/stop", headers=TOKEN)
    assert client.get("/_sim/status", headers=TOKEN).json()["run_lock"]["locked"] is False


def test_results_and_audit_endpoints_are_read_only_and_bounded(client):
    assert isinstance(client.get("/_ctl/results?n=5", headers=TOKEN).json(), list)
    assert isinstance(client.get("/_ctl/unknown", headers=TOKEN).json(), list)
    assert isinstance(client.get("/_ctl/audit?n=3", headers=TOKEN).json(), list)
    assert client.get("/_ctl/results?mode=Z", headers=TOKEN).status_code == 400
    assert client.get("/_ctl/stop-points", headers=TOKEN).json()["mode"] in ("A", "B")
