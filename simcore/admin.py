from __future__ import annotations

import hmac

from fastapi import APIRouter, HTTPException, Request

from .config import SimSettings
from .datasets import DatasetError, DatasetManager
from .reqlog import RequestLog


def build_admin_router(settings: SimSettings, dsm: DatasetManager, reqlog: RequestLog, extra_status=None, on_dataset_change=None) -> APIRouter:
    """Admin / observability endpoints. All paths live under /_sim/ which is outside every contract.

    /_sim/health is public (used by deployment checks). Everything else requires
    the X-Sim-Admin-Token header when SIM_ADMIN_TOKEN is configured.
    """
    router = APIRouter(prefix="/_sim")

    def guard(request: Request):
        if settings.admin_token is None:
            raise HTTPException(status_code=403, detail="admin endpoints disabled (SIM_ADMIN_TOKEN not set)")
        token = request.headers.get("x-sim-admin-token", "")
        if not hmac.compare_digest(token, settings.admin_token):
            raise HTTPException(status_code=401, detail="invalid admin token")

    @router.get("/health")
    def health():
        return {"sim": settings.sim, "status": "ok", "dataset": dsm.name}

    @router.get("/status")
    def status(request: Request):
        guard(request)
        body = {
            "sim": settings.sim,
            "dataset": dsm.name,
            "dataset_dir": str(dsm.dataset.directory),
            "dataset_files": dsm.dataset.files(),
            "datasets_available": dsm.available(),
            "requests_seen": reqlog.counter,
            "access_control": {"admin_allow_cidrs": settings.admin_allow_cidrs, "client_allow_cidrs": settings.client_allow_cidrs or "(open)"},
        }
        if extra_status:
            body.update(extra_status())
        return body

    @router.get("/requests")
    def requests_(request: Request, n: int = 50, path: str | None = None):
        guard(request)
        return reqlog.recent(n, path)

    @router.post("/reload")
    def reload(request: Request):
        guard(request)
        try:
            dsm.reload()
        except DatasetError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if on_dataset_change:
            on_dataset_change()
        return {"reloaded": True, "dataset": dsm.name, "files": dsm.dataset.files()}

    @router.post("/dataset")
    def dataset(request: Request, body: dict):
        guard(request)
        name = body.get("name")
        if not name or "/" in name or name.startswith("."):
            raise HTTPException(status_code=400, detail="name required")
        try:
            dsm.switch(name)
        except DatasetError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if on_dataset_change:
            on_dataset_change()
        return {"dataset": dsm.name, "files": dsm.dataset.files()}

    return router
