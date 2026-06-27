"""Center panel (centerpanel.1ex99.in) — saari APIs MongoDB se."""

from __future__ import annotations

import copy
import json
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any, Optional, Tuple

from mongodb.admin_compute import compute_fancy_session_positions
from mongodb.bet_logic import normalize_match_for_api, normalize_team_entry, parse_team_selections
from mongodb.centerpanel_cache import (
    _fancy_doc_from_save_payload,
    _sessions_for_ui,
    _to_bookmaker_ui_row,
    _to_session_ui_row,
    enrich_cp_sport_data,
    get_cp_bookmakers,
    get_cp_decision_logs,
    get_cp_sessions,
    get_cp_sessions_for_database,
)
from mongodb.bets import _calc_user_exposure, settle_sports_bet
from mongodb.bet_logic import is_fancy_market
from mongodb.matches_api import ADMIN_LIVE_MATCH_LIST, get_match_list
from mongodb.auth import (
    TOKEN_TTL_HOURS,
    _extract_bearer,
    _make_token,
    _now,
    mongo_logout,
    validate_session,
)
from mongodb.db import get_db, ping

ROOT = Path(__file__).resolve().parent.parent
CP_API_DATA = ROOT / "centerpanel" / "api_data"

CENTER_PANEL_TYPES = {"owner", "superadmin", "admin", "subadmin", "master"}

SPORT_NAMES = {
    "1": "Soccer",
    "2": "Tennis",
    "4": "Cricket",
    "7": "Horse Racing",
    "4339": "Greyhound Racing",
}

MATCH_ENDPOINTS = frozenset({
    "centerPanel/getSportsMatchList",
    "centerPanel/diamondMatchList",
    "centerPanel/matchListByDatabase",
    "centerPanel/getMatchListBySeriesIdLatiyal",
    "centerPanel/getSportDataByEventId",
    "centerPanel/getAllEvents",
    "centerPanel/getSeriesList",
    "centerPanel/getSeriesBySportId",
    "centerPanel/getLatiyalSeriesList",
    "centerPanel/getSessionListByApiAndDatabase",
    "centerPanel/getSessionByDatabase",
    "centerPanel/getManualFancyList",
    "centerPanel/getManualBookmakerList",
    "centerPanel/getDecisionLogs",
    "centerPanel/updateFancyDecision",
    "centerPanel/marketDecision",
    "centerPanel/rollbackFancy",
    "sports/matchList",
})

DEFAULT_PERMISSIONS = {
    "can_view_dashboard": True,
    "can_manage_cricket": True,
    "can_declare_fancy": True,
    "can_manage_bookmaker": True,
    "can_manage_users": True,
    "can_manage_racing": True,
    "can_manage_projects": True,
}

# endpoint → (collection, response_shape: list|paginated|object)
READ_ROUTES: dict[str, tuple[str, str]] = {
    "centerPanel/getProjectList": ("center_projects", "paginated"),
    "centerPanel/getFancyCategoryList": ("center_fancy_categories", "list"),
    "centerPanel/getDomainIpList": ("center_domain_ips", "list"),
    "centerPanel/getAllCustomSeries": ("center_custom_series", "list"),
    "centerPanel/getSquadTemplates": ("center_squad_templates", "list"),
    "centerPanel/getRacingEvents": ("center_racing_events", "list"),
    "centerPanel/getRacingEventsByCompetitionId": ("center_racing_events", "list"),
    "centerPanel/getRacingEventsByEventId": ("center_racing_events", "list"),
    "centerPanel/getBetfairResults": ("center_betfair_results", "list"),
    "centerPanel/getMasterSettingData": ("center_master_settings", "list"),
    "centerPanel/getManualScoreHistory": ("center_manual_scores", "list"),
    "centerPanel/getFancyAuditLog": ("center_fancy_audit", "list"),
    "centerPanel/getRacingSeriesList": ("center_racing_events", "list"),
    "centerPanel/getRacingSportsData": ("center_racing_events", "list"),
}

SCRAPED_FILES: dict[str, str] = {
    "centerPanel/getProjectList": "project_list.json",
    "decision/completeSportList": "sport_list.json",
}

WRITE_ROUTES: dict[str, str] = {
    "centerPanel/createUpdateProject": "center_projects",
    "centerPanel/createFancyCategory": "center_fancy_categories",
    "centerPanel/updateFancyCategory": "center_fancy_categories",
    "centerPanel/saveSquadTemplate": "center_squad_templates",
    "centerPanel/saveRacingEventsList": "center_racing_events",
    "centerPanel/saveRacingEventByMarketIdAndEventId": "center_racing_events",
    "centerPanel/createCustomSeries": "center_custom_series",
    "centerPanel/updateCustomSeries": "center_custom_series",
    "centerPanel/assignDomainIpToUser": "center_domain_ips",
    "centerPanel/masterSettingUpdate": "center_master_settings",
    "centerPanel/saveManualMatch": "matches",
    "centerPanel/saveSportsByEventId": "matches",
    "centerPanel/saveFancyAuditLog": "center_fancy_audit",
    "centerPanel/updateFancyDecision": "center_manual_fancy",
    "centerPanel/rollbackFancy": "center_manual_fancy",
    "centerPanel/marketDecision": "decision_logs",
    "centerPanel/userCreate": "users",
    "centerPanel/createCustomer": "users",
    "centerPanel/updateUser": "users",
    "centerPanel/updateCustomer": "users",
}

DELETE_ROUTES: dict[str, str] = {
    "centerPanel/deleteProject": "center_projects",
    "centerPanel/deleteFancyCategory": "center_fancy_categories",
    "centerPanel/deleteDomainIp": "center_domain_ips",
    "centerPanel/deleteSquadTemplate": "center_squad_templates",
    "centerPanel/deleteRacingEvents": "center_racing_events",
    "centerPanel/deleteMarket": "matches",
}


def _strip_mongo(doc: dict) -> dict:
    row = copy.deepcopy(doc)
    row.pop("_id", None)
    row.pop("password", None)
    return row


def _match_row(doc: dict) -> dict:
    return normalize_match_for_api(_strip_mongo(doc))


def _cp_match_response(rows: list[dict]) -> dict:
    return {"message": 0, "code": 0, "error": False, "data": _coerce_list(rows)}


def _cp_auth_token(session_user: Optional[dict]) -> str:
    if not session_user:
        return ""
    return str(session_user.get("_token") or session_user.get("token") or "")


