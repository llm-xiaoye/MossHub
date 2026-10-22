"""Serialise actual producer calls with IMPORT/QUERY transitions."""
from contextlib import contextmanager
import fcntl
import os
import stat
import threading

_local_guard = threading.RLock()

@contextmanager
def send_gate(settings):
    path = settings.home / "tmp/dispatch-mode.lock"
    with _local_guard:
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise RuntimeError("send coordination lock is not REGULAR")
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
