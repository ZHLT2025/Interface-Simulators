"""End-to-end single rounds against the local replica (never a real target).

Covers the state transition table, the confirmation gate, B's 62-stop-point discipline, mutual
exclusion, the KNOWN_REJECTED-continues / UNKNOWN-latches split, A/B independence and restart
recovery. The formal 318-event / 62-stop-point plan is exercised at the end with a virtual plan
clock (the production code has no speed knob; the clock is injected only by these tests).
"""
import json
import shutil
import time
from pathlib import Path

import pytest

from simcore.control import machine as M
from simcore.control.module import ControlError
from simcore.control.runlock import RunLock, RunLockBusy

from .conftest import VirtualClock, do, wait_for, wait_state, wait_stop_point, write_synthetic_dataset

FORMAL = "sww_all5"


def state(m):
    return m.store().get()


# =============================================================== mode A happy path
def test_mode_a_round_runs_once_and_completes(control_factory, replica):
    replica.mode = "accept_all"
    replica.reset()
    m = control_factory()
    assert state(m)["state"] == M.IDLE
    out = do(m, M.INIT_START)
    assert out["performed"] is True and out["run_id"].startswith("R-A-")
    st = wait_state(m, M.COMPLETED)
    assert st["counts"] == {"accepted": 6, "known_rejected": 0, "unknown": 0, "rejected_confirms": 0}
    assert st["progress"]["attempted"] == 6
    assert st.get("completed_with_known_rejections") is False
    sent = [e["event_id"] for e in replica.received]
    assert sent == [s["event_id"] for s in m._test_info["steps"]]
    assert m.run_lock.locked() is False
    assert len(m.results(100)) == 6
    assert {r["outcome"] for r in m.results(100)} == {"ACCEPTED"}


def test_requesting_without_confirming_changes_nothing(control_factory, replica):
    replica.reset()
    m = control_factory()
    req = m.request(M.INIT_START, {})
    assert state(m)["state"] == M.IDLE
    assert state(m)["run_id"] is None
    assert replica.received == []
    assert m.cancel(M.INIT_START, req["challenge_id"])["state_changed"] is False
    assert state(m)["state"] == M.IDLE
    with pytest.raises(ControlError) as e:
        m.confirm(M.INIT_START, req["challenge_id"], {})
    assert e.value.code == "E_CHALLENGE_UNKNOWN"
    assert replica.received == []


def test_a_confirmation_code_cannot_be_reused_or_used_after_expiry(control_factory, replica):
    replica.mode = "accept_all"
    m = control_factory()
    req = m.request(M.INIT_START, {})
    do(m, M.SELECT_MODE, mode="A")  # a different operation, same state family
    m.confirm(M.INIT_START, req["challenge_id"], {})
    wait_state(m, M.COMPLETED)
    with pytest.raises(ControlError) as e:
        m.confirm(M.INIT_START, req["challenge_id"], {})
    assert e.value.code in ("E_CHALLENGE_USED", "E_CHALLENGE_STALE")

    m2 = control_factory(challenge_ttl_s=0.05)
    req2 = m2.request(M.INIT_START, {})
    time.sleep(0.2)
    with pytest.raises(ControlError) as e:
        m2.confirm(M.INIT_START, req2["challenge_id"], {})
    assert e.value.code == "E_CHALLENGE_EXPIRED"
    assert state(m2)["state"] == M.IDLE


def test_double_click_never_creates_a_second_round(control_factory, replica, tmp_path):
    replica.mode = "accept_all"
    replica.reset()
    info = write_synthetic_dataset(tmp_path / "dbl", "dbl", segments=(6,), soundproof_positions=(1,), offsets=[0, 0, 0, 5.0, 5.0, 5.0])
    info["soundproof"], info["segments"] = 1, 1
    m = control_factory(dataset_info=info, tick_seconds=0.02)
    do(m, M.INIT_START)
    wait_for(lambda: state(m)["progress"]["attempted"] == 3)
    run_id = state(m)["run_id"]
    with pytest.raises(ControlError) as e:
        m.request(M.INIT_START, {})
    assert e.value.code == M.E_STATE
    assert e.value.status == 409
    assert state(m)["state"] in (M.RUNNING, M.INITIALIZING)
    do(m, M.STOP)
    wait_state(m, M.STOPPED)
    assert state(m)["run_id"] == run_id
    assert len(replica.received) == 3


