"""MOM simulator — IF-MOM-001 (获取订单列表) and IF-MOM-002 (请求订单下发).

Authority: 接口定义说明书一、MOM部分-0813.md. Behaviour that the definition leaves open
("待确认") is implemented as a clearly marked provisional policy (see docs/差异与未决事项说明.md)
and can be switched through environment variables without touching this module's contract code.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import Request
from fastapi.responses import JSONResponse

from simcore.app_factory import SimContext
from simcore.auth import check_basic
from simcore.config import env, env_bool, env_int
from simcore.timeutil import fmt_space, is_space_format, now_in, parse_space

ctx = SimContext("mom", "MOM simulator (IF-MOM-001 / IF-MOM-002)")
app = ctx.app
S = ctx.settings

# ---- configuration (contract-level constants come from the definition document) ----
BASIC_USER = env("MOM_BASIC_USER")
BASIC_PASS = env("MOM_BASIC_PASS")
SYSTEMTYPE = env_int("MOM_SYSTEMTYPE", 119)
MAX_RANGE_HOURS = env_int("MOM_MAX_RANGE_HOURS", 72)  # 定义 1.5-2 当前结论：最大查询时间范围为近3天
# 定义 1.2.1: WORKCENTER 必填 (definition wins). 使用说明 V1.2 采用 null 查四工位 => provisional opt-in.
WORKCENTER_NULL_POLICY = env("MOM_WORKCENTER_NULL_POLICY", "reject")  # reject | all
INTERVAL_MODE = env("MOM_INTERVAL_MODE", "half-open")  # half-open [START, END) | closed [START, END]
# Failure responses are NOT defined by the authority (1.3.2 / 2.3.2 待确认). Provisional policy:
#   http : HTTP 4xx + esbInfo.returnStatus="E" body   (default)
#   soft : HTTP 200 + esbInfo.returnStatus="E" body
ERROR_STYLE = env("MOM_ERROR_STYLE", "http")
REQUIRE_JSON_CT = env_bool("MOM_REQUIRE_JSON_CONTENT_TYPE", True)
WORKCENTERS = ["TS36120201", "TS36120202", "TS36120203", "TS36120204"]


def _esb_info(req_esb: dict | None, status: str, code: int, msg: str, extra: dict | None = None) -> dict:
    now = fmt_space(now_in(S.timezone))
    req_time = None
    if isinstance(req_esb, dict):
        req_time = req_esb.get("requestTime")
    info = {
        "instId": "",
        "requestTime": req_time if isinstance(req_time, str) else now,
        "responseTime": now,
        "returnStatus": status,
        "returnCode": code,
        "returnMsg": msg,
        "attr1": "",
        "attr2": "",
        "attr3": "",
    }
    if extra:
        info.update(extra)
    return info


def _error(req_esb, http_status: int, code: int, msg: str):
    body = {"esbInfo": _esb_info(req_esb, "E", code, msg), "resultInfo": None}
    status = 200 if ERROR_STYLE == "soft" else http_status
    return JSONResponse(status_code=status, content=body)


def _unauthorized():
    # Provisional: definition gives no auth-failure example; HTTP 401 is the Basic Auth standard (RFC 7617).
    return JSONResponse(
        status_code=401,
        content={"esbInfo": _esb_info(None, "E", 401, "unauthorized"), "resultInfo": None},
        headers={"WWW-Authenticate": 'Basic realm="MOM simulator"'},
    )


async def _read_json(request: Request):
    if REQUIRE_JSON_CT and "application/json" not in request.headers.get("content-type", "").lower():
        return None, "Content-Type must be application/json"
    try:
        body = await request.json()
    except Exception:
        return None, "request body is not valid JSON"
    if not isinstance(body, dict):
        return None, "request body must be a JSON object"
    return body, None


def _records() -> list[dict]:
    """Dataset: orders.json -> {"orders": [{"list": {...}, "detail": {...}}]}."""
    return ctx.ds.load("orders").get("orders", [])


def _in_range(ts: str, start: datetime, end: datetime) -> bool:
    try:
        t = datetime.fromisoformat(ts)
    except ValueError:
        return False
    if INTERVAL_MODE == "closed":
        return start <= t <= end
    return start <= t < end


# --------------------------------------------------------------------------------------
# IF-MOM-001 获取订单列表
# --------------------------------------------------------------------------------------
@app.post("/api/v1/get_orderlist")
async def get_orderlist(request: Request):
    if not check_basic(request, BASIC_USER, BASIC_PASS):
        return _unauthorized()
    body, err = await _read_json(request)
    if err:
        return _error(None, 400, 400, err)
    esb = body.get("esbInfo")
    req = body.get("requestInfo")
    if not isinstance(esb, dict) or not isinstance(req, dict):
        return _error(esb, 400, 400, "esbInfo and requestInfo objects are required")

    st = req.get("SYSTEMTYPE")
    if not isinstance(st, (int, float)) or isinstance(st, bool):
        return _error(esb, 400, 400, "requestInfo.SYSTEMTYPE must be a number")
    if int(st) != SYSTEMTYPE:
        return _error(esb, 400, 400, f"unknown SYSTEMTYPE {st}")

    start_s, end_s = req.get("STARTTIME"), req.get("ENDTIME")
    if not is_space_format(start_s) or not is_space_format(end_s):
        return _error(esb, 400, 400, "STARTTIME/ENDTIME are required in format yyyy-MM-dd HH:mm:ss")
    start, end = parse_space(start_s), parse_space(end_s)
    if end <= start:
        return _error(esb, 400, 400, "ENDTIME must be later than STARTTIME")
    if end - start > timedelta(hours=MAX_RANGE_HOURS):
        return _error(esb, 400, 400, f"query range exceeds {MAX_RANGE_HOURS} hours")

    wc = req.get("WORKCENTER")
    if wc is None:
        if WORKCENTER_NULL_POLICY != "all":
            return _error(esb, 400, 400, "requestInfo.WORKCENTER is required")
        wanted = set(WORKCENTERS)
    elif isinstance(wc, str) and wc in WORKCENTERS:
        wanted = {wc}
    else:
        return _error(esb, 400, 400, f"unknown WORKCENTER {wc!r}")

    # DEVICEPN: 孪生平台调用时不填写 (null). A non-null value is not rejected (no rule), only ignored.
    orders = []
    for rec in _records():
        li = rec.get("list") or {}
        if li.get("WORKCENTER") in wanted and _in_range(li.get("LASTUPDATEON", ""), start, end):
            orders.append(
                {
                    "WIPORDERNO": li.get("WIPORDERNO"),
                    "WIPORDERTYPE": li.get("WIPORDERTYPE"),
                    "ORDERSTATUS": li.get("ORDERSTATUS"),
                    "TEXT": li.get("TEXT"),
                    "OPRSTATUS": li.get("OPRSTATUS"),
                    "WORKCENTER": li.get("WORKCENTER"),
                    "LASTUPDATEON": li.get("LASTUPDATEON"),
                    "OPRSEQUENCENO": li.get("OPRSEQUENCENO"),
                }
            )
    orders.sort(key=lambda o: (o["LASTUPDATEON"] or "", o["WIPORDERNO"] or "", o["OPRSEQUENCENO"] or ""))
    return {
        "esbInfo": _esb_info(esb, "S", 200, "successfully", {"queryStartTime": start_s, "queryEndTime": end_s}),
        "resultInfo": {"SYSTEMTYPE": SYSTEMTYPE, "ORDERQUANTITY": len(orders), "MOMWIPORDERS": orders},
    }


# --------------------------------------------------------------------------------------
# IF-MOM-002 请求订单下发 (order detail)
# --------------------------------------------------------------------------------------
@app.post("/api/v1/get_order")
async def get_order(request: Request):
    if not check_basic(request, BASIC_USER, BASIC_PASS):
        return _unauthorized()
    body, err = await _read_json(request)
    if err:
        return _error(None, 400, 400, err)
    esb = body.get("esbInfo")
    req = body.get("requestInfo")
    if not isinstance(esb, dict) or not isinstance(req, dict):
        return _error(esb, 400, 400, "esbInfo and requestInfo objects are required")

    for k in ("SYSTEMTYPE", "WIPORDERNO", "WIPORDERTYPE", "OPRSEQUENCENO"):
        if not isinstance(req.get(k), str) or req.get(k) == "":
            return _error(esb, 400, 400, f"requestInfo.{k} must be a non-empty string")
    if req["SYSTEMTYPE"] != str(SYSTEMTYPE):
        return _error(esb, 400, 400, f"unknown SYSTEMTYPE {req['SYSTEMTYPE']!r}")

    for rec in _records():
        li = rec.get("list") or {}
        if (
            str(li.get("WIPORDERNO")) == req["WIPORDERNO"]
            and str(li.get("WIPORDERTYPE")) == req["WIPORDERTYPE"]
            and str(li.get("OPRSEQUENCENO")) == req["OPRSEQUENCENO"]
        ):
            detail = rec.get("detail")
            if not isinstance(detail, dict):
                break
            return {
                "esbInfo": _esb_info(esb, "S", 200, "successfully"),
                "resultInfo": {"SYSTEMTYPE": str(SYSTEMTYPE), "MOMWIPORDER": detail},
            }
    # 2.5-2: 订单号/类型/工序号不匹配时的返回 — 待确认. Provisional: 404 (http style) with returnStatus E.
    return _error(esb, 404, 404, "order/operation not found")


@app.get("/")
def index():
    return {
        "simulator": "MOM",
        "contracts": {
            "IF-MOM-001": "POST /api/v1/get_orderlist",
            "IF-MOM-002": "POST /api/v1/get_order",
        },
        "auth": "HTTP Basic",
        "admin": "/_sim/health, /_sim/status, /_sim/requests, /_sim/reload, /_sim/dataset",
    }


ctx.mount_admin(
    extra_status=lambda: {
        "policies": {
            "systemtype": SYSTEMTYPE,
            "max_range_hours": MAX_RANGE_HOURS,
            "workcenter_null_policy": WORKCENTER_NULL_POLICY,
            "interval_mode": INTERVAL_MODE,
            "error_style": ERROR_STYLE,
        },
        "records": len(_records()),
    }
)
