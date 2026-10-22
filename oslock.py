"""A lock file held for as long as a process runs.

The operating system releases the lock when the process exits, even after a
crash, so a stale file never blocks the next run. Used to keep radar runs
from overlapping (the app's scheduler, a manual run, or the old scheduled
task during an upgrade) and to keep the app itself to one copy per user.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


class FileLock:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._f = None

    def acquire(self) -> bool:
        """True when this process now holds the lock; False when another does."""
        if self._f:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        f = open(self.path, "a+b")
        try:
            if sys.platform == "win32":
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            return False
        f.seek(0)
        f.truncate()
        f.write(str(os.getpid()).encode())
        f.flush()
        self._f = f
        return True

    def release(self) -> None:
        if not self._f:
            return
        try:
            if sys.platform == "win32":
                import msvcrt
                self._f.seek(0)
                msvcrt.locking(self._f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._f.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        self._f.close()
        self._f = None

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.release()