# =============================================================== known rejection vs unknown
def test_known_type_rejection_is_recorded_and_the_round_continues(control_factory, replica):
    replica.mode = "enum_reject_known"
    replica.reset()
    m = control_factory()
    do(m, M.INIT_START)
    st = wait_state(m, M.COMPLETED)
    assert st["counts"]["known_rejected"] == 2
    assert st["counts"]["accepted"] == 4
    assert st["counts"]["unknown"] == 0
    assert st["completed_with_known_rejections"] is True
    assert len(replica.received) == 6  # all ten-style events were really attempted (F09)
    rejected = [r for r in m.results(100) if r["outcome"] == "KNOWN_REJECTED"]
    assert len(rejected) == 2
    assert all(r["http_status"] == 422 and r["reason"] == "EVENT_TYPE_ENUM_NOT_ACCEPTED" for r in rejected)
    assert all("not an enumerated event_type" in r["response_body"] for r in rejected)
    assert m.unknowns() == []


@pytest.mark.parametrize(
    "mode,reason",
    [
        ("other_422_known", "REJECTION_NOT_ONLY_EVENT_TYPE_ENUM"),
        ("mixed_422_known", "REJECTION_NOT_ONLY_EVENT_TYPE_ENUM"),
        ("extra_key_422_known", "NOT_CONTRACT_REJECTION_SHAPE"),
        ("enum_reject_all", "EVENT_NOT_IN_KNOWN_TEN"),
        ("error_500", "SERVER_ERROR"),
        ("auth_401", "AUTH_FAILED"),
        ("bad_request_400", "UNEXPECTED_4XX"),
        ("html_200", "RESPONSE_BODY_UNPARSEABLE"),
        ("ambiguous_200", "AMBIGUOUS_200_BODY"),
        ("timeout", "TIMEOUT"),
    ],
)
def test_an_undetermined_result_latches_the_round(control_factory, replica, mode, reason):
    replica.mode = mode
    replica.reset()
    m = control_factory()
    do(m, M.INIT_START)
    st = wait_state(m, M.ABORTED, timeout=15)
    assert st["error"]["code"] == "UNDETERMINED_TECHNICAL_RESULT"
    assert st["error"]["reason"] == reason
    assert st["counts"]["unknown"] == 1
    assert st["requires_manual_review"] is True
    assert len(st["unknown_pending"]) == 1
    # the round stopped advancing: nothing after the latched event was sent, nothing was resent
    attempted = st["progress"]["attempted"]
    assert attempted < 6
    assert len(replica.received) == attempted
    latched = m.unknowns()
    assert len(latched) == 1 and latched[0]["reason"] == reason
    # ... and no new round may start until a technician clears it
    with pytest.raises(ControlError) as e:
        m.request(M.INIT_START, {})
    assert e.value.code == M.E_UNKNOWN_PENDING
    do(m, M.CLEAR_UNKNOWN, note="pytest 核清")
    assert state(m)["unknown_pending"] == []
    assert m.request(M.INIT_START, {})["challenge_id"]
    # the evidence itself is never deleted
    kept = m.unknowns()
    assert any(r.get("record") == "CLEARED_BY_TECHNICIAN" for r in kept)
    assert any(r.get("reason") == reason for r in kept)


def test_the_control_layer_never_resends(control_factory, replica):
    replica.mode = "error_500"
    replica.reset()
    m = control_factory()
    do(m, M.INIT_START)
    wait_state(m, M.ABORTED)
    assert len(replica.received) == 1
    assert m.results(10)[0]["response_body"]
    headers = {k.lower(): v for k, v in m.results(10)[0]["response_headers"].items()}
    assert headers["content-type"] == "application/json"


