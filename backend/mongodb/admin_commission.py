"""Admin commission APIs — MongoDB se compute."""

from __future__ import annotations

import copy
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from mongodb.admin_compute import (
    _bet_created_at,
    _casino_games_map,
    _casino_game_name,
    _collect_descendant_ids,
    _find_match_doc,
    _find_user,
    _num,
    _parse_date,
    _session_may_access_user,
    _statement_in_range,
    _statement_ts,
)
from mongodb.auth import _now
from mongodb.bet_logic import is_fancy_market
from mongodb.db import get_db


def _user_info(user: dict) -> dict:
    return {
        "userId": user.get("userId"),
        "username": user.get("username"),
        "name": user.get("name"),
        "userType": user.get("userType"),
    }


def _comm_rate(user: dict, kind: str) -> float:
    key = {
        "odds": "matchCommission",
        "session": "sessionCommission",
        "casino": "casinoCommission",
    }.get(kind, "matchCommission")
    return _num(user.get(key, 0)) / 100.0


def _commission_from_stake(stake: float, rate: float) -> float:
    return round(stake * rate, 2)


def _reports_commission(report_type: str, payload: dict) -> list:
    db = get_db()
    q: dict = {"reportType": report_type}
    if payload.get("userId"):
        q["userId"] = payload["userId"]
    rows = []
    for doc in db.reports.find(q):
        row = copy.deepcopy(doc.get("payload") or {})
        if isinstance(row, dict):
            rows.append(row)
    return rows


def compute_user_commission_report(payload: dict, session_user: dict | None = None) -> list:
    """decision/userCommissionReport — /app/agentComm/{userType} (chunk 5844)."""
    payload = payload or {}
    session_user = session_user or {}
    db = get_db()
    cached = _reports_commission("decision/userCommissionReport", payload)
    if cached:
        return cached

    username = (payload.get("username") or payload.get("agentCode") or "").strip().upper()
    from_dt = _parse_date(payload.get("fromDate"))
    to_dt = _parse_date(payload.get("toDate"))
    root_id = str(payload.get("userId") or session_user.get("userId") or "")
    if root_id and not _session_may_access_user(db, session_user, root_id):
        return []

    agent_types = ["agent", "superagent", "master", "subadmin", "admin", "superadmin", "subowner", "owner"]
    agents: list[dict] = []
    if username:
        u = _find_user(db, username)
        if u and u.get("userType") in agent_types:
            agents = [u]
    elif root_id:
        subtree = _collect_descendant_ids(db, root_id)
        subtree.add(root_id)
        agents = list(db.users.find(
            {
                "userId": {"$in": list(subtree)},
                "userType": {"$in": ["agent", "superagent", "master"]},
                "isDeleted": {"$ne": True},
            },
            {"_id": 0},
        ))
    else:
        agents = list(db.users.find(
            {"userType": {"$in": ["agent", "superagent", "master"]}, "isDeleted": {"$ne": True}},
            {"_id": 0},
        ))

    rows: list[dict] = []
    for agent in agents:
        downline = _collect_descendant_ids(db, agent["userId"])
        clients = list(db.users.find({"userId": {"$in": list(downline)}, "userType": "client"}, {"_id": 0}))
        client_ids = {c["userId"] for c in clients}

        odds_comm = session_comm = casino_comm = 0.0
        dl_odds = dl_session = dl_casino = 0.0

        for bet in db.sports_bets.find({"userId": {"$in": list(client_ids)}}, {"_id": 0}):
            created = _bet_created_at(bet)
            if not _statement_in_range(created, from_dt, to_dt):
                continue
            stake = _num(bet.get("stake"))
            client = _find_user(db, bet["userId"]) or {}
            parent = _find_user(db, str(client.get("parentId") or agent["userId"])) or agent
            if is_fancy_market(str(bet.get("betFor") or ""), str(bet.get("oddsType") or ""), str(bet.get("gtype") or "")):
                c = _commission_from_stake(stake, _comm_rate(client, "session"))
                a = _commission_from_stake(stake, _comm_rate(parent, "session"))
                session_comm += c
                dl_session += a
            else:
                c = _commission_from_stake(stake, _comm_rate(client, "odds"))
                a = _commission_from_stake(stake, _comm_rate(parent, "odds"))
                odds_comm += c
                dl_odds += a

        for bet in db.casino_bets.find({"userId": {"$in": list(client_ids)}}, {"_id": 0}):
            created = _bet_created_at(bet)
            if not _statement_in_range(created, from_dt, to_dt):
                continue
            stake = _num(bet.get("stake"))
            client = _find_user(db, bet["userId"]) or {}
            parent = _find_user(db, str(client.get("parentId") or agent["userId"])) or agent
            c = _commission_from_stake(stake, _comm_rate(client, "casino"))
            a = _commission_from_stake(stake, _comm_rate(parent, "casino"))
            casino_comm += c
            dl_casino += a

        rows.append({
            "_id": agent["userId"],
            "userInfo": _user_info(agent),
            "oddsComm": round(odds_comm, 2),
            "sessionComm": round(session_comm, 2),
            "casinoComm": round(casino_comm, 2),
            "downlineOddsComm": round(dl_odds, 2),
            "downlineSessionComm": round(dl_session, 2),
            "downlineCasinoComm": round(dl_casino, 2),
        })
    rows.sort(key=lambda r: (r.get("userInfo") or {}).get("username") or "")
    return rows


