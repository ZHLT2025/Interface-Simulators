"""File-backed state for the single-round controller.

Layout (SIM_CONTROL_STATE_DIR, default <SIM_STATE_DIR>/control):

    <root>/run.lock              global run lock (flock target, see runlock.py)
    <root>/run.holder.json       current lock holder (diagnostics only)
    <root>/audit.jsonl           append-only audit of every human operation and refusal
    <root>/A/state.json          mode A round state         (A and B are fully independent)
    <root>/A/results.jsonl       append-only per-event technical result
    <root>/A/unknown.jsonl       append-only latch of results whose meaning is not determined
    <root>/B/...                 the same for mode B

Nothing here is ever rewritten in place: state.json is written to a temp file and renamed, the
jsonl files are only appended to. A previous round's technical errors stay retrievable and are
never re-labelled as success (V1.1 §3 保持内容).
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .machine import IDLE, recovered_state


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_state(mode: str) -> dict:
    return {
        "mode": mode,
        "state": IDLE,
        "run_id": None,
        "run_started_at": None,
        "run_finished_at": None,
        "plan": None,
        "progress": {"attempted": 0, "total": 0, "next_index": 0, "segment": 0},
        "confirm_seq": 0,
        "stop_point": None,
        "confirmed_stop_points": 0,
        "expected_stop_points": 0,
        "counts": {"accepted": 0, "known_rejected": 0, "unknown": 0, "rejected_confirms": 0},
        "last_result": None,
        "unknown_pending": [],
        "inflight": 0,
        "error": None,
        "requires_manual_review": False,
        "recovered_from": None,
        "updated_at": utc_now(),
    }


class ModeStore:
    """State of one mode (A or B). Thread-safe; a single process owns the directory."""

    def __init__(self, root: Path, mode: str):
        self.mode = mode
        self.dir = Path(root) / mode
        self.dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.dir / "state.json"
        self.results_path = self.dir / "results.jsonl"
        self.unknown_path = self.dir / "unknown.jsonl"
        self._lock = threading.RLock()
        self._state = self._load_or_recover()

    # ----------------------------------------------------------- load / recover
    def _load_or_recover(self) -> dict:
        if not self.state_path.exists():
            st = new_state(self.mode)
            self._write(st)
            return st
        try:
            with self.state_path.open("r", encoding="utf-8") as f:
                st = json.load(f)
        except Exception:
            st = new_state(self.mode)
            st["error"] = {"code": "STATE_FILE_UNREADABLE", "message": f"{self.state_path} could not be parsed"}
            st["requires_manual_review"] = True
            self._write(st)
            return st
        base = new_state(self.mode)
        base.update({k: v for k, v in st.items() if k in base})
        base["mode"] = self.mode
        dirty = False
        if base.get("inflight"):
            # a request was in flight when the process died: its real technical result is unknown and
            # must be cleared by a technician before another round may be initialised (V1.1 §1 再次启动)
            base["unknown_pending"] = list(base.get("unknown_pending") or []) + [
                {
                    "reason": "INDETERMINATE_INFLIGHT_AT_RESTART",
                    "run_id": base.get("run_id"),
                    "index": (base.get("progress") or {}).get("next_index"),
                    "at": utc_now(),
                }
            ]
            base["inflight"] = 0
            base["requires_manual_review"] = True
            dirty = True
        recovered, reason = recovered_state(base["state"])
        if reason is not None:
            # V1.1 §2: 进程恢复不能自动开始第二轮；恢复不明执行位置时保持停止并提示人工核查
            base["recovered_from"] = base["state"]
            base["state"] = recovered
            base["requires_manual_review"] = True
            base["inflight"] = 0
            base["error"] = {
                "code": reason,
                "message": "进程恢复时发现上一轮处于活动状态，执行位置不明：保持停止，等待人工核查后再由人工重新初始化启动",
                "at": utc_now(),
            }
            dirty = True
        if dirty:
            base["updated_at"] = utc_now()
            self._write(base)
        return base

    # ----------------------------------------------------------- accessors
    def get(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._state, ensure_ascii=False))

    def mutate(self, fn):
        """fn(state) mutates the live dict in place; the result is persisted atomically."""
        with self._lock:
            fn(self._state)
            self._state["updated_at"] = utc_now()
            self._write(self._state)
            return json.loads(json.dumps(self._state, ensure_ascii=False))

    def _write(self, state: dict):
        tmp = self.state_path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(self.state_path)

    # ----------------------------------------------------------- append-only logs
    def append_result(self, rec: dict):
        _append(self.results_path, rec)

    def append_unknown(self, rec: dict):
        _append(self.unknown_path, rec)

    def results(self, n: int = 50, run_id: str | None = None) -> list[dict]:
        rows = _read(self.results_path)
        if run_id:
            rows = [r for r in rows if r.get("run_id") == run_id]
        return rows[-n:]

    def unknowns(self, run_id: str | None = None) -> list[dict]:
        rows = _read(self.unknown_path)
        if run_id:
            rows = [r for r in rows if r.get("run_id") == run_id]
        return rows


class Audit:
    """Append-only audit of human operations, refusals and lock conflicts (technical log only)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def write(self, **fields) -> dict:
        rec = {"at": utc_now(), "ts": round(time.time(), 3), **fields}
        with self._lock:
            _append(self.path, rec)
        return rec

    def recent(self, n: int = 100) -> list[dict]:
        return _read(self.path)[-n:]


def _append(path: Path, rec: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def _read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                out.append({"unparseable_line": line[:500]})
    return out
