"""Live scorecard via Socket.IO (same as site game-details page)."""

from __future__ import annotations

import os
import threading
import time

import socketio

SCORE_WS = os.environ.get("SCORECARD_WS", "wss://dimclscore.external247services.com")
SCORE_TIMEOUT = float(os.environ.get("SCORECARD_TIMEOUT", "4"))
SCORE_RETRIES = max(1, int(os.environ.get("SCORECARD_RETRIES", "3")))

_PROXY_KEYS = (
    "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
    "ALL_PROXY", "all_proxy", "SOCKS_PROXY", "SOCKS5_PROXY",
    "socks_proxy", "socks5_proxy",
)


class _NoProxy:
    def __enter__(self):
        self._saved = {k: os.environ.pop(k, None) for k in _PROXY_KEYS}
        return self

    def __exit__(self, *args):
        for k, v in self._saved.items():
            if v is not None:
                os.environ[k] = v


def _fetch_once(event_id: str, provider_id: str = "1") -> dict | None:
    event_id = str(event_id)
    sio = socketio.Client(logger=False, engineio_logger=False)
    result: dict | None = None
    ready = threading.Event()

    def _capture(data):
        nonlocal result
        summary = data.get("summary") if isinstance(data, dict) else None
        if isinstance(summary, dict):
            result = summary.get("data") or summary
        else:
            result = summary
        if result:
            ready.set()

    @sio.on("summary_snapshot")
    def on_snapshot(data):
        _capture(data)

    @sio.on("summary_update")
    def on_update(data):
        _capture(data)

    @sio.event
    def connect():
        sio.emit("subscribe_event", {"eventId": event_id, "providerId": provider_id})

    try:
        with _NoProxy():
            sio.connect(SCORE_WS, transports=["websocket"], wait_timeout=min(10, SCORE_TIMEOUT + 4))
            deadline = time.time() + SCORE_TIMEOUT
            while time.time() < deadline and not ready.is_set():
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                ready.wait(timeout=min(0.25, remaining))
            sio.disconnect()
    except Exception:
        try:
            sio.disconnect()
        except Exception:
            pass
        if result:
            return result
        raise

    return result


def fetch_scorecard(event_id: str | int, provider_id: str = "1") -> dict | None:
    """
    Subscribe to dimclscore websocket and return latest scoreboard snapshot.

    Site uses gamedetailPrivate.info.oldgmid (usually same as gmid) as eventId.
    Only works when match info has scard=1.
    """
    event_id = str(event_id)
    last_err: Exception | None = None
    for attempt in range(SCORE_RETRIES):
        try:
            result = _fetch_once(event_id, provider_id)
            if result:
                return result
        except Exception as exc:
            last_err = exc
            if attempt + 1 < SCORE_RETRIES:
                time.sleep(0.35 * (attempt + 1))
    if last_err:
        raise last_err
    return None
