"""Advisory single-instance lock.

Two bot processes sharing one Alpaca account is dangerous: both act on the same
signals and submit duplicate orders, and each cancels the other's stops. This
takes an exclusive OS-level advisory lock on a file (via ``fcntl.flock`` on
POSIX) so a second instance refuses to start.

Best-effort: on platforms without ``fcntl`` (e.g. Windows) it degrades to a
no-op with a warning rather than blocking startup.
"""
from __future__ import annotations

import os
from typing import Optional

try:
    import fcntl  # POSIX only
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None  # type: ignore


class LockHeldError(RuntimeError):
    """Raised when another process already holds the instance lock."""


class InstanceLock:
    def __init__(self, path: str) -> None:
        self.path = path
        self._fh = None
        self.supported = fcntl is not None

    def acquire(self) -> None:
        if not self.supported:  # pragma: no cover
            return
        self._fh = open(self.path, "w")
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._fh.close()
            self._fh = None
            raise LockHeldError(
                f"another instance already holds {self.path}") from exc
        self._fh.write(str(os.getpid()))
        self._fh.flush()

    def release(self) -> None:
        if self._fh is not None:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            except Exception:  # pragma: no cover
                pass
            self._fh.close()
            self._fh = None

    def __enter__(self) -> "InstanceLock":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()
