"""IF-IOT-001..005 and IF-IOT-MQTT-001 — expectations from 接口定义说明书三、IOT部分-0813 §18-23."""
import json
import re
import threading
import time

import httpx
import paho.mqtt.client as mqtt
import pytest

TS = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


def H(creds, body=False):
    h = {"X-Factory-Code": creds["factory"], "Accept": "application/json"}
    if body:
        h["Content-Type"] = "application/json"
    return h


def ok(j):
    assert j["success"] is True and j["code"] == 0 and isinstance(j["message"], str)


def test_001_device_list(iot_url, creds):
    r = httpx.get(f"{iot_url}/open/deviceList", headers=H(creds))
    assert r.status_code == 200
    j = r.json()
    ok(j)
    assert isinstance(j["data"], list) and j["data"]
    assert set(j["data"][0]) == {"fundingCode", "deviceCode", "deviceName"}
    codes = {d["deviceCode"] for d in j["data"]}
    assert {"cqx11", "cqx12", "dxdm", "ckdm1", "ckdm2", "ckdm3"} <= codes  # §23.3 当前已知值


def test_factory_code_header_required(iot_url, creds):
    r = httpx.get(f"{iot_url}/open/deviceList", headers={"Accept": "application/json"})
    assert r.json()["success"] is False  # provisional envelope failure; HTTP status 待确认
    r = httpx.get(f"{iot_url}/open/deviceList", headers={"X-Factory-Code": "WRONG"})
    assert r.json()["success"] is False and r.json()["code"] != 0


def test_002_device_param_list(iot_url, creds):
    r = httpx.post(f"{iot_url}/open/deviceParamList", headers=H(creds, True), content=json.dumps(["cqx11"]))
    assert r.status_code == 200
    j = r.json()
    ok(j)
    assert len(j["data"]) == 1 and j["data"][0]["deviceCode"] == "cqx11"
    p = j["data"][0]["params"][0]
    assert set(p) == {"paramCode", "paramName", "paramType", "paramUnit"}
    codes = {x["paramCode"] for x in j["data"][0]["params"]}
    assert {"HJ1.S0.CurrentPS1", "HJ1.S0.MotWorkSpeed", "HJ1.S0.VoltagePS1"} <= codes
    types = {x["paramType"] for x in j["data"][0]["params"]}
    assert types <= {"float", "boolean", "int", "string"}
    # multiple devices
    r = httpx.post(f"{iot_url}/open/deviceParamList", headers=H(creds, True), content=json.dumps(["cqx11", "dxdm"]))
    assert [d["deviceCode"] for d in r.json()["data"]] == ["cqx11", "dxdm"]
    # empty array => empty data (provisional)
    r = httpx.post(f"{iot_url}/open/deviceParamList", headers=H(creds, True), content="[]")
    assert r.json()["data"] == []
    # wrong body shape
    r = httpx.post(f"{iot_url}/open/deviceParamList", headers=H(creds, True), content=json.dumps({"deviceCode": "cqx11"}))
    assert r.json()["success"] is False


def test_003_history_range(iot_url, creds):
    body = {"startTime": "2026-07-01 10:00:00", "endTime": "2026-07-01 12:00:00", "deviceCode": "cqx11"}
    r = httpx.post(f"{iot_url}/open/deviceHistory/getDeviceData", headers=H(creds, True), json=body)
    assert r.status_code == 200
    j = r.json()
    ok(j)
    by = {d["paramCode"]: d for d in j["data"]}
    assert by["HJ1.S0.CurrentPS1"]["deviceCode"] == "cqx11"
    rows = by["HJ1.S0.CurrentPS1"]["historyData"]
    assert len(rows) == 3  # seed: 11:40:59, 11:44:59, 11:48:59 all inside the window
    assert rows[-1] == {"serverTime": "2026-07-01 11:48:59", "sourceTime": "2009-09-01 11:46:06", "value": "203.99786"}  # 20.4.2
    for h in rows:
        assert set(h) == {"serverTime", "sourceTime", "value"} and isinstance(h["value"], str) and TS.match(h["serverTime"])
    # narrower window keeps only the last one
    body.update(startTime="2026-07-01 11:45:00")
    r = httpx.post(f"{iot_url}/open/deviceHistory/getDeviceData", headers=H(creds, True), json=body)
    by = {d["paramCode"]: d for d in r.json()["data"]}
    assert len(by["HJ1.S0.CurrentPS1"]["historyData"]) == 1


