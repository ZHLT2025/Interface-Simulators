"""Fixtures for the isolated manual-single-round control tests.

Everything runs inside the container against 127.0.0.1 only (`docker run --network none`):
no request ever leaves the test process' loopback interface and no deployed system is touched.
"""
from __future__ import annotations

import importlib
import json
import os
import threading
from pathlib import Path

import pytest

from .replica import Replica

EVENT_TYPES_CYCLE = ("ENTER", "LEAVE", "SPARK", "PERSON_COUNT")
SOUNDPROOF = "SOUNDPROOF_COVER_MOVE_COMPLETED"


# ----------------------------------------------------------------- virtual clock
class VirtualClock:
    """Plan-time clock for the tests: a bounded wait jumps straight to its deadline, an unbounded
    wait (a B stop point, a pause) really blocks on the event. The runner therefore honours the
    *order* and the *relative offsets* of the plan without the suite waiting 1:240 wall-clock time;
    the production code always uses the real clock (no speed knob exists)."""

    def __init__(self):
        self._t = 0.0
        self.jumps = 0

    def monotonic(self) -> float:
        return self._t

    def wait(self, event: threading.Event, timeout):
        if timeout is None:
            return event.wait()
        if event.is_set():
            return True
        self._t += float(timeout)
        self.jumps += 1
        return event.is_set()


# ----------------------------------------------------------------- synthetic plans
def _step(i: int, at: float, event_type: str, event_id: str) -> dict:
    cam = f"CAM-{(i % 20) + 1:02d}"
    roi = f"ROI_{(i % 20) + 1:02d}_02_01"
    return {
        "at": at,
        "event_id": event_id,
        "event_type": event_type,
        "operation": "创建",
        "timestamp": f"2026-03-0{(i % 5) + 1}T0{(i % 8) + 1}:0{i % 6}:00.000Z",
        "camera_id": cam,
        "roi_id": roi,
        "roi_name": "进料车",
        "targets": [{"cid": 5, "confidence": 0.98, "bbox": {"x": 1.0, "y": 2.0, "w": 3.0, "h": 4.0}, "center": {"x": 2.5, "y": 4.0}, "evidence_ref": {"file_id": "frame-001"}}],
    }