def _sport_comm_event_name(db, bet: dict) -> str:
    market_id = str(bet.get("marketId") or "")
    event_id = str(bet.get("eventId") or "")
    match = _find_match_doc(db, market_id)
    if not match and event_id:
        match = db.matches.find_one({"eventId": event_id}, {"_id": 0, "eventName": 1, "matchName": 1})
    if match:
        return str(match.get("eventName") or match.get("matchName") or "")
    return str(bet.get("runnerName") or bet.get("marketType") or "Sport")


def compute_commission_list_by_user(payload: dict, session_user: dict | None = None) -> list:
    """decision/commissionListByUserId — /app/agentCommHistory/{userId}."""
    payload = payload or {}
    cached = _reports_commission("decision/commissionListByUserId", payload)
    if cached:
        return cached

    db = get_db()
    user_id = str(payload.get("userId") or (session_user or {}).get("userId") or "")
    if user_id and not _session_may_access_user(db, session_user, user_id):
        return []
    viewer = _find_user(db, user_id)
    if not viewer:
        return []

    if str(viewer.get("userType") or "").lower() == "client":
        client_ids = [user_id]
        agent_user = _find_user(db, str(viewer.get("parentId") or "")) or {}
    else:
        downline = _collect_descendant_ids(db, user_id)
        client_ids = [
            u["userId"]
            for u in db.users.find(
                {"userId": {"$in": list(downline)}, "userType": "client", "isDeleted": {"$ne": True}},
                {"_id": 0, "userId": 1},
            )
        ]
        agent_user = viewer

    if not client_ids:
        return []

    games = _casino_games_map(db)
    rows: list[dict] = []
    bet_q: dict = {"userId": {"$in": client_ids}}
    if payload.get("marketId"):
        bet_q["marketId"] = payload["marketId"]

    for bet in db.sports_bets.find(bet_q, {"_id": 0}).limit(500):
        stake = _num(bet.get("stake"))
        is_fancy = is_fancy_market(
            str(bet.get("betFor") or ""),
            str(bet.get("oddsType") or ""),
            str(bet.get("gtype") or ""),
        )
        client = _find_user(db, bet["userId"]) or viewer
        parent = _find_user(db, str(client.get("parentId") or agent_user.get("userId") or "")) or agent_user
        rows.append({
            "_id": bet.get("betId"),
            "userInfo": _user_info(client),
            "eventName": _sport_comm_event_name(db, bet),
            "marketId": bet.get("marketId"),
            "isCasino": 0,
            "clientOddsComm": 0 if is_fancy else _commission_from_stake(stake, _comm_rate(client, "odds")),
            "clientSessionComm": _commission_from_stake(stake, _comm_rate(client, "session")) if is_fancy else 0,
            "agentOddsComm": 0 if is_fancy else _commission_from_stake(stake, _comm_rate(parent, "odds")),
            "agentSessionComm": _commission_from_stake(stake, _comm_rate(parent, "session")) if is_fancy else 0,
            "createdAt": _statement_ts(bet.get("createdAt")),
        })

    for bet in db.casino_bets.find({"userId": {"$in": client_ids}}, {"_id": 0}).limit(200):
        stake = _num(bet.get("stake"))
        client = _find_user(db, bet["userId"]) or viewer
        parent = _find_user(db, str(client.get("parentId") or agent_user.get("userId") or "")) or agent_user
        event_id = bet.get("eventId")
        game = games.get(int(event_id)) if event_id is not None else None
        if str(bet.get("gameType") or "").lower() == "aviator":
            event_name = "Aviator"
        else:
            event_name = _casino_game_name(game, event_id)
        rows.append({
            "_id": bet.get("betId"),
            "userInfo": _user_info(client),
            "eventName": event_name,
            "isCasino": 1,
            "clientOddsComm": _commission_from_stake(stake, _comm_rate(client, "casino")),
            "clientSessionComm": 0,
            "agentOddsComm": _commission_from_stake(stake, _comm_rate(parent, "casino")),
            "agentSessionComm": 0,
            "createdAt": _statement_ts(bet.get("createdAt")),
        })
    rows.sort(key=lambda r: r.get("createdAt") or 0, reverse=True)
    return rows