def _cp_filter_matches(rows: list[dict], payload: dict) -> list[dict]:
    payload = payload or {}
    filtered = rows
    if payload.get("seriesId"):
        sid = str(payload["seriesId"])
        filtered = [m for m in filtered if str(m.get("seriesId")) == sid]
    if payload.get("eventId"):
        eid = str(payload["eventId"])
        filtered = [m for m in filtered if str(m.get("eventId")) == eid]
    if payload.get("projectId"):
        filtered = [m for m in filtered if m.get("projectId") == payload["projectId"]]
    if payload.get("status"):
        st = str(payload["status"]).upper()
        if st == "INPLAY":
            filtered = [
                m for m in filtered
                if str(m.get("status") or "").upper() == "INPLAY"
                or m.get("inPlayStatus")
                or m.get("inplayStatus")
            ]
        else:
            filtered = [m for m in filtered if str(m.get("status", "")).upper() == st]
    if payload.get("marketId"):
        mid = str(payload["marketId"])
        filtered = [m for m in filtered if str(m.get("marketId")) == mid]
    return filtered


def _cp_fetch_matches(payload: dict, session_user: Optional[dict] = None) -> list[dict]:
    """Admin jaisa: live API se sync, display MongoDB se."""
    payload = payload or {}
    rows = get_match_list(
        payload,
        for_admin=True,
        auth_token=_cp_auth_token(session_user),
        prefer_live=ADMIN_LIVE_MATCH_LIST,
    )
    rows = _cp_filter_matches(rows, payload)
    if not rows and payload.get("marketId"):
        from mongodb.matches_api import _find_match_local

        local = _find_match_local(str(payload["marketId"]), str(payload.get("eventId") or ""))
        if local:
            rows = [normalize_match_for_api(local)]
    return rows


def _cp_sport_name(sport_id: Any) -> str:
    return SPORT_NAMES.get(str(sport_id), f"Sport {sport_id}")


def _cp_market_count(match: dict) -> int:
    ml = match.get("marketList") or []
    return len(ml) + (1 if match.get("marketId") else 0)


def _to_cp_sports_match_row(match: dict) -> dict:
    return {
        "eventId": str(match.get("eventId") or ""),
        "eventName": match.get("eventName") or match.get("matchName") or "",
        "sportId": match.get("sportId"),
        "seriesName": match.get("seriesName") or "",
        "seriesId": match.get("seriesId"),
        "marketCount": _cp_market_count(match),
        "openDate": match.get("openDate") or match.get("matchDate") or "",
        "inPlay": bool(
            match.get("inPlayStatus")
            or match.get("inplayStatus")
            or str(match.get("status", "")).upper() == "INPLAY"
        ),
    }


def _to_cp_database_match_row(match: dict) -> dict:
    row = _match_row(match)
    row.setdefault("eventName", row.get("matchName") or "")
    row.setdefault("hitApiType", "sportex")
    return row


def _build_cp_all_events(matches: list[dict]) -> list[dict]:
    counts: dict[str, int] = {}
    for match in matches:
        sid = str(match.get("sportId") or "")
        if not sid:
            continue
        counts[sid] = counts.get(sid, 0) + _cp_market_count(match)
    return [
        {
            "eventType": {"id": sid, "name": _cp_sport_name(sid)},
            "marketCount": count,
        }
        for sid, count in sorted(counts.items(), key=lambda item: int(item[0]) if item[0].isdigit() else item[0])
    ]


def _build_cp_series_list(matches: list[dict], payload: dict) -> list[dict]:
    payload = payload or {}
    sport_id = payload.get("sportId") or payload.get("sportsId")
    series: dict[str, dict] = {}
    for match in matches:
        if sport_id not in (None, "", "all") and str(match.get("sportId")) != str(sport_id):
            continue
        sid = match.get("seriesId")
        if sid in (None, ""):
            continue
        key = str(sid)
        if key not in series:
            series[key] = {
                "seriesId": key,
                "seriesName": match.get("seriesName") or "",
                "sportsId": match.get("sportId"),
                "sportId": match.get("sportId"),
                "sportName": match.get("sportType") or _cp_sport_name(match.get("sportId")),
                "marketCount": 0,
            }
        series[key]["marketCount"] += 1
    return list(series.values())


def _ok(data: Any, msg: str = "OK") -> dict:
    return {"message": msg, "code": 0, "error": False, "data": data}


def _err(msg: str, code: int = 1, data: Any = None) -> dict:
    return {"message": msg, "code": code, "error": True, "data": data if data is not None else {}}


def _auth_err(msg: str = "Session expired") -> dict:
    return _err(msg, 401, [])


def _coerce_list(data: Any) -> list:
    if data is None:
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        inner = data.get("list")
        if isinstance(inner, list):
            return inner
        if not data:
            return []
    return []


def _load_scraped(endpoint: str) -> Any | None:
    rel = SCRAPED_FILES.get(endpoint)
    if not rel:
        return None
    path = CP_API_DATA / rel
    if not path.exists():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    return raw.get("data")


def _resolve_cp_user(auth_header: str) -> Tuple[Optional[dict], Optional[dict]]:
    token = _extract_bearer(auth_header)
    if not token:
        return None, _auth_err()
    user = validate_session(token)
    if not user:
        return None, _auth_err()
    if user.get("userType") not in CENTER_PANEL_TYPES:
        return None, _err("Not authorised for center panel", 403, [])
    return user, None


def _user_to_cp_login(user: dict) -> dict:
    username = user.get("username", "")
    role = user.get("userType", "admin")
    if role == "owner":
        role = "admin"
    return {
        "userId": user.get("userId"),
        "username": username,
        "name": user.get("name", username),
        "role": role,
        "email": f"{username.lower()}@1ex99.local",
        "status": "active",
        "permissions": user.get("permissions") or DEFAULT_PERMISSIONS,
    }


def mongo_cp_login(payload: dict) -> dict:
    if not ping():
        return _err("MongoDB not running. python3 main.py --setup-mongo", 500)

    username = str(payload.get("username", "")).strip().upper()
    password = str(payload.get("password", ""))
    if not username or not password:
        return _err("Username and password required")

    db = get_db()
    user = db.users.find_one({
        "username": {"$regex": f"^{username}$", "$options": "i"},
        "password": password,
        "isDeleted": {"$ne": True},
    })
    if not user:
        return _err("Invalid username or password")
    if user.get("userType") not in CENTER_PANEL_TYPES:
        return _err("Not authorised for center panel")

    token = _make_token()
    now = _now()
    db.auth_sessions.delete_many({"userId": user["userId"], "panel": "centerpanel"})
    db.auth_sessions.insert_one({
        "token": token,
        "userId": user["userId"],
        "username": user["username"],
        "panel": "centerpanel",
        "createdAt": now,
        "expiresAt": now + timedelta(hours=TOKEN_TTL_HOURS),
    })
    db.user_activities.insert_one({
        "userId": user["userId"],
        "activityType": "login",
        "ip": str(payload.get("ip") or "127.0.0.1"),
        "device": "Center Panel",
        "payload": {"panel": "centerpanel"},
        "createdAt": now,
    })
    return {
        "message": "Login successful",
        "code": 0,
        "error": False,
        "token": token,
        "user": _user_to_cp_login(user),
        "data": {**_user_to_cp_login(user), "token": token},
    }


