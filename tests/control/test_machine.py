"""The frozen single-round guard table (V1.1 设计 03 §2, Owner 冻结边界). Pure functions, no I/O."""
from simcore.control import machine as M


ALL = M.STATES


def err(op, state, **kw):
    return M.transition_error(op, state, mode=kw.pop("mode", "A"), **kw)


def test_display_names_cover_every_state():
    assert set(M.DISPLAY) == set(ALL)
    assert M.DISPLAY[M.COMPLETED] == "发送计划完成"
    assert M.DISPLAY[M.ABORTED] == "异常停止"


def test_init_start_only_from_terminal_states():
    for s in (M.IDLE, M.COMPLETED, M.STOPPED, M.ABORTED):
        assert err(M.INIT_START, s) is None, s
    # 运行中 / 初始化中 / 已暂停 / B 等待确认 / 停止处理中 都不得再次启动
    for s in (M.INITIALIZING, M.RUNNING, M.PAUSED, M.AWAITING_CONFIRM, M.STOPPING):
        assert err(M.INIT_START, s) == M.E_STATE, s


def test_init_start_blocked_by_unresolved_results():
    assert err(M.INIT_START, M.STOPPED, unknown_pending=1) == M.E_UNKNOWN_PENDING
    assert err(M.INIT_START, M.ABORTED, unknown_pending=3) == M.E_UNKNOWN_PENDING
    assert err(M.INIT_START, M.STOPPED, inflight=1) == M.E_INFLIGHT
    assert err(M.INIT_START, M.STOPPED) is None


def test_mode_switch_forbidden_while_a_round_is_live():
    for s in (M.INITIALIZING, M.RUNNING, M.PAUSED, M.AWAITING_CONFIRM, M.STOPPING):
        assert err(M.SELECT_MODE, s) == M.E_STATE, s
    for s in (M.IDLE, M.COMPLETED, M.STOPPED, M.ABORTED):
        assert err(M.SELECT_MODE, s) is None, s
    assert err(M.SELECT_MODE, M.ABORTED, unknown_pending=1) == M.E_UNKNOWN_PENDING


def test_pause_resume_and_stop():
    assert err(M.PAUSE, M.RUNNING) is None
    for s in [x for x in ALL if x != M.RUNNING]:
        assert err(M.PAUSE, s) == M.E_STATE, s
    assert err(M.RESUME, M.PAUSED) is None
    # 普通继续不得绕过 B 停点确认, and it is not a way out of 停止处理中 either
    for s in [x for x in ALL if x != M.PAUSED]:
        assert err(M.RESUME, s) == M.E_STATE, s
    # 暂停 ≠ 停止: stop is accepted directly from 运行中 and from every other live state
    for s in (M.INITIALIZING, M.RUNNING, M.PAUSED, M.AWAITING_CONFIRM):
        assert err(M.STOP, s) is None, s
    for s in (M.IDLE, M.STOPPED, M.COMPLETED, M.ABORTED):
        assert err(M.STOP, s) == M.E_STATE, s


def test_stop_point_confirmation_only_in_b_while_awaiting():
    assert err(M.CONFIRM_STOP_POINT, M.AWAITING_CONFIRM, mode="B") is None
    assert err(M.CONFIRM_STOP_POINT, M.AWAITING_CONFIRM, mode="A") == M.E_MODE_NOT_B
    for s in [x for x in ALL if x != M.AWAITING_CONFIRM]:
        assert err(M.CONFIRM_STOP_POINT, s, mode="B") == M.E_STATE, s


def test_clear_unknown_requires_terminal_state_and_something_to_clear():
    assert err(M.CLEAR_UNKNOWN, M.ABORTED, unknown_pending=2) is None
    assert err(M.CLEAR_UNKNOWN, M.ABORTED) == M.E_NOTHING_TO_CLEAR
    assert err(M.CLEAR_UNKNOWN, M.RUNNING, unknown_pending=2) == M.E_STATE


def test_unknown_operation_is_refused():
    assert err("delete_everything", M.IDLE) == M.E_UNKNOWN_OP


def test_restart_never_resumes_a_live_round():
    for s in M.LIVE:
        assert M.recovered_state(s) == (M.ABORTED, "RECOVERED_INDETERMINATE_POSITION"), s
    for s in (M.IDLE, M.STOPPED, M.COMPLETED, M.ABORTED):
        assert M.recovered_state(s) == (s, None), s
