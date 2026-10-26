"""In-memory state shared by the IoT HTTP endpoints and the MQTT publisher.

* catalogue      : devices / params from the dataset (IF-IOT-001/002)
* history        : seed history from dataset + every sample the publisher emits (IF-IOT-003)
* send_config    : current push configuration (IF-IOT-004/005), persisted to the state dir
"""
from __future__ import annotations

import json
import threading
from collections import defaultdict, deque
from pathlib import Path


class IotStore:
    def __init__(self, state_dir: Path, history_cap: int):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.history_cap = history_cap
        self.lock = threading.RLock()
        self.devices: list[dict] = []
        self.params: dict[str, list[dict]] = {}
        self.history: dict[tuple[str, str], deque] = defaultdict(lambda: deque(maxlen=self.history_cap))
        self.send_config: dict = {"sendFrequency": 1, "deviceParams": []}
        self.realtime: dict = {}
        self.config_version = 0

    # ---------------- dataset loading ----------------
    def load_from_dataset(self, ds, keep_runtime_config: bool):
        with self.lock:
            self.devices = ds.load("devices").get("devices", [])
            self.params = {d["deviceCode"]: d.get("params", []) for d in ds.load("params").get("devices", [])}
            self.history.clear()
            for rec in ds.load("history", {"records": []}).get("records", []):
                key = (rec["deviceCode"], rec["paramCode"])
                for h in rec.get("historyData", []):
                    self.history[key].append(dict(h))
            self.realtime = ds.load("realtime", {})
            persisted = self.state_dir / "send_config.json"
            if keep_runtime_config and persisted.exists():
                with persisted.open("r", encoding="utf-8") as f:
                    self.send_config = json.load(f)
            else:
                self.send_config = ds.load("send_config")
                self._persist()
            self.config_version += 1

    def _persist(self):
        tmp = self.state_dir / "send_config.json.tmp"
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(self.send_config, f, ensure_ascii=False, indent=2)
        tmp.replace(self.state_dir / "send_config.json")

    # ---------------- queries ----------------
    def device_codes(self) -> set[str]:
        return {d["deviceCode"] for d in self.devices}

    def param_catalogue(self, device: str) -> list[dict]:
        return self.params.get(device, [])

    def param_type(self, device: str, param: str) -> str | None:
        for p in self.params.get(device, []):
            if p.get("paramCode") == param:
                return p.get("paramType")
        return None

    def get_send_config(self) -> dict:
        with self.lock:
            return json.loads(json.dumps(self.send_config))

    def set_send_config(self, cfg: dict):
        with self.lock:
            self.send_config = {"sendFrequency": cfg["sendFrequency"], "deviceParams": list(cfg["deviceParams"])}
            self.config_version += 1
            self._persist()

    def append_history(self, device: str, param: str, server_time: str, source_time: str, value: str):
        with self.lock:
            self.history[(device, param)].append({"serverTime": server_time, "sourceTime": source_time, "value": value})

    def latest(self, device: str) -> list[dict]:
        with self.lock:
            out = []
            for (d, p), q in self.history.items():
                if d == device and q:
                    out.append({"deviceCode": d, "paramCode": p, "historyData": [dict(q[-1])]})
            return sorted(out, key=lambda x: x["paramCode"])

    def range_(self, device: str, start: str, end: str, inclusive_end: bool) -> list[dict]:
        with self.lock:
            out = []
            for (d, p), q in self.history.items():
                if d != device:
                    continue
                rows = [dict(h) for h in q if start <= h["serverTime"] and (h["serverTime"] <= end if inclusive_end else h["serverTime"] < end)]
                if rows:
                    out.append({"deviceCode": d, "paramCode": p, "historyData": rows})
            return sorted(out, key=lambda x: x["paramCode"])

    def last_value(self, device: str, param: str):
        with self.lock:
            q = self.history.get((device, param))
            return q[-1]["value"] if q else None
