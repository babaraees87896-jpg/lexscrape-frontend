"""Background worker — wnp9 declare state se local sports bets auto-settle."""

from __future__ import annotations

import os
import threading
import time

from mongodb.process_lock import lock_path, try_acquire_lock

_INTERVAL_SEC = max(5, int(os.getenv("EX99_AUTO_DECISION_SEC", "15")))
_started = False
_lock_handle = None


def _worker_loop() -> None:
    while True:
        try:
            from mongodb.wnp9_auto_decision import run_auto_decision_sync

            result = run_auto_decision_sync()
            data = result.get("data") or {}
            total = int(data.get("totalSettledBets") or 0)
            synced = int(data.get("syncedFancy") or 0)
            if total:
                print(f"[auto-decision] settled {total} bet(s)")
            elif synced:
                print(f"[auto-decision] synced {synced} fancy result(s)")
            elif result.get("error"):
                print(f"[auto-decision] {result.get('message')}")
            elif data.get("errors"):
                print(f"[auto-decision] warnings: {data.get('errors')}")
        except Exception as exc:
            print(f"[auto-decision] loop error: {exc}")
        time.sleep(_INTERVAL_SEC)


def start_auto_decision_worker() -> bool:
    """Start daemon thread once per machine (fcntl lock). Returns True if started."""
    global _started, _lock_handle

    if _started:
        return False
    if str(os.getenv("EX99_AUTO_DECISION", "1")).lower() in ("0", "false", "off", "no"):
        return False

    try:
        acquired, handle = try_acquire_lock(lock_path("auto_decision_worker"))
        if not acquired:
            return False
        _lock_handle = handle
    except OSError:
        return False

    _started = True
    thread = threading.Thread(target=_worker_loop, name="auto-decision-worker", daemon=True)
    thread.start()
    print(f"[auto-decision] worker started (every {_INTERVAL_SEC}s)")
    return True