def test_003_history_latest_and_errors(iot_url, creds):
    r = httpx.post(f"{iot_url}/open/deviceHistory/getDeviceData", headers=H(creds, True), json={"deviceCode": "cqx11", "startTime": None, "endTime": None})
    assert r.status_code == 200
    j = r.json()
    ok(j)
    assert all(len(d["historyData"]) == 1 for d in j["data"])  # LATEST: 最后一条记录
    r = httpx.post(f"{iot_url}/open/deviceHistory/getDeviceData", headers=H(creds, True), json={"deviceCode": "no-such-device"})
    assert r.json()["data"] == []  # unknown device => empty array (provisional)
    r = httpx.post(f"{iot_url}/open/deviceHistory/getDeviceData", headers=H(creds, True), json={"startTime": "2026-07-01 10:00:00"})
    assert r.json()["success"] is False  # deviceCode required
    r = httpx.post(f"{iot_url}/open/deviceHistory/getDeviceData", headers=H(creds, True), json={"deviceCode": "cqx11", "startTime": "2026-07-01 10:00:00"})
    assert r.json()["success"] is False  # provisional: times must be given together


def test_004_005_send_config_roundtrip(iot_url, creds):
    r = httpx.get(f"{iot_url}/open/sendConfig/list", headers=H(creds))
    assert r.status_code == 200
    j = r.json()
    ok(j)
    original = j["data"]
    assert isinstance(original["sendFrequency"], (int, float)) and isinstance(original["deviceParams"], list)
    assert set(original["deviceParams"][0]) == {"deviceCode", "paramCode"}
    try:
        new = {"sendFrequency": 2, "deviceParams": [{"deviceCode": "cqx11", "paramCode": "HJ1.S0.CurrentPS1"}, {"deviceCode": "cqx11", "paramCode": "HJ1.S0.VoltagePS1"}]}
        r = httpx.put(f"{iot_url}/open/sendConfig/fullUpdate", headers=H(creds, True), json=new)
        assert r.status_code == 200
        j = r.json()
        ok(j)
        assert j["data"] == new  # data = 生效配置 (provisional: equals echo)
        r = httpx.get(f"{iot_url}/open/sendConfig/list", headers=H(creds))
        assert r.json()["data"] == new  # 全量更新 => 读回即新配置
        # validation
        for bad in ({"sendFrequency": 0, "deviceParams": []}, {"sendFrequency": "1", "deviceParams": []}, {"sendFrequency": 1}, {"sendFrequency": 1, "deviceParams": [{"deviceCode": "cqx11"}]}):
            r = httpx.put(f"{iot_url}/open/sendConfig/fullUpdate", headers=H(creds, True), json=bad)
            assert r.json()["success"] is False, bad
    finally:
        r = httpx.put(f"{iot_url}/open/sendConfig/fullUpdate", headers=H(creds, True), json=original)
        assert r.json()["success"] is True


class Sub:
    def __init__(self, host, port, user, pwd, topic, client_id):
        self.msgs = []
        self.connected = threading.Event()
        self.subscribed = threading.Event()
        self.conn_rc = None
        self.sub_rc = None
        self.c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id, clean_session=True, protocol=mqtt.MQTTv311)
        self.c.username_pw_set(user, pwd)
        self.topic = topic

        def on_connect(c, u, f, rc, p):
            self.conn_rc = rc
            self.connected.set()
            if rc == 0:
                c.subscribe(topic, qos=0)

        def on_subscribe(c, u, mid, rcs, p):
            self.sub_rc = [int(x.value) if hasattr(x, "value") else int(x) for x in rcs]
            self.subscribed.set()

        def on_message(c, u, m):
            self.msgs.append((m.topic, m.qos, m.retain, m.payload))

        self.c.on_connect, self.c.on_subscribe, self.c.on_message = on_connect, on_subscribe, on_message
        self.c.connect(host, port, keepalive=30)
        self.c.loop_start()

    def close(self):
        self.c.loop_stop()
        self.c.disconnect()


