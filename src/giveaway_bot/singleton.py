"""One bot process per data directory.

Exactly-once side effects (one commitment post, one results post) rely on in-process
locks plus SQLite, so a second process on the same database (a deploy overlap, a local
run against production data) must refuse to start rather than double-post."""

import os
from pathlib import Path


class AlreadyRunning(RuntimeError):
    pass


class InstanceLock:
    def __init__(self, fd: int) -> None:
        self._fd = fd

    def release(self) -> None:
        if self._fd >= 0:
            _unlock(self._fd)
            os.close(self._fd)
            self._fd = -1


def single_instance(path: str | Path) -> InstanceLock:
    """Take an exclusive, non-blocking lock on `path`; held until release() or exit."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        _lock(fd)
    except OSError as e:
        os.close(fd)
        raise AlreadyRunning(f"another bot instance holds {path}") from e
    return InstanceLock(fd)


if os.name == "nt":
    import msvcrt

    def _lock(fd: int) -> None:
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)
