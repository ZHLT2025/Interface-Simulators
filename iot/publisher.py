"""IF-IOT-MQTT-001 publisher: emits 设备运行实时数据 to the broker.

Contract (definition §23): topic pushToFactory/{factoryCode}/device/{deviceCode}, MQTT 3.1.1,
QoS 0, Retain false, JSON root array of {deviceCode, identifiers, serverTime, sourceTime, value(String)}.
Each message carries only the points whose value changed at this sampling instant (§23.5 实测说明).
The sampling period is data.sendFrequency from IF-IOT-004/005.

Two data modes, selected per dataset (realtime.json "mode"):
  generator : values are synthesised per param type / explicit generator spec (default)
  script    : a scripted sequence of point updates is replayed (for phase-3 test instances)
"""
from __future__ import annotations

import json
import logging
import random
import threading
import time
from collections import deque
from datetime import timedelta

import paho.mqtt.client as mqtt

from simcore.timeutil import fmt_space, now_in

log = logging.getLogger("iot.mqtt")


class Publisher(threading.Thread):
    def __init__(self, store, *, host, port, username, password, client_id, factory_code, tz, source_lag_s, enabled=True):
        super().__init__(daemon=True, name="mqtt-publisher")
        self.store = store
        self.host, self.port = host, port
        self.username, self.password = username, password
        self.client_id = client_id
        self.factory_code = factory_code
        self.tz = tz
        self.source_lag_s = source_lag_s
        self.enabled = enabled
        self.paused = False
        self.connected = False
        self.published = 0
        self.failed = 0
        self.last_error = None
        self.recent = deque(maxlen=30)
        self.rng = random.Random()
        self.values: dict[tuple[str, str], object] = {}
        self._seen_version = -1
        self._script_pos = 0
        self._script_started = None
        self.stop_event = threading.Event()
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id, clean_session=True, protocol=mqtt.MQTTv311)
        if username:
            self.client.username_pw_set(username, password)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect

    # ------------------------------------------------------------------ mqtt callbacks
    def _on_connect(self, client, userdata, flags, reason_code, properties):
        self.connected = reason_code == 0
        if not self.connected:
            self.last_error = f"CONNACK {reason_code}"
        log.info("mqtt connect reason=%s", reason_code)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        self.connected = False
        log.warning("mqtt disconnected reason=%s", reason_code)

    # ------------------------------------------------------------------ run loop
    def run(self):
        self.client.loop_start()
        while not self.stop_event.is_set():
            if not self.connected:
                try:
                    self.client.connect(self.host, self.port, keepalive=60)
                except Exception as e:  # broker not up yet
                    self.last_error = f"connect: {e}"
                    self.stop_event.wait(2)
                    continue
                self.stop_event.wait(1)
                continue
            cfg = self.store.get_send_config()
            period = float(cfg.get("sendFrequency") or 1)
            if period <= 0:
                period = 1.0
            if self.enabled and not self.paused:
                try:
                    self.tick(cfg)
                except Exception as e:
                    self.last_error = f"tick: {e}"
                    log.exception("publish tick failed")
            self.stop_event.wait(period)
        self.client.loop_stop()

    def stop(self):
        self.stop_event.set()

    # ------------------------------------------------------------------ sampling
    def tick(self, cfg: dict):
        rt = self.store.realtime or {}
        mode = rt.get("mode", "generator")
        now = now_in(self.tz)
        server_time = fmt_space(now)
        source_time = fmt_space(now - timedelta(seconds=self.source_lag_s))
        if self.store.config_version != self._seen_version:
            # new configuration => first sample of every configured point is published (initial snapshot)
            self.values.clear()
            self._seen_version = self.store.config_version
        if mode == "script":
            batches = self._script_batches(rt, now)
        else:
            batches = self._generator_batches(cfg, rt)
        for device, points in batches.items():
            if not points:
                continue
            payload = [
                {"deviceCode": device, "identifiers": p, "serverTime": server_time, "sourceTime": source_time, "value": v}
                for p, v in points
            ]
            for p, v in points:
                self.store.append_history(device, p, server_time, source_time, v)
            self.publish(device, payload)

    def publish(self, device: str, payload: list) -> bool:
        topic = f"pushToFactory/{self.factory_code}/device/{device}"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        info = self.client.publish(topic, data, qos=0, retain=False)
        ok = info.rc == mqtt.MQTT_ERR_SUCCESS
        if ok:
            self.published += 1
        else:
            self.failed += 1
            self.last_error = f"publish rc={info.rc}"
        self.recent.append({"topic": topic, "points": len(payload), "ok": ok, "serverTime": payload[0]["serverTime"] if payload else None})
        return ok

    # ------------------------------------------------------------------ generator mode
    def _generator_batches(self, cfg: dict, rt: dict) -> dict[str, list]:
        specs = rt.get("generators", {})
        change_prob = float(rt.get("change_probability", 0.6))
        batches: dict[str, list] = {}
        for item in cfg.get("deviceParams", []):
            device, param = item.get("deviceCode"), item.get("paramCode")
            if not device or not param:
                continue
            key = (device, param)
            spec = specs.get(f"{device}/{param}") or specs.get(param) or {}
            ptype = spec.get("type") or self.store.param_type(device, param) or "float"
            prev = self.values.get(key)
            if prev is None:
                prev = self.store.last_value(device, param)
                new = self._initial(ptype, spec, prev)
                changed = True
            else:
                if self.rng.random() > change_prob:
                    continue
                new = self._next(ptype, spec, prev)
                changed = new != prev
            if changed:
                self.values[key] = new
                batches.setdefault(device, []).append((param, new))
        return batches

    def _initial(self, ptype, spec, prev):
        if prev is not None and "values" not in spec:
            return str(prev)
        return self._next(ptype, spec, None)

    def _next(self, ptype: str, spec: dict, prev) -> str:
        if "values" in spec:  # enumerated strings
            vals = spec["values"]
            if prev in vals and len(vals) > 1 and self.rng.random() < 0.7:
                return prev
            return str(self.rng.choice(vals))
        if ptype == "boolean":
            if prev in ("true", "false"):
                return prev if self.rng.random() < float(spec.get("hold", 0.8)) else ("false" if prev == "true" else "true")
            return self.rng.choice(["true", "false"])
        if ptype == "int":
            lo, hi = int(spec.get("min", 0)), int(spec.get("max", 100))
            step = int(spec.get("step", 1))
            try:
                cur = int(float(prev))
            except (TypeError, ValueError):
                cur = self.rng.randint(lo, hi)
            cur = min(hi, max(lo, cur + self.rng.randint(-step, step)))
            return str(cur)
        if ptype == "string":
            return f"PRG{self.rng.randint(1, 9)}.PRG"
        # float: bounded random walk
        lo, hi = float(spec.get("min", 0.0)), float(spec.get("max", 250.0))
        step = float(spec.get("step", (hi - lo) * 0.02))
        dec = int(spec.get("decimals", 5))
        try:
            cur = float(prev)
        except (TypeError, ValueError):
            cur = self.rng.uniform(lo, hi)
        cur = min(hi, max(lo, cur + self.rng.uniform(-step, step)))
        return f"{cur:.{dec}f}"

    # ------------------------------------------------------------------ script mode
    def _script_batches(self, rt: dict, now) -> dict[str, list]:
        """script: [{"at": <seconds from start>, "deviceCode": "...", "points": [{"identifiers": "...", "value": "..."}]}]"""
        script = rt.get("script", [])
        if self._script_started is None:
            self._script_started = time.monotonic()
            self._script_pos = 0
        elapsed = time.monotonic() - self._script_started
        batches: dict[str, list] = {}
        while self._script_pos < len(script) and float(script[self._script_pos].get("at", 0)) <= elapsed:
            step = script[self._script_pos]
            dev = step.get("deviceCode")
            pts = [(p["identifiers"], str(p["value"])) for p in step.get("points", [])]
            batches.setdefault(dev, []).extend(pts)
            self._script_pos += 1
        if self._script_pos >= len(script) and rt.get("loop", False):
            self._script_started = None
        return batches

    def reset_script(self):
        self._script_started = None
        self._script_pos = 0

    def status(self) -> dict:
        return {
            "broker": f"{self.host}:{self.port}",
            "client_id": self.client_id,
            "connected": self.connected,
            "enabled": self.enabled,
            "paused": self.paused,
            "published": self.published,
            "failed": self.failed,
            "last_error": self.last_error,
            "recent": list(self.recent),
        }
