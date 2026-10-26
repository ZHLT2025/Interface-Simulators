from __future__ import annotations

import logging

from fastapi import FastAPI

from .admin import build_admin_router
from .config import SimSettings
from .datasets import DatasetManager
from .reqlog import RequestLog, RequestLogMiddleware

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


class SimContext:
    def __init__(self, sim: str, title: str):
        self.settings = SimSettings(sim)
        self.dsm = DatasetManager(self.settings.data_root, self.settings.dataset)
        self.reqlog = RequestLog(self.settings.request_log_size)
        self.app = FastAPI(title=title, docs_url=None, redoc_url=None, openapi_url=None)
        self.app.add_middleware(RequestLogMiddleware, reqlog=self.reqlog)

    def mount_admin(self, extra_status=None, on_dataset_change=None):
        self.app.include_router(build_admin_router(self.settings, self.dsm, self.reqlog, extra_status, on_dataset_change))

    @property
    def ds(self):
        return self.dsm.dataset