def compute_reset_comm_list(payload: dict) -> list:
    payload = payload or {}
    db = get_db()
    cached = _reports_commission("decision/resetCommList", payload)
    if cached:
        return cached

    user_id = str(payload.get("userId") or "")
    q: dict = {"category": "commission"}
    if user_id:
        q["userId"] = user_id
    rows = []
    for entry in db.ledger_entries.find(q, {"_id": 0}).sort("createdAt", -1).limit(200):
        created = entry.get("createdAt")
        ts = int(created.timestamp() * 1000) if hasattr(created, "timestamp") else created
        meta = entry.get("meta") or {}
        rows.append({
            "_id": entry.get("ledgerId"),
            "oddsComm": _num(meta.get("oddsComm", entry.get("amount") if meta.get("type") == "odds" else 0)),
            "sessionComm": _num(meta.get("sessionComm", 0)),
            "casinoComm": _num(meta.get("casinoComm", 0)),
            "remark": entry.get("description") or entry.get("remark") or "",
            "createdAt": ts,
        })
    return rows


def record_commission_reset(payload: dict, session_user: dict) -> dict:
    """Optional POST-style reset — ledger mein save."""
    payload = payload or {}
    uid = str(payload.get("userId") or session_user.get("userId") or "")
    now = _now()
    db = get_db()
    db.ledger_entries.insert_one({
        "ledgerId": uuid.uuid4().hex[:24],
        "userId": uid,
        "type": "credit",
        "amount": _num(payload.get("oddsComm", 0)) + _num(payload.get("sessionComm", 0)) + _num(payload.get("casinoComm", 0)),
        "category": "commission",
        "description": payload.get("remark") or "Commission reset",
        "remark": payload.get("remark") or "",
        "meta": {
            "oddsComm": _num(payload.get("oddsComm", 0)),
            "sessionComm": _num(payload.get("sessionComm", 0)),
            "casinoComm": _num(payload.get("casinoComm", 0)),
        },
        "createdAt": now,
    })
    return {"message": "Commission recorded", "code": 0, "error": False, "data": {}}
