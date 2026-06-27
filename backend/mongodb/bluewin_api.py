"""BlueWin staff panel — MongoDB owner login + operating panel APIs."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from typing import Any

from mongodb.admin_api import ADMIN_ROUTES, mongo_admin_login
from mongodb.admin_api import handle_admin_api as _handle_admin_api
from mongodb.auth import mongo_logout
from mongodb.centerpanel_api import CP_ROUTES, handle_centerpanel_api
from mongodb.bluewin_decision import BLUEWIN_DECISION_ROUTES, handle_bluewin_decision
from mongodb.bluewin_handlers import handle_bluewin_nav
from mongodb.bet_logic import is_fancy_market
from mongodb.db import get_db, ping
from mongodb.matches_api import prepare_match_for_admin

BLUEWIN_MATCH_ENDPOINTS = frozenset({
    "sports/matchList",
    "sports/sportByMarketId",
    "sports/getSessionList",
    "sports/clientListByMarketId",
    "sports/getOddsPosition",
    "reports/matchDetails",
})

# Staff panel expects admin response shapes (oddsBetData / fancyBetData), not CP flat arrays.
BLUEWIN_ADMIN_ROUTES = frozenset({
    "sports/betsList",
})

BLUEWIN_SKIP_MATCH_FIX = frozenset({
    "sports/betsList",
    "sports/getSessionList",
})

BLUEWIN_OWNER_DEFAULTS = {
    "username": "OW1000",
    "password": "Bluewin@4923",
    "name": "owner 01",
    "userType": "owner",
    "userPriority": 9,
    "matchShare": 100,
    "matchCommission": 10,
    "sessionCommission": 10,
    "casinoShare": 100,
    "casinoCommission": 10,
    "intCasinoStatus": True,
    "intCasinoMultiply": 1,
    "userFixLimit": 1_000_000_000,
    "coins": 0,
    "creditLimit": 1_000_000_000,
    "casinoStatus": True,
    "matkaStatus": True,
    "betStatus": True,
    "matchStatus": True,
    "isPasswordChanged": True,
    "isOneClickBet": False,
    "oneClickBetAmount": 10,
    "status": 1,
    "isDeleted": False,
}


def _now():
    return datetime.now(timezone.utc)


def _enrich_bluewin_login(data: dict) -> dict:
    """BlueWin UI ke liye extra fields."""
    row = copy.deepcopy(data)
    row.setdefault("userFixLimit", row.get("creditLimit", 1_000_000_000))
    row.setdefault("intCasinoStatus", True)
    row.setdefault("intCasinoMultiply", 1)
    row.setdefault("isOneClickBet", False)
    row.setdefault("oneClickBetAmount", 10)
    row.setdefault("referralCode", row.get("referralCode", ""))
    row.setdefault("creatorName", row.get("creatorName", ""))
    row.setdefault("betChipsModal", False)
    row.setdefault("balance", row.get("coins", row.get("balance", 0)))
    return row


def mongo_bluewin_login(payload: dict) -> dict:
    """MongoDB users se owner/admin login — BlueWin response shape."""
    result = mongo_admin_login(payload or {})
    if result.get("error"):
        return result

    for key in ("data", "user"):
        if isinstance(result.get(key), dict):
            result[key] = _enrich_bluewin_login(result[key])

    result["message"] = result.get("message") or ""
    return result


def ensure_bluewin_owner(
    username: str | None = None,
    password: str | None = None,
) -> dict[str, Any]:
    """MongoDB mein BlueWin owner upsert — login ke liye."""
    if not ping():
        return {"ok": False, "message": "MongoDB not running"}

    defaults = dict(BLUEWIN_OWNER_DEFAULTS)
    if username:
        defaults["username"] = username.strip().upper()
    if password:
        defaults["password"] = password

    db = get_db()
    uname = defaults["username"]
    existing = db.users.find_one({"username": {"$regex": f"^{uname}$", "$options": "i"}})

    now = _now()
    uid = existing.get("userId") if existing else f"uid-{uname.lower()}"
    doc = {
        **defaults,
        "userId": uid,
        "parentId": existing.get("parentId") if existing else None,
        "creatorId": existing.get("creatorId") if existing else uid,
        "mobile": (existing or {}).get("mobile", "9999999999"),
        "betChipsData": (existing or {}).get("betChipsData") or {
            "100": 100, "500": 500, "1000": 1000, "5000": 5000,
            "10000": 10000, "50000": 50000, "100000": 100000,
        },
        "updatedAt": now,
    }
    if not existing:
        doc["createdAt"] = now
        doc["referralCode"] = f"{uname}100100"

    db.users.update_one(
        {"username": {"$regex": f"^{uname}$", "$options": "i"}},
        {"$set": doc},
        upsert=True,
    )
    return {"ok": True, "username": uname, "userId": doc["userId"]}


def _json_stringify_field(row: dict, key: str) -> None:
    val = row.get(key)
    if isinstance(val, (dict, list)):
        row[key] = json.dumps(val, default=str)


def _prepare_match_for_bluewin(match: dict) -> dict:
    """BlueWin match APIs — original site jaisa response shape.

    String: teamData (prepare_match_for_admin), maxMinCoins
    Object/array: betDelaySetting, marketList (matchEdit page .map() ke liye)
    """
    row = prepare_match_for_admin(match)
    _json_stringify_field(row, "maxMinCoins")
    return row


def _normalize_bets_list_response(endpoint: str, body: bytes) -> bytes:
    """CP flat array ko admin shape mein convert — undeclare bet list UI ke liye."""
    if endpoint != "sports/betsList":
        return body
    try:
        payload = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return body

    data = payload.get("data")
    if isinstance(data, dict) and "casinoBetData" in data:
        return body
    if not isinstance(data, list):
        return body

    odds_rows: list[dict] = []
    fancy_rows: list[dict] = []
    for bet in data:
        if not isinstance(bet, dict):
            continue
        if is_fancy_market(
            str(bet.get("betFor") or ""),
            str(bet.get("oddsType") or ""),
            str(bet.get("gtype") or ""),
        ):
            fancy_rows.append(bet)
        else:
            odds_rows.append(bet)

    payload["data"] = {
        "oddsBetData": odds_rows,
        "fancyBetData": fancy_rows,
        "totalOddsCount": len(odds_rows),
        "totalFancyCount": len(fancy_rows),
    }
    return json.dumps(payload, default=str).encode("utf-8")


def _fix_bluewin_response(endpoint: str, body: bytes) -> bytes:
    if endpoint in BLUEWIN_SKIP_MATCH_FIX:
        return body
    if endpoint not in BLUEWIN_MATCH_ENDPOINTS and not endpoint.startswith("sports/"):
        return body
    try:
        payload = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return body

    data = payload.get("data")
    if isinstance(data, list):
        payload["data"] = [_prepare_match_for_bluewin(row) for row in data if isinstance(row, dict)]
    elif isinstance(data, dict):
        if endpoint in BLUEWIN_MATCH_ENDPOINTS or endpoint.endswith("matchList"):
            payload["data"] = [_prepare_match_for_bluewin(data)] if data else []
        else:
            payload["data"] = _prepare_match_for_bluewin(data)

    return json.dumps(payload, default=str).encode("utf-8")


def handle_bluewin_api(endpoint: str, payload: dict, auth_header: str) -> bytes:
    """
    BlueWin operating panel API router:
    - user/login → MongoDB owner/admin (mongo_bluewin_login)
    - centerpanel routes → operating panel handlers (matchList, decision, …)
    - admin routes → admin handlers (casino reports, user list, …)
    - baaki → centerpanel fallback (website/* stubs)
    """
    endpoint = endpoint.lstrip("/").split("?")[0]

    if not ping():
        return json.dumps({
            "error": True,
            "code": 500,
            "message": "MongoDB not running. brew services start mongodb-community",
        }).encode("utf-8")

    if endpoint.endswith("user/login"):
        return json.dumps(mongo_bluewin_login(payload or {}), default=str).encode("utf-8")

    if endpoint.endswith("logout"):
        return json.dumps(mongo_logout(auth_header), default=str).encode("utf-8")

    decision_body = handle_bluewin_decision(endpoint, payload, auth_header)
    if decision_body is not None:
        return decision_body

    nav_body = handle_bluewin_nav(endpoint, payload, auth_header)
    if nav_body is not None:
        return _normalize_bets_list_response(
            endpoint, _fix_bluewin_response(endpoint, nav_body)
        )

    if endpoint in BLUEWIN_ADMIN_ROUTES and endpoint in ADMIN_ROUTES:
        body = _handle_admin_api(endpoint, payload, auth_header)
    elif endpoint in CP_ROUTES:
        body = handle_centerpanel_api(endpoint, payload, auth_header)
    elif endpoint in ADMIN_ROUTES:
        body = _handle_admin_api(endpoint, payload, auth_header)
    else:
        body = handle_centerpanel_api(endpoint, payload, auth_header)

    return _normalize_bets_list_response(endpoint, _fix_bluewin_response(endpoint, body))
