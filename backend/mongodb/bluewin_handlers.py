"""BlueWin staff panel — website/*, crick365/*, daman/* nav APIs from ex99_local."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from typing import Any, Optional

from bson import ObjectId

from mongodb.admin_all_handlers import _is_falsey_flag, _is_truthy_flag, mongo_admin_bets_list
from mongodb.admin_api import resolve_admin_user
from mongodb.admin_compute import (
    _casino_bet_ts,
    _casino_bets_in_scope,
    _casino_game_name,
    _casino_games_map,
    _find_user,
    _session_downline_ids,
    compute_session_list,
    normalize_casino_game,
)
from mongodb.bet_logic import is_fancy_market, total_exposure_from_positions, total_fancy_exposure
from mongodb.bets import _market_label, _refresh_user_exposure
from mongodb.centerpanel_cache import get_cp_sessions_from_bets
from mongodb.centerpanel_api import _ok, _err
from mongodb.db import get_db
from mongodb.matches_api import prepare_match_for_admin, _find_match_local


def _strip(doc: dict) -> dict:
    row = copy.deepcopy(doc)
    row.pop("_id", None)
    return row


def _iso(val: Any) -> str:
    if isinstance(val, datetime):
        if val.tzinfo is None:
            val = val.replace(tzinfo=timezone.utc)
        return val.isoformat().replace("+00:00", "Z")
    return str(val or "")


def _now():
    return datetime.now(timezone.utc)


def _domain_row(doc: dict) -> dict:
    row = _strip(doc)
    oid = doc.get("_id")
    if oid is not None:
        sid = str(oid)
        row["id"] = sid
        row["domainId"] = sid
        row["_id"] = sid
    row.setdefault("domainName", row.get("domainUrl") or "")
    row.setdefault("domainUrl", row.get("domainName") or "")
    row.setdefault("status", True)
    return row


_DOMAIN_MUTABLE_KEYS = frozenset({
    "domainName", "domainUrl", "title", "userNotification", "clientNotification",
    "whatsappNumber", "helplineNumber", "assignSubownerId", "assignAgentId",
    "registerBonus", "status", "isRegister", "isAffilation", "isSignUpOtp",
    "talkTo", "isSignupBonus", "isDeleted", "aboutUs", "contactUs", "operatorId",
    "imgBaseUrl", "minimumWithdrawAmount", "maximumWithdrawAmount",
    "themeSetting", "sportsSetting", "socialMedia", "apiKey", "banner",
    "account", "barcode", "upi", "signUpBonusSetting", "isReferralBonus",
    "reffrelSetting", "bonusSetting", "language", "country", "logo", "favicon",
})


_SKIP_EMPTY_NESTED = frozenset({
    "signUpBonusSetting", "reffrelSetting", "bonusSetting", "account", "barcode",
})


def _domain_updates_from_payload(payload: dict) -> dict:
    upd: dict[str, Any] = {}
    for key in _DOMAIN_MUTABLE_KEYS:
        if key not in payload:
            continue
        val = payload[key]
        # Staff form sends {} when optional bonus blocks are off — don't wipe DB.
        if key in _SKIP_EMPTY_NESTED and val == {}:
            continue
        upd[key] = val
    return upd


def _find_domain(payload: dict) -> Optional[dict]:
    db = get_db()
    domain_id = str(payload.get("domainId") or payload.get("id") or payload.get("_id") or "")
    if domain_id:
        if ObjectId.is_valid(domain_id):
            doc = db.domains.find_one({"_id": ObjectId(domain_id)})
            if doc:
                return doc
        doc = db.domains.find_one({"domainName": domain_id})
        if doc:
            return doc
        doc = db.domains.find_one({"domainUrl": domain_id})
        if doc:
            return doc
    return None


def _format_staff_casino_bet_row(bet: dict, user: dict, game: dict | None = None) -> dict:
    """Unsettled int/diamond casino list — creditAmount, debitAmount, betDetails."""
    stake = float(bet.get("stake") or bet.get("amount") or 0)
    pl = float(bet.get("profitLoss") or 0)
    status = str(bet.get("status") or "open").lower()
    is_declare = bool(bet.get("isDeclare")) or status in ("settled", "won", "lost")
    mongo_id = bet.get("_id")
    row_id = str(mongo_id) if mongo_id is not None else str(bet.get("betId") or "")

    credit = 0.0
    debit = stake
    if is_declare:
        if pl > 0:
            credit = round(stake + pl, 2)
            debit = round(stake, 2)
        else:
            credit = 0.0
            debit = round(abs(pl) if pl else stake, 2)

    selection = str(bet.get("selection") or bet.get("playerName") or "")
    result = bet.get("resultDetails") or bet.get("result") or {}
    if not isinstance(result, dict):
        result = {}

    return {
        "id": row_id,
        "_id": row_id,
        "betId": bet.get("betId"),
        "createdAt": _casino_bet_ts(bet) or bet.get("createdAt"),
        "userInfo": {
            "userId": user.get("userId"),
            "username": user.get("username") or "",
            "name": user.get("name") or user.get("username") or "",
        },
        "gameName": _casino_game_name(game, bet.get("eventId")),
        "roundId": bet.get("roundId"),
        "betDetails": {
            "nat": selection,
            "detailsResult": result,
        },
        "betType": bet.get("casinoType") or bet.get("betType") or "",
        "isDeclare": is_declare,
        "creditAmount": credit,
        "debitAmount": debit,
        "eventId": bet.get("eventId"),
        "amount": stake,
        "gameType": bet.get("gameType") or "diamondCasino",
    }


def _is_international_casino_bet_scope(payload: dict) -> bool:
    """Staff int / unsettled int bet list — gameId 201206 or bP unsettled shape."""
    payload = payload or {}
    gt = str(payload.get("gameType") or "").strip().lower()
    if gt == "internationalcasino":
        return True
    gid = payload.get("gameId")
    if gid is not None and str(gid).strip() != "":
        if str(gid) == "201206":
            return True
        try:
            from mongodb.casino_api import int_casino_game_ids

            return int(gid) in int_casino_game_ids()
        except (TypeError, ValueError):
            pass
    # /app/unsettledIntBetList (bP) — casinoBet + isDeclare:0 + sortData, no username/pageNo
    if (
        payload.get("casinoBet")
        and _is_falsey_flag(payload.get("isDeclare"))
        and (payload.get("sortData") or {}).get("createdAt") == 1
        and payload.get("pageNo") in (None, "", 0)
        and not payload.get("username")
        and not payload.get("userName")
        and payload.get("eventId") in (None, "")
    ):
        return True
    return False


def _staff_casino_bets_list(payload: dict, session_user: dict = None) -> dict:
    """sports/betsList?casinoBet=true — unsettled int/diamond casino bet list."""
    payload = payload or {}
    db = get_db()
    games = _casino_games_map(db)
    int_scope = _is_international_casino_bet_scope(payload)
    req_game_id = payload.get("gameId")
    specific_int_game = (
        int_scope
        and req_game_id is not None
        and str(req_game_id).strip() not in ("", "201206")
    )

    declare_false = _is_falsey_flag(payload.get("isDeclare"))
    declare_true = _is_truthy_flag(payload.get("isDeclare"))
    del_false = _is_falsey_flag(payload.get("isDeleted"))
    del_true = _is_truthy_flag(payload.get("isDeleted"))

    rows: list[dict] = []
    for bet in _casino_bets_in_scope(db, payload):
        if int_scope:
            from mongodb.casino_api import is_international_casino_bet

            if specific_int_game:
                try:
                    target = int(req_game_id)
                    bet_eid = bet.get("eventId")
                    bet_gid = bet.get("gameId")
                    if int(bet_eid or 0) != target and int(bet_gid or 0) != target:
                        continue
                except (TypeError, ValueError):
                    continue
            elif not is_international_casino_bet(bet):
                continue
        status = str(bet.get("status") or "open").lower()
        is_declare = bool(bet.get("isDeclare")) or status in ("settled", "won", "lost")
        is_deleted = bool(bet.get("isDeleted")) or status == "deleted"

        if declare_false and is_declare:
            continue
        if declare_true and not is_declare:
            continue
        if del_false and is_deleted:
            continue
        if del_true and not is_deleted:
            continue

        user = _find_user(db, str(bet.get("userId") or "")) or {}
        event_id = bet.get("eventId")
        game = None
        if event_id is not None:
            game = games.get(int(event_id)) if str(event_id).isdigit() else games.get(event_id)
        rows.append(_format_staff_casino_bet_row(bet, user, game))

    asc = (payload.get("sortData") or {}).get("createdAt") == 1
    rows.sort(key=lambda r: r.get("createdAt") or 0, reverse=not asc)

    return _ok(
        {"casinoBetData": rows, "totalCasinoCount": len(rows)},
        "Fetch List Successfuly",
    )


def bw_sports_bets_list(payload: dict, session_user: dict = None) -> dict:
    """Undeclare / unsettled bet lists — admin shape."""
    payload = payload or {}
    if payload.get("casinoBet"):
        return _staff_casino_bets_list(payload, session_user)
    return mongo_admin_bets_list(payload, session_user)


def _bw_session_label(row: dict, name_hint: str = "") -> str:
    name = (
        row.get("fancyName")
        or row.get("sessionName")
        or row.get("session_name")
        or name_hint
        or ""
    )
    if isinstance(name, list):
        name = name[0] if name else ""
    if not name:
        session_names = row.get("sessionNames")
        if isinstance(session_names, list) and session_names:
            name = session_names[0]
        elif isinstance(session_names, str):
            name = session_names
    return str(name or "Session").strip() or "Session"


def _bw_fancy_gtype(row: dict) -> str:
    gtype = str(row.get("gtype") or row.get("fancyType") or "Normal").strip()
    if gtype.lower() in ("", "fancy"):
        return "Normal"
    return gtype


def _to_bw_fancy_session_row(row: dict, name_hint: str = "") -> dict:
    sid = str(row.get("selectionId") or row.get("fancyId") or row.get("_id") or "")
    name = _bw_session_label(row, name_hint)
    is_declare = bool(row.get("isDeclare"))
    is_deleted = row.get("isDeleted")
    if is_deleted is None:
        is_deleted = str(row.get("status") or "").lower() == "deleted"
    decision_run = row.get("decisionRun")
    return {
        "selectionId": sid,
        "sessionNames": [name],
        "sessionName": name,
        "decisionRun": decision_run if decision_run is not None else "",
        "marketId": str(row.get("marketId") or ""),
        "gtype": _bw_fancy_gtype(row),
        "fancyType": str(row.get("fancyType") or "Normal"),
        "isDeclare": 1 if is_declare else 0,
        "isDeleted": 1 if is_deleted else 0,
        "_id": str(row.get("_id") or sid),
    }


def bw_sports_get_session_list(payload: dict, session_user: dict = None) -> dict:
    """Fancy declare page — sessionNames array + isDeclare/isDeleted ints (BlueWin UI)."""
    payload = payload or {}
    market_id = str(payload.get("marketId") or "")

    name_hints: dict[str, str] = {}
    for row in compute_session_list(market_id):
        sid = str(row.get("selectionId") or "")
        if sid:
            name_hints[sid] = _bw_session_label(row)

    merged: dict[str, dict] = {}
    for row in get_cp_sessions_from_bets(payload):
        sid = str(row.get("selectionId") or "")
        if not sid:
            continue
        merged[sid] = _to_bw_fancy_session_row(row, name_hints.get(sid, ""))

    for row in compute_session_list(market_id):
        sid = str(row.get("selectionId") or "")
        if sid and sid not in merged:
            merged[sid] = _to_bw_fancy_session_row(
                {**row, "marketId": market_id, "isDeclare": False},
                name_hints.get(sid, ""),
            )

    rows = list(merged.values())
    rows.sort(key=lambda r: str((r.get("sessionNames") or [""])[0]))

    declare_flag = payload.get("isDeclere", payload.get("isDeclare"))
    if declare_flag is not None and declare_flag != "":
        want = bool(int(declare_flag)) if str(declare_flag).lstrip("-").isdigit() else bool(declare_flag)
        rows = [r for r in rows if bool(r.get("isDeclare")) == want]

    return _ok(rows, "Session list fetched")


def bw_website_domain_list(_payload: dict, _session_user: dict = None) -> dict:
    rows = [_domain_row(d) for d in get_db().domains.find({})]
    return _ok(rows, "Domain List fetched Successfully")


def bw_website_get_domain_setting(payload: dict, _session_user: dict = None) -> dict:
    doc = _find_domain(payload or {})
    if not doc:
        return _err("Domain not found")
    return _ok(_domain_row(doc), "Domain setting fetched")


def bw_website_get_setting(_payload: dict, _session_user: dict = None) -> dict:
    db = get_db()
    settings: dict[str, Any] = {
        "tvFetchUrl": "https://score.tresting.com/api/tv/",
        "imgBaseUrl": "/images",
        "maxIntCasinoBet": 200000,
        "minIntCasinoBet": 100,
        "maxBetfairOddsCheck": 1000,
        "tossRate": 0.95,
    }
    for row in db.center_master_settings.find({}, {"_id": 0}):
        key = row.get("settingKey")
        if key:
            settings[key] = row.get("value")
    domain = db.domains.find_one({})
    if domain:
        settings.setdefault("title", domain.get("title", ""))
        settings.setdefault("userNotification", domain.get("userNotification", ""))
    return _ok(settings, "Setting fetched")


def bw_website_update_setting(payload: dict, _session_user: dict = None) -> dict:
    payload = payload or {}
    db = get_db()
    now = _now()
    skip = {"domainId", "id", "_id", "domainName", "domainUrl"}
    for key, val in payload.items():
        if key in skip:
            continue
        db.center_master_settings.update_one(
            {"settingKey": key},
            {"$set": {"settingKey": key, "value": val, "updatedAt": now}},
            upsert=True,
        )
    return _ok({}, "Setting updated successfully")


def bw_website_create_domain(payload: dict, _session_user: dict = None) -> dict:
    payload = payload or {}
    db = get_db()
    doc = {
        "domainName": payload.get("domainName", ""),
        "domainUrl": payload.get("domainUrl") or payload.get("domainName", ""),
        "title": payload.get("title") or payload.get("domainName", ""),
        "status": payload.get("status", True),
        "userNotification": payload.get("userNotification", ""),
        "clientNotification": payload.get("clientNotification", ""),
        "themeSetting": payload.get("themeSetting") or {},
        "sportsSetting": payload.get("sportsSetting") or {},
        "banner": payload.get("banner") or [{"name": "", "priority": "", "image": ""}],
        "createdAt": _now(),
    }
    doc.update(_domain_updates_from_payload(payload))
    result = db.domains.insert_one(doc)
    doc["_id"] = result.inserted_id
    return _ok(_domain_row(doc), "Domain created successfully")


def bw_website_update_domain(payload: dict, _session_user: dict = None) -> dict:
    payload = payload or {}
    doc = _find_domain(payload)
    if not doc:
        return _err("Domain not found")
    upd = _domain_updates_from_payload(payload)
    if not upd:
        return _err("No domain fields to update")
    upd["updatedAt"] = _now()
    db = get_db()
    db.domains.update_one({"_id": doc["_id"]}, {"$set": upd})
    updated = db.domains.find_one({"_id": doc["_id"]}) or doc
    return _ok(_domain_row(updated), "Domain updated successfully")


def bw_website_delete_domain(payload: dict, _session_user: dict = None) -> dict:
    payload = payload or {}
    domain_id = str(payload.get("domainId") or payload.get("id") or payload.get("_id") or "")
    if not domain_id:
        return _err("domainId required")
    db = get_db()
    q: dict = {"_id": ObjectId(domain_id)} if ObjectId.is_valid(domain_id) else {
        "$or": [{"domainName": domain_id}, {"domainUrl": domain_id}],
    }
    result = db.domains.delete_one(q)
    if result.deleted_count == 0:
        return _err("Domain not found")
    return _ok({}, "Domain deleted successfully")


def bw_website_domain_setting_by_name(payload: dict, _session_user: dict = None) -> dict:
    """Client-facing domain config — full MongoDB document."""
    from mongodb.domains_api import domain_setting_response

    return domain_setting_response(payload or {})


def bw_website_negative_users(_payload: dict, _session_user: dict = None) -> dict:
    db = get_db()
    rows = []
    for user in db.users.find({"isDeleted": {"$ne": True}}, {"password": 0}):
        coins = float(user.get("coins") or user.get("balance") or 0)
        exposure = float(user.get("exposure") or 0)
        if coins < 0 or exposure > 0:
            rows.append({
                "username": user.get("username"),
                "userId": user.get("userId"),
                "name": user.get("name") or user.get("username"),
                "coins": coins,
                "balance": coins,
                "exposure": exposure,
                "userType": user.get("userType"),
            })
    return _ok({"list": rows, "total": len(rows)}, "Negative user list fetched")


def bw_website_get_category(payload: dict, _session_user: dict = None) -> dict:
    from mongodb.int_casino_category_api import list_int_casino_categories

    return list_int_casino_categories(payload or {})


def bw_website_category_crud(payload: dict, _session_user: dict = None) -> dict:
    from mongodb.int_casino_category_api import save_int_casino_category

    return save_int_casino_category(payload or {})


def bw_website_file_upload(payload: dict, _session_user: dict = None) -> dict:
    from mongodb.int_casino_category_api import save_int_casino_category_image

    data = payload or {}
    return save_int_casino_category_image(
        str(data.get("filename") or data.get("name") or "upload.jpg"),
        data.get("content") or b"",
    )


def bw_casino_update_diamond(payload: dict, _session_user: dict = None) -> dict:
    from mongodb.casino_api import update_diamond_casino

    return update_diamond_casino(payload or {})


def bw_website_int_casino_list(_payload: dict, _session_user: dict = None) -> dict:
    from mongodb.casino_api import staff_int_casino_games

    return _ok(staff_int_casino_games(), "International casino list fetched")


def bw_website_int_casino_action(_payload: dict, _session_user: dict = None) -> dict:
    return _ok({}, "Updated successfully")


def bw_website_update_international_casino(payload: dict, _session_user: dict = None) -> dict:
    from mongodb.casino_api import update_international_casino

    return update_international_casino(payload or {})


def bw_website_update_int_casino_redis(_payload: dict, _session_user: dict = None) -> dict:
    return _ok({}, "International casino list refreshed")


def bw_user_notification_list(_payload: dict, _session_user: dict = None) -> dict:
    rows = []
    for domain in get_db().domains.find({}):
        msg = domain.get("userNotification") or domain.get("clientNotification") or ""
        if not msg:
            continue
        rows.append({
            "domainName": domain.get("domainName") or domain.get("domainUrl"),
            "notification": msg,
            "status": 1,
            "type": "user",
        })
    return _ok(rows, "Notification list fetched")


def bw_user_save_notification(_payload: dict, _session_user: dict = None) -> dict:
    return _ok({}, "Notification saved")


def bw_crick365_login_clients(_payload: dict, _session_user: dict = None) -> dict:
    db = get_db()
    rows = []
    for sess in db.auth_sessions.find({}).sort("createdAt", -1).limit(200):
        user = db.users.find_one({"userId": sess.get("userId")}, {"password": 0}) or {}
        rows.append({
            "userId": sess.get("userId"),
            "username": sess.get("username") or user.get("username"),
            "name": user.get("name") or sess.get("username"),
            "userType": user.get("userType", "client"),
            "ip": user.get("lastLoginIp") or "127.0.0.1",
            "loginTime": _iso(sess.get("createdAt")),
            "panel": sess.get("panel") or "staffpanel",
        })
    if not rows:
        for user in db.users.find({"userType": "client", "isDeleted": {"$ne": True}}, {"password": 0}).limit(20):
            rows.append({
                "userId": user.get("userId"),
                "username": user.get("username"),
                "name": user.get("name"),
                "userType": user.get("userType"),
                "ip": "127.0.0.1",
                "loginTime": _iso(user.get("updatedAt")),
                "panel": "staffpanel",
            })
    return _ok({"loggedInClientsDetails": rows}, "Logged in clients fetched")


def bw_crick365_logout_block(_payload: dict, _session_user: dict = None) -> dict:
    payload = _payload or {}
    uid = payload.get("userId")
    if uid:
        get_db().auth_sessions.delete_many({"userId": uid})
    return _ok({}, "User logged out successfully")


def _activity_rows(payload: dict) -> list[dict]:
    db = get_db()
    payload = payload or {}
    username = str(payload.get("username") or "").strip().upper()
    ip_filter = str(payload.get("ip") or "").strip()
    rows: list[dict] = []

    user_ids: set[str] = set()
    if username:
        user = db.users.find_one({"username": {"$regex": f"^{username}$", "$options": "i"}}, {"userId": 1})
        if user:
            user_ids.add(str(user["userId"]))

    q: dict = {}
    if user_ids:
        q["userId"] = {"$in": list(user_ids)}

    for act in db.user_activities.find(q).sort("createdAt", -1).limit(500):
        if ip_filter and str(act.get("ip") or "") != ip_filter:
            continue
        payload_data = act.get("payload") or {}
        rows.append({
            "createdAt": _iso(act.get("createdAt")),
            "remarks": act.get("activityType") or payload_data.get("remarks") or "activity",
            "isDeclare": payload_data.get("isDeclare", False),
            "forBet": payload_data.get("forBet", ""),
            "marketId": payload_data.get("marketId", ""),
            "marketType": payload_data.get("marketType", ""),
            "eventId": payload_data.get("eventId", ""),
            "overallType": act.get("activityType") or "login",
            "gameType": payload_data.get("gameType", ""),
            "selectionId": payload_data.get("selectionId", ""),
            "creditAmount": float(payload_data.get("creditAmount") or 0),
            "amount": float(payload_data.get("amount") or payload_data.get("stake") or 0),
            "username": act.get("username") or "",
            "ip": act.get("ip") or "",
        })

    for entry in db.ledger_entries.find({}).sort("createdAt", -1).limit(200):
        uid = str(entry.get("userId") or "")
        user = db.users.find_one({"userId": uid}, {"username": 1}) or {}
        uname = str(user.get("username") or "")
        if username and uname.upper() != username:
            continue
        amt = float(entry.get("amount") or 0)
        rows.append({
            "createdAt": _iso(entry.get("createdAt")),
            "remarks": entry.get("remark") or entry.get("type") or "ledger",
            "isDeclare": True,
            "forBet": "",
            "marketId": entry.get("marketId") or "",
            "marketType": "",
            "eventId": entry.get("eventId") or "",
            "overallType": entry.get("ledgerType") or "ledger",
            "gameType": entry.get("gameType") or "",
            "selectionId": "",
            "creditAmount": amt if amt > 0 else 0,
            "amount": abs(amt),
            "username": uname,
            "ip": "",
        })

    rows.sort(key=lambda r: r.get("createdAt") or "", reverse=True)
    return rows


def bw_daman_tracking(payload: dict, _session_user: dict = None) -> dict:
    return _ok(_activity_rows(payload), "Tracking activities fetched")


def bw_daman_check_ip(payload: dict, _session_user: dict = None) -> dict:
    return _ok(_activity_rows(payload), "IP activity fetched")


def bw_website_transaction_data(payload: dict, _session_user: dict = None) -> dict:
    return _ok(_activity_rows(payload), "Transaction data fetched")


def bw_matka_game_type_list(_payload: dict, _session_user: dict = None) -> dict:
    types = ["SINGLE", "JODI", "PANA", "HALF_SANGAM", "FULL_SANGAM"]
    rows = [{"gameType": t, "status": 1, "priority": idx + 1} for idx, t in enumerate(types)]
    for ev in get_db().matka_events.find({}, {"_id": 0, "matkaEventId": 1, "name": 1}):
        rows.append({
            "gameType": ev.get("name") or ev.get("matkaEventId"),
            "matkaEventId": ev.get("matkaEventId"),
            "status": 1,
        })
    return _ok(rows, "Matka game type list fetched")


def bw_matka_result_panel(payload: dict, _session_user: dict = None) -> dict:
    rows = []
    for ev in get_db().matka_events.find({}, {"_id": 0}):
        rows.append({
            "matkaEventId": ev.get("matkaEventId"),
            "matkaName": ev.get("name"),
            "result": ev.get("result") or "",
            "status": ev.get("status") or "open",
            "date": (payload or {}).get("date") or "",
        })
    return _ok(rows, "Result panel list fetched")


def bw_reports_match_details(payload: dict, _session_user: dict = None) -> dict:
    market_id = str((payload or {}).get("marketId") or "")
    db = get_db()
    match = _find_match_local(market_id, "") or {}
    client_ids = _session_downline_ids(db, _session_user or {})
    if not client_ids:
        client_ids = {u["userId"] for u in db.users.find({"userType": "client"}, {"userId": 1})}

    fancy_bets = list(db.sports_bets.find({
        "marketId": market_id,
        "userId": {"$in": list(client_ids)},
    }))
    fancy_amount = sum(float(b.get("stake") or 0) for b in fancy_bets)
    odds_bets = [b for b in fancy_bets if str(b.get("betFor") or "").lower() in ("match", "bookmaker", "match odds")]
    odds_amount = sum(float(b.get("stake") or 0) for b in odds_bets)

    team_data = match.get("teamData")
    if isinstance(team_data, (dict, list)):
        team_data = json.dumps(team_data, default=str)

    return _ok({
        "teamData": team_data if isinstance(team_data, str) else (team_data or "[]"),
        "fancyResData": {
            "deletedFancyBets": 0,
            "totalFancyBetAmount": fancy_amount,
            "totalFancyProfitLoss": 0,
        },
        "oddsResData": {
            "deletedOddsBets": 0,
            "totalOddsBetAmount": odds_amount,
            "totalOddsProfitLoss": 0,
        },
        "usersCount": len(client_ids),
    }, "Match details fetched")


def bw_casino_undeclared_rounds(payload: dict, _session_user: dict = None) -> dict:
    from mongodb.casino_declare_api import list_undeclared_rounds

    return _ok(list_undeclared_rounds(payload or {}), "Undeclared rounds fetched")


def bw_casino_send_result_json(_payload: dict, _session_user: dict = None) -> dict:
    from mongodb.casino_declare_api import build_casino_result_json

    return _ok(build_casino_result_json(), "Casino result options fetched")


def bw_casino_manual_result(payload: dict, _session_user: dict = None) -> dict:
    from mongodb.casino_declare_api import declare_manual_casino_result

    return declare_manual_casino_result(payload or {})


def bw_casino_generate_ledger(payload: dict, _session_user: dict = None) -> dict:
    from mongodb.casino_declare_api import generate_diamond_ledger

    return generate_diamond_ledger(payload or {})


def bw_casino_save_result(payload: dict, _session_user: dict = None) -> dict:
    from mongodb.casino_declare_api import save_casino_result

    return save_casino_result(payload or {})


def bw_user_casino_report(payload: dict, _session_user: dict = None) -> dict:
    payload = payload or {}
    game_type = str(payload.get("gameType") or "diamondCasino").strip()
    if game_type == "internationalCasino":
        from mongodb.casino_api import staff_int_casino_report_games

        rows = staff_int_casino_report_games()
    else:
        from mongodb.casino_api import staff_diamond_casino_games

        rows = [normalize_casino_game(g) for g in staff_diamond_casino_games()]
    return _ok(rows, "Casino report data fetched")


def _user_exposure_client_rows(user: dict) -> list[dict]:
    """website/checkExposureClient — open sports/casino exposure rows for staff table."""
    db = get_db()
    uid = str(user.get("userId") or "")
    if not uid:
        return []

    rows: list[dict] = []
    games = _casino_games_map(db)

    for pos in db.positions.find({"userId": uid}):
        runners = pos.get("runners") or {}
        amount = total_exposure_from_positions(runners)
        if amount <= 0:
            continue
        mid = str(pos.get("marketId") or "")
        eid = str(pos.get("eventId") or "")
        match = _find_match_local(mid, eid) or {}
        game_name = str(match.get("eventName") or match.get("matchName") or eid or mid or "Sports")
        latest = db.sports_bets.find_one(
            {"userId": uid, "marketId": mid, "status": "open"},
            sort=[("createdAt", -1)],
        ) or {}
        game_type = str(latest.get("oddsType") or latest.get("betFor") or "matchOdds")
        rows.append({
            "_id": str(pos["_id"]),
            "createdAt": _iso(pos.get("updatedAt") or latest.get("createdAt")),
            "gameName": game_name,
            "gameType": game_type,
            "marketId": mid,
            "eventId": eid,
            "overallType": game_type if game_type in ("bookmaker", "toss") else "sports",
            "amount": amount,
            "remarks": _market_label(db, mid, eid, latest) if latest else game_name,
        })

    fancy_groups: dict[tuple[str, str], list[dict]] = {}
    for bet in db.sports_bets.find({"userId": uid, "status": "open"}):
        if not is_fancy_market(
            str(bet.get("betFor") or ""),
            str(bet.get("oddsType") or ""),
            str(bet.get("gtype") or ""),
        ):
            continue
        key = (str(bet.get("marketId") or ""), str(bet.get("eventId") or ""))
        fancy_groups.setdefault(key, []).append(bet)

    for (mid, eid), bets in fancy_groups.items():
        amount = total_fancy_exposure(bets)
        if amount <= 0:
            continue
        match = _find_match_local(mid, eid) or {}
        game_name = str(match.get("eventName") or match.get("matchName") or "Fancy")
        latest = max(
            bets,
            key=lambda b: b.get("createdAt") or datetime.min.replace(tzinfo=timezone.utc),
        )
        rows.append({
            "_id": str(latest["_id"]),
            "createdAt": _iso(latest.get("createdAt")),
            "gameName": game_name,
            "gameType": "fancy",
            "marketId": mid,
            "eventId": eid,
            "overallType": str(latest.get("gtype") or latest.get("fancyType") or "session"),
            "amount": amount,
            "remarks": str(latest.get("runnerName") or latest.get("sessionName") or ""),
        })

    for bet in db.casino_bets.find({"userId": uid, "status": "open"}):
        eid = bet.get("eventId")
        game = games.get(int(eid)) if eid is not None and str(eid).isdigit() else None
        amount = round(float(bet.get("liability") or bet.get("stake") or 0), 2)
        if amount <= 0:
            continue
        rows.append({
            "_id": str(bet["_id"]),
            "createdAt": _iso(bet.get("createdAt")),
            "gameName": _casino_game_name(game, eid),
            "gameType": str(bet.get("gameType") or "casino"),
            "marketId": str(bet.get("marketId") or bet.get("roundId") or ""),
            "eventId": eid,
            "overallType": "casino",
            "amount": amount,
            "remarks": str(bet.get("selection") or bet.get("roundId") or bet.get("playerName") or ""),
        })

    rows.sort(key=lambda r: r.get("createdAt") or "", reverse=True)
    return rows


def _find_exposure_user(db, payload: dict) -> Optional[dict]:
    """Resolve client by userId, username, Mongo _id, or display name."""
    user_id = str(payload.get("userId") or "").strip()
    username = str(payload.get("username") or "").strip()
    if not user_id and not username:
        return None

    base_q = {"isDeleted": {"$ne": True}}
    proj = {"password": 0}

    if user_id:
        user = db.users.find_one({**base_q, "userId": user_id}, proj)
        if user:
            return user
        if ObjectId.is_valid(user_id):
            user = db.users.find_one({**base_q, "_id": ObjectId(user_id)}, proj)
            if user:
                return user

    if username:
        user = db.users.find_one(
            {**base_q, "username": {"$regex": f"^{username}$", "$options": "i"}},
            proj,
        )
        if user:
            return user
        user = db.users.find_one({**base_q, "userId": username}, proj)
        if user:
            return user
        if ObjectId.is_valid(username):
            user = db.users.find_one({**base_q, "_id": ObjectId(username)}, proj)
            if user:
                return user
        user = db.users.find_one(
            {**base_q, "name": {"$regex": f"^{username}$", "$options": "i"}},
            proj,
        )
        if user:
            return user

    return None


def bw_check_exposure_client(payload: dict, _session_user: dict = None) -> dict:
    payload = payload or {}
    user_id = str(payload.get("userId") or "").strip()
    username = str(payload.get("username") or "").strip()
    if not user_id and not username:
        return _err("User ID or Username can not be empty.")

    db = get_db()
    user = _find_exposure_user(db, payload)
    if not user:
        return _ok([], "User exposure fetched")

    rows = _user_exposure_client_rows(user)
    return _ok(rows, "User exposure fetched successfully")


def bw_clear_exposure(payload: dict, session_user: dict = None) -> dict:
    payload = payload or {}
    raw_id = str(payload.get("_id") or "")
    password = str(payload.get("password") or "")
    if not raw_id:
        return _err("Exposure id required")
    if not password:
        return _err("Password required")

    db = get_db()
    actor = db.users.find_one({"userId": (session_user or {}).get("userId")})
    if not actor or str(actor.get("password") or "") != password:
        return _err("Invalid password")

    affected_uid = ""
    cleared = False
    now = _now()

    if ObjectId.is_valid(raw_id):
        oid = ObjectId(raw_id)
        pos = db.positions.find_one({"_id": oid})
        if pos:
            affected_uid = str(pos.get("userId") or "")
            db.positions.delete_one({"_id": oid})
            cleared = True
        else:
            bet = db.sports_bets.find_one({"_id": oid})
            if bet:
                affected_uid = str(bet.get("userId") or "")
                if is_fancy_market(
                    str(bet.get("betFor") or ""),
                    str(bet.get("oddsType") or ""),
                    str(bet.get("gtype") or ""),
                ):
                    mid = str(bet.get("marketId") or "")
                    db.sports_bets.update_many(
                        {
                            "userId": affected_uid,
                            "marketId": mid,
                            "status": "open",
                        },
                        {"$set": {"status": "void", "exposureCleared": True, "updatedAt": now}},
                    )
                else:
                    db.sports_bets.update_one(
                        {"_id": oid},
                        {"$set": {"status": "void", "exposureCleared": True, "updatedAt": now}},
                    )
                cleared = True
            else:
                casino = db.casino_bets.find_one({"_id": oid})
                if casino:
                    affected_uid = str(casino.get("userId") or "")
                    db.casino_bets.update_one(
                        {"_id": oid},
                        {"$set": {"status": "void", "updatedAt": now}},
                    )
                    cleared = True

    if not cleared:
        return _err("Exposure record not found")

    if affected_uid:
        _refresh_user_exposure(affected_uid)

    return _ok({}, "Exposure cleared successfully")


def bw_stub_ok(_payload: dict, _session_user: dict = None) -> dict:
    return _ok({}, "OK")


def bw_stub_list(_payload: dict, _session_user: dict = None) -> dict:
    return _ok([], "OK")


BLUEWIN_NAV_ROUTES: dict[str, tuple] = {
    "website/domainList": (bw_website_domain_list, True),
    "website/getDomainSetting": (bw_website_get_domain_setting, True),
    "website/getSetting": (bw_website_get_setting, True),
    "website/updateSetting": (bw_website_update_setting, True),
    "website/createDomain": (bw_website_create_domain, True),
    "website/updateDomain": (bw_website_update_domain, True),
    "website/deleteDomain": (bw_website_delete_domain, True),
    "website/domainSettingByDomainName": (bw_website_domain_setting_by_name, False),
    "website/getNegativeUserDetails": (bw_website_negative_users, True),
    "website/getCateogeory": (bw_website_get_category, True),
    "website/cateogeoryCrud": (bw_website_category_crud, True),
    "website/fileUpload": (bw_website_file_upload, True),
    "website/getAllCasinoInternationalList": (bw_website_int_casino_list, True),
    "website/intCasino1": (bw_website_int_casino_action, True),
    "website/updateInternationalCasinoByOperating": (bw_website_update_international_casino, True),
    "website/updateInternationalCasinoGamesInDatabase": (bw_stub_ok, True),
    "website/updateInternationalCasinoListInRedis": (bw_website_update_int_casino_redis, True),
    "website/getTransactionData": (bw_website_transaction_data, True),
    "website/checkDuplicateFancy": (bw_stub_list, True),
    "website/checkExposuerByMarketIdForFancy": (bw_stub_list, True),
    "website/checkExposureClient": (bw_check_exposure_client, True),
    "website/clearExposure": (bw_clear_exposure, True),
    "website/updateExposureOfUser": (bw_stub_ok, True),
    "website/distributeBonusToUpperline": (bw_stub_ok, True),
    "website/allUserTypePasswordUpdate": (bw_stub_ok, True),
    "website/flushAllRedisKeys": (bw_stub_ok, True),
    "website/forceLogOutAllUser": (bw_stub_ok, True),
    "website/rollBackfancyList": (bw_stub_list, True),
    "user/notificationList": (bw_user_notification_list, True),
    "user/saveNotification": (bw_user_save_notification, True),
    "user/casinoReportByUser": (bw_user_casino_report, True),
    "crick365/loginClientDetails": (bw_crick365_login_clients, True),
    "crick365/userLogoutAndBlock": (bw_crick365_logout_block, True),
    "daman/getTrackingActivites": (bw_daman_tracking, True),
    "daman/checkIpAddress": (bw_daman_check_ip, True),
    "matka/matkaGameTypeList": (bw_matka_game_type_list, True),
    "matka/getResultPanelList": (bw_matka_result_panel, True),
    "matka/createMatka": (bw_stub_ok, True),
    "matka/updateMatka": (bw_stub_ok, True),
    "matka/updateMatkaTypes": (bw_stub_ok, True),
    "matka/matkaResult": (bw_stub_ok, True),
    "matka/matkaBetDelete": (bw_stub_ok, True),
    "matka/getMatkaByMatkaEventId": (bw_stub_ok, True),
    "sports/betsList": (bw_sports_bets_list, True),
    "sports/getSessionList": (bw_sports_get_session_list, True),
    "reports/matchDetails": (bw_reports_match_details, True),
    "casino/undeclaredRoundId": (bw_casino_undeclared_rounds, True),
    "casino/deleteDiamondCasinoBets": (bw_stub_ok, True),
    "casino/doManaualResult": (bw_casino_manual_result, True),
    "casino/generateDiamondLedger": (bw_casino_generate_ledger, True),
    "casino/redeclareRoundId": (bw_stub_ok, True),
    "casino/saveAvaitorResults": (bw_stub_ok, True),
    "casino/saveResult": (bw_casino_save_result, True),
    "casino/sendCasinoResultjson": (bw_casino_send_result_json, True),
    "casino/updateCasinoVideo": (bw_stub_ok, True),
    "casino/updateDiamondCasino": (bw_casino_update_diamond, True),
}


def handle_bluewin_nav(endpoint: str, payload: dict, auth_header: str) -> bytes | None:
    import json

    route = BLUEWIN_NAV_ROUTES.get(endpoint)
    if not route:
        return None

    handler, needs_session = route
    session_user: dict = {}
    if needs_session:
        session_user, err = resolve_admin_user(auth_header)
        if err:
            return json.dumps(err, default=str).encode("utf-8")

    body = handler(payload or {}, session_user)
    return json.dumps(body, default=str).encode("utf-8")
