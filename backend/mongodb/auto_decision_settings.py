"""Per-match auto-decision toggles (bookmaker / fancy) for staff dashboard."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from mongodb.centerpanel_api import _err, _ok
from mongodb.db import get_db
from mongodb.matches_api import get_match_list

COLLECTION = "auto_decision_settings"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _default_flags() -> dict[str, bool]:
    return {"bookmakerAuto": True, "fancyAuto": True}


def get_auto_decision_flags(db, market_id: str) -> dict[str, bool]:
    """Return toggles for a market — missing doc means both ON."""
    doc = db[COLLECTION].find_one({"marketId": str(market_id)}, {"_id": 0, "bookmakerAuto": 1, "fancyAuto": 1})
    if not doc:
        return _default_flags()
    return {
        "bookmakerAuto": bool(doc.get("bookmakerAuto", True)),
        "fancyAuto": bool(doc.get("fancyAuto", True)),
    }


def is_bookmaker_auto_enabled(db, market_id: str) -> bool:
    return get_auto_decision_flags(db, market_id)["bookmakerAuto"]


def is_fancy_auto_enabled(db, market_id: str) -> bool:
    return get_auto_decision_flags(db, market_id)["fancyAuto"]


def _match_name(row: dict) -> str:
    return str(row.get("matchName") or row.get("eventName") or row.get("marketName") or "")


def mongo_auto_decision_match_list(_payload: dict, session_user: dict = None) -> dict:
    """decision/autoDecisionMatchList — INPLAY matches with toggle state."""
    token = ""
    if session_user:
        token = str(session_user.get("_token") or session_user.get("token") or "")

    payload = {"status": "INPLAY"}
    rows = get_match_list(
        payload,
        for_admin=True,
        prefer_live=True,
        auth_token=token,
    )
    if not rows:
        rows = get_match_list(
            payload,
            for_admin=False,
            prefer_live=True,
            auth_token=token,
        )

    db = get_db()
    data: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        mid = str(row.get("marketId") or "")
        if not mid or mid in seen:
            continue
        seen.add(mid)
        flags = get_auto_decision_flags(db, mid)
        data.append({
            "sno": len(data) + 1,
            "marketId": mid,
            "matchName": _match_name(row),
            "bookmakerAuto": flags["bookmakerAuto"],
            "fancyAuto": flags["fancyAuto"],
        })
    return _ok(data, "Auto decision match list fetched")


def mongo_update_auto_decision_setting(payload: dict, _session_user: dict = None) -> dict:
    """decision/updateAutoDecisionSetting — flip bookmakerAuto or fancyAuto."""
    payload = payload or {}
    market_id = str(payload.get("marketId") or "")
    field = str(payload.get("field") or "")
    if not market_id:
        return _err("marketId required")
    if field not in ("bookmakerAuto", "fancyAuto"):
        return _err("field must be bookmakerAuto or fancyAuto")
    if "value" not in payload:
        return _err("value required")

    value = bool(payload.get("value"))
    db = get_db()
    match = db.matches.find_one({"marketId": market_id}, {"matchName": 1, "eventName": 1})
    name = _match_name(match or {})
    existing = db[COLLECTION].find_one({"marketId": market_id})
    if existing:
        db[COLLECTION].update_one(
            {"marketId": market_id},
            {"$set": {field: value, "updatedAt": _now()}},
        )
    else:
        doc = {
            "marketId": market_id,
            "matchName": name,
            "bookmakerAuto": True,
            "fancyAuto": True,
            field: value,
            "updatedAt": _now(),
        }
        db[COLLECTION].insert_one(doc)
    flags = get_auto_decision_flags(db, market_id)
    return _ok({"marketId": market_id, **flags}, "Setting updated")
