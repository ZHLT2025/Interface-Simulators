"""F02/F04 targeted verification against the deployed stack."""
import json
import threading
import time
import urllib.request

import httpx


def A(creds):
    return {"X-Sim-Admin-Token": creds["admin"]}


def test_mom_ignores_forwarded_headers(mom_url, creds):
    """F02: client address recorded by the server must be the socket peer, not X-Forwarded-For."""
    body = {"esbInfo": {"requestTime": "2026-07-30 16:36:50"}, "requestInfo": {"SYSTEMTYPE": 119, "STARTTIME": "2026-07-28 00:00:00", "ENDTIME": "2026-07-28 01:00:00", "WORKCENTER": "TS36120201", "DEVICEPN": None}}
    marker = "2026-07-28 00:00:00"
    r = httpx.post(f"{mom_url}/api/v1/get_orderlist", json=body, auth=creds["mom"], headers={"X-Forwarded-For": "203.0.113.77", "X-Real-IP": "203.0.113.77"})
    assert r.status_code == 200
    rows = httpx.get(f"{mom_url}/_sim/requests?n=20&path=/api/v1/get_orderlist", headers=A(creds)).json()
    mine = [x for x in rows if marker in x["request_body"] and "203.0.113.77" in json.dumps(x["headers"])]
    assert mine and mine[-1]["client"] != "203.0.113.77"


def test_admin_status_reports_access_control(mom_url, creds):
    st = httpx.get(f"{mom_url}/_sim/status", headers=A(creds)).json()
    assert "admin_allow_cidrs" in st["access_control"]


def test_cv_target_url_outside_allowlist_rejected(cv_url, creds):
    r = httpx.post(f"{cv_url}/_sim/cv/emit", json={"event_type": "ENTER", "camera_id": "CAM-20", "roi_id": "ROI_20_02_01", "target_url": "http://203.0.113.5/api/v1/cv-events"}, headers=A(creds), timeout=30)
    assert r.status_code == 400 and "target_url host not allowed" in r.text
    r = httpx.post(f"{cv_url}/_sim/cv/emit-raw", json={"payload": {"a": 1}, "target_url": "http://203.0.113.5/x"}, headers=A(creds), timeout=30)
    assert r.status_code == 400
    r = httpx.post(f"{cv_url}/_sim/cv/scenario/run", json={"steps": [{"at": 0, "event_type": "ENTER", "target_url": "http://203.0.113.5/x"}]}, headers=A(creds))
    assert r.status_code == 400


def test_scenario_stop_then_immediate_restart(cv_url, creds):
    """F04: after /stop returns, /run must start a new scenario (previously answered 409)."""
    httpx.post(f"{cv_url}/_sim/cv/scenario/stop", headers=A(creds), timeout=60)
    r = httpx.post(f"{cv_url}/_sim/cv/scenario/run", json={"name": "all-types"}, headers=A(creds))
    assert r.status_code == 200
    time.sleep(1.0)
    r = httpx.post(f"{cv_url}/_sim/cv/scenario/stop", headers=A(creds), timeout=60)
    assert r.status_code == 200 and r.json()["stopped"] is True and r.json()["status"]["running"] is False
    r = httpx.post(f"{cv_url}/_sim/cv/scenario/run", json={"name": "default"}, headers=A(creds))
    assert r.status_code == 200, r.text
    r = httpx.post(f"{cv_url}/_sim/cv/scenario/stop", headers=A(creds), timeout=60)
    assert r.json()["stopped"] is True


def test_scenario_single_instance_under_concurrent_starts(cv_url, creds):
    """F04: 20 simultaneous /run requests -> exactly one scenario runs, exactly 6 events delivered."""
    httpx.post(f"{cv_url}/_sim/cv/scenario/stop", headers=A(creds), timeout=60)
    httpx.post(f"{cv_url}/_sim/cv/sink/clear", headers=A(creds))
    results = []
    barrier = threading.Barrier(20)

    def go():
        req = urllib.request.Request(f"{cv_url}/_sim/cv/scenario/run", method="POST", data=b'{"name":"default"}', headers={**A(creds), "Content-Type": "application/json"})
        barrier.wait()
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                results.append(resp.status)
        except urllib.error.HTTPError as e:
            results.append(e.code)

    ts = [threading.Thread(target=go) for _ in range(20)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert results.count(200) == 1 and results.count(409) == 19, results
    deadline = time.time() + 40
    while time.time() < deadline:
        st = httpx.get(f"{cv_url}/_sim/cv/scenario/status", headers=A(creds)).json()
        if not st["running"]:
            break
        time.sleep(1)
    assert not st["running"] and len(st["results"]) == 6
    assert len(httpx.get(f"{cv_url}/_sim/cv/sink/events?n=500", headers=A(creds)).json()) == 6
