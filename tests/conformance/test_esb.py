"""IF-SAP-001 / IF-WINDCHILL-001 — expectations from 接口定义说明书五、ESB部分-1013 §1.2-1.4, §2.2-2.4."""
import xml.etree.ElementTree as ET

import httpx
import pytest

ENV = "http://schemas.xmlsoap.org/soap/envelope/"
SAP = "http://www.tc.com/TCSAP/Inv/MaterialAndDocumentNew/v1.0"
WC = "http://adapter.tangche.ext/"


def sap_req(pmatnr, with_esb=True):
    esb = (
        "<esbInfo><instId>TEST-20260914-001</instId><requestTime>2026-09-14 16:20:00</requestTime>"
        "<attr1></attr1><attr2></attr2><attr3></attr3></esbInfo>"
        if with_esb
        else ""
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<soapenv:Envelope xmlns:soapenv="{ENV}"><soapenv:Header/><soapenv:Body>'
        f'<v1:request xmlns:v1="{SAP}">{esb}<requestInfo><PMATNR>{pmatnr}</PMATNR></requestInfo></v1:request>'
        "</soapenv:Body></soapenv:Envelope>"
    )


def wc_req(doc_json, api="getDocSecFileInfo"):
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<soapenv:Envelope xmlns:soapenv="{ENV}" xmlns:adap="{WC}"><soapenv:Header/><soapenv:Body>'
        f"<adap:BusAdapterService><apiName>{api}</apiName><apiDesc>文档附件接口</apiDesc>"
        f"<requestInfo><![CDATA[\n{doc_json}\n      ]]></requestInfo></adap:BusAdapterService>"
        "</soapenv:Body></soapenv:Envelope>"
    )


def find(root, path_locals, ns_list):
    """Walk by (namespace, local-name) pairs — never by prefix (definition instructs so)."""
    cur = root
    for local, ns in zip(path_locals, ns_list):
        nxt = None
        for ch in cur:
            tag = ch.tag
            if tag == f"{{{ns}}}{local}" or (ns is None and tag.split('}')[-1] == local):
                nxt = ch
                break
        assert nxt is not None, f"missing {local} in {ns}"
        cur = nxt
    return cur


SAP_HDR = {"Content-Type": "application/xml; charset=utf-8"}
WC_HDR = {"Content-Type": "text/xml; charset=UTF-8", "Accept": "text/xml, application/xml"}


@pytest.mark.parametrize("path", ["/ESB/SAP/SAP_MaterialAndDocumentNewQuery", "/ESB/SAP/SAP_MaterialAndDocumentNewQuery/1.0.0"])
def test_sap_success(esb_url, creds, path):
    r = httpx.post(esb_url + path, content=sap_req("M000004674786"), headers=SAP_HDR, auth=creds["esb"])
    assert r.status_code == 200
    assert "xml" in r.headers["content-type"]
    root = ET.fromstring(r.content)
    assert root.tag == f"{{{ENV}}}Envelope"
    docs = find(root, ["Body", "response", "resultInfo", "DOCS"], [ENV, SAP, SAP, SAP])
    body = find(root, ["Body"], [ENV])
    assert not any(ch.tag.endswith("}esbInfo") for ch in find(body, ["response"], [SAP]))  # 实测成功响应不携带 esbInfo
    items = [ch for ch in docs if ch.tag == f"{{{SAP}}}item"]
    assert items, "expected DOCS/item for M000004674786"
    fields = ["MANDT", "DOKAR", "DOKNR", "DOKVR", "DOKTL", "LANGU", "DKTXT", "LTXIN", "DKTXT_UC"]
    for it in items:
        assert [ch.tag.split('}')[-1] for ch in it] == fields  # all nodes present, order as sample
        assert find(it, ["MANDT"], [SAP]).text == "800" and find(it, ["DOKTL"], [SAP]).text == "000"
    by = {find(i, ["DOKNR"], [SAP]).text: i for i in items}
    assert find(by["CCD2035-12-00-0099-02-1"], ["DKTXT"], [SAP]).text == "02一位侧墙测量表"  # 1.4.2 sample
    assert find(by["CCD2035-12-01-0000-02"], ["DOKVR"], [SAP]).text == "C4"  # 最新版本 only (dataset also holds C3)
    assert len([i for i in items if find(i, ["DOKNR"], [SAP]).text == "CCD2035-12-01-0000-02"]) == 1


