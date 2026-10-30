"""Single global run lock shared by every sending entry point (IS-08 / G-SIM-04 / F02).

One simulator process must never have two concurrent sending tasks, no matter which door was
used: the manual single-round controller, the native `/_sim/cv/scenario/run` runner and the
single-shot `/_sim/cv/emit(-raw)` endpoints all take this same lock.

Two layers, both required:
  * an in-process `threading.Lock` (protects the threads of this uvicorn process);
  * an `fcntl.flock` on a file in the control state dir (protects a second process that was
    started against the same state directory, e.g. a stray container or a manual uvicorn).

Acquisition is always non-blocking: a busy lock is an immediate, audited 409 — never a queue.
"""
from __future__ import annotations

import fcntl
import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path


class RunLockBusy(Exception):
    def __init__(self, holder: dict | None):
        super().__init__("run lock busy")
        self.holder = holder


@dataclass
class LockToken:
    holder: str
    fd: int
    acquired_at: float


class RunLock:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.holder_path = self.path.with_suffix(".holder.json")
        self._mutex = threading.Lock()          # protects the fields below
        self._local = threading.Lock()          # the in-process ownership flag
        self._token: LockToken | None = None

    # ------------------------------------------------------------------ state
    def holder(self) -> dict | None:
        with self._mutex:
            if self._token is not None:
                return {"holder": self._token.holder, "pid": os.getpid(), "acquired_at": self._token.acquired_at, "scope": "in-process"}
        try:
            with self.holder_path.open("r", encoding="utf-8") as f:
                rec = json.load(f)
        except Exception:
            return None
        return rec if self.is_locked_externally() else None

    def is_locked_externally(self) -> bool:
        """True when some *other* process holds the file lock."""
        if self._token is not None:
            return False
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError:
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        finally:
            os.close(fd)

    def locked(self) -> bool:
        with self._mutex:
            if self._token is not None:
                return True
        return self.is_locked_externally()

    # ------------------------------------------------------------------ acquire / release
    def acquire(self, holder: str) -> LockToken:
        """Non-blocking. Raises RunLockBusy with the current holder when taken."""
        if not self._local.acquire(blocking=False):
            raise RunLockBusy(self.holder())
        fd = None
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise RunLockBusy(self._read_holder_file())
            token = LockToken(holder=holder, fd=fd, acquired_at=time.time())
            with self._mutex:
                self._token = token
            self._write_holder_file(holder, token.acquired_at)
            return token
        except BaseException:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            self._local.release()
            raise

    def release(self, token: LockToken) -> None:
        with self._mutex:
            if self._token is not token:
                return
            self._token = None
        try:
            fcntl.flock(token.fd, fcntl.LOCK_UN)
        finally:
            try:
                os.close(token.fd)
            except OSError:
                pass
            self._clear_holder_file()
            self._local.release()

    class _Guard:
        def __init__(self, lock: "RunLock", holder: str):
            self.lock, self.holder, self.token = lock, holder, None

        def __enter__(self):
            self.token = self.lock.acquire(self.holder)
            return self.token

        def __exit__(self, *exc):
            if self.token is not None:
                self.lock.release(self.token)
            return False

    def guard(self, holder: str) -> "RunLock._Guard":
        """`with run_lock.guard("cv_emit"): ...` — short-lived exclusive use."""
        return RunLock._Guard(self, holder)

    # ------------------------------------------------------------------ holder bookkeeping
    def _write_holder_file(self, holder: str, at: float):
        tmp = self.holder_path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump({"holder": holder, "pid": os.getpid(), "acquired_at": at}, f)
        tmp.replace(self.holder_path)

    def _clear_holder_file(self):
        try:
            self.holder_path.unlink()
        except FileNotFoundError:
            pass

    def _read_holder_file(self) -> dict | None:
        try:
            with self.holder_path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
