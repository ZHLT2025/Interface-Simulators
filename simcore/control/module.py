"""The manual single-round controller: human operations, the one runner thread, and the HTTP router.

Design authority: V1.1 正式设计 03 §1–§6 (frozen) + E02 三域设计说明 SIM 节.

Hard properties implemented here:
  * exactly one round, started by exactly one human click (`init_start`), two-phase confirmed;
  * no operation that changes state can happen without a fresh, single-use challenge;
  * no second task while one is live (the global run lock is shared with the native CV entries);
  * B's 62 stop points can only be passed by confirming the *current* stop point identity with the
    current run_id and a monotonically increasing confirm_seq — `resume` never passes a stop point;
  * an undetermined technical result latches the round: it stops advancing, nothing is resent, and
    a technician has to acknowledge the latch before another round can be initialised;
  * a process restart never continues a round: a live state recovers as 异常停止 + 人工核查提示.
"""
from __future__ import annotations

import logging
import secrets
import threading
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from . import machine as M
from .challenge import ChallengeError, ChallengeStore
from .config import MODES, ControlConfig
from .outcome import ACCEPTED, KNOWN_REJECTED, UNKNOWN, classify
from .page import render_page
from .plan import PlanError, load_plan
from .runlock import RunLock, RunLockBusy
from .store import Audit, ModeStore, utc_now
from .target import TargetCheckError, verify_target

log = logging.getLogger("sim.control")

E_RUN_LOCK_BUSY = "E_RUN_LOCK_BUSY"
E_BAD_PARAM = "E_BAD_PARAM"
E_STOP_POINT_MISMATCH = "E_STOP_POINT_MISMATCH"
E_RUN_ID_MISMATCH = "E_RUN_ID_MISMATCH"
E_CONFIRM_SEQ = "E_CONFIRM_SEQ_NOT_CURRENT"
E_NO_RUNNER = "E_NO_ACTIVE_RUNNER"


class ControlError(Exception):
    def __init__(self, code: str, message: str, detail: dict | None = None, status: int = 409):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}
        self.status = status

    def payload(self) -> dict:
        out = {"code": self.code, "message": self.message}
        if self.detail:
            out["detail"] = self.detail
        return out


class RealClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def wait(self, event: threading.Event, timeout: float | None) -> bool:
        return event.wait(timeout)


