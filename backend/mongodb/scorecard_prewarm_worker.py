"""Background — live WebSocket hub + scrape2 scorecard file fallback."""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

from mongodb.process_lock import lock_path, try_acquire_lock

_ROOT = Path(__file__).resolve().parents[1]
_SCRAPE2 = _ROOT / "scrape2"
_INTERVAL_SEC = max(1, int(os.getenv("EX99_SCORECARD_REFRESH_SEC", "1")))
_STALE_SEC = max(0.5, float(os.getenv("SCORECARD_LIVE_MAX_AGE", "1")))
_started = False
_lock_handle = None


def _inplay_scorecard_targets() -> list[tuple[str, str, str]]:
    """(eid, scrape2_gmid, ws_event_id) for live in-play cricket."""
    from scorecard_api import SCRAPE2_SPORT, _match_info, _read_json, _scrape2_highlights, _ws_event_id, resolve_scrape2_gmid

    out: dict[str, tuple[str, str, str]] = {}
    for row in _scrape2_highlights("4"):
        gmid = row.get("gmid")
        if gmid is None:
            continue
        gmid_s = str(gmid)
        mf = SCRAPE2_SPORT / "matches" / "4" / f"{gmid_s}.json"
        info = _match_info(_read_json(mf) or {}) if mf.is_file() else {}
        if not info.get("scard"):
            continue
        if row.get("iplay") or info.get("iplay"):
            ws_id = _ws_event_id("4", gmid_s)
            out[gmid_s] = ("4", gmid_s, ws_id)

    try:
        from mongodb.db import get_db

        for m in get_db().matches.find(
            {"status": {"$in": ["INPLAY", "OPEN", "inplay"]}},
            {"_id": 0, "eventId": 1, "sportId": 1},
        ):
            if str(m.get("sportId") or "4") not in ("4", "Cricket", "cricket"):
                continue
            gmid, _ = resolve_scrape2_gmid("4", str(m.get("eventId") or ""))
            if gmid:
                ws_id = _ws_event_id("4", gmid)
                out[gmid] = ("4", gmid, ws_id)
    except Exception:
        pass
    return list(out.values())


def refresh_scrape2_scorecards() -> int:
    """Stale files par one-shot fetch — hub miss / reconnect fallback."""
    from scorecard_api import refresh_scorecard_if_stale

    updated = 0
    for _eid, gmid_s, _ws in _inplay_scorecard_targets():
        if refresh_scorecard_if_stale("4", gmid_s, max_age=_STALE_SEC):
            updated += 1
    return updated


def _loop() -> None:
    from mongodb.scorecard_live_hub import get_scorecard_live_hub, live_hub_enabled

    hub = get_scorecard_live_hub() if live_hub_enabled() else None
    while True:
        try:
            targets = _inplay_scorecard_targets()
            if hub is not None:
                hub.sync_watchlist(targets)
            n = refresh_scrape2_scorecards()
            if n:
                print(f"[scorecard-prewarm] fallback refreshed {n} file(s)")
        except Exception as exc:
            print(f"[scorecard-prewarm] loop error: {exc}")
        time.sleep(_INTERVAL_SEC)


def start_scorecard_prewarm_worker() -> bool:
    global _started, _lock_handle
    if _started:
        return False
    if str(os.getenv("EX99_SCORECARD_PREWARM", "1")).lower() in ("0", "false", "off", "no"):
        return False
    try:
        acquired, handle = try_acquire_lock(lock_path("scorecard_prewarm"))
        if not acquired:
            return False
        _lock_handle = handle
    except OSError:
        return False
    _started = True
    threading.Thread(target=_loop, name="scorecard-prewarm", daemon=True).start()
    hub_on = os.getenv("EX99_SCORECARD_LIVE_HUB", "1").lower() not in ("0", "false", "off", "no")
    mode = "live-hub+" if hub_on else ""
    print(f"[scorecard-prewarm] worker started ({mode}every {_INTERVAL_SEC}s)")
    return True
