"""Configuration of the manual single-round controller.

Every value is explicit and fails closed: when the approved target is not configured the
controller refuses to start a round (it never falls back to CV_TARGET_URL, because the whole
point of the check is to detect a drifted CV_TARGET_URL).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from simcore.config import env, env_bool, env_float, env_int

SOUNDPROOF_EVENT_TYPE = "SOUNDPROOF_COVER_MOVE_COMPLETED"
MODES = ("A", "B")


def _ids(value: str | None) -> tuple[str, ...] | None:
    if not value:
        return None
    out = tuple(p.strip() for p in value.split(",") if p.strip())
    return out or None


def default_readyz_url(target_url: str) -> str:
    u = urlparse(target_url)
    return urlunparse((u.scheme, u.netloc, "/readyz", "", "", ""))


@dataclass
class ControlConfig:
    """Controller configuration. `state_dir` holds `A/` and `B/` sub-directories plus the run lock."""

    state_dir: Path
    cv_target_url: str | None = None
    approved_target_url: str | None = None
    readyz_url: str | None = None
    expect_data_domain: str = "TEST_ISOLATED"
    expect_artifact_prefix: str = "yqyd"
    require_readyz: bool = True
    readyz_timeout_s: float = 10.0
    # frozen plan expectations (Owner: A 1:240 / B 1:60, 318 events, 62 B stop points, 10 原类型事件)
    expect_events: int | None = 318
    expect_b_stop_points: int | None = 62
    expect_soundproof: int | None = 10
    known_reject_event_ids: tuple[str, ...] | None = None
    plan_a_template: str = "{dataset}_A240"
    plan_b_segment_template: str = "{dataset}_B060_p{seq:02d}"
    speed_a: int = 240
    speed_b: int = 60
    challenge_ttl_s: float = 60.0
    tick_seconds: float = 0.2
    max_segments_scan: int = 400

    def resolved_readyz_url(self) -> str | None:
        if self.readyz_url:
            return self.readyz_url
        if self.approved_target_url:
            return default_readyz_url(self.approved_target_url)
        return None

    @property
    def mode_dir(self):
        return lambda mode: self.state_dir / mode


def config_from_env(*, state_dir: Path, cv_target_url: str | None) -> ControlConfig:
    return ControlConfig(
        state_dir=Path(env("SIM_CONTROL_STATE_DIR", str(state_dir))),
        cv_target_url=cv_target_url,
        approved_target_url=env("SIM_CONTROL_APPROVED_TARGET_URL"),
        readyz_url=env("SIM_CONTROL_TARGET_READYZ_URL"),
        expect_data_domain=env("SIM_CONTROL_EXPECT_DATA_DOMAIN", "TEST_ISOLATED"),
        expect_artifact_prefix=env("SIM_CONTROL_EXPECT_ARTIFACT_PREFIX", "yqyd"),
        require_readyz=env_bool("SIM_CONTROL_REQUIRE_READYZ", True),
        readyz_timeout_s=env_float("SIM_CONTROL_READYZ_TIMEOUT_SECONDS", 10.0),
        expect_events=env_int("SIM_CONTROL_EXPECT_EVENTS", 318),
        expect_b_stop_points=env_int("SIM_CONTROL_EXPECT_B_STOP_POINTS", 62),
        expect_soundproof=env_int("SIM_CONTROL_EXPECT_SOUNDPROOF", 10),
        known_reject_event_ids=_ids(env("SIM_CONTROL_KNOWN_REJECT_EVENT_IDS")),
        plan_a_template=env("SIM_CONTROL_PLAN_A_TEMPLATE", "{dataset}_A240"),
        plan_b_segment_template=env("SIM_CONTROL_PLAN_B_SEGMENT_TEMPLATE", "{dataset}_B060_p{seq:02d}"),
        challenge_ttl_s=env_float("SIM_CONTROL_CHALLENGE_TTL_SECONDS", 60.0),
        tick_seconds=env_float("SIM_CONTROL_TICK_SECONDS", 0.2),
    )