def test_sap_no_docs_and_unknown(esb_url, creds):
    for m in ("M000004674799", "NOPE-0001"):
        r = httpx.post(esb_url + "/ESB/SAP/SAP_MaterialAndDocumentNewQuery", content=sap_req(m, with_esb=False), headers=SAP_HDR, auth=creds["esb"])
        assert r.status_code == 200
        docs = find(ET.fromstring(r.content), ["Body", "response", "resultInfo", "DOCS"], [ENV, SAP, SAP, SAP])
        assert len(list(docs)) == 0  # 1.4.3 空 <DOCS/>


def test_sap_faults_and_auth(esb_url, creds):
    r = httpx.post(esb_url + "/ESB/SAP/SAP_MaterialAndDocumentNewQuery", content=sap_req("M000004674786"), headers=SAP_HDR)
    assert r.status_code == 401
    # missing PMATNR => SOAP Fault (HTTP 500 per SOAP 1.1 §6.2) with Client faultcode
    bad = sap_req("").replace("<PMATNR></PMATNR>", "")
    r = httpx.post(esb_url + "/ESB/SAP/SAP_MaterialAndDocumentNewQuery", content=bad, headers=SAP_HDR, auth=creds["esb"])
    assert r.status_code == 500
    fault = find(ET.fromstring(r.content), ["Body", "Fault"], [ENV, ENV])
    assert find(fault, ["faultcode"], [None]).text.endswith(":Client")
    # malformed XML
    r = httpx.post(esb_url + "/ESB/SAP/SAP_MaterialAndDocumentNewQuery", content="<not-xml", headers=SAP_HDR, auth=creds["esb"])
    assert r.status_code == 500
    # wrong envelope namespace => VersionMismatch
    r = httpx.post(esb_url + "/ESB/SAP/SAP_MaterialAndDocumentNewQuery", content=sap_req("M000004674786").replace(ENV, "http://www.w3.org/2003/05/soap-envelope"), headers=SAP_HDR, auth=creds["esb"])
    assert r.status_code == 500 and b"VersionMismatch" in r.content


def test_windchill_success(esb_url, creds):
    r = httpx.post(esb_url + "/ESB/WINDCHILL/WINDCHILL_GeneralInterface", content=wc_req('{"docNumber":"D0000518627"}'), headers=WC_HDR, auth=creds["esb"])
    assert r.status_code == 200
    root = ET.fromstring(r.content)
    resp = find(root, ["Body", "BusAdapterServiceResponse"], [ENV, WC])
    assert [ch.tag for ch in resp] == ["return", "resultInfo", "returnStatus", "returnMsg"]  # unprefixed children as in 2.4.2
    assert find(resp, ["return"], [None]).text == "S" and find(resp, ["returnStatus"], [None]).text == "S"
    assert len(list(find(resp, ["resultInfo"], [None]))) == 0
    url = find(resp, ["returnMsg"], [None]).text
    assert url == "http://plm.tangche.com/Windchill/wtcore/test/fileShareTemp/temp/structDoc/D0000518627工艺结构化文档.xls"  # 2.4.2


def test_windchill_doc_not_found(esb_url, creds):
    r = httpx.post(esb_url + "/ESB/WINDCHILL/WINDCHILL_GeneralInterface", content=wc_req('{"docNumber":"D9999999999"}'), headers=WC_HDR, auth=creds["esb"])
    assert r.status_code == 200  # 2.3.2: HTTP 200 with returnStatus=E
    resp = find(ET.fromstring(r.content), ["Body", "BusAdapterServiceResponse"], [ENV, WC])
    assert find(resp, ["return"], [None]).text == "E" and find(resp, ["returnStatus"], [None]).text == "E"
    assert find(resp, ["returnMsg"], [None]).text == "文档不存在！"


def test_windchill_errors(esb_url, creds):
    r = httpx.post(esb_url + "/ESB/WINDCHILL/WINDCHILL_GeneralInterface", content=wc_req('{"docNumber":"D0000518627"}'), headers=WC_HDR)
    assert r.status_code == 401
    r = httpx.post(esb_url + "/ESB/WINDCHILL/WINDCHILL_GeneralInterface", content=wc_req("not json"), headers=WC_HDR, auth=creds["esb"])
    assert r.status_code == 200 and b"<returnStatus>E</returnStatus>" in r.content
    r = httpx.post(esb_url + "/ESB/WINDCHILL/WINDCHILL_GeneralInterface", content=wc_req('{"docNumber":"D0000518627"}', api="otherApi"), headers=WC_HDR, auth=creds["esb"])
    assert r.status_code == 200 and b"<returnStatus>E</returnStatus>" in r.content
