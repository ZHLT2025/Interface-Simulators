"""Target identity verification before a round may start (E02 SIM)."""
import pytest

from simcore.control.config import ControlConfig
from simcore.control import target as T


def cfg(**kw):
    base = dict(
        state_dir=None,
        cv_target_url="https://185.189.48.12:5110/api/v1/cv-events",
        approved_target_url="https://185.189.48.12:5110/api/v1/cv-events",
        readyz_url="https://185.189.48.12:5110/readyz",
    )
    base.update(kw)
    return ControlConfig(**base)


READY = '{"ready": true, "artifact": "yqyd-backend", "version": "0.6.0", "data_domain": "TEST_ISOLATED"}'


def getter(status=200, body=READY):
    def _get(url, timeout, verify):
        return status, body

    return _get


def test_happy_path_records_the_evidence():
    ev = T.verify_target(cfg(), http_get=getter())
    assert ev["identity"] == {"scheme": "https", "host": "185.189.48.12", "port": 5110, "path": "/api/v1/cv-events"}
    assert ev["readyz"]["data_domain"] == "TEST_ISOLATED"
    assert ev["readyz"]["artifact"] == "yqyd-backend"


def test_approved_target_must_be_configured_explicitly():
    with pytest.raises(T.TargetCheckError) as e:
        T.verify_target(cfg(approved_target_url=None), http_get=getter())
    assert e.value.code == T.E_APPROVED_TARGET_NOT_CONFIGURED


@pytest.mark.parametrize(
    "drifted",
    [
        "http://185.189.48.12:5110/api/v1/cv-events",      # scheme
        "https://185.189.48.13:5110/api/v1/cv-events",     # host
        "https://185.189.48.12:5111/api/v1/cv-events",     # port
        "https://185.189.48.12:5110/api/v2/cv-events",     # path
        "https://185.189.48.12/api/v1/cv-events",          # implicit port
        None,
    ],
)
def test_any_drift_of_cv_target_url_is_refused(drifted):
    with pytest.raises(T.TargetCheckError) as e:
        T.verify_target(cfg(cv_target_url=drifted), http_get=getter())
    assert e.value.code == T.E_TARGET_MISMATCH


def test_readyz_must_report_the_isolated_test_domain_and_the_yqyd_artifact():
    cases = {
        '{"ready": true, "artifact": "yqyd-backend", "data_domain": "PRODUCTION"}': T.E_DATA_DOMAIN,
        '{"ready": true, "artifact": "stm-backend", "data_domain": "TEST_ISOLATED"}': T.E_ARTIFACT,
        '{"ready": false, "artifact": "yqyd-backend", "data_domain": "TEST_ISOLATED"}': T.E_NOT_READY,
        '{"artifact": "yqyd-backend"}': T.E_DATA_DOMAIN,
        "not json": T.E_READYZ_UNPARSEABLE,
    }
    for body, code in cases.items():
        with pytest.raises(T.TargetCheckError) as e:
            T.verify_target(cfg(), http_get=getter(body=body))
        assert e.value.code == code, body


def test_readyz_transport_and_status_failures_refuse_the_start():
    with pytest.raises(T.TargetCheckError) as e:
        T.verify_target(cfg(), http_get=getter(status=503, body="{}"))
    assert e.value.code == T.E_READYZ_STATUS

    def boom(url, timeout, verify):
        raise OSError("connection refused")

    with pytest.raises(T.TargetCheckError) as e:
        T.verify_target(cfg(), http_get=boom)
    assert e.value.code == T.E_READYZ_UNREACHABLE


def test_nested_readyz_data_domain_is_understood():
    body = '{"ready": true, "artifact": "yqyd-backend", "data_domain": {"expected": "TEST_ISOLATED", "observed": {"data_domain": "TEST_ISOLATED"}}}'
    assert T.verify_target(cfg(), http_get=getter(body=body))["readyz"]["data_domain"] == "TEST_ISOLATED"


def test_default_readyz_url_is_derived_from_the_approved_target():
    c = cfg(readyz_url=None)
    assert c.resolved_readyz_url() == "https://185.189.48.12:5110/readyz"
