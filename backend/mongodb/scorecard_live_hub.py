"""Persistent dimclscore WebSocket — scorecard file/memory turant update (poll nahi)."""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SCRAPE2 = _ROOT / "scrape2"

_hub: "ScorecardLiveHub | None" = None
_hub_lock = threading.Lock()


class ScorecardLiveHub:
    def __init__(self) -> None:
        self._subs: dict[str, dict] = {}
        self._guard = threading.Lock()

    @staticmethod
    def _key(eid: str, gmid: str) -> str:
        return f"{eid}:{gmid}"

    def sync_watchlist(self, items: list[tuple[str, str, str]]) -> None:
        """Subscribe in-play matches: (eid, scrape2_gmid, ws_event_id)."""
        want = {self._key(e, g): (e, g, w) for e, g, w in items}
        with self._guard:
            for k in list(self._subs):
                if k not in want:
                    self._stop(k)
            for k, (eid, gmid, ws_id) in want.items():
                cur = self._subs.get(k)
                if cur and cur.get("ws_id") == ws_id:
                    continue
                if cur:
                    self._stop(k)
                self._start(eid, gmid, ws_id)

    def _stop(self, key: str) -> None:
        sub = self._subs.pop(key, None)
        if not sub:
            return
        sub["stop"].set()
        try:
            sub["sio"].disconnect()
        except Exception:
            pass

    def _start(self, eid: str, gmid: str, ws_id: str) -> None:
        if str(_SCRAPE2) not in sys.path:
            sys.path.insert(0, str(_SCRAPE2))

        import socketio

        from scorecard import SCORE_WS, _NoProxy
        from scorecard_api import _persist_scrape2_scorecard, set_live_scorecard_cache

        stop = threading.Event()
        sio = socketio.Client(logger=False, engineio_logger=False)
        key = self._key(eid, gmid)

        def _capture(data) -> None:
            summary = data.get("summary") if isinstance(data, dict) else None
            if isinstance(summary, dict):
                sc = summary.get("data") or summary
            else:
                sc = summary
            if isinstance(sc, dict) and sc and not sc.get("error"):
                try:
                    _persist_scrape2_scorecard(eid, gmid, ws_id, sc)
                    set_live_scorecard_cache(eid, gmid, sc)
                except Exception:
                    pass

        @sio.on("summary_snapshot")
        def on_snapshot(data):
            _capture(data)

        @sio.on("summary_update")
        def on_update(data):
            _capture(data)

        @sio.event
        def connect():
            sio.emit("subscribe_event", {"eventId": str(ws_id), "providerId": "1"})

        def run() -> None:
            backoff = 1.0
            while not stop.is_set():
                try:
                    with _NoProxy():
                        sio.connect(SCORE_WS, transports=["websocket"], wait_timeout=8)
                    backoff = 1.0
                    while not stop.is_set() and sio.connected:
                        time.sleep(0.35)
                except Exception:
                    pass
                finally:
                    try:
                        sio.disconnect()
                    except Exception:
                        pass
                if not stop.is_set():
                    time.sleep(backoff)
                    backoff = min(backoff * 1.4, 8.0)

        threading.Thread(target=run, daemon=True, name=f"sc-live-{gmid}").start()
        self._subs[key] = {"sio": sio, "stop": stop, "ws_id": ws_id, "eid": eid, "gmid": gmid}


def get_scorecard_live_hub() -> ScorecardLiveHub:
    global _hub
    with _hub_lock:
        if _hub is None:
            _hub = ScorecardLiveHub()
        return _hub


def live_hub_enabled() -> bool:
    return os.getenv("EX99_SCORECARD_LIVE_HUB", "1").lower() not in ("0", "false", "off", "no")