def _match_query(payload: dict) -> dict:
    q: dict = {}
    payload = payload or {}
    if payload.get("sportId") not in (None, "", "all"):
        sid = payload["sportId"]
        q["$or"] = [{"sportId": sid}, {"sportsId": sid}, {"sportId": int(sid) if str(sid).isdigit() else sid}]
    if payload.get("seriesId"):
        q["seriesId"] = str(payload["seriesId"])
    if payload.get("eventId"):
        q["eventId"] = str(payload["eventId"])
    if payload.get("projectId"):
        q["projectId"] = payload["projectId"]
    if payload.get("status"):
        q["status"] = str(payload["status"]).upper()
    if payload.get("marketId"):
        q["marketId"] = str(payload["marketId"])
    return q


def _read_collection(collection: str, shape: str, payload: dict) -> dict:
    db = get_db()
    q = _match_query(payload)

    strip = _match_row if collection == "matches" else _strip_mongo
    rows = [strip(d) for d in db[collection].find(q)]
    if shape == "paginated":
        page = int(payload.get("pageNo") or 1)
        size = int(payload.get("size") or payload.get("limit") or 50)
        start = (page - 1) * size if size > 0 else 0
        page_rows = rows[start: start + size] if size > 0 else rows
        return _ok({"list": page_rows, "total": len(rows)})
    if shape == "object":
        doc = rows[0] if rows else {}
        return _ok(doc)
    return _ok(_coerce_list(rows))


def _upsert_collection(collection: str, payload: dict) -> dict:
    db = get_db()
    doc = copy.deepcopy(payload or {})
    doc.pop("_id", None)
    doc["updatedAt"] = _now()
    key_fields = (
        "projectId", "categoryId", "fancyId", "bookmakerId", "templateId",
        "eventId", "marketId", "userId", "seriesId", "settingKey", "logId",
    )
    filt = None
    for k in key_fields:
        if doc.get(k) not in (None, ""):
            filt = {k: doc[k]}
            break
    if not filt:
        doc.setdefault("id", uuid.uuid4().hex[:24])
        filt = {"id": doc["id"]}
    if not doc.get("createdAt"):
        doc["createdAt"] = _now()
    db[collection].update_one(filt, {"$set": doc}, upsert=True)
    return _ok(doc, "Saved successfully")


def _delete_collection(collection: str, payload: dict) -> dict:
    db = get_db()
    filt = {}
    for k in ("projectId", "categoryId", "templateId", "eventId", "marketId", "domainUrl", "userId", "logId"):
        if payload.get(k) not in (None, ""):
            filt[k] = payload[k]
            break
    if not filt:
        return _err("Id required")
    db[collection].delete_many(filt)
    return _ok({}, "Deleted successfully")


def mongo_cp_user_list(payload: dict, _session_user: dict = None) -> dict:
    db = get_db()
    q: dict = {"userType": {"$in": list(CENTER_PANEL_TYPES)}, "isDeleted": {"$ne": True}}
    search = (payload or {}).get("searchTerm") or (payload or {}).get("keyWord")
    if search:
        q["$or"] = [
            {"username": {"$regex": search, "$options": "i"}},
            {"name": {"$regex": search, "$options": "i"}},
        ]
    rows = [_strip_mongo(u) for u in db.users.find(q)]
    page = int((payload or {}).get("pageNo") or 1)
    size = int((payload or {}).get("size") or 10)
    start = (page - 1) * size
    page_rows = rows[start: start + size]
    return _ok({"list": page_rows, "total": len(rows)}, "User list fetched")


def mongo_cp_sport_list(_payload: dict, _session_user: dict = None) -> dict:
    rows = [_strip_mongo(s) for s in get_db().sports_catalog.find({})]
    return _ok(rows)


def mongo_cp_match_list(payload: dict, session_user: dict = None) -> dict:
    rows = [_match_row(m) for m in _cp_fetch_matches(payload, session_user)]
    return _cp_match_response(rows)


def mongo_cp_match_list_by_database(payload: dict, session_user: dict = None) -> dict:
    rows = [_to_cp_database_match_row(m) for m in _cp_fetch_matches(payload, session_user)]
    return _cp_match_response(rows)


def mongo_cp_get_sports_match_list(payload: dict, session_user: dict = None) -> dict:
    rows = [_to_cp_sports_match_row(m) for m in _cp_fetch_matches(payload, session_user)]
    return _cp_match_response(rows)


def mongo_cp_diamond_match_list(payload: dict, session_user: dict = None) -> dict:
    return mongo_cp_match_list_by_database(payload, session_user)


def mongo_cp_all_events(payload: dict, session_user: dict = None) -> dict:
    rows = _build_cp_all_events(_cp_fetch_matches({}, session_user))
    return _ok(rows)


def mongo_cp_series_list(payload: dict, session_user: dict = None) -> dict:
    rows = _build_cp_series_list(_cp_fetch_matches({}, session_user), payload)
    return _ok(rows)


def mongo_cp_sport_data_by_event(payload: dict, session_user: dict = None) -> dict:
    rows = _cp_fetch_matches(payload, session_user)
    if not rows and (payload or {}).get("eventId"):
        doc = get_db().matches.find_one({"eventId": str(payload["eventId"])})
        if doc:
            rows = [_match_row(doc)]
    if not rows and (payload or {}).get("marketId"):
        doc = get_db().matches.find_one({"marketId": str(payload["marketId"])})
        if doc:
            rows = [_match_row(doc)]
    match = rows[0] if rows else {}
    if match:
        match = enrich_cp_sport_data(
            match,
            str((payload or {}).get("marketId") or match.get("marketId") or ""),
            str((payload or {}).get("eventId") or match.get("eventId") or ""),
        )
    return _ok(match)


def mongo_cp_session_list(payload: dict, _session_user: dict = None) -> dict:
    rows = _sessions_for_ui(payload or {}, enrich=True)
    return _ok(_coerce_list(rows))


def mongo_cp_manual_fancy_list(payload: dict, _session_user: dict = None) -> dict:
    rows = _sessions_for_ui(payload or {}, enrich=False)
    return _ok(_coerce_list(rows))


def mongo_cp_manual_bookmaker_list(payload: dict, _session_user: dict = None) -> dict:
    rows = get_cp_bookmakers(payload or {})
    return _ok(_coerce_list(rows))


