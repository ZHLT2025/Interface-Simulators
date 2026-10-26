"""IoT simulator — IF-IOT-001..005 (HTTP) and IF-IOT-MQTT-001 (publisher).

Authority: 接口定义说明书三、IOT部分-0813.md. The HTTP service and the MQTT publisher run in one
process so that IF-IOT-003 history, IF-IOT-004/005 configuration and the realtime stream stay consistent.
"""
from __future__ import annotations

import os

from fastapi import Request
from fastapi.responses import JSONResponse

from simcore.app_factory import SimContext
from simcore.config import env, env_bool, env_float, env_int
from simcore.timeutil import is_space_format

from .publisher import Publisher
from .store import IotStore

ctx = SimContext("iot", "IoT simulator (IF-IOT-001..005, IF-IOT-MQTT-001)")
app = ctx.app
S = ctx.settings

FACTORY_CODE = env("IOT_FACTORY_CODE", "BJZHLT")
# Failure responses are undefined by the authority (18.3.2 etc. 待确认). Provisional policy:
#   envelope : HTTP 200 + {success:false, code:<n>, message}   (default; success judged by success/code per 18.3.1)
#   http     : HTTP 4xx + same body
ERROR_STYLE = env("IOT_ERROR_STYLE", "envelope")
RANGE_END_INCLUSIVE = env_bool("IOT_RANGE_END_INCLUSIVE", True)  # 20.5-1 区间边界待确认
VALIDATE_CONFIG_PARAMS = env_bool("IOT_VALIDATE_CONFIG_PARAMS", False)  # 22.5-3 未知参数处理待确认
KEEP_RUNTIME_CONFIG = env_bool("IOT_KEEP_RUNTIME_CONFIG", True)
HISTORY_CAP = env_int("IOT_HISTORY_CAP_PER_PARAM", 5000)

store = IotStore(S.state_dir, HISTORY_CAP)
store.load_from_dataset(ctx.ds, KEEP_RUNTIME_CONFIG)

publisher = Publisher(
    store,
    host=env("IOT_MQTT_HOST", "mqtt"),
    port=env_int("IOT_MQTT_PORT", 1883),
    username=env("IOT_MQTT_PUB_USER"),
    password=env("IOT_MQTT_PUB_PASS"),
    client_id=env("IOT_MQTT_PUB_CLIENT_ID", "sim-iot-pub"),
    factory_code=FACTORY_CODE,
    tz=S.timezone,
    source_lag_s=env_float("IOT_SOURCE_TIME_LAG_SECONDS", 3.0),
    enabled=env_bool("IOT_MQTT_ENABLED", True),
)


@app.on_event("startup")
def _start():
    if os.environ.get("IOT_PUBLISHER_THREAD", "1") == "1":
        publisher.start()


@app.on_event("shutdown")
def _stop():
    publisher.stop()


# ---------------------------------------------------------------- helpers
def _ok(data):
    return {"success": True, "code": 0, "message": "操作成功", "data": data}


def _fail(http_status: int, code: int, message: str):
    body = {"success": False, "code": code, "message": message, "data": None}
    return JSONResponse(status_code=200 if ERROR_STYLE == "envelope" else http_status, content=body)


def _check_factory(request: Request):
    v = request.headers.get("x-factory-code")
    if v is None:
        return _fail(400, 400, "X-Factory-Code header is required")
    if v != FACTORY_CODE:
        return _fail(403, 403, f"unknown factory code {v!r}")
    return None


async def _json(request: Request):
    try:
        return await request.json(), None
    except Exception:
        return None, _fail(400, 400, "request body is not valid JSON")


# ---------------------------------------------------------------- IF-IOT-001
@app.get("/open/deviceList")
def device_list(request: Request):
    bad = _check_factory(request)
    if bad:
        return bad
    return _ok([{"fundingCode": d.get("fundingCode"), "deviceCode": d.get("deviceCode"), "deviceName": d.get("deviceName")} for d in store.devices])


# ---------------------------------------------------------------- IF-IOT-002
@app.post("/open/deviceParamList")
async def device_param_list(request: Request):
    bad = _check_factory(request)
    if bad:
        return bad
    body, err = await _json(request)
    if err:
        return err
    if not isinstance(body, list) or not all(isinstance(x, str) for x in body):
        return _fail(400, 400, "request body must be a JSON array of deviceCode strings")
    data = []
    for code in body:
        if code in store.params:  # 19.5-2: unknown device => omitted (provisional)
            data.append({"deviceCode": code, "params": [dict(p) for p in store.param_catalogue(code)]})
    return _ok(data)


# ---------------------------------------------------------------- IF-IOT-003
@app.post("/open/deviceHistory/getDeviceData")
async def device_history(request: Request):
    bad = _check_factory(request)
    if bad:
        return bad
    body, err = await _json(request)
    if err:
        return err
    if not isinstance(body, dict):
        return _fail(400, 400, "request body must be a JSON object")
    device = body.get("deviceCode")
    if not isinstance(device, str) or not device:
        return _fail(400, 400, "deviceCode is required")
    start, end = body.get("startTime"), body.get("endTime")
    if start is None and end is None:
        return _ok(store.latest(device))  # LATEST mode: 不指定时间范围 => 最后一条记录
    if start is None or end is None:
        return _fail(400, 400, "startTime and endTime must be given together")  # 20.5-1 成对出现 (provisional)
    if not is_space_format(start) or not is_space_format(end):
        return _fail(400, 400, "startTime/endTime must use format yyyy-MM-dd HH:mm:ss")
    if end < start:
        return _fail(400, 400, "endTime must not be earlier than startTime")
    return _ok(store.range_(device, start, end, RANGE_END_INCLUSIVE))