# ===================================================================== runner
class Runner(threading.Thread):
    """The single round. One thread, one plan, one run_id; it never starts another round."""

    GO, STOPPED = "GO", "STOPPED"

    def __init__(self, module: "ControlModule", mode: str, plan, run_id: str, token):
        super().__init__(daemon=True, name=f"ctl-run-{mode}")
        self.m = module
        self.mode = mode
        self.plan = plan
        self.run_id = run_id
        self.token = token
        self.store: ModeStore = module.stores[mode]
        self.clock = module.clock
        self.tick = module.cfg.tick_seconds
        self._wake = threading.Event()
        self._stop_req = False
        self._pause_req = False
        self._confirm_ack = False
        self._t0 = 0.0
        self._excluded = 0.0

    # ------------------------------------------------------------ external signals
    def signal(self, *, stop=False, pause=False, resume=False, confirm=False):
        if stop:
            self._stop_req = True
        if pause:
            self._pause_req = True
        if resume:
            self._pause_req = False
        if confirm:
            self._confirm_ack = True
        self._wake.set()

    # ------------------------------------------------------------ plan clock
    def _elapsed(self) -> float:
        return self.clock.monotonic() - self._t0 - self._excluded

    def _wait_until(self, offset: float) -> str:
        while True:
            if self._stop_req:
                return self.STOPPED
            if self._pause_req:
                if self._pause() == self.STOPPED:
                    return self.STOPPED
                continue
            remaining = offset - self._elapsed()
            if remaining <= 0:
                return self.GO
            self._wake.clear()
            self.clock.wait(self._wake, min(remaining, self.tick))

    def _pause(self) -> str:
        """已暂停: the remaining offset is preserved and the waiting time is never caught up."""
        started = self.clock.monotonic()
        self.m.set_state(self.mode, M.PAUSED)
        while self._pause_req and not self._stop_req:
            self._wake.clear()
            self.clock.wait(self._wake, None)
        self._excluded += self.clock.monotonic() - started
        if self._stop_req:
            return self.STOPPED
        self.m.set_state(self.mode, M.RUNNING)
        return self.GO

    def _await_stop_point(self, sp, index_done: int) -> str:
        """等待阶段确认: only the current stop point's own confirmation may let the round continue."""
        started = self.clock.monotonic()
        self._confirm_ack = False
        stop_point = {
            "stop_point_id": f"{self.run_id}:SP{sp.sequence:02d}",
            "run_id": self.run_id,
            "expected_confirm_seq": sp.sequence,
            **sp.as_dict(),
            "total_stop_points": len(self.plan.stop_points),
            "events_completed": index_done,
            "awaiting_since": utc_now(),
        }

        def apply(st):
            st["state"] = M.AWAITING_CONFIRM
            st["stop_point"] = stop_point

        self.store.mutate(apply)
        self.m.audit.write(event="stop_point_reached", mode=self.mode, run_id=self.run_id, stop_point_id=stop_point["stop_point_id"], sequence=sp.sequence)
        while not self._confirm_ack and not self._stop_req:
            self._wake.clear()
            self.clock.wait(self._wake, None)
        self._excluded += self.clock.monotonic() - started
        if self._stop_req and not self._confirm_ack:
            return self.STOPPED
        self._confirm_ack = False

        def done(st):
            st["state"] = M.RUNNING
            st["confirm_seq"] = sp.sequence
            st["confirmed_stop_points"] = sp.sequence
            st["stop_point"] = None

        self.store.mutate(done)
        return self.GO

    # ------------------------------------------------------------ the round
    def run(self):
        try:
            self.m.set_state(self.mode, M.RUNNING)
            self._t0 = self.clock.monotonic()
            if self.mode == "A":
                outcome = self._run_continuous()
            else:
                outcome = self._run_segments()
            if outcome == self.GO:
                self._finish_completed()
        except BaseException as e:  # never leave the state machine in a live state
            log.exception("control runner crashed")
            self._finish_aborted({"code": "RUNNER_CRASHED", "message": f"{type(e).__name__}: {e}", "at": utc_now()})
        finally:
            self.m.runner_finished(self)

    def _run_continuous(self) -> str:
        for i, step in enumerate(self.plan.steps):
            if self._wait_until(float(step.get("at", 0))) == self.STOPPED:
                return self._finish_stopped(i)
            if self._send(i, step, segment=0) == self.STOPPED:
                return self.STOPPED
        return self.GO

    def _run_segments(self) -> str:
        index = 0
        base = 0.0
        for sp, seg in zip(self.plan.stop_points, self.plan.segments):
            for step in seg:
                if self._wait_until(base + float(step.get("at", 0))) == self.STOPPED:
                    return self._finish_stopped(index)
                if self._send(index, step, segment=sp.sequence) == self.STOPPED:
                    return self.STOPPED
                index += 1
            length = sp.nominal_seconds if sp.nominal_seconds is not None else float(seg[-1].get("at", 0))
            base += float(length)
            # 最后停点也需确认 (V1.1 §1): every segment, including the last, waits for its own confirmation
            if self._await_stop_point(sp, index) == self.STOPPED:
                return self._finish_stopped(index)
        return self.GO

    # ------------------------------------------------------------ one event
    def _send(self, index: int, step: dict, segment: int) -> str:
        try:
            event = self.m.build_event(dict(step))
        except Exception as e:
            self._finish_aborted(
                {"code": "EVENT_BUILD_FAILED", "message": f"step {index}: {type(e).__name__}: {getattr(e, 'detail', e)}", "index": index, "at": utc_now()}
            )
            return self.STOPPED

        self.store.mutate(lambda st: st.update(inflight=1))
        try:
            record = self.m.send_once(event, self.m.target_url())
        except Exception as e:  # the sender itself failed: the real result is not known
            record = {
                "target": self.m.target_url(),
                "http_status": None,
                "transport_error": f"{type(e).__name__}: {e}",
                "transport_kind": "sender_exception",
                "response_complete": False,
                "attempt_at": utc_now(),
            }
        verdict = classify(
            record,
            event=event,
            step=step,
            known_event_ids=self.plan.soundproof_event_ids,
            approved_target=self.m.target_url(),
        )
        result = {
            "run_id": self.run_id,
            "mode": self.mode,
            "index": index,
            "segment": segment,
            "event_id": event.get("event_id"),
            "event_type": event.get("event_type"),
            "timestamp": event.get("timestamp"),
            "outcome": verdict["outcome"],
            "reason": verdict["reason"],
            "http_status": record.get("http_status"),
            "target": record.get("target"),
            "attempt_at": record.get("attempt_at"),
            "sent_at": record.get("sent_at"),
            "elapsed_ms": record.get("elapsed_ms"),
            "response_headers": record.get("response_headers"),
            "response_body": record.get("response_body"),
            "body_bytes": record.get("body_bytes"),
            "body_truncated": record.get("body_truncated"),
            "response_complete": record.get("response_complete"),
            "transport_error": record.get("transport_error"),
            "detail": verdict.get("detail"),
            "recorded_at": utc_now(),
        }
        self.store.append_result(result)

        def apply(st):
            st["inflight"] = 0
            st["progress"] = {
                "attempted": index + 1,
                "total": self.plan.total_events,
                "next_index": index + 1,
                "segment": segment,
            }
            st["last_result"] = {k: result[k] for k in ("index", "event_id", "event_type", "outcome", "reason", "http_status", "attempt_at", "elapsed_ms")}
            c = st["counts"]
            if verdict["outcome"] == ACCEPTED:
                c["accepted"] += 1
            elif verdict["outcome"] == KNOWN_REJECTED:
                c["known_rejected"] += 1
            else:
                c["unknown"] += 1

        self.store.mutate(apply)

        if verdict["outcome"] == UNKNOWN:
            self.store.append_unknown(result)
            self.m.audit.write(event="unknown_result_latched", mode=self.mode, run_id=self.run_id, index=index, reason=verdict["reason"], http_status=record.get("http_status"))
            self._finish_aborted(
                {
                    "code": "UNDETERMINED_TECHNICAL_RESULT",
                    "message": "实际技术结果未明或不属于已知类型拒绝：停止后续推进，保留实际结果，控制层不重发",
                    "reason": verdict["reason"],
                    "index": index,
                    "event_id": event.get("event_id"),
                    "http_status": record.get("http_status"),
                    "at": utc_now(),
                },
                latch=result,
            )
            return self.STOPPED
        return self.GO

    # ------------------------------------------------------------ terminal states
    # A terminal state is written only after the global run lock has been given back, so that the
    # moment a human sees 已停止 / 发送计划完成 / 异常停止 the next 初始化启动 is really acceptable.
    def _finish_stopped(self, index: int) -> str:
        def apply(st):
            st["state"] = M.STOPPING
            st["stop_point"] = None

        self.store.mutate(apply)
        # the controller only ever has one synchronous request in flight; by the time we are here it
        # has a determinate recorded result, so 停止处理中 resolves immediately to 已停止.
        pending = [u for u in (self.store.get().get("unknown_pending") or [])]
        self.m.runner_finished(self)

        def done(st):
            st["state"] = M.ABORTED if pending else M.STOPPED
            st["run_finished_at"] = utc_now()
            st["stop_point"] = None
            st["requires_manual_review"] = bool(pending)

        self.store.mutate(done)
        self.m.audit.write(event="round_stopped", mode=self.mode, run_id=self.run_id, attempted=index, state=self.store.get()["state"])
        return self.STOPPED

    def _finish_aborted(self, error: dict, latch: dict | None = None) -> None:
        self.m.runner_finished(self)

        def apply(st):
            st["state"] = M.ABORTED
            st["run_finished_at"] = utc_now()
            st["error"] = error
            st["inflight"] = 0
            st["stop_point"] = None
            st["requires_manual_review"] = True
            if latch is not None:
                st["unknown_pending"] = list(st.get("unknown_pending") or []) + [
                    {"reason": latch["reason"], "index": latch["index"], "event_id": latch["event_id"], "http_status": latch["http_status"], "at": latch["recorded_at"]}
                ]

        self.store.mutate(apply)
        self.m.audit.write(event="round_aborted", mode=self.mode, run_id=self.run_id, error=error.get("code"), reason=error.get("reason"))

    def _finish_completed(self) -> None:
        self.m.runner_finished(self)
        st = self.store.get()
        counts = st["counts"]
        determinate = counts["accepted"] + counts["known_rejected"]
        confirmed = st.get("confirmed_stop_points", 0)
        expected_sp = len(self.plan.stop_points)
        ok = (
            st["progress"]["attempted"] == self.plan.total_events
            and determinate == self.plan.total_events
            and counts["unknown"] == 0
            and confirmed == expected_sp
        )
        if not ok:
            self._finish_aborted(
                {
                    "code": "COMPLETION_PRECONDITION_NOT_MET",
                    "message": "末项已到但计划完成条件不成立（进度、结果归属或B停点确认不完整）",
                    "attempted": st["progress"]["attempted"],
                    "determinate": determinate,
                    "confirmed_stop_points": confirmed,
                    "expected_stop_points": expected_sp,
                    "at": utc_now(),
                }
            )
            return

        def apply(s):
            s["state"] = M.COMPLETED
            s["run_finished_at"] = utc_now()
            s["completed_with_known_rejections"] = counts["known_rejected"] > 0
            s["stop_point"] = None

        self.store.mutate(apply)
        self.m.audit.write(
            event="send_plan_completed",
            mode=self.mode,
            run_id=self.run_id,
            events=self.plan.total_events,
            known_rejected=counts["known_rejected"],
            confirmed_stop_points=confirmed,
        )


