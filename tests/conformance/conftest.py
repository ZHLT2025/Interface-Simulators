"""Conformance tests run against a *deployed* stack (public IP by default) — never against in-process apps.

Expected values below are transcribed from the interface definition documents (Authority), not from the
simulator's own output. Configure targets via environment variables (see tests/conformance/run.sh).
"""
from __future__ import annotations

import os

import pytest

HOST = os.environ.get("SIM_HOST", "185.189.48.12")


def _url(port_env, default_port):
    return os.environ.get(port_env) or f"http://{HOST}:{default_port}"


@pytest.fixture(scope="session")
def mom_url():
    return _url("MOM_URL", 18001)


@pytest.fixture(scope="session")
def iot_url():
    return _url("IOT_URL", 18002)


@pytest.fixture(scope="session")
def esb_url():
    return _url("ESB_URL", 18003)


@pytest.fixture(scope="session")
def cv_url():
    return _url("CV_URL", 18004)


@pytest.fixture(scope="session")
def mqtt_target():
    return os.environ.get("MQTT_HOST", HOST), int(os.environ.get("MQTT_PORT", "18830"))


@pytest.fixture(scope="session")
def creds():
    return {
        "mom": (os.environ["MOM_BASIC_USER"], os.environ["MOM_BASIC_PASS"]),
        "esb": (os.environ["ESB_BASIC_USER"], os.environ["ESB_BASIC_PASS"]),
        "cv_sink": (os.environ["CV_SINK_BASIC_USER"], os.environ["CV_SINK_BASIC_PASS"]),
        "mqtt_sub": (os.environ["IOT_MQTT_SUB_USER"], os.environ["IOT_MQTT_SUB_PASS"]),
        "mqtt_pub": (os.environ["IOT_MQTT_PUB_USER"], os.environ["IOT_MQTT_PUB_PASS"]),
        "admin": os.environ["SIM_ADMIN_TOKEN"],
        "factory": os.environ.get("IOT_FACTORY_CODE", "BJZHLT"),
    }
