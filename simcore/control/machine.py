"""Single-round state machine (V1.1 设计 03 §2) and the human-operation guard table.

States (V1.1 显示状态 → code):
    待启动            IDLE
    初始化中          INITIALIZING
    运行中            RUNNING
    等待阶段确认      AWAITING_CONFIRM      (B only)
    已暂停            PAUSED
    停止处理中        STOPPING
    已停止            STOPPED
    发送计划完成      COMPLETED
    异常停止          ABORTED

Everything in this module is a pure function so the whole guard table is unit-testable without
threads, sockets or a filesystem.
"""
from __future__ import annotations

IDLE = "IDLE"
INITIALIZING = "INITIALIZING"
RUNNING = "RUNNING"
AWAITING_CONFIRM = "AWAITING_CONFIRM"
PAUSED = "PAUSED"
STOPPING = "STOPPING"
STOPPED = "STOPPED"
COMPLETED = "COMPLETED"
ABORTED = "ABORTED"

STATES = (IDLE, INITIALIZING, RUNNING, AWAITING_CONFIRM, PAUSED, STOPPING, STOPPED, COMPLETED, ABORTED)

DISPLAY = {
    IDLE: "待启动",
    INITIALIZING: "初始化中",
    RUNNING: "运行中",
    AWAITING_CONFIRM: "等待阶段确认",
    PAUSED: "已暂停",
    STOPPING: "停止处理中",
    STOPPED: "已停止",
    COMPLETED: "发送计划完成",
    ABORTED: "异常停止",
}

#: states from which a new round may be initialised / the mode may be switched
RESTARTABLE = (IDLE, COMPLETED, STOPPED, ABORTED)
#: states in which a round is live; a process restart found here means the position is indeterminate
LIVE = (INITIALIZING, RUNNING, AWAITING_CONFIRM, PAUSED, STOPPING)

SELECT_MODE = "select_mode"
INIT_START = "init_start"
PAUSE = "pause"
RESUME = "resume"
STOP = "stop"
CONFIRM_STOP_POINT = "confirm_stop_point"
#: 核清: the technician acknowledges the latched in-flight / undetermined results of the previous
#: round. V1.1 §1 makes 「在途与未知结果已由技术人员核清」 a precondition of re-initialising, so the
#: acknowledgement itself has to be an explicit, confirmed, audited human operation. It only moves
#: the latch into a cleared list — the append-only unknown.jsonl evidence is never removed.
CLEAR_UNKNOWN = "clear_unknown"
OPS = (SELECT_MODE, INIT_START, PAUSE, RESUME, STOP, CONFIRM_STOP_POINT, CLEAR_UNKNOWN)

# error codes (always answered with HTTP 409)
E_STATE = "E_STATE_NOT_ALLOWED"
E_UNKNOWN_PENDING = "E_UNKNOWN_RESULTS_PENDING"
E_INFLIGHT = "E_INFLIGHT_PENDING"
E_MODE_NOT_B = "E_MODE_NOT_B"
E_UNKNOWN_OP = "E_UNKNOWN_OP"
E_NOTHING_TO_CLEAR = "E_NOTHING_TO_CLEAR"


def transition_error(op: str, state: str, *, mode: str, unknown_pending: int = 0, inflight: int = 0) -> str | None:
    """Return None when the human operation is allowed in this state, else a stable error code.

    V1.1 §1/§2 and Owner frozen boundaries:
      * mode may only be switched while no round is live, and it never starts a round;
      * re-initialising requires 本轮已完成或已停止 + 无活动发送任务 + 在途与未知结果已核清
        (so PAUSED and B's AWAITING_CONFIRM explicitly do NOT qualify);
      * 普通继续 (resume) must never pass a B stop point;
      * 暂停 ≠ 停止: stop is accepted directly from RUNNING.
    """
    if op not in OPS:
        return E_UNKNOWN_OP
    if op in (SELECT_MODE, INIT_START):
        if state not in RESTARTABLE:
            return E_STATE
        if unknown_pending:
            return E_UNKNOWN_PENDING
        if inflight:
            return E_INFLIGHT
        return None
    if op == CLEAR_UNKNOWN:
        if state not in RESTARTABLE:
            return E_STATE
        return None if unknown_pending or inflight else E_NOTHING_TO_CLEAR
    if op == PAUSE:
        return None if state == RUNNING else E_STATE
    if op == RESUME:
        return None if state == PAUSED else E_STATE
    if op == STOP:
        return None if state in LIVE else E_STATE
    if op == CONFIRM_STOP_POINT:
        if state != AWAITING_CONFIRM:
            return E_STATE
        if mode != "B":
            return E_MODE_NOT_B
        return None
    return E_UNKNOWN_OP  # pragma: no cover - OPS is exhaustive


def recovered_state(state: str) -> tuple[str, str | None]:
    """State to adopt after a process restart. A live round can never be resumed automatically."""
    if state in LIVE:
        return ABORTED, "RECOVERED_INDETERMINATE_POSITION"
    return state, None
