"""IF-MOM-001 / IF-MOM-002 — expectations from 接口定义说明书一、MOM部分-0813 §1.2-1.4, §2.2-2.4."""
import re

import httpx
import pytest

TS = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


def body_001(**over):
    b = {
        "esbInfo": {"instId": None, "requestTime": "2026-07-30 16:36:50", "attr1": None, "attr2": None, "attr3": None},
        "requestInfo": {"SYSTEMTYPE": 119, "STARTTIME": "2026-07-28 00:00:00", "ENDTIME": "2026-07-30 16:00:00", "WORKCENTER": "TS36120201", "DEVICEPN": None},
    }
    b["requestInfo"].update(over)
    return b


def body_002(**over):
    b = {
        "esbInfo": {"instId": None, "requestTime": None, "attr1": None, "attr2": None, "attr3": None},
        "requestInfo": {"SYSTEMTYPE": "119", "WIPORDERNO": "001107007298", "WIPORDERTYPE": "101", "OPRSEQUENCENO": "0010", "DUMMY1": None, "DUMMY2": None, "DUMMY3": None},
    }
    b["requestInfo"].update(over)
    return b


def test_001_success_shape(mom_url, creds):
    r = httpx.post(f"{mom_url}/api/v1/get_orderlist", json=body_001(), auth=creds["mom"])
    assert r.status_code == 200  # 1.3.1 成功 HTTP 状态码
    assert r.headers["content-type"].startswith("application/json")
    j = r.json()
    e, ri = j["esbInfo"], j["resultInfo"]
    assert e["returnStatus"] == "S" and e["returnCode"] == 200  # 1.3.1 成功判定
    for k in ("instId", "requestTime", "responseTime", "returnMsg", "queryStartTime", "queryEndTime"):
        assert k in e
    assert e["requestTime"] == "2026-07-30 16:36:50"  # echoed
    assert TS.match(e["responseTime"])
    assert e["queryStartTime"] == "2026-07-28 00:00:00" and e["queryEndTime"] == "2026-07-30 16:00:00"
    assert ri["SYSTEMTYPE"] == 119 and isinstance(ri["SYSTEMTYPE"], int)  # Number
    assert isinstance(ri["MOMWIPORDERS"], list)
    assert ri["ORDERQUANTITY"] == len(ri["MOMWIPORDERS"])  # 应与数组元素数量一致
    assert ri["ORDERQUANTITY"] >= 1  # sample dataset holds 001107506304 @ TS36120201 2026-07-30T08:19:42
    o = ri["MOMWIPORDERS"][0]
    assert set(o) == {"WIPORDERNO", "WIPORDERTYPE", "ORDERSTATUS", "TEXT", "OPRSTATUS", "WORKCENTER", "LASTUPDATEON", "OPRSEQUENCENO"}
    assert isinstance(o["WIPORDERTYPE"], int) and isinstance(o["ORDERSTATUS"], int) and isinstance(o["OPRSTATUS"], int)
    assert isinstance(o["OPRSEQUENCENO"], str) and o["OPRSEQUENCENO"].startswith("0")  # 保留前导零
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$", o["LASTUPDATEON"])  # ISO 8601
    assert all(x["WORKCENTER"] == "TS36120201" for x in ri["MOMWIPORDERS"])


def test_001_time_window_filters(mom_url, creds):
    # window that excludes the TS36120201 record of 2026-07-30T08:19:42 => success with empty array (1.3.1)
    r = httpx.post(f"{mom_url}/api/v1/get_orderlist", json=body_001(STARTTIME="2026-07-30 09:00:00", ENDTIME="2026-07-30 16:00:00"), auth=creds["mom"])
    assert r.status_code == 200
    ri = r.json()["resultInfo"]
    assert ri["ORDERQUANTITY"] == 0 and ri["MOMWIPORDERS"] == []


def test_001_each_workcenter(mom_url, creds):
    for wc in ("TS36120201", "TS36120202", "TS36120203", "TS36120204"):
        r = httpx.post(f"{mom_url}/api/v1/get_orderlist", json=body_001(WORKCENTER=wc), auth=creds["mom"])
        assert r.status_code == 200 and r.json()["esbInfo"]["returnStatus"] == "S"


