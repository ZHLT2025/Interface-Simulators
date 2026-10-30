"""人工 A/B 单轮控制模块 (IS-06/07/08).

Authority: V1.1 正式设计 03《A/B 模拟器人工单轮运行设计》(冻结) + E02 三域设计说明 SIM 节.

This package implements the *manual single-round* controller: one explicit human click
(two-phase confirmed) initialises the simulator itself and runs exactly one round of the
frozen replay plan (A 1:240 continuous, B 1:60 with 62 stop points). It never starts a
second round on its own, never waits for a platform end-point rule, never writes a
business report and never touches another system's state.
"""
from .config import MODES, SOUNDPROOF_EVENT_TYPE, ControlConfig
from .machine import DISPLAY, OPS, STATES, transition_error
from .module import ControlModule, build_control_router
from .outcome import ACCEPTED, KNOWN_REJECTED, UNKNOWN, classify
from .runlock import RunLock

__all__ = [
    "MODES",
    "SOUNDPROOF_EVENT_TYPE",
    "ControlConfig",
    "ControlModule",
    "build_control_router",
    "RunLock",
    "STATES",
    "OPS",
    "DISPLAY",
    "transition_error",
    "classify",
    "ACCEPTED",
    "KNOWN_REJECTED",
    "UNKNOWN",
]