def mongo_cp_decision_logs(payload: dict, _session_user: dict = None) -> dict:
    rows = get_cp_decision_logs(payload or {})
    return _ok(_coerce_list(rows))


def mongo_cp_session_by_database(payload: dict, _session_user: dict = None) -> dict:
    rows = get_cp_sessions_for_database(payload or {})
    return _ok(_coerce_list([_to_session_ui_row(r) for r in rows]))


def mongo_cp_save_manual_fancy(payload: dict, _session_user: dict = None) -> dict:
    payload = payload or {}
    db = get_db()
    sid = str(payload.get("selectionId") or payload.get("fancyId") or "")
    create = not sid
    doc = _fancy_doc_from_save_payload(payload, create=create)
    sid = doc["fancyId"]
    doc["updatedAt"] = _now()
    if create:
        doc["createdAt"] = _now()
    db.center_manual_fancy.update_one({"fancyId": sid}, {"$set": doc}, upsert=True)
    return _ok(_to_session_ui_row(doc), "Saved successfully")


def mongo_cp_save_fancy_by_center_panel(payload: dict, _session_user: dict = None) -> dict:
    payload = payload or {}
    db = get_db()
    sid = str(payload.get("selectionId") or payload.get("fancyId") or "")
    if not sid:
        return _err("selectionId is required")
    upd = {"updatedAt": _now()}
    for key in ("isOddsPanelOpen", "oddsMode", "playStatus", "remark"):
        if key in payload and payload[key] is not None:
            upd[key] = payload[key]
    if "playStatus" in upd:
        upd["playStatus"] = _fancy_doc_from_save_payload({"playStatus": upd["playStatus"]})["playStatus"]
    db.center_manual_fancy.update_one(
        {"$or": [{"fancyId": sid}, {"selectionId": sid}, {"Selection_id": sid}]},
        {"$set": upd},
    )
    doc = db.center_manual_fancy.find_one(
        {"$or": [{"fancyId": sid}, {"selectionId": sid}, {"Selection_id": sid}]},
        {"_id": 0},
    )
    return _ok(_to_session_ui_row(doc or {"fancyId": sid, **upd}), "Saved successfully")


def mongo_cp_save_manual_bookmaker(payload: dict, _session_user: dict = None) -> dict:
    payload = payload or {}
    db = get_db()
    event_id = str(payload.get("eventId") or "")
    market_id = str(payload.get("marketId") or "")
    sel = payload.get("selectionId")
    if sel is None:
        return _err("selectionId is required")
    doc = {
        "bookmakerId": f"bm-{market_id}-{sel}",
        "eventId": event_id,
        "marketId": market_id,
        "selectionId": sel,
        "bookmakerSelectionId": sel,
        "backSize": payload.get("backSize", payload.get("back", 0)),
        "laySize": payload.get("laySize", payload.get("lay", 0)),
        "range": payload.get("range", 1),
        "teamName": payload.get("teamName") or payload.get("runnerName") or "",
        "runnerName": payload.get("teamName") or payload.get("runnerName") or "",
        "updatedAt": _now(),
        "source": "centerpanel",
    }
    if not doc.get("createdAt"):
        doc["createdAt"] = _now()
    db.center_manual_bookmaker.update_one({"bookmakerId": doc["bookmakerId"]}, {"$set": doc}, upsert=True)
    return _ok(_to_bookmaker_ui_row(doc), "Saved successfully")


def mongo_cp_update_sport_by_event(payload: dict, _session_user: dict = None) -> dict:
    payload = payload or {}
    event_id = str(payload.get("eventId") or "")
    if not event_id:
        return _err("eventId is required")
    allowed = (
        "fancyStatus", "oddsStatus", "bookmakerRange", "matchName", "matchType",
        "matchDate", "betPerm", "isFancy", "isBookmaker", "isMatchOdds",
    )
    upd = {k: payload[k] for k in allowed if k in payload}
    upd["updatedAt"] = _now()
    db = get_db()
    db.matches.update_one({"eventId": event_id}, {"$set": upd})
    doc = db.matches.find_one({"eventId": event_id}, {"_id": 0})
    if not doc:
        return _err("Match not found")
    match = enrich_cp_sport_data(
        _match_row(doc),
        str(payload.get("marketId") or doc.get("marketId") or ""),
        event_id,
    )
    return _ok(match, "Updated successfully")


def mongo_cp_update_fancy_decision(payload: dict, session_user: dict = None) -> dict:
    payload = payload or {}
    db = get_db()
    sid = str(payload.get("selectionId") or payload.get("Selection_id") or payload.get("fancyId") or "")
    event_id = str(payload.get("eventId") or "")
    market_id = str(payload.get("marketId") or "")
    fancy_name = str(payload.get("fancyName") or payload.get("session_name") or payload.get("sessionName") or "")

    try:
        decision_run = int(payload.get("decisionRun") if payload.get("decisionRun") is not None else payload.get("run"))
    except (TypeError, ValueError):
        return _err("Decision Run is required")

    if not sid:
        return _err("selectionId is required")

    if not market_id:
        bet = db.sports_bets.find_one({"selectionId": sid}, {"marketId": 1, "_id": 0})
        market_id = str((bet or {}).get("marketId") or "")
    if not market_id and event_id:
        match = db.matches.find_one({"eventId": event_id, "marketId": {"$exists": True}}, {"marketId": 1, "_id": 0})
        market_id = str((match or {}).get("marketId") or "")

    comm_perm = str(payload.get("commPerm") or payload.get("com_perm") or "YES")
    user_block = {
        "username": (session_user or {}).get("username") or "admin",
        "name": (session_user or {}).get("name") or (session_user or {}).get("username") or "Admin",
    }
    upd = {
        "isDeclare": True,
        "decisionRun": decision_run,
        "commPerm": comm_perm,
        "com_perm": comm_perm,
        "fancyName": fancy_name,
        "session_name": fancy_name,
        "sessionName": fancy_name,
        "declareUserDetails": user_block,
        "updatedAt": _now(),
    }

    db.center_manual_fancy.update_one(
        {"fancyId": sid},
        {
            "$set": {
                **upd,
                "fancyId": sid,
                "Selection_id": sid,
                "session_id": sid,
                "selectionId": sid,
                "eventId": event_id,
                "marketId": market_id,
            }
        },
        upsert=True,
    )

    bet_q: dict = {"selectionId": sid, "status": "open"}
    if market_id:
        bet_q["marketId"] = market_id
    if event_id:
        bet_q["eventId"] = event_id

    settled = 0
    wallet_updates: list[dict] = []
    seen_users: set[str] = set()
    for bet in db.sports_bets.find(bet_q, {"betId": 1, "userId": 1, "_id": 0}):
        result = settle_sports_bet(bet["betId"], {"decisionRun": decision_run})
        if not result.get("error"):
            settled += 1
            uid = str(bet.get("userId") or "")
            if uid and uid not in seen_users:
                seen_users.add(uid)
                user = db.users.find_one({"userId": uid}, {"_id": 0, "coins": 1, "exposure": 1, "creditLimit": 1, "username": 1})
                if user:
                    wallet_updates.append({
                        "userId": uid,
                        "username": user.get("username"),
                        "coins": round(float(user.get("coins") or 0), 2),
                        "exposure": round(float(user.get("exposure") or 0), 2),
                        "creditLimit": round(float(user.get("creditLimit") or 0), 2),
                        "profitLoss": result.get("data", {}).get("profitLoss"),
                    })

    db.decision_logs.insert_one({
        "logId": uuid.uuid4().hex[:24],
        "marketId": market_id,
        "eventId": event_id,
        "selectionId": sid,
        "fancyName": fancy_name,
        "action": "fancy_declare",
        "type": "fancy_declare",
        "status": "completed",
        "userName": user_block["username"],
        "decisionRun": decision_run,
        "payload": payload,
        "createdAt": _now(),
    })

    return _ok(
        {
            "selectionId": sid,
            "decisionRun": decision_run,
            "isDeclare": True,
            "settledBets": settled,
            "walletUpdates": wallet_updates,
        },
        "Fancy decision updated successfully",
    )


