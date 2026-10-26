"""ESB simulator — IF-SAP-001 (SAP物料对应文档的最新版查询) and IF-WINDCHILL-001 (Windchill获取文档链接).

Authority: 接口定义说明书五、ESB部分-1013.md. Both are SOAP 1.1 over HTTP POST. Response envelopes
reproduce the real production messages recorded in the definition (prefixes soap/ns2 and S/ns2).
XML is parsed by namespace URI + local-name (never by prefix), as the definition instructs consumers.
"""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from simcore.app_factory import SimContext
from simcore.auth import check_basic
from simcore.config import env, env_bool

ctx = SimContext("esb", "ESB simulator (IF-SAP-001 / IF-WINDCHILL-001)")
app = ctx.app
S = ctx.settings

BASIC_USER = env("ESB_BASIC_USER")
BASIC_PASS = env("ESB_BASIC_PASS")
SOAP_ENV_NS = "http://schemas.xmlsoap.org/soap/envelope/"
SAP_NS = "http://www.tc.com/TCSAP/Inv/MaterialAndDocumentNew/v1.0"
WC_NS = "http://adapter.tangche.ext/"
# 1.5-3: 物料号不存在 vs 存在但无文档 — 待确认. Provisional: unknown material => empty <DOCS/> (same as no docs).
UNKNOWN_MATERIAL = env("ESB_UNKNOWN_MATERIAL", "empty")  # empty | fault
STRICT_NAMESPACE = env_bool("ESB_STRICT_NAMESPACE", True)  # business root must be in the documented namespace
REQUIRE_SOAPACTION = env_bool("ESB_REQUIRE_SOAPACTION", False)  # 1.5-6 实测不携带亦可调用


# ---------------------------------------------------------------- SOAP helpers
def _xml_response(body: str, status: int = 200) -> Response:
    return Response(content=body, status_code=status, media_type="text/xml", headers={"Content-Type": "text/xml; charset=utf-8"})


def _fault(code: str, string: str, detail: str | None = None) -> Response:
    # SOAP 1.1 §6.2: Fault => HTTP 500; faultcode qualified with the envelope namespace prefix.
    d = f"<detail>{escape(detail)}</detail>" if detail else ""
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<soap:Envelope xmlns:soap="{SOAP_ENV_NS}"><soap:Body><soap:Fault>'
        f"<faultcode>soap:{code}</faultcode><faultstring>{escape(string)}</faultstring>{d}"
        "</soap:Fault></soap:Body></soap:Envelope>"
    )
    return _xml_response(body, 500)


def _unauthorized():
    # Provisional: definition gives no auth-failure example; HTTP 401 per RFC 7617.
    return Response(status_code=401, content="unauthorized", media_type="text/plain", headers={"WWW-Authenticate": 'Basic realm="ESB simulator"'})


def _local(tag: str) -> tuple[str, str]:
    if tag.startswith("{"):
        ns, _, local = tag[1:].partition("}")
        return ns, local
    return "", tag


def _find_child(elem, local: str, ns: str | None = None):
    for ch in elem:
        cns, cl = _local(ch.tag)
        if cl == local and (ns is None or cns == ns or (not STRICT_NAMESPACE)):
            return ch
    return None


async def _parse_envelope(request: Request):
    """Return (body_element, error_response)."""
    raw = await request.body()
    ct = request.headers.get("content-type", "").lower()
    if "xml" not in ct:
        return None, _fault("Client", f"unsupported Content-Type {ct or '(none)'}; expected XML")
    if REQUIRE_SOAPACTION and "soapaction" not in {k.lower() for k in request.headers.keys()}:
        return None, _fault("Client", "SOAPAction header required")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        return None, _fault("Client", f"malformed XML: {e}")
    rns, rl = _local(root.tag)
    if rl != "Envelope":
        return None, _fault("Client", "root element must be Envelope")
    if rns != SOAP_ENV_NS:
        return None, _fault("VersionMismatch", f"unsupported envelope namespace {rns!r}")
    body = _find_child(root, "Body", SOAP_ENV_NS)
    if body is None:
        return None, _fault("Client", "Envelope/Body missing")
    return body, None


def _text(elem) -> str:
    return (elem.text or "").strip() if elem is not None else ""


# ---------------------------------------------------------------- IF-SAP-001
def _sap_docs() -> dict:
    return ctx.ds.load("sap_docs").get("materials", {})


def _latest_per_doknr(items: list[dict]) -> list[dict]:
    """Return one item per DOKNR (实测: 每个文档编号仅返回最新版本). Latest = last listed in dataset,
    or the item flagged "latest": true. Dataset authors list versions oldest -> newest."""
    out: dict[str, dict] = {}
    for it in items:
        key = it.get("DOKNR")
        if key in out and out[key].get("latest") and not it.get("latest"):
            continue
        out[key] = it
    return list(out.values())


