"""Target identity verification before a round may start (E02 SIM 目标身份校验).

Two independent checks, both mandatory, both run *before* a run_id is allocated, so a wrong or
drifted target can never consume a round number and never sends a single event:

  1. CV_TARGET_URL must equal the separately configured approved target in scheme, host, port and
     path. Two config values are compared on purpose: a drifted CV_TARGET_URL is exactly what this
     check is for, so the approved target is never defaulted from it.
  2. The approved target's own /readyz must answer with the expected isolated-test environment
     identity: data_domain == TEST_ISOLATED and artifact belonging to YQYD (both configurable),
     and ready must not be false.

No other request is made to the target; nothing is written there.
"""
from __future__ import annotations

import json
from urllib.parse import urlparse

import httpx

E_APPROVED_TARGET_NOT_CONFIGURED = "E_APPROVED_TARGET_NOT_CONFIGURED"
E_TARGET_MISMATCH = "E_TARGET_MISMATCH"
E_TARGET_SCHEME = "E_TARGET_SCHEME"
E_READYZ_UNREACHABLE = "E_READYZ_UNREACHABLE"
E_READYZ_UNPARSEABLE = "E_READYZ_UNPARSEABLE"
E_READYZ_STATUS = "E_READYZ_STATUS"
E_DATA_DOMAIN = "E_DATA_DOMAIN_MISMATCH"
E_ARTIFACT = "E_ARTIFACT_MISMATCH"
E_NOT_READY = "E_TARGET_NOT_READY"


def identity(url: str | None) -> dict:
    u = urlparse(url or "")
    return {"scheme": u.scheme, "host": u.hostname, "port": u.port or (443 if u.scheme == "https" else 80 if u.scheme == "http" else None), "path": u.path}


class TargetCheckError(Exception):
    def __init__(self, code: str, message: str, detail: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, **({"detail": self.detail} if self.detail else {})}


def verify_target(cfg, *, http_get=None, tls_verify: bool = True) -> dict:
    """Returns the evidence record on success, raises TargetCheckError otherwise."""
    approved = cfg.approved_target_url
    if not approved:
        raise TargetCheckError(
            E_APPROVED_TARGET_NOT_CONFIGURED,
            "SIM_CONTROL_APPROVED_TARGET_URL is not configured; the controller refuses to start a round",
        )
    want, got = identity(approved), identity(cfg.cv_target_url)
    if want["scheme"] not in ("http", "https"):
        raise TargetCheckError(E_TARGET_SCHEME, "approved target must be http or https", {"approved": approved})
    if want != got:
        raise TargetCheckError(
            E_TARGET_MISMATCH,
            "CV_TARGET_URL does not match the approved target in scheme+host+port+path",
            {"approved": want, "configured": got, "approved_url": approved, "configured_url": cfg.cv_target_url},
        )
    evidence = {"approved_target": approved, "identity": want, "readyz": None}
    if not cfg.require_readyz:
        evidence["readyz"] = {"checked": False, "reason": "SIM_CONTROL_REQUIRE_READYZ=false"}
        return evidence

    readyz_url = cfg.resolved_readyz_url()
    getter = http_get or _default_get
    try:
        status, text = getter(readyz_url, cfg.readyz_timeout_s, tls_verify)
    except Exception as e:
        raise TargetCheckError(E_READYZ_UNREACHABLE, f"{readyz_url} could not be reached: {type(e).__name__}: {e}", {"readyz_url": readyz_url})
    if status != 200:
        raise TargetCheckError(E_READYZ_STATUS, f"{readyz_url} answered HTTP {status}", {"readyz_url": readyz_url, "status": status, "body": (text or "")[:500]})
    try:
        body = json.loads(text)
    except Exception:
        raise TargetCheckError(E_READYZ_UNPARSEABLE, f"{readyz_url} did not answer JSON", {"readyz_url": readyz_url, "body": (text or "")[:500]})
    if not isinstance(body, dict):
        raise TargetCheckError(E_READYZ_UNPARSEABLE, f"{readyz_url} did not answer a JSON object", {"readyz_url": readyz_url})
    domain = body.get("data_domain")
    if isinstance(domain, dict):  # nested form: {"data_domain": {"observed": {"data_domain": ...}}}
        domain = (domain.get("observed") or {}).get("data_domain") or domain.get("expected")
    artifact = body.get("artifact")
    if domain != cfg.expect_data_domain:
        raise TargetCheckError(
            E_DATA_DOMAIN,
            "target data_domain is not the expected isolated test domain",
            {"expected": cfg.expect_data_domain, "observed": domain, "readyz_url": readyz_url},
        )
    if not isinstance(artifact, str) or cfg.expect_artifact_prefix.lower() not in artifact.lower():
        raise TargetCheckError(
            E_ARTIFACT,
            "target artifact is not the expected YQYD artifact",
            {"expected_contains": cfg.expect_artifact_prefix, "observed": artifact, "readyz_url": readyz_url},
        )
    if body.get("ready") is False:
        raise TargetCheckError(E_NOT_READY, "target reports ready=false", {"not_ready_because": body.get("not_ready_because"), "readyz_url": readyz_url})
    evidence["readyz"] = {
        "checked": True,
        "url": readyz_url,
        "status": status,
        "ready": body.get("ready"),
        "artifact": artifact,
        "version": body.get("version"),
        "data_domain": domain,
    }
    return evidence


def _default_get(url: str, timeout: float, tls_verify: bool):
    with httpx.Client(timeout=timeout, verify=tls_verify) as c:
        r = c.get(url)
    return r.status_code, r.text