# ===================================================================== module
class ControlModule:
    def __init__(
        self,
        *,
        cfg: ControlConfig,
        run_lock: RunLock,
        dataset_name,
        dataset,
        build_event,
        send_once,
        clock=None,
        tls_verify: bool = True,
        http_get=None,
    ):
        self.cfg = cfg
        self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
        self.run_lock = run_lock
        self._dataset_name = dataset_name
        self._dataset = dataset
        self.build_event = build_event
        self.send_once = send_once
        self.clock = clock or RealClock()
        self.tls_verify = tls_verify
        self.http_get = http_get
        self.audit = Audit(cfg.state_dir / "audit.jsonl")
        self.stores = {m: ModeStore(cfg.state_dir, m) for m in MODES}
        self.challenges = ChallengeStore(cfg.challenge_ttl_s)
        self._mode_path = cfg.state_dir / "selected_mode.json"
        self.mode = self._load_mode()
        self._op_lock = threading.Lock()
        self._runner: Runner | None = None
        self._plan_cache: dict = {}

    # ------------------------------------------------------------ selected mode
    def _load_mode(self) -> str:
        try:
            import json

            with self._mode_path.open("r", encoding="utf-8") as f:
                m = json.load(f).get("mode")
            return m if m in MODES else "A"
        except Exception:
            return "A"

    def _save_mode(self, mode: str):
        import json

        tmp = self._mode_path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump({"mode": mode, "at": utc_now()}, f)
        tmp.replace(self._mode_path)

    # ------------------------------------------------------------ helpers
    def target_url(self) -> str | None:
        return self.cfg.approved_target_url

    def store(self, mode: str | None = None) -> ModeStore:
        return self.stores[mode or self.mode]

    def set_state(self, mode: str, state: str):
        self.stores[mode].mutate(lambda st: st.update(state=state))

    def fingerprint(self, mode: str | None = None) -> str:
        st = self.store(mode).get()
        return f"{mode or self.mode}:{st['state']}:{st.get('run_id')}:{st.get('confirm_seq')}"

    def runner_finished(self, runner: Runner):
        with self._op_lock:
            if self._runner is runner:
                self._runner = None
        self.run_lock.release(runner.token)

    # ------------------------------------------------------------ status
    def status(self) -> dict:
        st = self.store().get()
        plan = self._plan_cache.get(self.mode)
        return {
            "selected_mode": self.mode,
            "modes": {m: _mode_view(self.stores[m].get()) for m in MODES},
            "state": st["state"],
            "state_display": M.DISPLAY.get(st["state"], st["state"]),
            "run_id": st.get("run_id"),
            "plan": st.get("plan") or (plan.summary() if plan else None),
            "progress": st.get("progress"),
            "stop_point": st.get("stop_point"),
            "confirm_seq": st.get("confirm_seq"),
            "confirmed_stop_points": st.get("confirmed_stop_points"),
            "expected_stop_points": st.get("expected_stop_points"),
            "counts": st.get("counts"),
            "known_rejected": (st.get("counts") or {}).get("known_rejected"),
            "last_result": st.get("last_result"),
            "unknown_pending": st.get("unknown_pending"),
            "error": st.get("error"),
            "requires_manual_review": st.get("requires_manual_review"),
            "recovered_from": st.get("recovered_from"),
            "completed_with_known_rejections": st.get("completed_with_known_rejections"),
            "run_lock": {"locked": self.run_lock.locked(), "holder": self.run_lock.holder()},
            "allowed_ops": self.allowed_ops(),
            "pending_challenges": self.challenges.pending(),
            "target": {
                "approved_target_url": self.cfg.approved_target_url,
                "cv_target_url": self.cfg.cv_target_url,
                "readyz_url": self.cfg.resolved_readyz_url(),
                "expect_data_domain": self.cfg.expect_data_domain,
                "expect_artifact_prefix": self.cfg.expect_artifact_prefix,
            },
            "dataset": self._dataset_name(),
            "scope_notes": [
                "技术运行状态；不产生业务测试报告、截图档案、问题工单或通过/失败判定",
                "发送计划完成只表示计划已执行且技术结果有归属，不代表业务测试通过或平台终点成立",
            ],
            "at": utc_now(),
        }

    def allowed_ops(self) -> dict:
        st = self.store().get()
        out = {}
        for op in M.OPS:
            err = M.transition_error(
                op,
                st["state"],
                mode=self.mode,
                unknown_pending=len(st.get("unknown_pending") or []),
                inflight=st.get("inflight") or 0,
            )
            out[op] = err is None
        return out

    # ------------------------------------------------------------ two-phase operations
    def request(self, op: str, params: dict) -> dict:
        params = params or {}
        with self._op_lock:
            self._check_op(op, params)
            summary = self._summary(op, params)
            ch = self.challenges.issue(op, params, self.fingerprint(), summary)
        self.audit.write(event="op_requested", op=op, mode=self.mode, challenge_id=ch.id, params=_safe(params))
        return {
            "op": op,
            "challenge_id": ch.id,
            "expires_at": round(ch.expires, 3),
            "expires_in_s": round(ch.expires - time.time(), 1),
            "summary": summary,
            "confirm_endpoint": f"/_ctl/{op}/confirm",
            "cancel_endpoint": f"/_ctl/{op}/cancel",
        }

    def cancel(self, op: str, challenge_id: str) -> dict:
        try:
            self.challenges.cancel(challenge_id, op)
        except ChallengeError as e:
            self.audit.write(event="op_cancel_refused", op=op, code=e.code, challenge_id=challenge_id)
            raise ControlError(e.code, "challenge could not be cancelled", e.detail)
        self.audit.write(event="op_cancelled", op=op, mode=self.mode, challenge_id=challenge_id)
        return {"op": op, "cancelled": True, "state_changed": False, "state": self.store().get()["state"]}

    def confirm(self, op: str, challenge_id: str, params: dict | None = None) -> dict:
        with self._op_lock:
            try:
                ch = self.challenges.take(challenge_id, op, self.fingerprint())
            except ChallengeError as e:
                self.audit.write(event="op_refused", op=op, mode=self.mode, code=e.code, challenge_id=challenge_id, detail=e.detail)
                if op == M.CONFIRM_STOP_POINT:
                    self._note_rejected_confirm(e.code)
                raise ControlError(e.code, "二次确认无效：确认码错误、已使用、已过期或状态已变化", e.detail)
            merged = dict(ch.params)
            for k, v in (params or {}).items():
                if k != "challenge_id" and v is not None:
                    if k in merged and merged[k] != v:
                        self.audit.write(event="op_refused", op=op, mode=self.mode, code=E_BAD_PARAM, field=k)
                        raise ControlError(E_BAD_PARAM, f"confirm parameter {k!r} contradicts the requested operation", {"requested": merged[k], "confirmed": v})
                    merged[k] = v
            self._check_op(op, merged)
            handler = {
                M.SELECT_MODE: self._do_select_mode,
                M.INIT_START: self._do_init_start,
                M.PAUSE: self._do_pause,
                M.RESUME: self._do_resume,
                M.STOP: self._do_stop,
                M.CONFIRM_STOP_POINT: self._do_confirm_stop_point,
                M.CLEAR_UNKNOWN: self._do_clear_unknown,
            }[op]
            result = handler(merged)
        self.audit.write(event="op_confirmed", op=op, mode=self.mode, challenge_id=challenge_id, params=_safe(merged), result=_safe(result))
        out = {"op": op, "performed": True, **result}
        out["status"] = self.status()
        return out

    # ------------------------------------------------------------ guards
    def _check_op(self, op: str, params: dict):
        if op not in M.OPS:
            raise ControlError(M.E_UNKNOWN_OP, f"unknown operation {op!r}", status=404)
        st = self.store().get()
        err = M.transition_error(
            op,
            st["state"],
            mode=self.mode,
            unknown_pending=len(st.get("unknown_pending") or []),
            inflight=st.get("inflight") or 0,
        )
        if err is not None:
            self.audit.write(event="op_refused", op=op, mode=self.mode, state=st["state"], code=err)
            if op == M.CONFIRM_STOP_POINT:
                self._note_rejected_confirm(err)
            raise ControlError(
                err,
                f"操作 {op} 在当前状态 {st['state']}（{M.DISPLAY.get(st['state'])}）下不被接受",
                {
                    "state": st["state"],
                    "state_display": M.DISPLAY.get(st["state"]),
                    "unknown_pending": len(st.get("unknown_pending") or []),
                    "inflight": st.get("inflight") or 0,
                },
            )
        if op == M.SELECT_MODE:
            if params.get("mode") not in MODES:
                raise ControlError(E_BAD_PARAM, "mode must be 'A' or 'B'", {"mode": params.get("mode")}, status=400)
        if op == M.CONFIRM_STOP_POINT:
            try:
                self._check_stop_point_params(st, params)
            except ControlError as e:
                self.audit.write(event="op_refused", op=op, mode=self.mode, state=st["state"], code=e.code, detail=e.detail)
                self._note_rejected_confirm(e.code)
                raise

    def _check_stop_point_params(self, st: dict, params: dict):
        sp = st.get("stop_point") or {}
        for field in ("stop_point_id", "run_id", "confirm_seq"):
            if params.get(field) in (None, ""):
                raise ControlError(E_BAD_PARAM, f"{field} is required to confirm a B stop point", {"required": ["stop_point_id", "run_id", "confirm_seq"]}, status=400)
        if params["run_id"] != st.get("run_id"):
            raise ControlError(E_RUN_ID_MISMATCH, "确认携带的 run_id 不是当前轮次", {"current": st.get("run_id"), "given": params["run_id"]})
        if params["stop_point_id"] != sp.get("stop_point_id"):
            raise ControlError(E_STOP_POINT_MISMATCH, "确认的不是当前停点（旧、重复或错阶段确认不放行）", {"current": sp.get("stop_point_id"), "given": params["stop_point_id"]})
        try:
            seq = int(params["confirm_seq"])
        except (TypeError, ValueError):
            raise ControlError(E_BAD_PARAM, "confirm_seq must be an integer", {"given": params["confirm_seq"]}, status=400)
        expected = sp.get("expected_confirm_seq")
        if seq != expected or seq <= int(st.get("confirm_seq") or 0):
            raise ControlError(
                E_CONFIRM_SEQ,
                "confirm_seq 必须是当前停点序号且严格大于上一次确认序号",
                {"expected": expected, "given": seq, "last_confirmed": st.get("confirm_seq")},
            )

    def _note_rejected_confirm(self, code: str):
        """旧、重复或错阶段确认一律留痕计数；它永远不会放行下一停点。"""

        def apply(st):
            st["counts"]["rejected_confirms"] = int(st["counts"].get("rejected_confirms") or 0) + 1
            st["last_rejected_confirm"] = {"code": code, "at": utc_now()}

        self.stores[self.mode].mutate(apply)

    def _summary(self, op: str, params: dict) -> dict:
        st = self.store().get()
        base = {
            "op": op,
            "mode": self.mode,
            "current_state": st["state"],
            "current_state_display": M.DISPLAY.get(st["state"]),
            "effect": {
                M.SELECT_MODE: f"仅切换模式为 {params.get('mode')}（不自动启动本轮）",
                M.INIT_START: "模拟器自身初始化并开始该模式的一轮（一次点击，无第二个开始按钮）",
                M.PAUSE: "暂停后续计划推进（不撤回已发事件，不冻结下游）",
                M.RESUME: "沿原剩余偏移继续（不跨过 B 停点）",
                M.STOP: "停止本轮后续计划（不撤回已发事件，不清空平台，不自动重播）",
                M.CONFIRM_STOP_POINT: "确认当前 B 停点并继续下一段（仅代表允许继续，不代表业务测试通过）",
                M.CLEAR_UNKNOWN: "登记在途/未知结果已由技术人员核清（留痕保留，不删除实际结果）",
            }[op],
        }
        if op == M.CONFIRM_STOP_POINT:
            base["stop_point"] = st.get("stop_point")
        if op == M.CLEAR_UNKNOWN:
            base["unknown_pending"] = st.get("unknown_pending")
        if op == M.INIT_START:
            base["plan_preview"] = {"mode": self.mode, "speed_ratio": f"1:{self.cfg.speed_a if self.mode == 'A' else self.cfg.speed_b}"}
        return base

    # ------------------------------------------------------------ operations
    def _do_select_mode(self, params: dict) -> dict:
        mode = params["mode"]
        previous, self.mode = self.mode, mode
        self._save_mode(mode)
        return {"mode": mode, "previous_mode": previous, "auto_started": False, "state": self.stores[mode].get()["state"]}

    def _do_init_start(self, params: dict) -> dict:
        mode = self.mode
        try:
            token = self.run_lock.acquire(f"control:{mode}:round")
        except RunLockBusy as e:
            self.audit.write(event="op_refused", op=M.INIT_START, mode=mode, code=E_RUN_LOCK_BUSY, holder=e.holder)
            raise ControlError(E_RUN_LOCK_BUSY, "已有活动发送任务占用全局运行锁，不创建第二个任务", {"holder": e.holder})
        try:
            # 核对阶段：数据集/场景/计数 与 目标身份。失败不分配 run_id、不发送任何事件。
            plan = load_plan(self._dataset(), self._dataset_name(), mode, self.cfg)
            evidence = verify_target(self.cfg, http_get=self.http_get, tls_verify=self.tls_verify)
        except PlanError as e:
            self.run_lock.release(token)
            self.audit.write(event="init_refused", mode=mode, code=e.code, detail=e.detail)
            raise ControlError(e.code, f"初始化核对失败，未分配轮次、未发送任何事件：{e.message}", e.detail)
        except TargetCheckError as e:
            self.run_lock.release(token)
            self.audit.write(event="init_refused", mode=mode, code=e.code, detail=e.detail)
            raise ControlError(e.code, f"目标身份校验失败，未分配轮次、未发送任何事件：{e.message}", e.detail)
        except Exception as e:
            self.run_lock.release(token)
            self.audit.write(event="init_refused", mode=mode, code="E_INIT_CHECK_FAILED", detail={"error": f"{type(e).__name__}: {e}"})
            raise ControlError("E_INIT_CHECK_FAILED", f"初始化核对失败，未分配轮次、未发送任何事件：{type(e).__name__}: {e}")

        run_id = f"R-{mode}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{secrets.token_hex(3)}"
        self._plan_cache[mode] = plan

        def apply(st):
            st["state"] = M.INITIALIZING
            st["run_id"] = run_id
            st["run_started_at"] = utc_now()
            st["run_finished_at"] = None
            st["plan"] = {**plan.summary(), "target": self.target_url(), "target_evidence": evidence}
            st["progress"] = {"attempted": 0, "total": plan.total_events, "next_index": 0, "segment": 0}
            st["confirm_seq"] = 0            # B: 清除上一轮确认状态
            st["confirmed_stop_points"] = 0
            st["expected_stop_points"] = len(plan.stop_points)
            st["stop_point"] = None
            st["counts"] = {"accepted": 0, "known_rejected": 0, "unknown": 0, "rejected_confirms": 0}
            st["last_result"] = None
            st["inflight"] = 0
            st["error"] = None
            st["recovered_from"] = None
            st["requires_manual_review"] = False
            st.pop("completed_with_known_rejections", None)

        self.stores[mode].mutate(apply)
        self.audit.write(event="round_initialised", mode=mode, run_id=run_id, plan=plan.summary(), target=self.target_url(), readyz=evidence.get("readyz"))
        runner = Runner(self, mode, plan, run_id, token)
        self._runner = runner
        runner.start()
        return {"run_id": run_id, "plan": plan.summary(), "target_evidence": evidence}

    def _require_runner(self) -> Runner:
        r = self._runner
        if r is None or not r.is_alive():
            raise ControlError(E_NO_RUNNER, "当前没有活动轮次线程；状态需人工核查", {"state": self.store().get()["state"]})
        return r

    def _do_pause(self, params: dict) -> dict:
        self._require_runner().signal(pause=True)
        return {"paused": True}

    def _do_resume(self, params: dict) -> dict:
        # 普通继续永远不推进 B 停点：这里只解除暂停，不触发 confirm。
        self._require_runner().signal(resume=True)
        return {"resumed": True, "passes_stop_point": False}

    def _do_stop(self, params: dict) -> dict:
        st = self.store().get()
        if st["state"] == M.INITIALIZING and self._runner is None:
            self.stores[self.mode].mutate(lambda s: s.update(state=M.STOPPED, run_finished_at=utc_now()))
            return {"stopping": False, "stopped": True}
        self._require_runner().signal(stop=True)
        return {"stopping": True}

    def _do_confirm_stop_point(self, params: dict) -> dict:
        runner = self._require_runner()
        st = self.store().get()
        sp = st.get("stop_point") or {}
        runner.signal(confirm=True)
        return {"confirmed_stop_point_id": sp.get("stop_point_id"), "sequence": sp.get("expected_confirm_seq"), "of": sp.get("total_stop_points")}

    def _do_clear_unknown(self, params: dict) -> dict:
        mode = self.mode
        st = self.stores[mode].get()
        pending = list(st.get("unknown_pending") or [])
        note = params.get("note") or ""
        cleared = {"cleared_at": utc_now(), "cleared_items": pending, "note": str(note)[:500], "run_id": st.get("run_id")}
        self.stores[mode].append_unknown({"record": "CLEARED_BY_TECHNICIAN", **cleared})

        def apply(s):
            s["unknown_pending"] = []
            s["unknown_cleared"] = list(s.get("unknown_cleared") or []) + [cleared]
            s["requires_manual_review"] = False

        self.stores[mode].mutate(apply)
        return {"cleared": len(pending), "evidence_retained": True}

    # ------------------------------------------------------------ read-only extras
    def plan_stop_points(self, mode: str | None = None) -> list[dict]:
        plan = self._plan_cache.get(mode or self.mode)
        return [sp.as_dict() for sp in plan.stop_points] if plan else []

    def results(self, n: int = 50, mode: str | None = None) -> list[dict]:
        return self.store(mode).results(n)

    def unknowns(self, mode: str | None = None) -> list[dict]:
        return self.store(mode).unknowns()

    def shutdown(self):
        r = self._runner
        if r is not None and r.is_alive():
            r.signal(stop=True)