# =============================================================== mode B stop points
def test_mode_b_requires_every_stop_point_including_the_last(control_factory, replica):
    replica.mode = "accept_all"
    replica.reset()
    m = control_factory()
    do(m, M.SELECT_MODE, mode="B")
    assert state(m)["state"] == M.IDLE  # 切换不自动启动
    do(m, M.INIT_START)
    for k in (1, 2, 3):
        sp = wait_stop_point(m, k)
        assert (sp["sequence"], sp["total_stop_points"]) == (k, 3)
        assert sp["expected_confirm_seq"] == k
        # 普通继续不得绕过当前停点
        with pytest.raises(ControlError) as e:
            m.request(M.RESUME, {})
        assert e.value.code == M.E_STATE
        with pytest.raises(ControlError) as e:
            m.request(M.PAUSE, {})
        assert e.value.code == M.E_STATE
        do(m, M.CONFIRM_STOP_POINT, stop_point_id=sp["stop_point_id"], run_id=sp["run_id"], confirm_seq=k)
    st = wait_state(m, M.COMPLETED)
    assert st["confirmed_stop_points"] == 3 and st["confirm_seq"] == 3
    assert st["progress"]["attempted"] == 6
    assert len(replica.received) == 6


def test_wrong_old_and_duplicate_stop_point_confirmations_never_pass(control_factory, replica):
    replica.mode = "accept_all"
    m = control_factory()
    do(m, M.SELECT_MODE, mode="B")
    do(m, M.INIT_START)
    sp = wait_stop_point(m, 1)

    for params, code in [
        ({"stop_point_id": "nope", "run_id": sp["run_id"], "confirm_seq": 1}, "E_STOP_POINT_MISMATCH"),
        ({"stop_point_id": sp["stop_point_id"], "run_id": "R-B-other", "confirm_seq": 1}, "E_RUN_ID_MISMATCH"),
        ({"stop_point_id": sp["stop_point_id"], "run_id": sp["run_id"], "confirm_seq": 0}, "E_CONFIRM_SEQ_NOT_CURRENT"),
        ({"stop_point_id": sp["stop_point_id"], "run_id": sp["run_id"], "confirm_seq": 2}, "E_CONFIRM_SEQ_NOT_CURRENT"),
        ({"stop_point_id": sp["stop_point_id"], "run_id": sp["run_id"]}, "E_BAD_PARAM"),
    ]:
        with pytest.raises(ControlError) as e:
            m.request(M.CONFIRM_STOP_POINT, params)
        assert e.value.code == code, params
    assert state(m)["state"] == M.AWAITING_CONFIRM
    assert state(m)["confirmed_stop_points"] == 0
    assert state(m)["counts"]["rejected_confirms"] >= 5

    # a second challenge for the same stop point goes stale once the point has been passed
    good = {"stop_point_id": sp["stop_point_id"], "run_id": sp["run_id"], "confirm_seq": 1}
    replay = m.request(M.CONFIRM_STOP_POINT, good)
    do(m, M.CONFIRM_STOP_POINT, **good)
    wait_for(lambda: state(m)["confirmed_stop_points"] == 1)
    with pytest.raises(ControlError) as e:
        m.confirm(M.CONFIRM_STOP_POINT, replay["challenge_id"], good)
    assert e.value.code in ("E_CHALLENGE_STALE", "E_STOP_POINT_MISMATCH")
    assert state(m)["confirmed_stop_points"] == 1
    do(m, M.STOP)
    wait_state(m, M.STOPPED)


def test_b_waiting_for_confirmation_cannot_be_re_initialised(control_factory, replica):
    replica.mode = "accept_all"
    m = control_factory()
    do(m, M.SELECT_MODE, mode="B")
    do(m, M.INIT_START)
    wait_stop_point(m, 1)
    for op in (M.INIT_START, M.SELECT_MODE):
        with pytest.raises(ControlError) as e:
            m.request(op, {"mode": "A"})
        assert e.value.code == M.E_STATE
    do(m, M.STOP)
    wait_state(m, M.STOPPED)
    assert m.request(M.INIT_START, {})["challenge_id"]


# =============================================================== pause / resume / stop
def test_pause_keeps_the_position_and_does_not_catch_up_the_waiting_time(control_factory, replica, tmp_path):
    replica.mode = "accept_all"
    replica.reset()
    info = write_synthetic_dataset(tmp_path / "slow", "slow", segments=(3, 3), soundproof_positions=(1,), offsets=[0, 0, 0, 1.0, 1.0, 1.0])
    info["soundproof"] = 1
    m = control_factory(dataset_info=info, tick_seconds=0.02)
    do(m, M.INIT_START)
    wait_for(lambda: state(m)["progress"]["attempted"] == 3)
    do(m, M.PAUSE)
    st = wait_state(m, M.PAUSED)
    assert st["progress"]["attempted"] == 3
    time.sleep(0.4)
    assert state(m)["progress"]["attempted"] == 3, "暂停期间不得继续推进"
    assert len(replica.received) == 3
    t_resume = time.monotonic()
    do(m, M.RESUME)
    wait_state(m, M.COMPLETED, timeout=10)
    waited = time.monotonic() - t_resume
    assert waited > 0.4, f"resume caught up the paused time ({waited:.3f}s)"
    assert len(replica.received) == 6


