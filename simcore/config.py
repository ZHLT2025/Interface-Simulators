from __future__ import annotations

import os
from pathlib import Path


def env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return v


def env_bool(name: str, default: bool) -> bool:
    v = env(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def env_int(name: str, default: int) -> int:
    v = env(name)
    if v is None:
        return default
    return int(v)


def env_float(name: str, default: float) -> float:
    v = env(name)
    if v is None:
        return default
    return float(v)


class SimSettings:
    """Settings common to every simulator process."""

    def __init__(self, sim: str):
        self.sim = sim
        # dataset root: <data_root>/<dataset>/...  (data_root defaults to the simulator's own data dir)
        default_root = Path(__file__).resolve().parent.parent / sim / "data"
        self.data_root = Path(env(f"{sim.upper()}_DATA_ROOT", env("SIM_DATA_ROOT", str(default_root))))
        self.dataset = env(f"{sim.upper()}_DATASET", env("SIM_DATASET", "sample"))
        self.state_dir = Path(env("SIM_STATE_DIR", "/state")) / sim
        self.admin_token = env("SIM_ADMIN_TOKEN")  # None => admin endpoints disabled (except health)
        self.public_base_url = env(f"{sim.upper()}_PUBLIC_BASE_URL", "")
        self.request_log_size = env_int("SIM_REQUEST_LOG_SIZE", 500)
        self.timezone = env("SIM_TIMEZONE", "Asia/Shanghai")
        # F02: source-IP allow-lists (see simcore/ipguard.py)
        self.admin_allow_cidrs = env("SIM_ADMIN_ALLOW_CIDRS", "127.0.0.0/8,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16")
        self.client_allow_cidrs = env("SIM_CLIENT_ALLOW_CIDRS", "")

    @property
    def dataset_dir(self) -> Path:
        return self.data_root / self.dataset