def test_mqtt_realtime_stream(mqtt_target, creds):
    host, port = mqtt_target
    s = Sub(host, port, *creds["mqtt_sub"], "pushToFactory/BJZHLT/device/cqx11", "if-23-sub-test")
    try:
        assert s.connected.wait(10) and s.conn_rc == 0  # 23.6.4-1 CONNECT accepted
        assert s.subscribed.wait(10) and s.sub_rc == [0]  # 23.6.4-2 SUBACK granted QoS0
        deadline = time.time() + 15
        while time.time() < deadline and len(s.msgs) < 2:
            time.sleep(0.5)
        assert len(s.msgs) >= 2, "no realtime messages received within 15s (sendFrequency=1)"
        for topic, qos, retain, payload in s.msgs[:5]:
            assert topic == "pushToFactory/BJZHLT/device/cqx11"  # §23.3 topic template
            assert qos == 0 and retain is False  # §23.3 QoS 0 / Retain false
            arr = json.loads(payload.decode("utf-8"))
            assert isinstance(arr, list) and arr  # §23.4 root array, no "data" wrapper
            for item in arr:
                assert set(item) == {"deviceCode", "identifiers", "serverTime", "sourceTime", "value"}
                assert item["deviceCode"] == "cqx11" and isinstance(item["value"], str)
                assert TS.match(item["serverTime"]) and TS.match(item["sourceTime"])
    finally:
        s.close()


def test_mqtt_auth_and_acl(mqtt_target, creds):
    host, port = mqtt_target
    # anonymous rejected
    s = Sub(host, port, None, None, "pushToFactory/#", "if-23-anon-test") if False else None
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="if-23-anon-test", protocol=mqtt.MQTTv311)
    got = {}
    ev = threading.Event()

    def on_connect(cl, u, f, rc, p):
        got["rc"] = rc
        ev.set()

    c.on_connect = on_connect
    c.connect(host, port, 30)
    c.loop_start()
    ev.wait(10)
    c.loop_stop()
    assert got.get("rc") is not None and got["rc"] != 0  # allow_anonymous false
    # subscriber account may not publish (ACL) — broker silently drops; verify via second subscriber not receiving
    s2 = Sub(host, port, *creds["mqtt_sub"], "pushToFactory/BJZHLT/device/acltest", "if-23-sub-acl")
    assert s2.connected.wait(10) and s2.subscribed.wait(10)
    pub = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="if-23-sub-as-pub", protocol=mqtt.MQTTv311)
    pub.username_pw_set(*creds["mqtt_sub"])
    pub.connect(host, port, 30)
    pub.loop_start()
    time.sleep(1)
    pub.publish("pushToFactory/BJZHLT/device/acltest", b"[]", qos=0)
    time.sleep(2)
    pub.loop_stop()
    assert s2.msgs == []  # write denied for sub account
    # publisher account can publish (23.6.3 发布示例 equivalent)
    pub2 = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="if-23-pub-test", protocol=mqtt.MQTTv311)
    pub2.username_pw_set(*creds["mqtt_pub"])
    pub2.connect(host, port, 30)
    pub2.loop_start()
    time.sleep(1)
    payload = [{"deviceCode": "acltest", "identifiers": "HJ1.S0.CurrentPS1", "serverTime": "2026-06-04 15:10:00", "sourceTime": "2026-06-04 15:09:00", "value": "203.99786"}]
    pub2.publish("pushToFactory/BJZHLT/device/acltest", json.dumps(payload).encode(), qos=0).wait_for_publish(5)
    time.sleep(2)
    pub2.loop_stop()
    s2.close()
    assert len(s2.msgs) == 1 and json.loads(s2.msgs[0][3]) == payload
