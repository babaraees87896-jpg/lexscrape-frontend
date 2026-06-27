"""Admin panel — saari APIs MongoDB se (demo mode nahi)."""

from __future__ import annotations

import copy
import json
import uuid
from datetime import timedelta
from typing import Any, Optional, Tuple

from mongodb.bet_logic import normalize_match_for_api
from mongodb.auth import (
    TOKEN_TTL_HOURS,
    _extract_bearer,
    _make_token,
    _now,
    mongo_logout,
    validate_session,
)
from mongodb.db import get_db, ping
from mongodb.matches_api import ADMIN_LIVE_MATCH_LIST, ADMIN_MONGO_ONLY, get_match_list as _shared_match_list
from mongodb.admin_all_handlers import (
    mongo_admin_block_market,
    mongo_admin_block_market_list,
    mongo_admin_casino_by_event,
    mongo_admin_casino_plus_minus,
    mongo_admin_casino_profit_loss_pos,
    mongo_admin_casino_realtime_pos,
    mongo_admin_casino_report_by_user,
    mongo_admin_casino_round_result,
    mongo_admin_casino_result_by_round,
    mongo_admin_client_plus_minus,
    mongo_admin_day_wise_casino_report,
    mongo_admin_delete_ledger,
    mongo_admin_ledger_credit_debit,
    mongo_admin_ledger_from_entries,
    mongo_admin_lena_dena as mongo_admin_lena_dena_transfer,
    mongo_admin_match_by_market,
    mongo_admin_bets_list,
    mongo_admin_client_list_by_market,
    mongo_admin_matka_day_wise,
    mongo_admin_matka_bet_list,
    mongo_admin_matka_list,
    mongo_admin_matka_profit_loss,
    mongo_admin_odds_position,
    mongo_admin_plus_minus_market,
    mongo_admin_plus_minus_user_wise,
    mongo_admin_profit_loss_report,
    mongo_admin_session_position,
    mongo_admin_session_list,
    mongo_admin_user_commission_report,
    mongo_admin_commission_list_by_user,
    mongo_admin_reset_comm_list,
    mongo_admin_reset_comm,
    mongo_admin_user_statement,
    record_coin_transfer,
    _user_has_credit_history,
)

ADMIN_PANEL_TYPES = {
    "owner", "subowner", "superadmin", "admin", "subadmin",
    "master", "superagent", "agent",
}

USER_TYPE_PRIORITY = {
    "owner": 9, "subowner": 8, "superadmin": 7, "admin": 6,
    "subadmin": 5, "master": 4, "superagent": 3, "agent": 2, "client": 1,
}

USER_TYPE_CODE = {
    "client": ("C", 324001),
    "agent": ("A", 6786),
    "superagent": ("SA", 5667),
    "master": ("MA", 4289),
    "subadmin": ("AD", 3212),
    "admin": ("ADM", 3122),
    "superadmin": ("SUA", 2322),
    "subowner": ("SOW", 1211),
    "owner": ("OW", 1),
}


def _next_username_num(db, prefix: str, start: int) -> int:
    """Prefix ke saath existing codes scan karke next number."""
    prefix_up = prefix.upper()
    max_n = start - 1
    for doc in db.users.find({}, {"username": 1}):
        uname = str(doc.get("username") or "").upper()
        if not uname.startswith(prefix_up):
            continue
        suffix = uname[len(prefix_up):]
        if suffix.isdigit():
            max_n = max(max_n, int(suffix))
    return max(max_n + 1, start)


def _gen_username(db, user_type: str) -> str:
    prefix, start = USER_TYPE_CODE.get(user_type, ("U", 1))
    n = _next_username_num(db, prefix, start)
    while True:
        candidate = f"{prefix}{n}"
        if not db.users.find_one({"username": candidate}):
            return candidate
        n += 1


BET_CHIPS = {
    "100": 100, "500": 500, "1000": 1000, "2000": 2000,
    "5000": 5000, "10000": 10000, "25000": 25000, "50000": 50000,
    "100000": 100000, "200000": 200000, "300000": 300000, "500000": 500000,
}

ARRAY_ENDPOINT_KEYS = ("Report", "List", "list", "Pos", "Result", "Bets", "casino/", "matka/", "sports/", "decision/")