def mongo_cp_market_decision(payload: dict, session_user: dict = None) -> dict:
    payload = payload or {}
    won = (
        payload.get("decisionSelectionId")
        if payload.get("decisionSelectionId") is not None
        else payload.get("wonSelectionId")
    )
    if payload.get("marketId") and won not in (None, ""):
        from mongodb.bluewin_decision import mongo_bw_odds_decision

        return mongo_bw_odds_decision(payload, session_user)

    db = get_db()
    doc = {
        "logId": uuid.uuid4().hex[:24],
        "marketId": str(payload.get("marketId") or ""),
        "eventId": str(payload.get("eventId") or ""),
        "action": payload.get("action") or "market_decision",
        "type": payload.get("type") or "bookmaker_declare",
        "status": "completed",
        "userName": (session_user or {}).get("username") or "admin",
        "payload": payload,
        "createdAt": _now(),
    }
    db.decision_logs.insert_one(doc)
    mid = doc["marketId"]
    if mid and payload.get("wonTeamName"):
        db.matches.update_one({"marketId": mid}, {"$set": {"wonTeamName": payload["wonTeamName"], "status": "COMPLETED"}})
    return _ok(doc, "Market decision saved successfully")


def _fancy_selection_filter(selection_id: str, market_id: str = "") -> dict:
    filt: dict = {
        "$or": [
            {"fancyId": selection_id},
            {"Selection_id": selection_id},
            {"session_id": selection_id},
            {"selectionId": selection_id},
        ]
    }
    if market_id:
        return {"$and": [filt, {"marketId": market_id}]}
    return filt


def _sync_match_fancy_rollback(db, market_id: str, selection_id: str) -> None:
    match = db.matches.find_one({"marketId": market_id}, {"sessionList": 1, "fancyList": 1})
    if not match:
        return
    patch: dict = {}
    for key in ("sessionList", "fancyList"):
        sessions = match.get(key) or []
        changed = False
        new_rows = []
        for row in sessions:
            doc = copy.deepcopy(row) if isinstance(row, dict) else row
            if isinstance(doc, dict):
                sid = str(doc.get("selectionId") or doc.get("fancyId") or doc.get("Selection_id") or "")
                if sid == selection_id:
                    doc["isDeclare"] = False
                    doc["decisionRun"] = None
                    changed = True
            new_rows.append(doc)
        if changed:
            patch[key] = new_rows
    if patch:
        db.matches.update_one({"marketId": market_id}, {"$set": patch})


def mongo_cp_rollback_fancy(payload: dict, _session_user: dict = None) -> dict:
    payload = payload or {}
    db = get_db()
    market_id = str(payload.get("marketId") or "")
    sid = str(payload.get("selectionId") or payload.get("Selection_id") or "")
    if not market_id or not sid:
        return _err("marketId and selectionId required")

    bet_q: dict[str, Any] = {"marketId": market_id, "selectionId": sid}
    if payload.get("gtype"):
        bet_q["gtype"] = payload["gtype"]
    if payload.get("fancyType"):
        bet_q["fancyType"] = payload["fancyType"]

    now = _now()
    affected_users: set[str] = set()
    reverted = 0

    for bet in db.sports_bets.find(bet_q):
        if not is_fancy_market(
            str(bet.get("betFor") or ""),
            str(bet.get("oddsType") or ""),
            str(bet.get("gtype") or ""),
        ):
            continue
        status = str(bet.get("status") or "open").lower()
        settled = status == "settled" or bool(bet.get("isDeclare"))
        uid = str(bet.get("userId") or "")

        if settled and uid:
            pl = round(float(bet.get("profitLoss") or 0), 2)
            user = db.users.find_one({"userId": uid}) or {}
            coins = round(float(user.get("coins") or 0) - pl, 2)
            credit = round(float(user.get("creditLimit") or 0) - pl, 2)
            db.users.update_one(
                {"userId": uid},
                {"$set": {"coins": coins, "creditLimit": credit, "updatedAt": now}},
            )
            affected_users.add(uid)

        if settled or status == "deleted" or bet.get("isDeclare"):
            db.sports_bets.update_one(
                {"_id": bet["_id"]},
                {
                    "$set": {
                        "status": "open",
                        "isDeclare": False,
                        "isDeleted": False,
                        "profitLoss": 0,
                        "decisionRun": None,
                        "wonSelectionId": None,
                        "decisionSelectionId": None,
                        "settledAt": None,
                        "deletedAt": None,
                        "deletedBy": "",
                        "deletedRemark": "",
                        "updatedAt": now,
                    }
                },
            )
            reverted += 1

    for uid in affected_users:
        new_exp = round(_calc_user_exposure(uid), 2)
        db.users.update_one({"userId": uid}, {"$set": {"exposure": new_exp, "updatedAt": now}})

    db.center_manual_fancy.update_many(
        _fancy_selection_filter(sid, market_id),
        {
            "$set": {
                "isDeclare": False,
                "decisionRun": None,
                "isRollback": True,
                "isCancel": False,
                "isDeleted": False,
                "updatedAt": now,
            }
        },
    )
    _sync_match_fancy_rollback(db, market_id, sid)

    db.decision_logs.insert_one({
        "logId": uuid.uuid4().hex[:24],
        "marketId": market_id,
        "eventId": str(payload.get("eventId") or ""),
        "selectionId": sid,
        "action": "rollback",
        "type": "fancy_declare",
        "status": "completed",
        "payload": payload,
        "revertedBets": reverted,
        "createdAt": now,
    })
    return _ok({"revertedBets": reverted}, "Fancy rollback successful")


