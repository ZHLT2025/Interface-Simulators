"""IS-08: one global run lock for every sending entry point of the process."""
import threading

import pytest

from simcore.control.runlock import RunLock, RunLockBusy


def test_second_acquire_is_refused_not_queued(tmp_path):
    lock = RunLock(tmp_path / "run.lock")
    t = lock.acquire("control:A:round")
    assert lock.locked() is True
    assert lock.holder()["holder"] == "control:A:round"
    with pytest.raises(RunLockBusy):
        lock.acquire("native_scenario:x")
    lock.release(t)
    assert lock.locked() is False
    assert lock.holder() is None
    lock.release(lock.acquire("cv_emit"))


def test_guard_releases_on_exception(tmp_path):
    lock = RunLock(tmp_path / "run.lock")
    with pytest.raises(ValueError):
        with lock.guard("cv_emit"):
            raise ValueError("boom")
    assert lock.locked() is False


def test_lock_is_exclusive_across_threads(tmp_path):
    lock = RunLock(tmp_path / "run.lock")
    held = lock.acquire("holder")
    refused = []

    def worker():
        try:
            lock.release(lock.acquire("other"))
            refused.append(False)
        except RunLockBusy:
            refused.append(True)

    th = threading.Thread(target=worker)
    th.start()
    th.join(5)
    lock.release(held)
    assert refused == [True]


def test_a_second_process_sees_the_file_lock(tmp_path):
    """The flock is what protects the state dir from a second process, not just a thread."""
    import os
    import subprocess
    import sys
    import textwrap

    lock = RunLock(tmp_path / "run.lock")
    token = lock.acquire("control:B:round")
    script = textwrap.dedent(
        f"""
        import fcntl, os, sys
        fd = os.open({str(tmp_path / 'run.lock')!r}, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("BUSY")
        else:
            print("FREE")
        """
    )
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30)
    assert out.stdout.strip() == "BUSY", out
    lock.release(token)
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30)
    assert out.stdout.strip() == "FREE", out
    assert os.path.exists(tmp_path / "run.lock")