def _mode_view(st: dict) -> dict:
    return {
        "state": st["state"],
        "state_display": M.DISPLAY.get(st["state"], st["state"]),
        "run_id": st.get("run_id"),
        "progress": st.get("progress"),
        "confirmed_stop_points": st.get("confirmed_stop_points"),
        "expected_stop_points": st.get("expected_stop_points"),
        "counts": st.get("counts"),
        "unknown_pending": len(st.get("unknown_pending") or []),
        "requires_manual_review": st.get("requires_manual_review"),
    }


def _safe(obj):
    """Audit payloads never carry credentials; keep them small and JSON-clean."""
    import json

    try:
        txt = json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:
        return {"unserialisable": str(type(obj))}
    return json.loads(txt) if len(txt) <= 4000 else {"truncated": txt[:4000]}


# ===================================================================== router
def build_control_router(control: ControlModule, guard) -> APIRouter:
    """`guard(request)` must raise for unauthorised callers (admin token + source CIDR)."""
    router = APIRouter(prefix="/_ctl")

    def _raise(e: ControlError):
        raise HTTPException(status_code=e.status, detail=e.payload())

    @router.get("/status")
    def ctl_status(request: Request):
        guard(request)
        return control.status()

    @router.get("/results")
    def ctl_results(request: Request, n: int = 50, mode: str | None = None):
        guard(request)
        if mode is not None and mode not in MODES:
            raise HTTPException(status_code=400, detail="mode must be A or B")
        return control.results(n, mode)

    @router.get("/unknown")
    def ctl_unknown(request: Request, mode: str | None = None):
        guard(request)
        if mode is not None and mode not in MODES:
            raise HTTPException(status_code=400, detail="mode must be A or B")
        return control.unknowns(mode)

    @router.get("/audit")
    def ctl_audit(request: Request, n: int = 100):
        guard(request)
        return control.audit.recent(n)

    @router.get("/stop-points")
    def ctl_stop_points(request: Request):
        guard(request)
        return {"mode": control.mode, "stop_points": control.plan_stop_points()}

    @router.post("/{op}/request")
    def ctl_request(op: str, request: Request, body: dict | None = None):
        guard(request)
        try:
            return control.request(op, body or {})
        except ControlError as e:
            _raise(e)

    @router.post("/{op}/confirm")
    def ctl_confirm(op: str, request: Request, body: dict | None = None):
        guard(request)
        body = body or {}
        try:
            return control.confirm(op, body.get("challenge_id"), body)
        except ControlError as e:
            _raise(e)

    @router.post("/{op}/cancel")
    def ctl_cancel(op: str, request: Request, body: dict | None = None):
        guard(request)
        body = body or {}
        try:
            return control.cancel(op, body.get("challenge_id"))
        except ControlError as e:
            _raise(e)

    @router.get("/", response_class=HTMLResponse)
    def ctl_page(request: Request):
        return HTMLResponse(render_page())

    return router
