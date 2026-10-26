from __future__ import annotations

import base64
import hmac

from fastapi import Request


def check_basic(request: Request, username: str | None, password: str | None) -> bool:
    """Return True when the request carries valid HTTP Basic credentials.

    If no username is configured the check is disabled (returns True) so that a
    deployment can run the contract without auth for isolated tests. Production-like
    deployments always configure credentials via the environment file.
    """
    if not username:
        return True
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("basic "):
        return False
    try:
        raw = base64.b64decode(header[6:].strip(), validate=True).decode("utf-8")
    except Exception:
        return False
    user, sep, pwd = raw.partition(":")
    if not sep:
        return False
    return hmac.compare_digest(user, username) and hmac.compare_digest(pwd, password or "")