def test_stop_from_running_is_accepted_and_leaves_the_rest_unsent(control_factory, replica, tmp_path):
    replica.mode = "accept_all"
    replica.reset()
    info = write_synthetic_dataset(tmp_path / "slow2", "slow2", segments=(3, 3), soundproof_positions=(1,), offsets=[0, 0, 0, 5.0, 5.0, 5.0])
    info["soundproof"] = 1
    m = control_factory(dataset_info=info, tick_seconds=0.02)
    do(m, M.INIT_START)
    wait_for(lambda: state(m)["progress"]["attempted"] == 3)
    do(m, M.STOP)
    st = wait_state(m, M.STOPPED)
    assert st["progress"]["attempted"] == 3
    assert st["counts"]["unknown"] == 0
    assert len(replica.received) == 3
    assert st.get("completed_with_known_rejections") is None
    assert m.run_lock.locked() is False
    # 停止后可以再次人工初始化启动，从原计划第一项开始
    do(m, M.INIT_START)
    wait_for(lambda: state(m)["progress"]["attempted"] >= 1)
    assert replica.received[3]["event_id"] == info["steps"][0]["event_id"]
    do(m, M.STOP)
    wait_state(m, M.STOPPED)


# =============================================================== mutual exclusion
def test_the_run_lock_is_shared_with_the_other_sending_entries(control_factory, replica, tmp_path):
    replica.mode = "accept_all"
    lock = RunLock(tmp_path / "shared" / "run.lock")
    m = control_factory(run_lock=lock)
    held = lock.acquire("native_scenario:sww_all5_A240")
    with pytest.raises(ControlError) as e:
        do(m, M.INIT_START)
    assert e.value.code == "E_RUN_LOCK_BUSY"
    assert e.value.detail["holder"]["holder"] == "native_scenario:sww_all5_A240"
    assert state(m)["state"] == M.IDLE
    assert state(m)["run_id"] is None
    lock.release(held)

    info = write_synthetic_dataset(tmp_path / "slow3", "slow3", segments=(6,), soundproof_positions=(1,), offsets=[0, 0, 0, 5.0, 5.0, 5.0])
    info["soundproof"], info["segments"] = 1, 1
    m2 = control_factory(run_lock=lock, dataset_info=info, tick_seconds=0.02)
    do(m2, M.INIT_START)
    wait_for(lambda: state(m2)["progress"]["attempted"] >= 1)
    with pytest.raises(RunLockBusy):
        lock.acquire("cv_emit")          # the native single-shot entry would get a 409
    do(m2, M.STOP)
    wait_state(m2, M.STOPPED)
    lock.release(lock.acquire("cv_emit"))


# =============================================================== target identity
def test_a_target_that_is_not_the_approved_one_refuses_the_start(control_factory, replica):
    replica.reset()
    replica.readyz_mode = "wrong_domain"
    try:
        m = control_factory()
        with pytest.raises(ControlError) as e:
            do(m, M.INIT_START)
        assert e.value.code == "E_DATA_DOMAIN_MISMATCH"
    finally:
        replica.readyz_mode = "ready"
    st = state(m)
    assert st["state"] == M.IDLE
    assert st["run_id"] is None, "a refused target check must not consume a run_id"
    assert replica.received == []
    assert m.run_lock.locked() is False

    m2 = control_factory(cv_target_url="http://127.0.0.1:1/api/v1/cv-events")
    with pytest.raises(ControlError) as e:
        do(m2, M.INIT_START)
    assert e.value.code == "E_TARGET_MISMATCH"
    assert state(m2)["run_id"] is None

    m3 = control_factory(approved_target_url=None)
    with pytest.raises(ControlError) as e:
        do(m3, M.INIT_START)
    assert e.value.code == "E_APPROVED_TARGET_NOT_CONFIGURED"