def _sap_item_xml(it: dict) -> str:
    fields = ["MANDT", "DOKAR", "DOKNR", "DOKVR", "DOKTL", "LANGU", "DKTXT", "LTXIN", "DKTXT_UC"]
    parts = [f"<ns2:{f}>{escape(str(it.get(f, '') if it.get(f) is not None else ''))}</ns2:{f}>" for f in fields]
    return "<ns2:item>" + "".join(parts) + "</ns2:item>"


async def _sap_handler(request: Request):
    if not check_basic(request, BASIC_USER, BASIC_PASS):
        return _unauthorized()
    body, err = await _parse_envelope(request)
    if err:
        return err
    req = _find_child(body, "request", SAP_NS)
    if req is None:
        return _fault("Client", f"Body/request element (namespace {SAP_NS}) not found")
    info = _find_child(req, "requestInfo")
    pmatnr = _text(_find_child(info, "PMATNR")) if info is not None else ""
    if not pmatnr:
        return _fault("Client", "requestInfo/PMATNR is required")
    materials = _sap_docs()
    if pmatnr not in materials and UNKNOWN_MATERIAL == "fault":
        return _fault("Client", f"material {pmatnr} not found")
    items = _latest_per_doknr(materials.get(pmatnr, []))
    docs = "<ns2:DOCS/>" if not items else "<ns2:DOCS>" + "".join(_sap_item_xml(i) for i in items) + "</ns2:DOCS>"
    xml = (
        f'<soap:Envelope xmlns:soap="{SOAP_ENV_NS}"><soap:Body>'
        f'<ns2:response xmlns:ns2="{SAP_NS}"><ns2:resultInfo>{docs}</ns2:resultInfo></ns2:response>'
        "</soap:Body></soap:Envelope>"
    )
    return _xml_response(xml)


@app.post("/ESB/SAP/SAP_MaterialAndDocumentNewQuery")
async def sap_default_version(request: Request):
    return await _sap_handler(request)


@app.post("/ESB/SAP/SAP_MaterialAndDocumentNewQuery/{version}")
async def sap_versioned(request: Request, version: str):
    if version != "1.0.0":
        return _fault("Client", f"unknown interface version {version}")
    return await _sap_handler(request)


# ---------------------------------------------------------------- IF-WINDCHILL-001
def _wc_docs() -> dict:
    return ctx.ds.load("windchill_docs").get("documents", {})


def _wc_response(status: str, msg: str) -> Response:
    xml = (
        f'<S:Envelope xmlns:S="{SOAP_ENV_NS}"><S:Body>'
        f'<ns2:BusAdapterServiceResponse xmlns:ns2="{WC_NS}">'
        f"<return>{status}</return><resultInfo/><returnStatus>{status}</returnStatus><returnMsg>{escape(msg)}</returnMsg>"
        "</ns2:BusAdapterServiceResponse></S:Body></S:Envelope>"
    )
    return _xml_response(xml)


@app.post("/ESB/WINDCHILL/WINDCHILL_GeneralInterface")
async def windchill_general(request: Request):
    if not check_basic(request, BASIC_USER, BASIC_PASS):
        return _unauthorized()
    body, err = await _parse_envelope(request)
    if err:
        return err
    svc = _find_child(body, "BusAdapterService", WC_NS)
    if svc is None:
        return _fault("Client", f"Body/BusAdapterService element (namespace {WC_NS}) not found")
    api_name = _text(_find_child(svc, "apiName"))
    if api_name != "getDocSecFileInfo":
        # Only the documented apiName is covered (2.2 说明 1). Provisional business failure.
        return _wc_response("E", f"不支持的apiName：{api_name or '(空)'}")
    raw_info = _text(_find_child(svc, "requestInfo"))
    try:
        info = json.loads(raw_info) if raw_info else None
    except json.JSONDecodeError:
        info = None
    if not isinstance(info, dict) or not isinstance(info.get("docNumber"), str) or not info["docNumber"]:
        return _wc_response("E", "requestInfo格式错误：需要JSON对象且包含docNumber")  # provisional
    doc = _wc_docs().get(info["docNumber"])
    if doc is None:
        return _wc_response("E", "文档不存在！")  # confirmed real failure message (2.3.2 / 2.4.3)
    url = doc if isinstance(doc, str) else doc.get("url", "")
    return _wc_response("S", url)


@app.get("/")
def index():
    return {
        "simulator": "ESB",
        "contracts": {
            "IF-SAP-001": "POST /ESB/SAP/SAP_MaterialAndDocumentNewQuery[/1.0.0]  (SOAP 1.1, application/xml)",
            "IF-WINDCHILL-001": "POST /ESB/WINDCHILL/WINDCHILL_GeneralInterface  (SOAP 1.1, text/xml, CDATA JSON)",
        },
        "auth": "HTTP Basic",
        "admin": "/_sim/health, /_sim/status, /_sim/requests, /_sim/reload, /_sim/dataset",
    }


ctx.mount_admin(
    extra_status=lambda: {
        "policies": {"unknown_material": UNKNOWN_MATERIAL, "strict_namespace": STRICT_NAMESPACE, "require_soapaction": REQUIRE_SOAPACTION},
        "materials": len(_sap_docs()),
        "documents": len(_wc_docs()),
    }
)
