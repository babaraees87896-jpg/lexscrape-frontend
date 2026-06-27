"""MongoDB auth — client website login / logout / balance."""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from mongodb.db import get_db, ping

LOCAL_TOKEN_PREFIX = "local-ex99-"
TOKEN_TTL_HOURS = 24


def _now():
    return datetime.now(timezone.utc)


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _normalize_client_username(username: str) -> str:
    username = (username or "").strip()
    if not username:
        return username
    if username.upper().startswith("C"):
        return "C" + username.lstrip("Cc")
    return f"C{username}"


def _user_to_login_data(user: dict) -> dict:
    coins = user.get("coins", 0)
    return {
        "userId": user.get("userId"),
        "username": user.get("username"),
        "name": user.get("name"),
        "userType": user.get("userType", "client"),
        "userPriority": user.get("userPriority", 1),
        "parentId": user.get("parentId"),
        "creatorId": user.get("creatorId"),
        "balance": coins,
        "coins": coins,
        "exposure": user.get("exposure", 0),
        "profitLoss": user.get("profitLoss", 0),
        "casinoStatus": user.get("casinoStatus", True),
        "intCasinoStatus": user.get("intCasinoStatus", False),
        "intCasinoMultiply": user.get("intCasinoMultiply", 1),
        "matkaStatus": user.get("matkaStatus", True),
        "betStatus": user.get("betStatus", True),
        "matchStatus": user.get("matchStatus", True),
        "matchShare": user.get("matchShare", 0),
        "matchCommission": user.get("matchCommission", 0),
        "sessionCommission": user.get("sessionCommission", 0),
        "casinoShare": user.get("casinoShare", 0),
        "casinoCommission": user.get("casinoCommission", 0),
        "betChipsData": user.get("betChipsData", {}),
        "betChipsModal": user.get("betChipsModal", False),
        "isPasswordChanged": user.get("isPasswordChanged", True),
        "isOneClickBet": user.get("isOneClickBet", False),
        "oneClickBetAmount": user.get("oneClickBetAmount", 10),
        "referralCode": user.get("referralCode", ""),
        "rateReffrence": user.get("rateReffrence", 0.01),
        "isDemoClient": user.get("isDemoClient", False),
        "userCount": user.get("userCount", {}),
        "creatorName": user.get("creatorName", ""),
        "updatedAt": user.get("updatedAt", _now()).isoformat()
        if hasattr(user.get("updatedAt"), "isoformat")
        else user.get("updatedAt"),
    }


def _make_token() -> str:
    return LOCAL_TOKEN_PREFIX + secrets.token_urlsafe(32)


def is_local_token(token: str) -> bool:
    return bool(token) and token.startswith(LOCAL_TOKEN_PREFIX)


def _extract_bearer(auth_header: str) -> Optional[str]:
    if not auth_header:
        return None
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()
    return auth_header.strip()


def resolve_client_user(
    auth_header: str,
    payload: Optional[dict] = None,
    *,
    require_bet_access: bool = False,
) -> Tuple[Optional[dict], Optional[dict]]:
    """Logged-in client user — payload.userId mismatch par 403."""
    token = _extract_bearer(auth_header)
    user = validate_session(token) if token else None
    if not user:
        return None, {"message": "You are logged in from another device. Please login again.", "code": 401, "error": True, "data": {}}
    req_uid = (payload or {}).get("userId")
    if req_uid and str(req_uid) != str(user.get("userId")):
        return None, {"message": "Unauthorized", "code": 403, "error": True, "data": {}}
    if require_bet_access and not user.get("betStatus", True):
        return None, {"message": "Betting is disabled for this account", "code": 1, "error": True, "data": {}}
    return user, None


def validate_session(token: str) -> Optional[dict]:
    if not is_local_token(token):
        return None
    db = get_db()
    session = db.auth_sessions.find_one({"token": token})
    if not session:
        return None
    expires = session.get("expiresAt")
    if expires and _as_utc(expires) < _now():
        db.auth_sessions.delete_one({"token": token})
        return None
    user = db.users.find_one({"userId": session.get("userId")})
    return user


def mongo_login(payload: dict) -> dict:
    if not ping():
        return {"error": True, "code": 500, "message": "MongoDB not running. Run: python3 main.py --setup-mongo"}

    username = _normalize_client_username(payload.get("username", ""))
    password = str(payload.get("password", ""))

    db = get_db()
    user = db.users.find_one({
        "username": {"$regex": f"^{username}$", "$options": "i"},
        "password": password,
        "userType": "client",
        "isDeleted": {"$ne": True},
    })

    if not user:
        return {
            "error": True,
            "code": 1,
            "message": "Invalid username or password",
        }

    token = _make_token()
    now = _now()
    # Client website — ek time par ek hi session (dusri jagah login = purani session band)
    db.auth_sessions.delete_many({
        "userId": user["userId"],
        "panel": {"$exists": False},
    })
    db.auth_sessions.insert_one({
        "token": token,
        "userId": user["userId"],
        "username": user["username"],
        "createdAt": now,
        "expiresAt": now + timedelta(hours=TOKEN_TTL_HOURS),
    })

    data = _user_to_login_data(user)
    from mongodb.bets import sync_user_balance
    coins, exposure = sync_user_balance(user["userId"])
    data["coins"] = coins
    data["balance"] = coins
    data["exposure"] = exposure
    data["token"] = token
    data["jwtToken"] = token
    user_payload = dict(data)
    user_payload["data"] = dict(data)
    return {
        "message": "Login successful",
        "code": 0,
        "error": False,
        "token": token,
        "user": user_payload,
        "data": data,
    }