def test_001_basic_auth_required(mom_url):
    r = httpx.post(f"{mom_url}/api/v1/get_orderlist", json=body_001())
    assert r.status_code == 401
    r = httpx.post(f"{mom_url}/api/v1/get_orderlist", json=body_001(), auth=("x", "y"))
    assert r.status_code == 401 and r.json()["esbInfo"]["returnStatus"] == "E"


@pytest.mark.parametrize(
    "over",
    [
        {"STARTTIME": "2026/07/28 00:00:00"},  # wrong format
        {"ENDTIME": None},  # required
        {"SYSTEMTYPE": "119"},  # 001 requires Number
        {"SYSTEMTYPE": 120},
        {"WORKCENTER": "TS99999999"},
        {"WORKCENTER": None},  # definition: 必填 (default policy reject)
        {"STARTTIME": "2026-07-20 00:00:00", "ENDTIME": "2026-07-30 16:00:00"},  # > 72h (待确认事项2 当前结论)
    ],
)
def test_001_parameter_errors(mom_url, creds, over):
    r = httpx.post(f"{mom_url}/api/v1/get_orderlist", json=body_001(**over), auth=creds["mom"])
    assert r.status_code == 400  # provisional policy MOM_ERROR_STYLE=http
    assert r.json()["esbInfo"]["returnStatus"] == "E"


def test_002_success_shape(mom_url, creds):
    r = httpx.post(f"{mom_url}/api/v1/get_order", json=body_002(), auth=creds["mom"])
    assert r.status_code == 200
    j = r.json()
    assert j["esbInfo"]["returnStatus"] == "S" and j["esbInfo"]["returnCode"] == 200
    ri = j["resultInfo"]
    assert ri["SYSTEMTYPE"] == "119"  # String in 002
    o = ri["MOMWIPORDER"]
    required = [
        "WIPORDERNO", "WIPORDERTYPE", "ORDERSTATUS", "PARENTWIPORDERNO", "PRODUCTNO", "PRODUCTTEXT", "WBSCODE", "ZXNUM", "ZCC",
        "SCHEDULEDSTARTDATE", "SCHEDULEDCOMPLETIONDATE", "PROJECTNO", "PROJECTTEXT", "SERIALNO", "ORDERACTUALSTARTDATE",
        "ORDERACTUALCOMPLETIONDATE", "OPRACTUALSTARTDATE", "OPRACTUALCOMPLETIONDATE", "SERIALNOUPDATETIME", "INSPCOMPLETETIME", "CARTYPE", "MOMWIPORDEROPR",
    ]
    for k in required:
        assert k in o, k
    assert o["WIPORDERNO"] == "001107007298" and o["WIPORDERTYPE"] == "101"
    assert isinstance(o["ORDERSTATUS"], str)
    opr = o["MOMWIPORDEROPR"]
    assert opr["OPRSEQUENCENO"] == "0010"
    for k in ("TEXT", "OPRSTATUS", "WORKCENTER", "SCHEDULEDSTARTDATE", "SCHEDULEDCOMPLETIONDATE", "MOMCONFIG"):
        assert k in opr
    assert "PPRODUCTNO" in opr["MOMCONFIG"]


def test_002_identity_matches_request(mom_url, creds):
    r = httpx.post(f"{mom_url}/api/v1/get_order", json=body_002(WIPORDERNO="001107007298", OPRSEQUENCENO="0020"), auth=creds["mom"])
    assert r.status_code == 200
    o = r.json()["resultInfo"]["MOMWIPORDER"]
    assert o["WIPORDERNO"] == "001107007298" and o["MOMWIPORDEROPR"]["OPRSEQUENCENO"] == "0020"


def test_002_not_found_and_errors(mom_url, creds):
    r = httpx.post(f"{mom_url}/api/v1/get_order", json=body_002(WIPORDERNO="000000000000"), auth=creds["mom"])
    assert r.status_code == 404 and r.json()["esbInfo"]["returnStatus"] == "E"  # provisional (2.5-2 待确认)
    r = httpx.post(f"{mom_url}/api/v1/get_order", json=body_002(SYSTEMTYPE=119), auth=creds["mom"])  # must be String
    assert r.status_code == 400
    r = httpx.post(f"{mom_url}/api/v1/get_order", json=body_002(), auth=("bad", "creds"))
    assert r.status_code == 401