def _num(val, default=0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _is_owner_user(user: dict | None) -> bool:
    """Owner unlimited coins — balance 0 ho tab bhi de sakta hai."""
    return str((user or {}).get("userType") or "").lower() == "owner"


def _owner_mints_coins(user: dict | None) -> bool:
    """Owner se debit nahi — balance negative nahi hota, coins seedhe credit."""
    return _is_owner_user(user)


def _actor_user(db, session_user: dict) -> dict:
    """Fresh DB row for logged-in user (coins/share caps)."""
    uid = str(session_user.get("userId") or "")
    if uid:
        row = db.users.find_one({"userId": uid, "isDeleted": {"$ne": True}}, {"password": 0})
        if row:
            return row
    return session_user


def _coins_cap(user: dict | None) -> float:
    if _is_owner_user(user):
        return float("inf")
    return _num((user or {}).get("coins", 0))


def _validate_assignable_coins(amount: float, actor: dict) -> Optional[str]:
    if amount <= 0 or _is_owner_user(actor):
        return None
    cap = _coins_cap(actor)
    if amount > cap:
        return f"Limit cannot exceed your balance ({int(cap) if cap == int(cap) else cap})"
    return None


def _validate_assignable_shares(payload: dict, actor: dict) -> Optional[str]:
    if _is_owner_user(actor):
        return None
    checks = (
        ("matchShare", "matchShare", 100, "Match share"),
        ("casinoShare", "casinoShare", 100, "Casino share"),
        ("matkaShare", "matkaShare", 0, "Matka share"),
        ("matchFlatShare", "matchFlatShare", 0, "Match flat share"),
        ("casinoFlatShare", "casinoFlatShare", 0, "Casino flat share"),
        ("matchCommission", "matchCommission", 0, "Match commission"),
        ("sessionCommission", "sessionCommission", 0, "Session commission"),
        ("casinoCommission", "casinoCommission", 0, "Casino commission"),
        ("matkaCommission", "matkaCommission", 0, "Matka commission"),
    )
    for key, actor_key, default, label in checks:
        if key not in payload or payload[key] is None:
            continue
        val = _num(payload[key])
        cap = _num(actor.get(actor_key, default))
        if val > cap:
            return f"{label} cannot be more than {cap}"
    return None


def _strip_mongo(doc: dict) -> dict:
    row = copy.deepcopy(doc)
    row.pop("_id", None)
    row.pop("password", None)
    return row


def _status_as_int(val) -> int:
    if val in (0, "0", False):
        return 0
    if val in (1, "1", True):
        return 1
    return 1 if val else 0


def _api_ts(val) -> int:
    """API list fields — createdAt hamesha epoch ms."""
    if isinstance(val, (int, float)):
        return int(val)
    if hasattr(val, "timestamp"):
        return int(val.timestamp() * 1000)
    if isinstance(val, str) and val.strip():
        text = val.strip()
        if text.isdigit():
            return int(text)
        try:
            from datetime import datetime

            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            return int(datetime.fromisoformat(text).timestamp() * 1000)
        except (ValueError, TypeError, OverflowError):
            return 0
    return 0


def _mixed_sort_key(val):
    if val is None:
        return (2, "")
    if isinstance(val, (int, float)):
        return (0, float(val))
    return (1, str(val).lower())


def _user_for_api(user: dict) -> dict:
    row = _strip_mongo(user)
    row["coins"] = _num(row.get("coins", row.get("balance", 0)))
    row["balance"] = _num(row.get("balance", row["coins"]))
    row["exposure"] = _num(row.get("exposure", 0))
    row["profitLoss"] = _num(row.get("profitLoss", 0))
    if row.get("parentId") is None:
        row["parentId"] = ""
    row.setdefault("status", "1")
    row.setdefault("isDeleted", "false")
    row.setdefault("casinoStatus", True)
    row.setdefault("betStatus", True)
    row.setdefault("intCasinoStatus", False)
    row.setdefault("betChipsData", {})
    row.setdefault("isPasswordChanged", True)
    return row


def _user_list_row_for_api(user: dict, db=None) -> dict:
    """User list dropdown — scraped site jaisa fields."""
    row = _user_for_api(user)
    uid = user.get("userId", "")
    row["id"] = uid
    row["userId"] = uid
    row["passwordShow"] = user.get("password", "")
    row["status"] = _status_as_int(user.get("status", 1))
    row["betStatus"] = bool(user.get("betStatus", True))
    row["casinoStatus"] = bool(user.get("casinoStatus", True))
    row["userMatchCommission"] = _num(user.get("matchCommission", 0))
    row["userSessionCommission"] = _num(user.get("sessionCommission", 0))
    row["userCasinoCommission"] = _num(user.get("casinoCommission", 0))
    row["userMatchShare"] = _num(user.get("matchShare", 100))
    row["name"] = str(user.get("name") or user.get("username") or "").strip()
    creator_id = user.get("creatorId") or user.get("parentId") or ""
    row["creatorId"] = creator_id
    row["creatorName"] = ""
    if db is not None and creator_id:
        creator = db.users.find_one({"userId": creator_id}, {"username": 1, "name": 1})
        if creator:
            row["creatorName"] = creator.get("username") or creator.get("name") or ""
    created = user.get("createdAt")
    row["createdAt"] = _api_ts(created)
    return row


def _user_details_for_api(user: dict) -> dict:
    """Create/edit page — scraped userDetails jaisa (id, userMatchShare, ...)."""
    row = _user_for_api(user)
    uid = user.get("userId", "")
    row["id"] = uid
    row["userId"] = uid
    row["passwordShow"] = user.get("password", "")
    row["creatorId"] = user.get("creatorId") or user.get("parentId") or ""
    row["name"] = user.get("name", "")
    row["mobile"] = user.get("mobile", "")
    row["userType"] = user.get("userType", "")
    match_share = _num(user.get("matchShare", 100))
    casino_share = _num(user.get("casinoShare", 100))
    matka_share = _num(user.get("matkaShare", 0))
    row["userMatchShare"] = match_share
    row["userCasinoShare"] = casino_share
    row["userMatkaShare"] = matka_share
    row["userShowMatchShare"] = match_share
    row["userShowCasinoShare"] = casino_share
    row["userShowMatkaShare"] = matka_share
    row["userMatchCommission"] = _num(user.get("matchCommission", 0))
    row["userSessionCommission"] = _num(user.get("sessionCommission", 0))
    row["userCasinoCommission"] = _num(user.get("casinoCommission", 0))
    row["userMatkaCommission"] = _num(user.get("matkaCommission", 0))
    row["userCommissionType"] = user.get("commissionType") or "NoCommission"
    row["userPriority"] = user.get("userPriority", USER_TYPE_PRIORITY.get(user.get("userType", ""), 0))
    row["intCasinoMultiply"] = user.get("intCasinoMultiply", 1)
    row["intCasinoExpoLimit"] = user.get("intCasinoExpoLimit", 0)
    row["intCasinoStatus"] = bool(user.get("intCasinoStatus", False))
    row["matchFlatShare"] = _num(user.get("matchFlatShare", 0))
    row["casinoFlatShare"] = _num(user.get("casinoFlatShare", 0))
    row["matkaFlatShare"] = _num(user.get("matkaFlatShare", 0))
    row["userFlatSharePermission"] = bool(user.get("userFlatSharePermission", False))
    row["commChangeType"] = user.get("commChangeType") or ""
    row["maxCommValue"] = _num(user.get("maxCommValue", 0))
    row["reference"] = user.get("reference", "")
    row["status"] = _status_as_int(user.get("status", 1))
    row["betStatus"] = bool(user.get("betStatus", True))
    row["casinoStatus"] = bool(user.get("casinoStatus", True))
    return row


def _compute_user_count(db, root_id: str) -> list:
    """Dashboard widgets read userCount[0].{role}Count."""
    type_keys = {
        "subowner": "sunOwnerCount",
        "superadmin": "superadminCount",
        "admin": "adminCount",
        "subadmin": "subadminCount",
        "master": "masterCount",
        "superagent": "superagentCount",
        "agent": "agentCount",
        "client": "userCount",
    }
    descendants = _collect_descendant_ids(db, root_id)
    counts: dict[str, int] = {}
    query = {"isDeleted": {"$ne": True}}
    if descendants:
        query["userId"] = {"$in": list(descendants)}
    for row in db.users.find(query, {"userType": 1}):
        key = type_keys.get(str(row.get("userType", "")).lower())
        if key:
            counts[key] = counts.get(key, 0) + 1
    return [counts] if counts else [{}]


def _user_to_login_data(user: dict) -> dict:
    data = _user_for_api(user)
    db = get_db()
    data["userCount"] = _compute_user_count(db, user.get("userId", ""))
    updated = data.get("updatedAt")
    if hasattr(updated, "isoformat"):
        data["updatedAt"] = updated.isoformat()
    return data


def resolve_admin_user(auth_header: str) -> Tuple[Optional[dict], Optional[dict]]:
    token = _extract_bearer(auth_header)
    if not token:
        return None, {"message": "Session expired", "code": 401, "error": True, "data": {}}

    user = validate_session(token)
    if not user:
        return None, {"message": "Session expired", "code": 401, "error": True, "data": {}}

    if user.get("userType") == "client":
        return None, {"message": "Clients cannot use admin panel", "code": 403, "error": True, "data": {}}

    if user.get("userType") not in ADMIN_PANEL_TYPES:
        return None, {"message": "Not authorised", "code": 403, "error": True, "data": {}}

    return user, None


def mongo_admin_login(payload: dict) -> dict:
    if not ping():
        return {"error": True, "code": 500, "message": "MongoDB not running"}

    username = str(payload.get("username", "")).strip().upper()
    password = str(payload.get("password", ""))
    if not username or not password:
        return {"error": True, "code": 1, "message": "Username and password required"}

    db = get_db()
    user = db.users.find_one({
        "username": {"$regex": f"^{username}$", "$options": "i"},
        "password": password,
        "isDeleted": {"$ne": True},
    })

    if not user:
        return {"error": True, "code": 1, "message": "Invalid username or password"}

    if user.get("userType") == "client":
        return {"error": True, "code": 1, "message": "Client login — use client website"}

    if user.get("userType") not in ADMIN_PANEL_TYPES:
        return {"error": True, "code": 1, "message": "Not authorised for admin panel"}

    token = _make_token()
    now = _now()
    db.auth_sessions.delete_many({"userId": user["userId"]})
    db.auth_sessions.insert_one({
        "token": token,
        "userId": user["userId"],
        "username": user["username"],
        "panel": "admin",
        "createdAt": now,
        "expiresAt": now + timedelta(hours=TOKEN_TTL_HOURS),
    })
    db.user_activities.insert_one({
        "userId": user["userId"],
        "activityType": "login",
        "ip": str(payload.get("ip") or "127.0.0.1"),
        "device": str(payload.get("device") or "Browser"),
        "payload": {"panel": "admin"},
        "createdAt": now,
    })

    user_data = _user_to_login_data(user)
    return {
        "message": "Login successful",
        "code": 0,
        "error": False,
        "token": token,
        "user": user_data,
        "data": user_data,
    }


def _status_query(status: Any) -> dict:
    if not status or status == "both":
        return {}
    if str(status) == "1":
        return {"$or": [{"status": "1"}, {"status": 1}, {"status": True}]}
    return {"status": status}


def _collect_descendant_ids(db, root_id: str) -> set[str]:
    """Logged-in user ke neeche saare userId (direct + indirect)."""
    if not root_id:
        return set()

    rows = list(db.users.find({"isDeleted": {"$ne": True}}, {"userId": 1, "parentId": 1}))
    children: dict[str, list[str]] = {}
    for u in rows:
        pid = u.get("parentId")
        if pid not in (None, ""):
            children.setdefault(str(pid), []).append(u["userId"])

    seen: set[str] = set()
    queue = [root_id]
    while queue:
        uid = queue.pop(0)
        for child in children.get(uid, []):
            if child not in seen:
                seen.add(child)
                queue.append(child)
    return seen


def _can_manage_parent(session_user: dict, parent_id: str) -> bool:
    if not parent_id:
        return False
    sid = session_user.get("userId", "")
    if parent_id == sid:
        return True
    return parent_id in _collect_descendant_ids(get_db(), sid)


def mongo_admin_user_list(payload: dict, session_user: dict) -> dict:
    payload = payload or {}
    downline = payload.get("downlineUserType") or "client"
    parent_id = payload.get("parentId")

    # URL .../userlist/subadmin/1 → parentId "" / "1" = apni poori downline
    # URL .../userlist/master/uid-subadmin → specific parent ke direct bache
    drill_down = bool(parent_id and str(parent_id) not in ("", "1"))

    db = get_db()
    query: dict = {"userType": downline, "isDeleted": {"$ne": True}}

    if drill_down:
        pid = str(parent_id)
        sid = str(session_user.get("userId") or "")
        from mongodb.admin_compute import _session_downline_ids
        allowed = _session_downline_ids(db, session_user) | {sid}
        if pid not in allowed:
            return {
                "message": "User List fetched Successfully",
                "code": 0,
                "error": False,
                "data": {"total": 0, "list": []},
            }
        query["parentId"] = parent_id
    else:
        from mongodb.admin_compute import _session_downline_ids
        downline_ids = _session_downline_ids(db, session_user)
        if not downline_ids:
            return {
                "message": "User List fetched Successfully",
                "code": 0,
                "error": False,
                "data": {"total": 0, "list": []},
            }
        query["userId"] = {"$in": list(downline_ids)}

    query.update(_status_query(payload.get("status")))

    search = (
        payload.get("keyWord")
        or payload.get("searchTerm")
        or payload.get("username")
        or ""
    ).strip()
    if search:
        query["$or"] = [
            {"username": {"$regex": search, "$options": "i"}},
            {"name": {"$regex": search, "$options": "i"}},
        ]

    db = get_db()
    rows = [_user_list_row_for_api(u, db) for u in db.users.find(query)]
    sort_data = payload.get("sortData") or {}
    if isinstance(sort_data, dict) and sort_data:
        field = next(iter(sort_data))
        reverse = sort_data[field] in (-1, "-1", True)
        rows.sort(
            key=lambda r: _mixed_sort_key(r.get(field)),
            reverse=reverse,
        )
    else:
        rows.sort(key=lambda r: r.get("username", ""))

    page_no = int(payload.get("pageNo") or 1)
    size = int(payload.get("size") or payload.get("limit") or 10)
    start = (page_no - 1) * size if size > 0 else 0
    page_rows = rows[start : start + size] if size > 0 else rows

    return {
        "message": "User List fetched Successfully",
        "code": 0,
        "error": False,
        "data": {"total": len(rows), "list": page_rows},
    }


def _resolve_user_id(payload: dict, session_user: dict | None) -> str | None:
    uid = (payload or {}).get("userId")
    if uid not in (None, ""):
        return str(uid)
    if session_user:
        return session_user.get("userId")
    return None


def mongo_admin_user_details(payload: dict, session_user: dict = None) -> dict:
    uid = _resolve_user_id(payload, session_user)
    if not uid:
        return {"message": "userId required", "code": 1, "error": True, "data": {}}
    user = get_db().users.find_one({"userId": uid, "isDeleted": {"$ne": True}})
    if not user:
        # username se bhi try — purane URLs / scraped ids ke liye
        user = get_db().users.find_one({"username": uid.upper(), "isDeleted": {"$ne": True}})
    if not user:
        return {"message": "User not found", "code": 1, "error": True, "data": {}}
    if not _session_may_view_user(session_user, str(user.get("userId") or "")):
        return {"message": "Not authorised", "code": 1, "error": True, "data": {}}
    return {"message": "OK", "code": 0, "error": False, "data": _user_details_for_api(user)}


def mongo_admin_get_user_share_data(payload: dict, session_user: dict = None) -> dict:
    uid = _resolve_user_id(payload, session_user)
    if not uid:
        return {"message": "userId required", "code": 1, "error": True, "data": {}}
    user = get_db().users.find_one({"userId": uid}, {"password": 0})
    if not user:
        return {"message": "User not found", "code": 1, "error": True, "data": {}}
    if not _session_may_view_user(session_user, str(user.get("userId") or "")):
        return {"message": "Not authorised", "code": 1, "error": True, "data": {}}
    ut = user.get("userType", "")
    return {
        "message": "OK",
        "code": 0,
        "error": False,
        "data": {
            f"{ut}MatchShare": _num(user.get("matchShare", 100)),
            f"{ut}CasinoShare": _num(user.get("casinoShare", 100)),
            f"{ut}MatkaShare": _num(user.get("matkaShare", 0)),
        },
    }


def mongo_admin_user_balance(payload: dict, session_user: dict = None) -> dict:
    uid = _resolve_user_id(payload, session_user)
    user = get_db().users.find_one({"userId": uid}, {"password": 0}) if uid else None
    if not user:
        return {"message": "User not found", "code": 1, "error": True, "data": {}}
    if not _session_may_view_user(session_user, str(user.get("userId") or "")):
        return {"message": "Not authorised", "code": 1, "error": True, "data": {}}
    coins = _num(user.get("coins", 0))
    return {
        "message": "",
        "code": 0,
        "error": False,
        "data": {
            "intCasinoStatus": str(user.get("intCasinoStatus", False)).lower(),
            "exposure": str(_num(user.get("exposure", 0))),
            "intCasinoMultiply": str(user.get("intCasinoMultiply", 1)),
            "isDeleted": str(user.get("isDeleted", False)).lower(),
            "status": "1",
            "coins": str(coins),
            "casinoToken": "",
            "authenticateToken": uuid.uuid4().hex,
            "creatorId": user.get("creatorId") or user.get("parentId") or "",
            "profitLoss": str(_num(user.get("profitLoss", 0))),
        },
    }


def mongo_admin_user_ledger(payload: dict, session_user: dict = None) -> dict:
    return mongo_admin_ledger_from_entries(payload, session_user)


def mongo_admin_match_list(payload: dict, session_user: dict = None) -> dict:
    if payload and payload.get("marketId"):
        result = mongo_admin_match_by_market(payload, session_user)
        data = result.get("data")
        if isinstance(data, dict) and data:
            result["data"] = [data]
        elif not data:
            result["data"] = []
        return result
    token = ""
    if session_user:
        token = str(session_user.get("_token") or session_user.get("token") or "")
    # Live API → MongoDB sync, phir MongoDB se return (frontend jaisa flow)
    prefer_live = ADMIN_LIVE_MATCH_LIST
    rows = _shared_match_list(
        payload or {}, for_admin=True, auth_token=token, prefer_live=prefer_live
    )
    return {"message": 0, "code": 0, "error": False, "data": rows}


def mongo_admin_update_diamond_casino(payload: dict, _session_user: dict = None) -> dict:
    from mongodb.casino_api import update_diamond_casino

    return update_diamond_casino(payload or {})


def mongo_admin_casino_data(payload: dict, session_user: dict = None) -> dict:
    from mongodb.casino_api import find_casino_game_by_event_id, staff_diamond_casino_games

    if payload and payload.get("eventId") is not None:
        game = find_casino_game_by_event_id(payload.get("eventId"))
        if not game:
            return {"message": "Game not found", "code": 1, "error": True, "data": {}}
        return {"message": "data fetched", "code": 0, "error": False, "data": game}

    rows = staff_diamond_casino_games()
    return {"message": "data fetched", "code": 0, "error": False, "data": rows}


def mongo_admin_casino_bets(payload: dict, _session_user: dict = None) -> dict:
    from mongodb.admin_compute import _casino_bets_in_scope, _casino_games_map, _format_casino_bet_row

    payload = payload or {}
    db = get_db()
    games = _casino_games_map(db)
    bets = _casino_bets_in_scope(db, payload)
    rows = []
    for bet in bets:
        user = db.users.find_one({"userId": bet.get("userId")}, {"_id": 0, "userId": 1, "username": 1, "name": 1}) or {}
        eid = bet.get("eventId")
        game = games.get(int(eid)) if eid is not None and str(eid).isdigit() else None
        rows.append(_format_casino_bet_row(bet, user, game))
    rows.sort(key=lambda r: r.get("createdAt") or 0, reverse=True)
    page = max(int(payload.get("pageNo") or 1), 1)
    size = max(int(payload.get("size") or 50), 1)
    total = len(rows)
    page_rows = rows[(page - 1) * size: page * size]
    return {
        "message": "Diamond Casino Bet List Fetch Successfully",
        "code": 0, "error": False,
        "data": {"casinoBetData": page_rows, "totalCasinoCount": total},
    }


def mongo_admin_sports_bets(payload: dict, _session_user: dict = None) -> dict:
    q = {}
    if payload.get("userId"):
        q["userId"] = payload["userId"]
    if payload.get("marketId"):
        q["marketId"] = payload["marketId"]
    rows = [_strip_mongo(b) for b in get_db().sports_bets.find(q)]
    return {"message": "Fetch List Successfuly", "code": 0, "error": False, "data": rows}


def _default_allowed_domains(db) -> list[str]:
    urls: list[str] = []
    for d in db.domains.find({}):
        url = d.get("domainUrl") or d.get("domainName")
        if url:
            urls.append(str(url))
    return urls or ["1ex99.in"]


def mongo_admin_domain_list(_payload: dict, session_user: dict = None) -> dict:
    if session_user.get("userType") != "owner":
        return {
            "message": "You Are Not Authorised List Domain ! Only owner allowed to List domain",
            "code": 0,
            "error": False,
            "data": [],
        }
    rows = []
    for d in get_db().domains.find({}):
        url = d.get("domainUrl") or d.get("domainName") or ""
        name = d.get("domainName") or url
        rows.append({
            "id": url,
            "domainName": name,
            "domainUrl": url,
        })
    return {"message": "Domain List fetched Successfully", "code": 0, "error": False, "data": rows}


def mongo_admin_create_user(payload: dict, session_user: dict) -> dict:
    payload = payload or {}
    db = get_db()

    user_type = str(payload.get("userType") or "").strip().lower()
    parent_id = payload.get("parentId")
    password = str(payload.get("password") or "")
    name = str(payload.get("name") or "").strip()

    if not user_type or user_type not in USER_TYPE_PRIORITY:
        return {"message": "Invalid user type", "code": 1, "error": True, "data": {}}
    if not parent_id:
        return {"message": "Parent required", "code": 1, "error": True, "data": {}}
    if not password:
        return {"message": "Password required", "code": 1, "error": True, "data": {}}
    if not name:
        return {"message": "Name required", "code": 1, "error": True, "data": {}}

    session_priority = USER_TYPE_PRIORITY.get(session_user.get("userType"), 0)
    new_priority = USER_TYPE_PRIORITY[user_type]
    if session_priority <= new_priority:
        return {"message": "Not authorised to create this user type", "code": 1, "error": True, "data": {}}
    if not _can_manage_parent(session_user, str(parent_id)):
        return {"message": "Not authorised for this parent", "code": 1, "error": True, "data": {}}

    parent = db.users.find_one({"userId": parent_id, "isDeleted": {"$ne": True}})
    if not parent:
        return {"message": "Parent user not found", "code": 1, "error": True, "data": {}}

    if user_type == "subowner" and session_user.get("userType") != "owner":
        return {"message": "Only owner can create subowner", "code": 1, "error": True, "data": {}}
    subowner_domains: list[str] = []
    if user_type == "subowner":
        subowner_domains = list(payload.get("allowedDomains") or [])
        if not subowner_domains:
            subowner_domains = _default_allowed_domains(db)

    coins = _num(payload.get("coins", 0))
    actor = _actor_user(db, session_user)
    coin_err = _validate_assignable_coins(coins, actor)
    if coin_err:
        return {"message": coin_err, "code": 1, "error": True, "data": {}}
    share_err = _validate_assignable_shares(payload, actor)
    if share_err:
        return {"message": share_err, "code": 1, "error": True, "data": {}}

    username = _gen_username(db, user_type)
    user_id = uuid.uuid4().hex[:24]
    now = _now()

    doc = {
        "userId": user_id,
        "username": username,
        "password": password,
        "name": name,
        "mobile": payload.get("mobile") or "9999999999",
        "userType": user_type,
        "userPriority": new_priority,
        "parentId": parent_id,
        "creatorId": session_user.get("userId") or parent_id,
        "coins": coins,
        "balance": coins,
        "creditLimit": coins,
        "exposure": 0,
        "profitLoss": 0,
        "status": payload.get("status", 1),
        "isDeleted": False,
        "betStatus": bool(payload.get("betStatus", True)),
        "matchStatus": bool(payload.get("matchStatus", True)),
        "casinoStatus": bool(payload.get("casinoStatus", True)),
        "matkaStatus": bool(payload.get("matkaStatus", False)),
        "intCasinoStatus": bool(payload.get("intCasinoStatus", False)),
        "intCasinoShare": _num(payload.get("intCasinoShare", 0)),
        "intCasinoExpoLimit": _num(payload.get("intCasinoExpoLimit", 0)),
        "intCasinoMultiply": payload.get("intCasinoMultiply") or parent.get("intCasinoMultiply", 1),
        "matchShare": _num(payload.get("matchShare", 0)),
        "casinoShare": _num(payload.get("casinoShare", 0)),
        "matkaShare": _num(payload.get("matkaShare", 0)),
        "matchFlatShare": _num(payload.get("matchFlatShare", 0)),
        "casinoFlatShare": _num(payload.get("casinoFlatShare", 0)),
        "matchCommission": _num(payload.get("matchCommission", 0)),
        "sessionCommission": _num(payload.get("sessionCommission", 0)),
        "casinoCommission": _num(payload.get("casinoCommission", 0)),
        "matkaCommission": _num(payload.get("matkaCommission", 0)),
        "commissionType": payload.get("commissionType") or "NoCommission",
        "reference": payload.get("reference") or "",
        "creditReference": _num(payload.get("creditReference", 0)),
        "maxCommValue": _num(payload.get("maxCommValue", 0)),
        "allowedDomains": subowner_domains if user_type == "subowner" else (payload.get("allowedDomains") or []),
        "isPasswordChanged": True,
        "betChipsData": copy.deepcopy(BET_CHIPS),
        "referralCode": f"{username}100100",
        "createdAt": now,
        "updatedAt": now,
    }

    db.users.insert_one(doc)
    if coins > 0:
        actor = _actor_user(db, session_user)
        if not _owner_mints_coins(actor):
            db.users.update_one(
                {"userId": actor["userId"]},
                {"$inc": {"coins": -coins, "balance": -coins}},
            )
        actor_coins = _num(actor.get("coins", 0))
        record_coin_transfer(
            db,
            debit_user={
                **actor,
                "coins": actor_coins if _owner_mints_coins(actor) else actor_coins - coins,
                "balance": actor_coins if _owner_mints_coins(actor) else actor_coins - coins,
            },
            credit_user={**doc, "coins": coins, "balance": coins},
            amount=coins,
            transfer_type="user_create",
            debit_description=f"Limit assigned to {username}",
            credit_description="First Deposit",
            remark=f"New {user_type} account created",
            created_at=now,
        )

    created = db.users.find_one({"userId": user_id}, {"password": 0})
    return {
        "message": f"{user_type.capitalize()} created successfully",
        "code": 0,
        "error": False,
        "data": _user_details_for_api(created),
    }


def _can_manage_user(session_user: dict, target_user_id: str) -> bool:
    """Logged-in user apni downline par hi action le sake (scraped hierarchy)."""
    sid = str(session_user.get("userId") or "")
    tid = str(target_user_id or "")
    if not sid:
        return False
    if not tid or tid == sid:
        return True
    from mongodb.admin_compute import _session_downline_ids
    return tid in _session_downline_ids(get_db(), session_user)


def _session_may_view_user(session_user: dict | None, target_user_id: str) -> bool:
    if not session_user:
        return True
    return _can_manage_user(session_user, target_user_id)


def _find_user(db, user_id: str) -> dict | None:
    if not user_id:
        return None
    user = db.users.find_one({"userId": user_id, "isDeleted": {"$ne": True}})
    if user:
        return user
    return db.users.find_one({"username": str(user_id).upper(), "isDeleted": {"$ne": True}})


def mongo_admin_update_coins(payload: dict, session_user: dict) -> dict:
    payload = payload or {}
    user_id = str(payload.get("userId") or "")
    coins_delta = _num(payload.get("coins", 0))
    if not user_id:
        return {"message": "userId required", "code": 1, "error": True, "data": {}}
    if coins_delta == 0:
        return {"message": "coins required", "code": 1, "error": True, "data": {}}

    db = get_db()
    target = _find_user(db, user_id)
    if not target:
        return {"message": "User not found", "code": 1, "error": True, "data": {}}
    if not _can_manage_user(session_user, target["userId"]):
        return {"message": "Not authorised", "code": 1, "error": True, "data": {}}

    actor = _actor_user(db, session_user)
    target_coins = _num(target.get("coins", 0))
    actor_coins = _num(actor.get("coins", 0))

    if coins_delta > 0:
        coin_err = _validate_assignable_coins(coins_delta, actor)
        if coin_err:
            return {"message": coin_err, "code": 1, "error": True, "data": {}}
    elif abs(coins_delta) > target_coins:
        return {"message": "Not sufficiant Coins for withdrawal.", "code": 1, "error": True, "data": {}}

    now = _now()
    db.users.update_one(
        {"userId": target["userId"]},
        {
            "$inc": {"coins": coins_delta, "balance": coins_delta, "creditLimit": coins_delta},
            "$set": {"updatedAt": now},
        },
    )
    actor_mints = _owner_mints_coins(actor)
    if not (coins_delta > 0 and actor_mints):
        db.users.update_one(
            {"userId": actor["userId"]},
            {
                "$inc": {"coins": -coins_delta, "balance": -coins_delta},
                "$set": {"updatedAt": now},
            },
        )

    target_uname = target.get("username", user_id)
    actor_after = actor_coins if (coins_delta > 0 and actor_mints) else actor_coins - coins_delta
    if coins_delta > 0:
        is_first = target_coins == 0 and not _user_has_credit_history(db, target["userId"])
        transfer_type = "first_deposit" if is_first else "deposit"
        credit_desc = "First Deposit" if is_first else "Deposit"
        debit_desc = (
            f"First deposit to {target_uname}"
            if is_first
            else f"Deposit to {target_uname}"
        )
        record_coin_transfer(
            db,
            debit_user={**actor, "coins": actor_after},
            credit_user={**target, "coins": target_coins + coins_delta},
            amount=coins_delta,
            transfer_type=transfer_type,
            debit_description=debit_desc,
            credit_description=credit_desc,
            created_at=now,
        )
    else:
        amount = abs(coins_delta)
        record_coin_transfer(
            db,
            debit_user={**target, "coins": target_coins + coins_delta},
            credit_user={**actor, "coins": actor_coins - coins_delta},
            amount=amount,
            transfer_type="withdraw",
            debit_description="Withdrawal",
            credit_description=f"Withdrawal from {target_uname}",
            created_at=now,
        )

    return {
        "message": "Coins Updated Successfully",
        "code": 0,
        "error": False,
        "data": {"userId": target["userId"], "coins": coins_delta},
    }


def _mongo_admin_bulk_status_update(
    user_ids: list,
    status: int,
    session_user: dict,
) -> dict:
    """All Active / All Deactive — userId array se bulk status update."""
    ids = [str(uid or "").strip() for uid in user_ids if str(uid or "").strip()]
    if not ids:
        return {"message": "userId required", "code": 1, "error": True, "data": {}}

    db = get_db()
    updated = 0
    for uid in ids:
        target = _find_user(db, uid)
        if not target or not _can_manage_user(session_user, uid):
            continue
        db.users.update_one(
            {"userId": uid},
            {"$set": {"status": status, "updatedAt": _now()}},
        )
        updated += 1

    if not updated:
        return {"message": "No users updated", "code": 1, "error": True, "data": {}}

    label = "activated" if status == 1 else "deactivated"
    return {
        "message": f"{updated} user(s) {label} successfully",
        "code": 0,
        "error": False,
        "data": {"updated": updated},
    }


def mongo_admin_update_bulk_status(payload: dict, session_user: dict) -> dict:
    """user/updateBulkStatus — scraped registry alias."""
    payload = payload or {}
    user_ids = payload.get("userIds") or payload.get("userId") or []
    if not isinstance(user_ids, (list, tuple)):
        user_ids = [user_ids]
    if "status" not in payload:
        return {"message": "status required", "code": 1, "error": True, "data": {}}
    return _mongo_admin_bulk_status_update(
        list(user_ids), _status_as_int(payload["status"]), session_user
    )


def mongo_admin_user_update(payload: dict, session_user: dict) -> dict:
    payload = payload or {}
    user_ids_raw = payload.get("userId") or payload.get("userIds")
    if isinstance(user_ids_raw, (list, tuple)) and "status" in payload:
        return _mongo_admin_bulk_status_update(
            list(user_ids_raw), _status_as_int(payload["status"]), session_user
        )

    user_id = str(payload.get("userId") or "")
    if not user_id:
        return {"message": "userId required", "code": 1, "error": True, "data": {}}

    db = get_db()
    target = _find_user(db, user_id)
    if not target:
        return {"message": "User not found", "code": 1, "error": True, "data": {}}
    if not _can_manage_user(session_user, target["userId"]):
        return {"message": "Not authorised", "code": 1, "error": True, "data": {}}

    actor = _actor_user(db, session_user)
    share_err = _validate_assignable_shares(payload, actor)
    if share_err:
        return {"message": share_err, "code": 1, "error": True, "data": {}}

    updates: dict = {"updatedAt": _now()}
    msg = "User updated successfully"

    scalar_fields = {
        "name": str,
        "mobile": str,
        "reference": str,
        "commissionType": str,
        "commChangeType": str,
    }
    num_fields = (
        "matchShare", "casinoShare", "matkaShare",
        "matchFlatShare", "casinoFlatShare", "matkaFlatShare",
        "matchCommission", "sessionCommission", "casinoCommission", "matkaCommission",
        "intCasinoExpoLimit", "intCasinoMultiply", "maxCommValue", "creditReference",
    )
    bool_fields = (
        "betStatus", "matchStatus", "casinoStatus", "matkaStatus",
        "intCasinoStatus", "userFlatSharePermission",
    )

    for key, cast in scalar_fields.items():
        if key in payload and payload[key] is not None:
            updates[key] = cast(payload[key])
    for key in num_fields:
        if key in payload and payload[key] is not None:
            updates[key] = _num(payload[key])
    for key in bool_fields:
        if key in payload:
            updates[key] = bool(payload[key])
    if "status" in payload:
        updates["status"] = _status_as_int(payload["status"])
        msg = "User status updated"
    if payload.get("password"):
        updates["password"] = str(payload["password"])
        updates["isPasswordChanged"] = True
        msg = "Password updated successfully"

    if len(updates) <= 1:
        return {"message": "Nothing to update", "code": 1, "error": True, "data": {}}

    db.users.update_one({"userId": target["userId"]}, {"$set": updates})
    return {"message": msg, "code": 0, "error": False, "data": {}}


def mongo_admin_update_password(payload: dict, session_user: dict) -> dict:
    """user/updateUserPassword — change-password page (PATCH, session userId implicit)."""
    payload = payload or {}
    new_password = str(payload.get("password") or payload.get("newPassword") or "")
    old_password = str(payload.get("oldPassword") or payload.get("currentPass") or "")
    user_id = str(payload.get("userId") or session_user.get("userId") or "")

    if not new_password:
        return {"message": "Password required", "code": 1, "error": True, "data": {}}
    if not user_id:
        return {"message": "userId required", "code": 1, "error": True, "data": {}}
    if not old_password:
        return {"message": "Please enter current password", "code": 1, "error": True, "data": {}}

    db = get_db()
    target = _find_user(db, user_id)
    if not target:
        return {"message": "User not found", "code": 1, "error": True, "data": {}}
    if not _can_manage_user(session_user, target["userId"]):
        return {"message": "Not authorised", "code": 1, "error": True, "data": {}}
    if str(target.get("password") or "") != old_password:
        return {"message": "Invalid old password", "code": 1, "error": True, "data": {}}

    now = _now()
    db.users.update_one(
        {"userId": target["userId"]},
        {"$set": {"password": new_password, "isPasswordChanged": True, "updatedAt": now}},
    )
    db.auth_sessions.delete_many({"userId": target["userId"]})
    return {"message": "Password updated successfully", "code": 0, "error": False, "data": {}}


def _activity_date_range(payload: dict) -> dict | None:
    """userLoginActivity / userActivity — optional fromDate/toDate filter."""
    from datetime import timezone

    from mongodb.admin_compute import _parse_date

    from_dt = _parse_date(payload.get("fromDate"))
    to_dt = _parse_date(payload.get("toDate"))
    if not from_dt and not to_dt:
        return None
    q: dict = {}
    if from_dt:
        q["$gte"] = from_dt
    if to_dt:
        end = to_dt.replace(hour=23, minute=59, second=59, microsecond=999999)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        q["$lte"] = end
    return q


def mongo_admin_user_login_activity(payload: dict, _session_user: dict = None) -> dict:
    payload = payload or {}
    uid = payload.get("userId")
    db = get_db()
    query: dict = {"activityType": "login"}
    if uid:
        query["userId"] = uid
    date_q = _activity_date_range(payload)
    if date_q:
        query["createdAt"] = date_q

    rows = []
    for doc in db.user_activities.find(query).sort("createdAt", -1).limit(500):
        user = db.users.find_one({"userId": doc.get("userId")}, {"username": 1}) or {}
        created = doc.get("createdAt")
        ts = int(created.timestamp() * 1000) if hasattr(created, "timestamp") else created
        payload_doc = doc.get("payload") or {}
        rows.append({
            "userId": doc.get("userId", ""),
            "username": user.get("username", ""),
            "ip": doc.get("ip", ""),
            "isp": doc.get("device", "") or payload_doc.get("isp", ""),
            "createdAt": ts,
        })

    return {"message": "Login activity fetched", "code": 0, "error": False, "data": rows}


def mongo_admin_user_activity(payload: dict, _session_user: dict = None) -> dict:
    payload = payload or {}
    uid = payload.get("userId")
    db = get_db()
    query: dict = {}
    if uid:
        query["userId"] = uid
    date_q = _activity_date_range(payload)
    if date_q:
        query["createdAt"] = date_q

    rows = []
    for doc in db.user_activities.find(query).sort("createdAt", -1).limit(500):
        user = db.users.find_one({"userId": doc.get("userId")}, {"username": 1}) or {}
        created = doc.get("createdAt")
        ts = int(created.timestamp() * 1000) if hasattr(created, "timestamp") else created
        rows.append({
            "userId": doc.get("userId", ""),
            "username": user.get("username", ""),
            "activityType": doc.get("activityType", ""),
            "ip": doc.get("ip", ""),
            "createdAt": ts,
        })

    return {"message": "User activity fetched", "code": 0, "error": False, "data": rows}


def mongo_admin_sport_list(payload: dict, _session_user: dict = None) -> dict:
    from mongodb.admin_compute import compute_complete_sport_list
    data = compute_complete_sport_list(payload or {})
    return {"message": "OK", "code": 0, "error": False, "data": data}


def mongo_admin_day_wise_casino(payload: dict, session_user: dict = None) -> dict:
    return mongo_admin_day_wise_casino_report(payload, session_user)


def mongo_admin_profit_loss(payload: dict, session_user: dict = None) -> dict:
    return mongo_admin_profit_loss_report(payload, session_user)


def mongo_admin_domain_setting(payload: dict, _session_user: dict = None) -> dict:
    from mongodb.domains_api import domain_setting_response

    return domain_setting_response(payload or {})


def mongo_admin_lena_dena(payload: dict, session_user: dict) -> dict:
    return mongo_admin_lena_dena_transfer(payload, session_user)


def _empty_list(msg: str = "OK") -> dict:
    return {"message": msg, "code": 0, "error": False, "data": []}


def _empty_paginated(msg: str = "OK") -> dict:
    return {"message": msg, "code": 0, "error": False, "data": {"total": 0, "list": []}}


# endpoint → (handler, needs_session) — scraped admin panel ke saare 43 APIs
ADMIN_ROUTES: dict[str, tuple] = {
    "user/userList": (mongo_admin_user_list, True),
    "user/userSearch": (mongo_admin_user_list, True),
    "user/userDetails": (mongo_admin_user_details, True),
    "user/getUserShareData": (mongo_admin_get_user_share_data, True),
    "user/userBalance": (mongo_admin_user_balance, True),
    "user/userLedger": (mongo_admin_user_ledger, True),
    "user/ledgerCreditDebit": (mongo_admin_ledger_credit_debit, True),
    "user/deleteUserLedger": (mongo_admin_delete_ledger, True),
    "user/domainList": (mongo_admin_domain_list, True),
    "user/create": (mongo_admin_create_user, True),
    "user/updateCoins": (mongo_admin_update_coins, True),
    "user/userUpdate": (mongo_admin_user_update, True),
    "user/userupdate": (mongo_admin_user_update, True),
    "user/updateBulkStatus": (mongo_admin_update_bulk_status, True),
    "user/updateUserPassword": (mongo_admin_update_password, True),
    "user/userLoginActivity": (mongo_admin_user_login_activity, True),
    "user/userActivity": (mongo_admin_user_activity, True),
    "user/lenaDena": (mongo_admin_lena_dena, True),
    "user/userStatement": (mongo_admin_user_statement, True),
    "website/domainSettingByDomainName": (mongo_admin_domain_setting, True),
    "sports/matchList": (mongo_admin_match_list, True),
    "sports/sportByMarketId": (mongo_admin_match_by_market, True),
    "sports/betsList": (mongo_admin_bets_list, True),
    "sports/clientListByMarketId": (mongo_admin_client_list_by_market, True),
    "sports/getSessionList": (mongo_admin_session_list, True),
    "sports/getOddsPosition": (mongo_admin_odds_position, True),
    "sports/getSessionPositionBySelectionId": (mongo_admin_session_position, True),
    "casino/getDiamondCasinoData": (mongo_admin_casino_data, True),
    "casino/getDiamondCasinoByEventId": (mongo_admin_casino_by_event, True),
    "casino/updateDiamondCasino": (mongo_admin_update_diamond_casino, True),
    "casino/diamondBetsList": (mongo_admin_casino_bets, True),
    "casino/dayWiseCasinoReport": (mongo_admin_day_wise_casino, True),
    "casino/diamondCasinoReportByUser": (mongo_admin_casino_report_by_user, True),
    "casino/getPlusMinusCasinoDetail": (mongo_admin_casino_plus_minus, True),
    "casino/getProfitLossPos": (mongo_admin_casino_profit_loss_pos, True),
    "casino/realTimeDataPosDataDiamondCasino": (mongo_admin_casino_realtime_pos, True),
    "casino/roundWiseResult": (mongo_admin_casino_round_result, True),
    "casino/resultByRoundWise": (mongo_admin_casino_result_by_round, True),
    "matka/dayWiseMatkaReport": (mongo_admin_matka_day_wise, True),
    "matka/getMatkaList": (mongo_admin_matka_list, True),
    "matka/matkaBetList": (mongo_admin_matka_bet_list, True),
    "matka/getProfitLossPosMatka": (mongo_admin_matka_profit_loss, True),
    "decision/completeSportList": (mongo_admin_sport_list, True),
    "decision/getPlusMinusByMarketId": (mongo_admin_plus_minus_market, True),
    "decision/userCommissionReport": (mongo_admin_user_commission_report, True),
    "decision/commissionListByUserId": (mongo_admin_commission_list_by_user, True),
    "decision/resetCommList": (mongo_admin_reset_comm_list, True),
    "decision/resetComm": (mongo_admin_reset_comm, True),
    "reports/userProfitLoss": (mongo_admin_profit_loss, True),
    "reports/getPlusMinusByMarketIdByUserWise": (mongo_admin_plus_minus_user_wise, True),
    "reports/blockMarket": (mongo_admin_block_market, True),
    "reports/userWiseBlockMarketList": (mongo_admin_block_market_list, True),
    "bluexchReports/clientPlusMinus": (mongo_admin_client_plus_minus, True),
}


def handle_admin_api(endpoint: str, payload: dict, auth_header: str) -> bytes:
    """Saari admin APIs — MongoDB only."""
    endpoint = endpoint.lstrip("/").split("?")[0]

    if not ping():
        return json.dumps({
            "error": True, "code": 500,
            "message": "MongoDB not running. brew services start mongodb-community",
        }).encode("utf-8")

    if endpoint.endswith("user/login"):
        return json.dumps(mongo_admin_login(payload or {}), default=str).encode("utf-8")

    if endpoint.endswith("logout"):
        return json.dumps(mongo_logout(auth_header), default=str).encode("utf-8")

    route = ADMIN_ROUTES.get(endpoint)
    session_user: dict = {}

    if route:
        handler, needs_session = route
        if needs_session:
            session_user, err = resolve_admin_user(auth_header)
            if err:
                return json.dumps(err, default=str).encode("utf-8")
            token = _extract_bearer(auth_header)
            if token:
                session_user = {**session_user, "_token": token}
        body = handler(payload or {}, session_user)
        return json.dumps(body, default=str).encode("utf-8")

    # Baaki endpoints — empty safe response
    if any(k in endpoint for k in ARRAY_ENDPOINT_KEYS):
        body = _empty_list()
    elif "user/" in endpoint:
        body = _empty_paginated()
    else:
        body = {"message": "OK", "code": 0, "error": False, "data": {}}

    return json.dumps(body, default=str).encode("utf-8")