def mongo_cp_sports_bets(payload: dict, _session_user: dict = None) -> dict:
    q: dict = {}
    if payload.get("userId"):
        q["userId"] = payload["userId"]
    if payload.get("marketId"):
        q["marketId"] = str(payload["marketId"])
    if payload.get("eventId"):
        q["eventId"] = str(payload["eventId"])
    if payload.get("selectionId") is not None:
        q["selectionId"] = str(payload["selectionId"])
    rows = [_strip_mongo(b) for b in get_db().sports_bets.find(q)]
    return _ok(rows, "Fetch List Successfuly")


def mongo_cp_session_position(payload: dict, _session_user: dict = None) -> dict:
    rows = compute_fancy_session_positions(payload or {})
    return _ok(rows)


def mongo_cp_user_details(payload: dict, _session_user: dict = None) -> dict:
    uid = (payload or {}).get("userId")
    user = get_db().users.find_one({"userId": uid}) if uid else None
    if not user:
        return _err("User not found")
    row = _strip_mongo(user)
    row["passwordShow"] = user.get("password", "")
    return _ok(row)


def mongo_cp_fancy_groups(payload: dict, _session_user: dict = None) -> dict:
    db = get_db()
    q: dict = {}
    if payload.get("eventId"):
        q["eventId"] = str(payload["eventId"])
    if payload.get("marketId"):
        q["marketId"] = str(payload["marketId"])
    rows = [_strip_mongo(d) for d in db.center_fancy_groups.find(q)]
    return _ok(_coerce_list(rows))


def mongo_cp_create_fancy_group(payload: dict, _session_user: dict = None) -> dict:
    payload = payload or {}
    db = get_db()
    doc = copy.deepcopy(payload)
    doc.pop("_id", None)
    doc.setdefault("groupId", uuid.uuid4().hex[:24])
    doc["eventId"] = str(payload.get("eventId") or doc.get("eventId") or "")
    doc["marketId"] = str(payload.get("marketId") or doc.get("marketId") or "")
    doc["createdAt"] = _now()
    db.center_fancy_groups.update_one({"groupId": doc["groupId"]}, {"$set": doc}, upsert=True)
    return _ok(doc, "Group created")


def mongo_cp_update_fancy_group(payload: dict, _session_user: dict = None) -> dict:
    payload = payload or {}
    gid = str(payload.get("groupId") or "")
    if not gid:
        return _err("groupId is required")
    db = get_db()
    upd = copy.deepcopy(payload)
    upd.pop("_id", None)
    upd["updatedAt"] = _now()
    db.center_fancy_groups.update_one({"groupId": gid}, {"$set": upd}, upsert=True)
    return _ok(upd, "Group updated")


def mongo_cp_delete_fancy_group(payload: dict, _session_user: dict = None) -> dict:
    gid = str((payload or {}).get("groupId") or "")
    if gid:
        get_db().center_fancy_groups.delete_one({"groupId": gid})
    return _ok({}, "Group deleted")


def _default_team_score(team_name: str = "") -> dict:
    return {
        "teamName": team_name,
        "fullName": team_name,
        "shortName": team_name[:3].upper() if team_name else "",
        "flag": "",
        "runs": 0,
        "wicket": 0,
        "overs": 0,
        "scoreMsg": "",
        "extras": 0,
        "byes": 0,
        "legByes": 0,
        "wides": 0,
        "noBalls": 0,
        "penaltyRuns": 0,
    }


def _team_names_for_event(event_id: str, market_id: str = "") -> tuple[str, str]:
    db = get_db()
    filt: dict[str, str] = {}
    if event_id:
        filt["eventId"] = str(event_id)
    elif market_id:
        filt["marketId"] = str(market_id)
    if not filt:
        return ("", "")
    doc = db.matches.find_one(filt, {"_id": 0, "teamData": 1, "matchName": 1, "eventName": 1})
    if not doc:
        return ("", "")
    teams = [normalize_team_entry(t) for t in parse_team_selections(doc)]
    names = []
    for t in teams[:2]:
        name = str(
            t.get("runnerName")
            or t.get("runner_name")
            or t.get("team_name")
            or t.get("teamName")
            or ""
        ).strip()
        names.append(name)
    while len(names) < 2:
        names.append("")
    return names[0], names[1]


def _default_manual_score_data(event_id: Any = 0, team_names: tuple[str, str] = ("", "")) -> dict:
    return {
        "players": [
            {
                "name": "Player 1", "run": 0, "ball": 0, "fours": 0, "sixes": 0,
                "dotBalls": 0, "strike": "Yes", "strike_rate": 0,
            },
            {
                "name": "Player 2", "run": 0, "ball": 0, "fours": 0, "sixes": 0,
                "dotBalls": 0, "strike": "No", "strike_rate": 0,
            },
        ],
        "team1": _default_team_score(team_names[0]),
        "team2": _default_team_score(team_names[1]),
        "bowler": {
            "name": "", "run": 0, "over": "", "wicket": 0, "economy": "0.00",
            "maidens": 0, "dotBalls": 0, "wides": 0, "noBalls": 0,
        },
        "bowlers": [],
        "batsmen": {
            "striker": {"name": "", "runs": 0, "balls": 0},
            "nonStriker": {"name": "", "runs": 0, "balls": 0},
        },
        "fieldingTeam": "",
        "lastBalls": [],
        "lastBowler": {},
        "lastWicket": {"playerName": "", "bowlerName": "", "wicketType": "", "runs": 0},
        "partnership": {"runs": 0, "balls": 0, "playerOne": "", "playerTwo": ""},
        "tossInfo": {"wonBy": "", "decision": "", "date": str(_now())[:10]},
        "eventId": event_id or 0,
        "matchStatus": "toss",
        "status": "",
        "inning": "",
        "battingTeamName": "",
        "overSummaries": [],
    }


def _merge_manual_score(base: dict, stored: dict) -> dict:
    merged = copy.deepcopy(base)
    for key, val in (stored or {}).items():
        if key in ("team1", "team2") and isinstance(val, dict):
            merged[key] = {**merged.get(key, {}), **val}
        elif key == "bowler" and isinstance(val, dict):
            merged["bowler"] = {**merged.get("bowler", {}), **val}
        elif key == "batsmen" and isinstance(val, dict):
            merged["batsmen"] = {**merged.get("batsmen", {}), **val}
        else:
            merged[key] = val
    return merged


