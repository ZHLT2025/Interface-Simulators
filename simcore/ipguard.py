"""Source-IP access control (F02 hardening).

Two independent allow-lists, both evaluated on the *socket* peer address (proxy headers are never
trusted, so X-Forwarded-For cannot be used to spoof an allowed address):

* SIM_ADMIN_ALLOW_CIDRS  — applies to every /_sim/* path except the public exemptions below.
                           Default: loopback + RFC1918 (host, docker networks, LAN). Empty => deny all.
* SIM_CLIENT_ALLOW_CIDRS — applies to the contract paths (everything else). Default: empty => open,
                           because the customer contracts are plain HTTP/MQTT and tester addresses are
                           not known in advance; operators may narrow it per deployment.

Public exemptions (never restricted): /_sim/health (deployment probes) and /_sim/cv/evidence/* (image
bytes referenced by CV messages, served without credentials by design).
"""
from __future__ import annotations

import ipaddress
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

log = logging.getLogger("sim.ipguard")

DEFAULT_ADMIN_CIDRS = "127.0.0.0/8,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
PUBLIC_PREFIXES = ("/_sim/health", "/_sim/cv/evidence/")


def parse_cidrs(value: str | None) -> list:
    nets = []
    for part in (value or "").split(","):
        part = part.strip()
        if not part:
            continue
        nets.append(ipaddress.ip_network(part, strict=False))
    return nets


def ip_allowed(ip: str | None, nets: list) -> bool:
    if not nets or ip is None:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if addr.version == 6 and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return any(addr in n for n in nets)


def decide(path: str, client_ip: str | None, admin_nets: list, client_nets: list) -> str | None:
    """Return None when allowed, otherwise a short reason string."""
    if path.startswith(PUBLIC_PREFIXES):
        return None
    if path.startswith("/_sim/"):
        return None if ip_allowed(client_ip, admin_nets) else "admin endpoint not allowed from this address"
    if not client_nets:  # open by default for contract paths
        return None
    return None if ip_allowed(client_ip, client_nets) else "client address not allowed"


class IpGuardMiddleware:
    def __init__(self, app: ASGIApp, admin_cidrs: str | None, client_cidrs: str | None):
        self.app = app
        self.admin_nets = parse_cidrs(admin_cidrs)
        self.client_nets = parse_cidrs(client_cidrs)

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request = Request(scope)
        client_ip = request.client.host if request.client else None
        reason = decide(request.url.path, client_ip, self.admin_nets, self.client_nets)
        if reason is None:
            return await self.app(scope, receive, send)
        log.warning("denied %s %s from %s: %s", request.method, request.url.path, client_ip, reason)
        resp = JSONResponse(status_code=403, content={"detail": reason})
        await resp(scope, receive, send)