def test_a_plan_that_does_not_match_the_frozen_counts_refuses_the_start(control_factory, replica):
    replica.reset()
    m = control_factory(expect_events=318, expect_b_stop_points=62)
    with pytest.raises(ControlError) as e:
        do(m, M.INIT_START)
    assert e.value.code == "E_EVENT_COUNT"
    assert state(m)["run_id"] is None
    assert replica.received == []


# =============================================================== A / B independence and restart
def test_a_and_b_keep_independent_state_directories(control_factory, replica):
    replica.mode = "accept_all"
    m = control_factory()
    do(m, M.INIT_START)
    wait_state(m, M.COMPLETED)
    a_run = state(m)["run_id"]
    do(m, M.SELECT_MODE, mode="B")
    do(m, M.INIT_START)
    for k in (1, 2, 3):
        sp = wait_stop_point(m, k)
        do(m, M.CONFIRM_STOP_POINT, stop_point_id=sp["stop_point_id"], run_id=sp["run_id"], confirm_seq=k)
    wait_state(m, M.COMPLETED)
    b_run = state(m)["run_id"]
    assert a_run.startswith("R-A-") and b_run.startswith("R-B-") and a_run != b_run

    root = m.cfg.state_dir
    a = json.loads((root / "A" / "state.json").read_text(encoding="utf-8"))
    b = json.loads((root / "B" / "state.json").read_text(encoding="utf-8"))
    assert a["run_id"] == a_run and b["run_id"] == b_run
    assert a["expected_stop_points"] == 0 and b["expected_stop_points"] == 3
    assert a["confirmed_stop_points"] == 0 and b["confirmed_stop_points"] == 3
    assert (root / "A" / "results.jsonl").exists() and (root / "B" / "results.jsonl").exists()
    assert len(m.results(100, "A")) == 6 and len(m.results(100, "B")) == 6
    assert m.stores["A"].dir != m.stores["B"].dir


def test_a_restart_never_resumes_a_round(control_factory, replica, tmp_path):
    """A live state found on disk recovers as 异常停止 + 人工核查, never as a second round."""
    replica.mode = "accept_all"
    replica.reset()
    m = control_factory()
    do(m, M.SELECT_MODE, mode="B")
    do(m, M.INIT_START)
    wait_stop_point(m, 1)
    live = state(m)
    live_file = json.loads((m.cfg.state_dir / "B" / "state.json").read_text(encoding="utf-8"))
    assert live_file["state"] == M.AWAITING_CONFIRM
    do(m, M.STOP)
    wait_state(m, M.STOPPED)

    # the genuine live state file, as a crashed process would have left it behind
    restart_dir = tmp_path / "restart"
    (restart_dir / "B").mkdir(parents=True)
    (restart_dir / "B" / "state.json").write_text(json.dumps(live_file), encoding="utf-8")
    m2 = control_factory(state_dir=restart_dir)
    do(m2, M.SELECT_MODE, mode="B")
    st = state(m2)
    assert st["state"] == M.ABORTED
    assert st["recovered_from"] == M.AWAITING_CONFIRM
    assert st["requires_manual_review"] is True
    assert st["error"]["code"] == "RECOVERED_INDETERMINATE_POSITION"
    assert len(replica.received) == live["progress"]["attempted"], "recovery must not send anything"
    assert m2.allowed_ops()[M.INIT_START] is True  # only after an explicit human click

    # an in-flight request at the moment of the crash additionally blocks re-initialising
    (restart_dir / "A").mkdir(parents=True, exist_ok=True)
    crashed = dict(live_file, mode="A", state=M.RUNNING, inflight=1)
    (restart_dir / "A" / "state.json").write_text(json.dumps(crashed), encoding="utf-8")
    m3 = control_factory(state_dir=restart_dir)
    st3 = m3.stores["A"].get()
    assert st3["state"] == M.ABORTED and st3["unknown_pending"]
    assert st3["unknown_pending"][0]["reason"] == "INDETERMINATE_INFLIGHT_AT_RESTART"
    m3.mode = "A"
    with pytest.raises(ControlError) as e:
        m3.request(M.INIT_START, {})
    assert e.value.code == M.E_UNKNOWN_PENDING