def _manual_score_query(payload: dict) -> dict:
    payload = payload or {}
    q: dict[str, str] = {}
    event_id = str(payload.get("eventId") or "")
    market_id = str(payload.get("marketId") or "")
    if event_id:
        q["eventId"] = event_id
    if market_id:
        q["marketId"] = market_id
    return q


def _load_manual_score_doc(payload: dict) -> Optional[dict]:
    q = _manual_score_query(payload)
    if not q:
        return None
    return get_db().center_manual_scores.find_one(q, {"_id": 0})


def _manual_score_payload_from_doc(doc: Optional[dict], payload: dict) -> dict:
    payload = payload or {}
    event_id = str(payload.get("eventId") or (doc or {}).get("eventId") or "")
    market_id = str(payload.get("marketId") or (doc or {}).get("marketId") or "")
    team_names = _team_names_for_event(event_id, market_id)
    score_data = _default_manual_score_data(event_id or 0, team_names)

    stored = (doc or {}).get("score") or (doc or {}).get("data") or {}
    if isinstance(stored, dict) and stored:
        if isinstance(stored.get("team1"), dict) or isinstance(stored.get("team2"), dict):
            score_data = _merge_manual_score(score_data, stored)
        else:
            if isinstance(stored.get("team1"), (int, float)):
                score_data["team1"]["runs"] = stored["team1"]
            if isinstance(stored.get("team2"), (int, float)):
                score_data["team2"]["runs"] = stored["team2"]
            if stored.get("overs") is not None:
                score_data["team1"]["overs"] = stored.get("overs", 0)
    return score_data


def mongo_cp_get_manual_score(payload: dict, _session_user: dict = None) -> dict:
    doc = _load_manual_score_doc(payload or {})
    score_data = _manual_score_payload_from_doc(doc, payload or {})
    return _ok({"data": score_data})


def mongo_cp_update_manual_score(payload: dict, _session_user: dict = None) -> dict:
    payload = payload or {}
    score = copy.deepcopy(payload.get("data") or {})
    event_id = str(payload.get("eventId") or score.get("eventId") or "")
    market_id = str(payload.get("marketId") or score.get("marketId") or "")
    if not event_id:
        return _err("eventId required")

    now = _now()
    history_entry = {
        "eventId": event_id,
        "marketId": market_id,
        "inning": score.get("inning") or "",
        "status": score.get("status") or "",
        "battingTeamName": score.get("battingTeamName") or "",
        "team1Runs": (score.get("team1") or {}).get("runs"),
        "team2Runs": (score.get("team2") or {}).get("runs"),
        "updatedAt": now,
    }

    filt = {"eventId": event_id}
    if market_id:
        filt["marketId"] = market_id

    get_db().center_manual_scores.update_one(
        filt,
        {
            "$set": {
                "eventId": event_id,
                "marketId": market_id,
                "score": score,
                "updatedAt": now,
            },
            "$setOnInsert": {"createdAt": now},
            "$push": {"history": {"$each": [history_entry], "$slice": -50}},
        },
        upsert=True,
    )
    return _ok({"data": score})


def mongo_cp_get_manual_score_history(payload: dict, _session_user: dict = None) -> dict:
    doc = _load_manual_score_doc(payload or {})
    history = (doc or {}).get("history") or []
    if not isinstance(history, list):
        history = []
    return _ok({"history": history})


def _normalize_squad_player(player: Any, index: int) -> dict:
    if isinstance(player, str):
        return {
            "name": player,
            "position": index + 1,
            "role": "Batsman",
            "image": "",
            "battingStyle": "",
            "bowlingStyle": "",
            "isCaptain": False,
            "isWicketKeeper": False,
        }
    if isinstance(player, dict):
        row = copy.deepcopy(player)
        row.setdefault("name", "")
        row.setdefault("position", index + 1)
        row.setdefault("role", "Batsman")
        row.setdefault("image", "")
        row.setdefault("battingStyle", "")
        row.setdefault("bowlingStyle", "")
        row.setdefault("isCaptain", False)
        row.setdefault("isWicketKeeper", False)
        return row
    return _normalize_squad_player("", index)


def mongo_cp_get_squad_templates(_payload: dict, _session_user: dict = None) -> dict:
    rows = list(get_db().center_squad_templates.find({}, {"_id": 0}))
    normalized: list[dict] = []
    for row in rows:
        players_raw = row.get("players") or []
        players = [_normalize_squad_player(p, i) for i, p in enumerate(players_raw)]
        normalized.append({
            "templateId": row.get("templateId") or row.get("id") or "",
            "templateName": row.get("templateName") or row.get("name") or "",
            "teamName": row.get("teamName") or row.get("name") or "",
            "sportId": row.get("sportId"),
            "players": players,
            "updatedAt": row.get("updatedAt") or row.get("createdAt") or _now(),
        })
    return _ok(normalized)


def mongo_cp_stub_ok(_payload: dict, _session_user: dict = None) -> dict:
    return _ok({})


def mongo_cp_stub_list(_payload: dict, _session_user: dict = None) -> dict:
    return _ok([])


