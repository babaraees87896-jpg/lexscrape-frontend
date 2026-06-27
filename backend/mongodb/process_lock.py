"""Cross-platform single-instance lock for background workers."""

from __future__ import annotations

import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Optional, Tuple

_thread_locks: dict[str, threading.Lock] = {}
_thread_locks_guard = threading.Lock()


def lock_path(name: str) -> Path:
    base = Path(os.getenv("EX99_LOCK_DIR", tempfile.gettempdir()))
    return base / f"ex99_{name}.lock"


def try_acquire_lock(path: Path) -> Tuple[bool, Optional[Any]]:
    try:
        import fcntl

        handle = open(path, "w")
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True, handle
    except (ImportError, OSError):
        pass

    key = str(path)
    with _thread_locks_guard:
        lock = _thread_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _thread_locks[key] = lock
        if lock.acquire(blocking=False):
            return True, lock
    return False, None