def write_synthetic_dataset(
    root: Path,
    dataset: str = "synth",
    *,
    segments=(2, 2, 2),
    soundproof_positions=(1, 4),
    offsets=None,
) -> dict:
    """Build a dataset directory with <ds>_A240 and <ds>_B060_pNN scenarios carrying the same
    events in the same order (the real package guarantees exactly that)."""
    d = root / dataset
    d.mkdir(parents=True, exist_ok=True)
    total = sum(segments)
    steps, seq = [], 40400000000000000
    for i in range(total):
        et = SOUNDPROOF if i in soundproof_positions else EVENT_TYPES_CYCLE[i % len(EVENT_TYPES_CYCLE)]
        steps.append(_step(i, 0.0, et, str(seq + i)))
    if offsets is not None:
        for s, at in zip(steps, offsets):
            s["at"] = at
    scenarios = {f"{dataset}_A240": {"steps": [dict(s) for s in steps]}}
    idx = 0
    b_segments = []
    for n, count in enumerate(segments, start=1):
        seg = [dict(s) for s in steps[idx : idx + count]]
        base = seg[0]["at"]
        for s in seg:
            s["at"] = round(s["at"] - base, 6)
        scenarios[f"{dataset}_B060_p{n:02d}"] = {"steps": seg}
        b_segments.append(
            {
                "sequence": n,
                "name": f"{dataset}_B060_p{n:02d}",
                "event_count": count,
                "nominal_seconds": float(seg[-1]["at"]),
                "business_interval_start_utc": "2026-03-01T00:00:00Z",
                "business_interval_end_utc": "2026-03-01T01:00:00Z",
                "stop_evidence": [{"interval_id": f"synth:{n}", "area_id": "S-QL-01"}] if n < len(segments) else [{"kind": "last_actual_cv_event"}],
            }
        )
        idx += count
    with (d / "scenarios.json").open("w", encoding="utf-8") as f:
        json.dump({"scenarios": scenarios}, f, ensure_ascii=False)
    manifest = {
        "dataset_id": dataset,
        "continuous": [
            {"name": f"{dataset}_A240", "speed": 240, "event_count": total, "nominal_seconds": float(steps[-1]["at"])},
            {"name": f"{dataset}_B060", "speed": 60, "event_count": total, "nominal_seconds": float(steps[-1]["at"]) * 4},
        ],
        "B_segments": b_segments,
    }
    with (d / "manifest.json").open("w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False)
    return {"dir": d, "dataset": dataset, "total": total, "segments": len(segments), "soundproof": len(soundproof_positions), "steps": steps}


# ----------------------------------------------------------------- session wiring
@pytest.fixture(scope="session")
def replica():
    r = Replica()
    yield r
    r.close()


@pytest.fixture(scope="session")
def cvapp(replica, tmp_path_factory):
    """Import cv.app once, configured against the local replica. Module-level configuration is read
    at import time, so the environment is set first."""
    state = tmp_path_factory.mktemp("cvstate")
    os.environ.update(
        SIM_ADMIN_TOKEN="test-admin-token",
        SIM_STATE_DIR=str(state),
        SIM_DATASET="sample",
        CV_TARGET_URL=replica.events_url,
        CV_TARGET_ALLOW_HOSTS="127.0.0.1",
        CV_MAX_RETRIES="0",
        CV_HTTP_TIMEOUT_SECONDS="0.6",
        CV_TLS_VERIFY="false",
        CV_PUBLIC_BASE_URL="http://127.0.0.1:18004",
        SIM_CONTROL_APPROVED_TARGET_URL=replica.events_url,
        SIM_CONTROL_TARGET_READYZ_URL=replica.readyz_url,
        SIM_CONTROL_EXPECT_EVENTS="6",
        SIM_CONTROL_EXPECT_B_STOP_POINTS="3",
        SIM_CONTROL_EXPECT_SOUNDPROOF="2",
        SIM_CONTROL_STATE_DIR=str(state / "control"),
        CV_SINK_BASIC_USER="",
        CV_SINK_BASIC_PASS="",
    )
    mod = importlib.import_module("cv.app")
    return mod


@pytest.fixture
def control_factory(cvapp, replica, tmp_path):
    """Build an independent ControlModule (own state dir, own expectations) for one test."""
    from simcore.control import ControlModule
    from simcore.control.config import ControlConfig
    from simcore.control.runlock import RunLock
    from simcore.datasets import Dataset

    created = []

    def make(*, dataset_info=None, state_dir=None, clock=None, run_lock=None, **cfg_kwargs):
        info = dataset_info or write_synthetic_dataset(tmp_path / "data")
        sd = Path(state_dir or (tmp_path / f"ctl{len(created)}"))
        defaults = dict(
            state_dir=sd,
            cv_target_url=replica.events_url,
            approved_target_url=replica.events_url,
            readyz_url=replica.readyz_url,
            expect_events=info.get("total"),
            expect_b_stop_points=info.get("segments"),
            expect_soundproof=info.get("soundproof"),
            tick_seconds=0.05,
        )
        defaults.update(cfg_kwargs)
        cfg = ControlConfig(**defaults)
        ds = Dataset(info["dir"])
        m = ControlModule(
            cfg=cfg,
            run_lock=run_lock or RunLock(sd / "run.lock"),
            dataset_name=lambda: info["dataset"],
            dataset=lambda: ds,
            build_event=cvapp.make_event,
            send_once=lambda event, target: cvapp.pusher.send_once(event, target),
            clock=clock,
            tls_verify=False,
        )
        m._test_info = info
        created.append(m)
        return m

    yield make
    for m in created:
        m.shutdown()


def do(module, op, **params):
    """The two-phase human operation as the page performs it: request -> confirm."""
    req = module.request(op, params)
    return module.confirm(op, req["challenge_id"], params)


def wait_for(fn, timeout=8.0, interval=0.005):
    """Poll a predicate of the live state (the runner is a real thread)."""
    import time as _t

    end = _t.monotonic() + timeout
    while _t.monotonic() < end:
        v = fn()
        if v:
            return v
        _t.sleep(interval)
    return fn()


def wait_state(module, state, timeout=8.0):
    got = wait_for(lambda: module.store().get()["state"] == state, timeout)
    assert got, f"state did not become {state}; current={module.store().get()['state']} error={module.store().get().get('error')}"
    return module.store().get()


def wait_stop_point(module, k, timeout=20.0):
    """Wait until the round is waiting at B stop point number `k` (and not at the previous one)."""
    from simcore.control import machine as _M

    def ready():
        st = module.store().get()
        sp = st.get("stop_point") or {}
        return sp if st["state"] == _M.AWAITING_CONFIRM and sp.get("sequence") == k else None

    sp = wait_for(ready, timeout)
    st = module.store().get()
    assert sp, f"stop point {k} not reached; state={st['state']} sp={st.get('stop_point')} error={st.get('error')}"
    return sp


@pytest.fixture
def v11_cv_dir():
    """The frozen V1.1 CV inputs, mounted read-only into the test container (optional)."""
    p = os.environ.get("V11_CV_DIR")
    if not p or not Path(p).is_dir():
        pytest.skip("V11_CV_DIR not mounted; the formal 318/62 plan tests need the V1.1 package")
    return Path(p)
