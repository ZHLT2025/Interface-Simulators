"""The two-phase confirmation gate: single-use, 60 s, op-bound and state-bound."""
import pytest

from simcore.control import challenge as C


class FakeNow:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def store(ttl=60.0):
    now = FakeNow()
    return C.ChallengeStore(ttl, now=now), now


def test_confirm_requires_the_issued_challenge():
    s, _ = store()
    ch = s.issue("stop", {}, "fp1", {"effect": "x"})
    with pytest.raises(C.ChallengeError) as e:
        s.take("chl_not_issued", "stop", "fp1")
    assert e.value.code == C.E_CHALLENGE_UNKNOWN
    assert s.take(ch.id, "stop", "fp1") is ch


def test_a_challenge_is_single_use():
    s, _ = store()
    ch = s.issue("confirm_stop_point", {"stop_point_id": "x"}, "fp", {})
    s.take(ch.id, "confirm_stop_point", "fp")
    with pytest.raises(C.ChallengeError) as e:
        s.take(ch.id, "confirm_stop_point", "fp")
    assert e.value.code == C.E_CHALLENGE_USED


def test_expiry_is_60_seconds_by_default():
    s, now = store()
    ch = s.issue("init_start", {}, "fp", {})
    assert round(ch.expires - ch.created, 3) == 60.0
    now.t += 59.0
    assert s.take(ch.id, "init_start", "fp") is ch
    ch2 = s.issue("init_start", {}, "fp", {})
    now.t += 60.1
    with pytest.raises(C.ChallengeError) as e:
        s.take(ch2.id, "init_start", "fp")
    assert e.value.code == C.E_CHALLENGE_EXPIRED


def test_challenge_is_bound_to_its_operation():
    s, _ = store()
    ch = s.issue("pause", {}, "fp", {})
    with pytest.raises(C.ChallengeError) as e:
        s.take(ch.id, "stop", "fp")
    assert e.value.code == C.E_CHALLENGE_OP_MISMATCH


def test_challenge_is_bound_to_the_state_it_was_issued_for():
    """A challenge created before the round moved on can never be replayed."""
    s, _ = store()
    ch = s.issue("confirm_stop_point", {}, "B:AWAITING_CONFIRM:R1:3", {})
    with pytest.raises(C.ChallengeError) as e:
        s.take(ch.id, "confirm_stop_point", "B:AWAITING_CONFIRM:R1:4")
    assert e.value.code == C.E_CHALLENGE_STALE


def test_cancel_discards_without_performing():
    s, _ = store()
    ch = s.issue("stop", {}, "fp", {})
    s.cancel(ch.id, "stop")
    with pytest.raises(C.ChallengeError):
        s.take(ch.id, "stop", "fp")
    assert s.pending() == []