def test_switching_mode_only_changes_the_selection(control_factory, replica):
    replica.reset()
    m = control_factory()
    out = do(m, M.SELECT_MODE, mode="B")
    assert out["mode"] == "B" and out["auto_started"] is False
    assert state(m)["state"] == M.IDLE
    assert replica.received == []
    with pytest.raises(ControlError) as e:
        m.request(M.SELECT_MODE, {"mode": "C"})
    assert e.value.code == "E_BAD_PARAM"
    assert m.mode == "B"


def test_audit_records_every_operation_and_refusal(control_factory, replica):
    replica.mode = "accept_all"
    m = control_factory()
    with pytest.raises(ControlError):
        m.request(M.PAUSE, {})
    do(m, M.INIT_START)
    wait_state(m, M.COMPLETED)
    events = [r["event"] for r in m.audit.recent(200)]
    assert "op_refused" in events
    assert "op_requested" in events and "op_confirmed" in events
    assert "round_initialised" in events and "send_plan_completed" in events


# =============================================================== the formal frozen plan
@pytest.fixture
def formal_info(v11_cv_dir, tmp_path):
    src = v11_cv_dir / "data" / FORMAL
    dst = tmp_path / "formal" / FORMAL
    dst.mkdir(parents=True)
    shutil.copy2(src / "scenarios.json", dst / "scenarios.json")
    shutil.copy2(v11_cv_dir / "manifests" / f"{FORMAL}.json", dst / "manifest.json")
    return {"dir": dst.parent / FORMAL, "dataset": FORMAL, "total": 318, "segments": 62, "soundproof": 10}


def test_formal_mode_a_round_sends_318_events_in_plan_order(control_factory, replica, formal_info):
    replica.mode = "accept_all"
    replica.reset()
    clock = VirtualClock()
    m = control_factory(dataset_info=formal_info, clock=clock, tick_seconds=1e9)
    do(m, M.INIT_START)
    st = wait_state(m, M.COMPLETED, timeout=240)
    assert st["counts"]["accepted"] == 318
    assert st["counts"]["unknown"] == 0
    assert st["plan"]["events"] == 318 and st["plan"]["speed_ratio"] == "1:240"
    plan_ids = [s["event_id"] for s in json.loads((formal_info["dir"] / "scenarios.json").read_text(encoding="utf-8"))["scenarios"][f"{FORMAL}_A240"]["steps"]]
    assert [e["event_id"] for e in replica.received] == plan_ids
    assert clock.monotonic() == pytest.approx(3738.75, abs=1.0), "the 1:240 nominal timeline was honoured"


def test_formal_mode_b_round_has_62_stop_points_and_keeps_the_ten(control_factory, replica, formal_info):
    replica.mode = "enum_reject_known"
    replica.reset()
    clock = VirtualClock()
    m = control_factory(dataset_info=formal_info, clock=clock, tick_seconds=1e9)
    do(m, M.SELECT_MODE, mode="B")
    do(m, M.INIT_START)
    seen = []
    for k in range(1, 63):
        sp = wait_stop_point(m, k, timeout=120)
        assert sp["sequence"] == k and sp["total_stop_points"] == 62
        seen.append((sp["sequence"], sp["label"], sp["events_completed"]))
        do(m, M.CONFIRM_STOP_POINT, stop_point_id=sp["stop_point_id"], run_id=sp["run_id"], confirm_seq=k)
    st = wait_state(m, M.COMPLETED, timeout=120)
    assert st["confirmed_stop_points"] == 62 and st["confirm_seq"] == 62
    assert st["progress"]["attempted"] == 318
    assert st["counts"]["known_rejected"] == 10, "the original ten were really attempted and really rejected"
    assert st["counts"]["accepted"] == 308
    assert st["counts"]["unknown"] == 0
    assert st["completed_with_known_rejections"] is True
    assert len(replica.received) == 318
    assert seen[0][1] == "02-13右:S-QL-01:profile_11:1"
    assert seen[-1] == (62, "last_actual_cv_event", 318)
    rejected = [r for r in m.results(400) if r["outcome"] == "KNOWN_REJECTED"]
    assert len(rejected) == 10
    assert {r["event_type"] for r in rejected} == {"SOUNDPROOF_COVER_MOVE_COMPLETED"}
