"""Read-only loading and checking of the frozen A/B replay plan (V1.1 §3 数据集与场景核对).

Source of truth on disk is the CV dataset the simulator already serves:

    <CV_DATA_ROOT>/<dataset>/scenarios.json
        scenarios["<dataset>_A240"].steps          318 events, offsets on the 1:240 timeline
        scenarios["<dataset>_B060_p01".."p62"]     the same 318 events pre-cut into 62 B segments,
                                                   offsets relative to the segment start
    <CV_DATA_ROOT>/<dataset>/manifest.json         optional: the 剧本包 cv/manifests/<dataset>.json
                                                   (B_segments[].stop_evidence, continuous[].speed)

The loader never mutates anything and never invents a plan: the B stop points are the pre-cut
segments themselves, so the 62 stop points cannot be silently reduced, merged or re-timed. The
manifest, when present, is used for the stop-point evidence labels *and* cross-checked against
the scenarios; a contradiction is a load error, not a silent preference.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .config import SOUNDPROOF_EVENT_TYPE


class PlanError(Exception):
    def __init__(self, code: str, message: str, detail: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, **({"detail": self.detail} if self.detail else {})}


@dataclass
class StopPoint:
    sequence: int
    name: str
    event_count: int
    nominal_seconds: float | None = None
    business_interval_start_utc: str | None = None
    business_interval_end_utc: str | None = None
    stop_evidence: list = field(default_factory=list)

    def label(self) -> str:
        ev = self.stop_evidence[0] if self.stop_evidence else {}
        if ev.get("interval_id"):
            return str(ev["interval_id"])
        if ev.get("kind"):
            return str(ev["kind"])
        return self.name

    def as_dict(self) -> dict:
        return {
            "sequence": self.sequence,
            "name": self.name,
            "event_count": self.event_count,
            "label": self.label(),
            "nominal_seconds": self.nominal_seconds,
            "business_interval_start_utc": self.business_interval_start_utc,
            "business_interval_end_utc": self.business_interval_end_utc,
            "stop_evidence": self.stop_evidence,
        }


@dataclass
class Plan:
    mode: str
    dataset: str
    name: str
    speed: int
    steps: list = field(default_factory=list)                 # flat, plan order
    segments: list = field(default_factory=list)              # mode B: list[list[step]]
    stop_points: list = field(default_factory=list)           # mode B: list[StopPoint]
    soundproof_event_ids: tuple = ()
    manifest_present: bool = False
    nominal_seconds: float | None = None

    @property
    def total_events(self) -> int:
        return len(self.steps)

    def summary(self) -> dict:
        return {
            "mode": self.mode,
            "dataset": self.dataset,
            "scenario": self.name,
            "speed_ratio": f"1:{self.speed}",
            "events": self.total_events,
            "stop_points": len(self.stop_points),
            "soundproof_events": len(self.soundproof_event_ids),
            "manifest_present": self.manifest_present,
            "nominal_seconds": self.nominal_seconds,
        }


def _scenarios(ds) -> dict:
    data = ds.load("scenarios", {"scenarios": {}})
    sc = data.get("scenarios") if isinstance(data, dict) else None
    if not isinstance(sc, dict):
        raise PlanError("E_SCENARIOS_MISSING", "scenarios.json does not contain a 'scenarios' object")
    return sc


def _steps(sc: dict, name: str) -> list:
    scen = sc.get(name)
    if scen is None:
        raise PlanError("E_SCENARIO_NOT_FOUND", f"scenario {name!r} not found in the active CV dataset", {"scenario": name})
    steps = scen.get("steps")
    if not isinstance(steps, list) or not steps:
        raise PlanError("E_SCENARIO_EMPTY", f"scenario {name!r} has no steps", {"scenario": name})
    last = None
    for i, st in enumerate(steps):
        if not isinstance(st, dict) or not st.get("event_id") or not st.get("event_type"):
            raise PlanError("E_STEP_INVALID", f"scenario {name!r} step {i} lacks event_id/event_type", {"scenario": name, "index": i})
        at = st.get("at", 0)
        if not isinstance(at, (int, float)) or isinstance(at, bool):
            raise PlanError("E_STEP_OFFSET", f"scenario {name!r} step {i} has a non-numeric offset", {"scenario": name, "index": i})
        if last is not None and at < last:
            # the original business order must be preserved; an out-of-order offset would make the
            # runner silently reorder or skip same-instant events.
            raise PlanError("E_STEP_ORDER", f"scenario {name!r} offsets are not non-decreasing at step {i}", {"scenario": name, "index": i})
        last = at
    return steps


def load_plan(ds, dataset: str, mode: str, cfg) -> Plan:
    """Load and fully check the plan for `mode`. Raises PlanError; never sends anything."""
    sc = _scenarios(ds)
    manifest = ds.load("manifest", {}) or {}
    manifest_present = bool(manifest)
    if mode == "A":
        name = cfg.plan_a_template.format(dataset=dataset)
        steps = _steps(sc, name)
        plan = Plan(mode="A", dataset=dataset, name=name, speed=cfg.speed_a, steps=steps, manifest_present=manifest_present)
    elif mode == "B":
        segments, stop_points, flat = [], [], []
        for seq in range(1, cfg.max_segments_scan + 1):
            nm = cfg.plan_b_segment_template.format(dataset=dataset, seq=seq)
            if nm not in sc:
                break
            seg = _steps(sc, nm)
            segments.append(seg)
            flat.extend(seg)
            stop_points.append(StopPoint(sequence=seq, name=nm, event_count=len(seg)))
        if not segments:
            raise PlanError(
                "E_B_SEGMENTS_MISSING",
                "no B segment scenarios found (expected %s)" % cfg.plan_b_segment_template.format(dataset=dataset, seq=1),
            )
        name = cfg.plan_b_segment_template.format(dataset=dataset, seq=1).rsplit("_p", 1)[0]
        plan = Plan(
            mode="B",
            dataset=dataset,
            name=name,
            speed=cfg.speed_b,
            steps=flat,
            segments=segments,
            stop_points=stop_points,
            manifest_present=manifest_present,
        )
    else:
        raise PlanError("E_MODE_INVALID", f"mode must be A or B, got {mode!r}")

    if mode == "B":
        _cross_check_b(sc, plan, cfg, dataset)
    _apply_manifest(plan, manifest, cfg)
    _check(plan, cfg)
    return plan


def _cross_check_b(sc: dict, plan: Plan, cfg, dataset: str) -> None:
    """The 62 pre-cut segments must carry exactly the same events, in the same order, as the
    continuous scenarios. A mismatch means the plan on disk is not the frozen plan."""
    ids = [st["event_id"] for st in plan.steps]
    for other in (plan.name, cfg.plan_a_template.format(dataset=dataset)):
        scen = sc.get(other)
        if not isinstance(scen, dict) or not isinstance(scen.get("steps"), list):
            continue
        other_ids = [st.get("event_id") for st in scen["steps"]]
        if other_ids != ids:
            raise PlanError(
                "E_B_SEGMENT_SEQUENCE",
                "the pre-cut B segments do not reproduce the continuous plan event sequence",
                {"compared_with": other, "segments_events": len(ids), "continuous_events": len(other_ids)},
            )


def _apply_manifest(plan: Plan, manifest: dict, cfg) -> None:
    if not manifest:
        return
    conts = manifest.get("continuous") or []
    speeds = [c.get("speed") for c in conts if isinstance(c, dict)]
    if speeds and cfg.speed_a in speeds and cfg.speed_b in speeds:
        for c in conts:
            if c.get("speed") == plan.speed:
                plan.nominal_seconds = c.get("nominal_seconds")
                if plan.mode == "A" and c.get("event_count") not in (None, plan.total_events):
                    raise PlanError(
                        "E_MANIFEST_EVENT_COUNT",
                        "manifest event_count does not match the scenario",
                        {"manifest": c.get("event_count"), "scenario": plan.total_events},
                    )
    elif speeds:
        raise PlanError("E_MANIFEST_SPEED", "manifest speeds do not include the frozen 1:240 / 1:60 pair", {"speeds": speeds})
    if plan.mode != "B":
        return
    segs = manifest.get("B_segments") or []
    if not segs:
        return
    if len(segs) != len(plan.stop_points):
        raise PlanError(
            "E_MANIFEST_SEGMENT_COUNT",
            "manifest B_segments count does not match the pre-cut B scenarios",
            {"manifest": len(segs), "scenarios": len(plan.stop_points)},
        )
    for sp, m in zip(plan.stop_points, segs):
        if m.get("name") not in (None, sp.name):
            raise PlanError("E_MANIFEST_SEGMENT_NAME", "manifest B_segments name mismatch", {"manifest": m.get("name"), "scenario": sp.name})
        if m.get("event_count") not in (None, sp.event_count):
            raise PlanError(
                "E_MANIFEST_SEGMENT_EVENTS",
                "manifest B segment event_count mismatch",
                {"segment": sp.name, "manifest": m.get("event_count"), "scenario": sp.event_count},
            )
        sp.nominal_seconds = m.get("nominal_seconds")
        sp.business_interval_start_utc = m.get("business_interval_start_utc")
        sp.business_interval_end_utc = m.get("business_interval_end_utc")
        sp.stop_evidence = m.get("stop_evidence") or []


def _check(plan: Plan, cfg) -> None:
    sound = tuple(st["event_id"] for st in plan.steps if st.get("event_type") == SOUNDPROOF_EVENT_TYPE)
    plan.soundproof_event_ids = sound
    if cfg.expect_events is not None and plan.total_events != cfg.expect_events:
        raise PlanError(
            "E_EVENT_COUNT",
            "plan event count does not match the frozen contract count",
            {"expected": cfg.expect_events, "actual": plan.total_events, "scenario": plan.name},
        )
    if plan.mode == "B" and cfg.expect_b_stop_points is not None and len(plan.stop_points) != cfg.expect_b_stop_points:
        raise PlanError(
            "E_STOP_POINT_COUNT",
            "B stop point count does not match the frozen contract count",
            {"expected": cfg.expect_b_stop_points, "actual": len(plan.stop_points)},
        )
    if cfg.expect_soundproof is not None and len(sound) != cfg.expect_soundproof:
        raise PlanError(
            "E_SOUNDPROOF_COUNT",
            "the plan does not carry exactly the expected number of 原类型事件",
            {"expected": cfg.expect_soundproof, "actual": len(sound), "event_type": SOUNDPROOF_EVENT_TYPE},
        )
    if len(set(sound)) != len(sound):
        raise PlanError("E_SOUNDPROOF_DUPLICATE", "duplicate event_id among the 原类型事件", {"ids": list(sound)})
    if cfg.known_reject_event_ids is not None and set(cfg.known_reject_event_ids) != set(sound):
        raise PlanError(
            "E_KNOWN_REJECT_IDS",
            "configured 原10条 identity list does not match the plan",
            {"configured": sorted(cfg.known_reject_event_ids), "plan": sorted(sound)},
        )