# Explicit routes
CP_ROUTES: dict[str, tuple] = {
    "centerPanel/userList": (mongo_cp_user_list, True),
    "centerPanel/matchListByDatabase": (mongo_cp_match_list_by_database, True),
    "centerPanel/getSportsMatchList": (mongo_cp_get_sports_match_list, True),
    "centerPanel/diamondMatchList": (mongo_cp_diamond_match_list, True),
    "centerPanel/getMatchListBySeriesIdLatiyal": (mongo_cp_match_list_by_database, True),
    "centerPanel/getSportDataByEventId": (mongo_cp_sport_data_by_event, True),
    "centerPanel/getAllEvents": (mongo_cp_all_events, True),
    "centerPanel/getSeriesList": (mongo_cp_series_list, True),
    "centerPanel/getSeriesBySportId": (mongo_cp_series_list, True),
    "centerPanel/getLatiyalSeriesList": (mongo_cp_series_list, True),
    "centerPanel/getSessionListByApiAndDatabase": (mongo_cp_session_list, True),
    "centerPanel/getSessionByDatabase": (mongo_cp_session_by_database, True),
    "centerPanel/getManualFancyList": (mongo_cp_manual_fancy_list, True),
    "centerPanel/getManualBookmakerList": (mongo_cp_manual_bookmaker_list, True),
    "centerPanel/saveManualFancy": (mongo_cp_save_manual_fancy, True),
    "centerPanel/saveFancyByCenterPanel": (mongo_cp_save_fancy_by_center_panel, True),
    "centerPanel/saveManualBookmaker": (mongo_cp_save_manual_bookmaker, True),
    "centerPanel/updateSportByEventId": (mongo_cp_update_sport_by_event, True),
    "centerPanel/getDecisionLogs": (mongo_cp_decision_logs, True),
    "centerPanel/getManualScore": (mongo_cp_get_manual_score, True),
    "centerPanel/updateManualScore": (mongo_cp_update_manual_score, True),
    "centerPanel/getManualScoreHistory": (mongo_cp_get_manual_score_history, True),
    "centerPanel/getSquadTemplates": (mongo_cp_get_squad_templates, True),
    "centerPanel/updateFancyDecision": (mongo_cp_update_fancy_decision, True),
    "centerPanel/marketDecision": (mongo_cp_market_decision, True),
    "centerPanel/rollbackFancy": (mongo_cp_rollback_fancy, True),
    "decision/completeSportList": (mongo_cp_sport_list, True),
    "sports/matchList": (mongo_cp_match_list, True),
    "sports/betsList": (mongo_cp_sports_bets, True),
    "sports/getSessionPositionBySelectionId": (mongo_cp_session_position, True),
    "user/userDetails": (mongo_cp_user_details, True),
    "user/create1": (mongo_cp_stub_ok, True),
    # manualOdds — allFancy page (fancyGroups must be array, not {})
    "manualOdds/getFancyGroups": (mongo_cp_fancy_groups, True),
    "manualOdds/getFancyList": (mongo_cp_session_list, True),
    "manualOdds/getBookmakerList": (mongo_cp_manual_bookmaker_list, True),
    "manualOdds/getFancyTemplates": (mongo_cp_stub_list, True),
    "manualOdds/getDeletedFancyList": (mongo_cp_stub_list, True),
    "manualOdds/createFancyGroup": (mongo_cp_create_fancy_group, True),
    "manualOdds/updateFancyGroup": (mongo_cp_update_fancy_group, True),
    "manualOdds/deleteFancyGroup": (mongo_cp_delete_fancy_group, True),
    "manualOdds/declareFancy": (mongo_cp_update_fancy_decision, True),
    "manualOdds/createFancy": (mongo_cp_stub_ok, True),
    "manualOdds/updateFancy": (mongo_cp_stub_ok, True),
    "manualOdds/deleteFancy": (mongo_cp_stub_ok, True),
    "manualOdds/restoreFancy": (mongo_cp_stub_ok, True),
    "manualOdds/cancelFancyDeclare": (mongo_cp_rollback_fancy, True),
    "manualOdds/upsertBookmaker": (mongo_cp_stub_ok, True),
    "manualOdds/deleteBookmaker": (mongo_cp_stub_ok, True),
    "manualOdds/addBookmakerTeam": (mongo_cp_stub_ok, True),
    "manualOdds/updateGroupedFancyOdds": (mongo_cp_stub_ok, True),
    "manualOdds/createFancyTemplate": (mongo_cp_stub_ok, True),
    "manualOdds/deleteFancyTemplate": (mongo_cp_stub_ok, True),
    "manualOdds/applyFancyTemplate": (mongo_cp_stub_ok, True),
    "manualOdds/autoGenerateFancies": (mongo_cp_stub_ok, True),
    "manualOdds/getLlmSuggestion": (mongo_cp_stub_ok, True),
    "manualOdds/bulkUpdateFancyStatus": (mongo_cp_stub_ok, True),
    "manualOdds/toggleLlmForFancy": (mongo_cp_stub_ok, True),
}


def handle_centerpanel_api(endpoint: str, payload: dict, auth_header: str) -> bytes:
    endpoint = endpoint.lstrip("/").split("?")[0]
    if endpoint.startswith("v1/"):
        endpoint = endpoint[3:]

    if not ping():
        return json.dumps(_err("MongoDB not running. brew services start mongodb-community", 500)).encode()

    if endpoint.endswith("userLogin"):
        return json.dumps(mongo_cp_login(payload or {}), default=str).encode()

    if endpoint.endswith("logout"):
        return json.dumps(mongo_logout(auth_header), default=str).encode()

    route = CP_ROUTES.get(endpoint)
    session_user: dict = {}

    if route:
        handler, needs_session = route
        if needs_session:
            session_user, err = _resolve_cp_user(auth_header)
            if err:
                return json.dumps(err, default=str).encode()
            token = _extract_bearer(auth_header)
            if token:
                session_user = {**session_user, "_token": token}
        body = handler(payload or {}, session_user)
        return json.dumps(body, default=str).encode()

    if needs := READ_ROUTES.get(endpoint):
        session_user, err = _resolve_cp_user(auth_header)
        if err:
            return json.dumps(err, default=str).encode()
        coll, shape = needs
        body = _read_collection(coll, shape, payload or {})
        if not body["data"] or (isinstance(body["data"], list) and len(body["data"]) == 0):
            scraped = _load_scraped(endpoint)
            if scraped is not None:
                if shape == "paginated" and isinstance(scraped, dict):
                    body = _ok(scraped)
                elif shape == "paginated":
                    body = _ok({"list": _coerce_list(scraped), "total": len(_coerce_list(scraped))})
                else:
                    body = _ok(_coerce_list(scraped))
        return json.dumps(body, default=str).encode()

    if coll := WRITE_ROUTES.get(endpoint):
        session_user, err = _resolve_cp_user(auth_header)
        if err:
            return json.dumps(err, default=str).encode()
        body = _upsert_collection(coll, payload or {})
        return json.dumps(body, default=str).encode()

    if coll := DELETE_ROUTES.get(endpoint):
        session_user, err = _resolve_cp_user(auth_header)
        if err:
            return json.dumps(err, default=str).encode()
        body = _delete_collection(coll, payload or {})
        return json.dumps(body, default=str).encode()

    # Baaki update/save endpoints — generic OK
    session_user, err = _resolve_cp_user(auth_header)
    if err and endpoint not in ("centerPanel/getManualScore",):
        return json.dumps(err, default=str).encode()

    array_keys = ("List", "list", "Events", "Series", "Match", "Logs", "History", "Audit", "Groups", "Templates")
    if endpoint in MATCH_ENDPOINTS:
        body = _ok([])
    elif endpoint.startswith("manualOdds/get"):
        body = _ok([])
    elif any(k in endpoint for k in array_keys):
        scraped = _load_scraped(endpoint)
        body = _ok(_coerce_list(scraped) if scraped is not None else [])
    elif "user" in endpoint.lower():
        body = _ok({"list": [], "total": 0})
    else:
        body = _ok({})

    return json.dumps(body, default=str).encode()
