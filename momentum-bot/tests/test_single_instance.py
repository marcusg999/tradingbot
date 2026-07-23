import pytest

from single_instance import InstanceLock, LockHeldError


def test_second_lock_is_refused(tmp_path):
    path = str(tmp_path / "bot.lock")
    a = InstanceLock(path)
    a.acquire()
    try:
        if not a.supported:  # pragma: no cover - non-POSIX
            pytest.skip("flock not available on this platform")
        b = InstanceLock(path)
        with pytest.raises(LockHeldError):
            b.acquire()
    finally:
        a.release()


def test_lock_reacquirable_after_release(tmp_path):
    path = str(tmp_path / "bot.lock")
    a = InstanceLock(path)
    a.acquire()
    a.release()
    b = InstanceLock(path)
    b.acquire()          # should succeed now that a released
    b.release()


def test_context_manager(tmp_path):
    path = str(tmp_path / "bot.lock")
    with InstanceLock(path) as lock:
        if lock.supported:
            other = InstanceLock(path)
            with pytest.raises(LockHeldError):
                other.acquire()
    # After the with-block the lock is released and reacquirable.
    again = InstanceLock(path)
    again.acquire()
    again.release()