def mongo_logout(auth_header: str) -> dict:
    token = _extract_bearer(auth_header)
    if token and is_local_token(token):
        get_db().auth_sessions.delete_one({"token": token})
    return {"message": "Logged out", "code": 0, "error": False}


def mongo_user_balance(payload: dict, auth_header: str) -> dict:
    token = _extract_bearer(auth_header)
    user = validate_session(token) if token else None

    if not user:
        return {"error": True, "code": 401, "message": "You are logged in from another device. Please login again."}

    req_uid = payload.get("userId")
    if req_uid and str(req_uid) != str(user.get("userId")):
        return {"error": True, "code": 403, "message": "Unauthorized"}

    from mongodb.bets import sync_user_balance

    uid_key = user.get("userId")
    if uid_key:
        coins, exposure = sync_user_balance(uid_key)
        user = get_db().users.find_one({"userId": uid_key}) or user
    else:
        coins = float(user.get("coins") or 0)
        exposure = float(user.get("exposure") or 0)

    return {
        "message": "",
        "code": 0,
        "error": False,
        "data": {
            "intCasinoStatus": str(user.get("intCasinoStatus", False)).lower(),
            "exposure": str(exposure),
            "intCasinoMultiply": str(user.get("intCasinoMultiply", 1)),
            "isDeleted": str(user.get("isDeleted", False)).lower(),
            "jwtToken": token,
            "status": "1",
            "coins": str(coins),
            "casinoToken": "",
            "authenticateToken": uuid.uuid4().hex,
            "creatorId": user.get("creatorId") or user.get("parentId") or "",
            "profitLoss": str(user.get("profitLoss", 0)),
        },
    }


def mongo_client_update_password(payload: dict, auth_header: str) -> dict:
    """Client sidebar — oldPassword verify karke naya password save karo."""
    token = _extract_bearer(auth_header)
    user = validate_session(token) if token else None
    if not user:
        return {
            "message": "You are logged in from another device. Please login again.",
            "code": 401,
            "error": True,
            "data": {},
        }

    payload = payload or {}
    old_password = str(payload.get("oldPassword") or "")
    new_password = str(payload.get("password") or "")
    confirm = str(payload.get("confirm_password") or payload.get("confirmPassword") or "")

    if not old_password:
        return {"message": "Please enter current password", "code": 1, "error": True, "data": {}}
    if not new_password:
        return {"message": "Password required", "code": 1, "error": True, "data": {}}
    if confirm and new_password != confirm:
        return {
            "message": "New Password and Confirm Password must be same",
            "code": 1,
            "error": True,
            "data": {},
        }
    if str(user.get("password") or "") != old_password:
        return {"message": "Invalid old password", "code": 1, "error": True, "data": {}}

    db = get_db()
    now = _now()
    db.users.update_one(
        {"userId": user["userId"]},
        {"$set": {"password": new_password, "isPasswordChanged": True, "updatedAt": now}},
    )
    db.auth_sessions.delete_many({"userId": user["userId"], "panel": {"$exists": False}})

    return {
        "message": "Password updated successfully",
        "code": 0,
        "error": False,
        "data": {},
    }


def mongo_client_update_password(payload: dict, auth_header: str) -> dict:
    """Client sidebar — oldPassword verify karke naya password save karo."""
    token = _extract_bearer(auth_header)
    user = validate_session(token) if token else None
    if not user:
        return {
            "message": "You are logged in from another device. Please login again.",
            "code": 401,
            "error": True,
            "data": {},
        }

    payload = payload or {}
    old_password = str(payload.get("oldPassword") or "")
    new_password = str(payload.get("password") or "")
    confirm = str(payload.get("confirm_password") or payload.get("confirmPassword") or "")

    if not old_password:
        return {"message": "Please enter current password", "code": 1, "error": True, "data": {}}
    if not new_password:
        return {"message": "Password required", "code": 1, "error": True, "data": {}}
    if confirm and new_password != confirm:
        return {
            "message": "New Password and Confirm Password must be same",
            "code": 1,
            "error": True,
            "data": {},
        }
    if str(user.get("password") or "") != old_password:
        return {"message": "Invalid old password", "code": 1, "error": True, "data": {}}

    db = get_db()
    now = _now()
    db.users.update_one(
        {"userId": user["userId"]},
        {"$set": {"password": new_password, "isPasswordChanged": True, "updatedAt": now}},
    )
    db.auth_sessions.delete_many({"userId": user["userId"], "panel": {"$exists": False}})

    return {
        "message": "Password updated successfully",
        "code": 0,
        "error": False,
        "data": {},
    }