# ---------------------------------------------------------------- IF-IOT-004
@app.get("/open/sendConfig/list")
def send_config_list(request: Request):
    bad = _check_factory(request)
    if bad:
        return bad
    return _ok(store.get_send_config())


# ---------------------------------------------------------------- IF-IOT-005
@app.put("/open/sendConfig/fullUpdate")
async def send_config_full_update(request: Request):
    bad = _check_factory(request)
    if bad:
        return bad
    body, err = await _json(request)
    if err:
        return err
    if not isinstance(body, dict):
        return _fail(400, 400, "request body must be a JSON object")
    freq = body.get("sendFrequency")
    if not isinstance(freq, (int, float)) or isinstance(freq, bool) or freq <= 0:
        return _fail(400, 400, "sendFrequency must be a positive number (seconds)")
    dps = body.get("deviceParams")
    if not isinstance(dps, list):
        return _fail(400, 400, "deviceParams must be an array")
    cleaned = []
    for i, dp in enumerate(dps):
        if not isinstance(dp, dict) or not isinstance(dp.get("deviceCode"), str) or not isinstance(dp.get("paramCode"), str):
            return _fail(400, 400, f"deviceParams[{i}] must contain deviceCode and paramCode strings")
        if VALIDATE_CONFIG_PARAMS and store.param_type(dp["deviceCode"], dp["paramCode"]) is None:
            return _fail(400, 400, f"unknown device/param {dp['deviceCode']}/{dp['paramCode']}")
        cleaned.append({"deviceCode": dp["deviceCode"], "paramCode": dp["paramCode"]})
    # 22.5-1 全量更新语义: provisional = replace entire configuration (empty array => nothing is pushed)
    store.set_send_config({"sendFrequency": freq, "deviceParams": cleaned})
    return _ok(store.get_send_config())  # 22.5-5: data = 最终生效配置 (equals request echo here)


@app.get("/")
def index():
    return {
        "simulator": "IoT",
        "contracts": {
            "IF-IOT-001": "GET /open/deviceList",
            "IF-IOT-002": "POST /open/deviceParamList",
            "IF-IOT-003": "POST /open/deviceHistory/getDeviceData",
            "IF-IOT-004": "GET /open/sendConfig/list",
            "IF-IOT-005": "PUT /open/sendConfig/fullUpdate",
            "IF-IOT-MQTT-001": f"mqtt topic pushToFactory/{FACTORY_CODE}/device/{{deviceCode}} (QoS0, retain false)",
        },
        "header": "X-Factory-Code: " + FACTORY_CODE,
        "admin": "/_sim/health, /_sim/status, /_sim/requests, /_sim/reload, /_sim/dataset, /_sim/mqtt/*",
    }


# ---------------------------------------------------------------- admin extras (outside contract)
def _reload_store():
    store.load_from_dataset(ctx.ds, keep_runtime_config=False)
    publisher.reset_script()


ctx.mount_admin(
    extra_status=lambda: {
        "factory_code": FACTORY_CODE,
        "policies": {"error_style": ERROR_STYLE, "range_end_inclusive": RANGE_END_INCLUSIVE, "validate_config_params": VALIDATE_CONFIG_PARAMS},
        "devices": len(store.devices),
        "send_config": store.get_send_config(),
        "mqtt": publisher.status(),
    },
    on_dataset_change=_reload_store,
)

from fastapi import HTTPException  # noqa: E402


def _admin_guard(request: Request):
    import hmac

    if S.admin_token is None:
        raise HTTPException(status_code=403, detail="admin endpoints disabled")
    if not hmac.compare_digest(request.headers.get("x-sim-admin-token", ""), S.admin_token):
        raise HTTPException(status_code=401, detail="invalid admin token")


@app.get("/_sim/mqtt/status")
def mqtt_status(request: Request):
    _admin_guard(request)
    return publisher.status()


@app.post("/_sim/mqtt/pause")
def mqtt_pause(request: Request):
    _admin_guard(request)
    publisher.paused = True
    return {"paused": True}


@app.post("/_sim/mqtt/resume")
def mqtt_resume(request: Request):
    _admin_guard(request)
    publisher.paused = False
    return {"paused": False}


@app.post("/_sim/mqtt/publish")
async def mqtt_publish(request: Request):
    """Publish one explicit payload (root array) on behalf of a device — for manual tests / replay."""
    _admin_guard(request)
    body = await request.json()
    device = body.get("deviceCode")
    payload = body.get("payload")
    if not isinstance(device, str) or not isinstance(payload, list):
        raise HTTPException(status_code=400, detail="deviceCode (string) and payload (array) required")
    ok = publisher.publish(device, payload)
    for item in payload:
        if isinstance(item, dict) and {"identifiers", "serverTime", "sourceTime", "value"} <= set(item):
            store.append_history(device, item["identifiers"], item["serverTime"], item["sourceTime"], str(item["value"]))
    return {"published": ok, "topic": f"pushToFactory/{FACTORY_CODE}/device/{device}"}


@app.post("/_sim/mqtt/script/reset")
def mqtt_script_reset(request: Request):
    _admin_guard(request)
    publisher.reset_script()
    return {"reset": True}
