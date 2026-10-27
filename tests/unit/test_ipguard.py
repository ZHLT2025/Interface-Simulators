"""Unit tests for simcore.ipguard decisions (F02). Pure-function tests; no network."""
from simcore.ipguard import DEFAULT_ADMIN_CIDRS, decide, parse_cidrs

ADMIN = parse_cidrs(DEFAULT_ADMIN_CIDRS)


def test_admin_paths_only_from_private_or_loopback():
    assert decide("/_sim/status", "127.0.0.1", ADMIN, []) is None
    assert decide("/_sim/status", "172.20.0.1", ADMIN, []) is None
    assert decide("/_sim/status", "::ffff:10.1.2.3", ADMIN, []) is None
    assert decide("/_sim/status", "185.209.161.145", ADMIN, []) is not None  # public internet
    assert decide("/_sim/cv/emit", "8.8.8.8", ADMIN, []) is not None
    assert decide("/_sim/status", None, ADMIN, []) is not None


def test_public_exemptions():
    for ip in ("8.8.8.8", "127.0.0.1", None):
        assert decide("/_sim/health", ip, ADMIN, []) is None
        assert decide("/_sim/cv/evidence/123/frame_001.jpg", ip, ADMIN, []) is None


def test_contract_paths_open_unless_client_cidrs_set():
    assert decide("/api/v1/get_orderlist", "8.8.8.8", ADMIN, []) is None
    nets = parse_cidrs("203.0.113.0/24, 198.51.100.7/32")
    assert decide("/api/v1/get_orderlist", "203.0.113.9", ADMIN, nets) is None
    assert decide("/api/v1/get_orderlist", "198.51.100.7", ADMIN, nets) is None
    assert decide("/api/v1/get_orderlist", "8.8.8.8", ADMIN, nets) is not None


def test_empty_admin_list_denies_all_admin():
    assert decide("/_sim/status", "127.0.0.1", [], []) is not None


def test_operator_cidr_extension():
    nets = parse_cidrs(DEFAULT_ADMIN_CIDRS + ",185.209.161.0/24")
    assert decide("/_sim/status", "185.209.161.145", nets, []) is None
