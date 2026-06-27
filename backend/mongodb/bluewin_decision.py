"""BlueWin dashboard — match/fancy result declare (MongoDB)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional, Tuple

from bson import ObjectId

from mongodb.admin_api import resolve_admin_user
from mongodb.auth import _extract_bearer
from mongodb.admin_all_handlers import _market_scope_filter
from mongodb.bet_logic import (
    _bookmaker_sid,
    _find_team_by_selection,
    is_fancy_market,
    parse_team_selections,
    resolve_team_name,
    toss_canonical_selection_id,
)
from mongodb.bets import _calc_user_exposure, settle_sports_bet, _sync_event_position_from_bets
from mongodb.auto_decision_settings import (
    mongo_auto_decision_match_list,
    mongo_update_auto_decision_setting,
)
from mongodb.centerpanel_api import (
    _err,
    _ok,
    mongo_cp_rollback_fancy,
    mongo_cp_update_fancy_decision,
)
from mongodb.db import get_db
from mongodb.wnp9_auto_decision import run_auto_decision_sync


def _now():
    return datetime.now(timezone.utc)


def _require_session(auth_header: str) -> Tuple[Optional[dict], Optional[dict]]:
    return resolve_admin_user(auth_header)


def _verify_staff_password(session_user: dict, password: str) -> bool:
    if not password:
        return False
    db = get_db()
    uid = session_user.get("userId")
    uname = str(session_user.get("username") or "").strip()
    queries = []
    if uid:
        queries.append({"userId": uid})
    if uname:
        queries.append({"username": {"$regex": f"^{uname}$", "$options": "i"}})
    for q in queries:
        actor = db.users.find_one(q)
        if actor and str(actor.get("password") or "") == password:
            return True
    return False


def _reverse_settled_pl(db, bet: dict, now) -> str | None:
    """Undo wallet credit from a settled bet. Returns userId if adjusted."""
    uid = str(bet.get("userId") or "")
    status = str(bet.get("status") or "open").lower()
    if not uid:
        return None
    if not (status == "settled" or bool(bet.get("isDeclare"))):
        return None
    pl = round(float(bet.get("profitLoss") or 0), 2)
    if not pl:
        return uid
    user = db.users.find_one({"userId": uid}) or {}
    coins = round(float(user.get("coins") or 0) - pl, 2)
    credit = round(float(user.get("creditLimit") or 0) - pl, 2)
    db.users.update_one(
        {"userId": uid},
        {"$set": {"coins": coins, "creditLimit": credit, "updatedAt": now}},
    )
    return uid


def _normalize_decision_winner(match: dict, won_sel: str) -> str:
    """UI selection_id / betfair id → bookmaker display id (1/2) for settlement."""
    won = str(won_sel or "").strip()
    if won.lower() in ("draw", "abonded", "abandoned"):
        return won.lower()
    teams = parse_team_selections(match)
    team = _find_team_by_selection(teams, won)
    if team:
        sid = _bookmaker_sid(team)
        if sid is not None:
            return str(sid)
    return won


def _open_odds_bets_query(market_id: str) -> dict:
    """Same event ki saari open odds/bookmaker bets — scraped admin scope."""
    scope = _market_scope_filter(market_id)
    base = {"status": "open", "isDeclare": {"$ne": True}}
    if not scope:
        return base
    return {"$and": [scope, base]}


def mongo_bw_odds_decision(payload: dict, session_user: dict = None) -> dict:
    """decision/oddsDecision — match / bookmaker / toss declare from dashboard."""
    payload = payload or {}
    market_id = str(payload.get("marketId") or "")
    raw_won = payload.get("decisionSelectionId") if payload.get("decisionSelectionId") is not None else payload.get("wonSelectionId")
    is_toss = bool(payload.get("isToss"))

    if not market_id or raw_won in (None, ""):
        return _err("marketId and decisionSelectionId required")

    db = get_db()
    match = db.matches.find_one({"marketId": market_id}) or {}
    if is_toss:
        won_sel = toss_canonical_selection_id(match, str(raw_won))
    else:
        won_sel = _normalize_decision_winner(match, str(raw_won))
    event_id = str(match.get("eventId") or payload.get("eventId") or "")

    settled = 0
    wallet_updates: dict[str, dict] = {}
    affected_positions: set[tuple[str, str, str]] = set()

    for bet in db.sports_bets.find(_open_odds_bets_query(market_id)):
        bf = str(bet.get("betFor") or "")
        ot = str(bet.get("oddsType") or "")
        gt = str(bet.get("gtype") or "")
        fancy = is_fancy_market(bf, ot, gt)
        toss_bet = bf.lower() == "toss" or ot.lower() == "toss"

        if is_toss:
            if not toss_bet:
                continue
        elif fancy or toss_bet:
            continue

        result = settle_sports_bet(bet["betId"], {"wonSelectionId": won_sel})
        if result.get("error"):
            continue

        settled += 1
        uid = str(bet.get("userId") or "")
        bet_mid = str(bet.get("marketId") or market_id)
        bet_eid = str(bet.get("eventId") or event_id)
        if uid:
            affected_positions.add((uid, bet_mid, bet_eid))
            pl = round(float(result.get("data", {}).get("profitLoss") or 0), 2)
            if uid not in wallet_updates:
                user = db.users.find_one(
                    {"userId": uid},
                    {"_id": 0, "coins": 1, "exposure": 1, "creditLimit": 1, "username": 1},
                ) or {}
                wallet_updates[uid] = {
                    "userId": uid,
                    "username": user.get("username"),
                    "coins": round(float(user.get("coins") or 0), 2),
                    "exposure": round(float(user.get("exposure") or 0), 2),
                    "creditLimit": round(float(user.get("creditLimit") or 0), 2),
                    "profitLoss": 0.0,
                }
            wallet_updates[uid]["profitLoss"] = round(wallet_updates[uid]["profitLoss"] + pl, 2)
            wallet_updates[uid]["coins"] = round(float(result.get("data", {}).get("coins") or wallet_updates[uid]["coins"]), 2)
            wallet_updates[uid]["exposure"] = round(float(result.get("data", {}).get("exposure") or wallet_updates[uid]["exposure"]), 2)

    for uid, bet_mid, bet_eid in affected_positions:
        _sync_event_position_from_bets(uid, bet_mid, bet_eid)

    team_name = resolve_team_name(match, won_sel) or ""
    if won_sel in ("draw", "abonded", "abandoned"):
        team_name = won_sel.title()

    upd: dict[str, Any] = {"updatedAt": _now()}
    if is_toss:
        upd["wonTossSelectionId"] = won_sel
        upd["wonTossTeamName"] = team_name
    else:
        upd["wonTeamBookmakerSelectionId"] = int(won_sel) if str(won_sel).isdigit() else won_sel
        upd["wonTeamName"] = team_name
        upd["isDeclare"] = True

    db.matches.update_one({"marketId": market_id}, {"$set": upd})

    db.decision_logs.insert_one({
        "logId": uuid.uuid4().hex[:24],
        "marketId": market_id,
        "eventId": event_id,
        "action": "toss_decision" if is_toss else "odds_decision",
        "type": "toss_decision" if is_toss else "match_odds",
        "status": "completed",
        "userName": (session_user or {}).get("username") or "owner",
        "payload": payload,
        "settledBets": settled,
        "wonSelectionId": won_sel,
        "wonTeamName": team_name,
        "createdAt": _now(),
    })

    msg = (
        "Toss decision declared successfully"
        if is_toss
        else "Match decision declared successfully"
    )
    return _ok({
        "marketId": market_id,
        "settledBets": settled,
        "wonTeamName": team_name,
        "walletUpdates": list(wallet_updates.values()),
    }, msg)


def mongo_bw_session_decision(payload: dict, session_user: dict = None) -> dict:
    """decision/sessionDecision — fancy/session declare."""
    return mongo_cp_update_fancy_decision(payload, session_user)


def mongo_bw_rollback_fancy(payload: dict, session_user: dict = None) -> dict:
    """decision/rollbackFancy."""
    return mongo_cp_rollback_fancy(payload, session_user)


def _fancy_bet_query(payload: dict) -> tuple[str, str, dict]:
    """marketId + selectionId (+ optional gtype/fancyType) bet filter."""
    market_id = str(payload.get("marketId") or "")
    selection_id = str(payload.get("selectionId") or "")
    bet_q: dict[str, Any] = {"marketId": market_id, "selectionId": selection_id}
    gtype = payload.get("gtype")
    fancy_type = payload.get("fancyType")
    if gtype:
        bet_q["gtype"] = gtype
    if fancy_type:
        bet_q["fancyType"] = fancy_type
    return market_id, selection_id, bet_q


def mongo_bw_cancel_fancy(payload: dict, session_user: dict = None) -> dict:
    """decision/cancelFancy — completed/incomplete fancy session delete (scraped site)."""
    payload = payload or {}
    market_id, selection_id, bet_q = _fancy_bet_query(payload)
    deleted_remark = str(
        payload.get("deletedRemark") or payload.get("deletedReamrk") or "cancel Fancy"
    )

    if not market_id or not selection_id:
        return _err("marketId and selectionId required")

    db = get_db()
    bets = [
        b for b in db.sports_bets.find(bet_q)
        if is_fancy_market(
            str(b.get("betFor") or ""),
            str(b.get("oddsType") or ""),
            str(b.get("gtype") or ""),
        )
    ]

    now = _now()
    actor_id = str((session_user or {}).get("userId") or "")
    canceled = 0
    affected_users: set[str] = set()

    for bet in bets:
        if bet.get("isDeleted") and str(bet.get("status") or "").lower() == "deleted":
            continue

        uid = str(bet.get("userId") or "")
        status = str(bet.get("status") or "open").lower()
        settled = status == "settled" or bool(bet.get("isDeclare"))

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

        db.sports_bets.update_one(
            {"_id": bet["_id"]},
            {
                "$set": {
                    "isDeleted": True,
                    "isDeclare": False,
                    "status": "deleted",
                    "deletedRemark": deleted_remark,
                    "deletedAt": now,
                    "deletedBy": actor_id,
                    "updatedAt": now,
                }
            },
        )
        canceled += 1
        if uid and not settled:
            affected_users.add(uid)

    for uid in affected_users:
        new_exposure = round(_calc_user_exposure(uid), 2)
        db.users.update_one(
            {"userId": uid},
            {"$set": {"exposure": new_exposure, "updatedAt": now}},
        )

    fancy_filter: dict[str, Any] = {
        "$or": [
            {"fancyId": selection_id},
            {"Selection_id": selection_id},
            {"session_id": selection_id},
            {"selectionId": selection_id},
        ]
    }
    if market_id:
        fancy_filter = {"$and": [fancy_filter, {"marketId": market_id}]}

    db.center_manual_fancy.update_many(
        fancy_filter,
        {
            "$set": {
                "isDeleted": True,
                "isCancel": True,
                "isDeclare": False,
                "deletedRemark": deleted_remark,
                "updatedAt": now,
            }
        },
    )

    match = db.matches.find_one({"marketId": market_id}, {"eventId": 1, "_id": 0}) or {}
    db.decision_logs.insert_one({
        "logId": uuid.uuid4().hex[:24],
        "marketId": market_id,
        "eventId": str(match.get("eventId") or payload.get("eventId") or ""),
        "selectionId": selection_id,
        "action": "cancel_fancy",
        "type": "fancy_cancel",
        "status": "completed",
        "userName": (session_user or {}).get("username") or "owner",
        "payload": payload,
        "canceledBets": canceled,
        "createdAt": now,
    })

    return _ok(
        {"selectionId": selection_id, "canceledBets": canceled},
        "Fancy canceled successfully",
    )


def mongo_bw_manual_betfair_decision(payload: dict, session_user: dict = None) -> dict:
    """decision/manualBetfairDecision."""
    payload = dict(payload or {})
    if payload.get("betfairMarketId") and not payload.get("marketId"):
        payload["marketId"] = str(payload["betfairMarketId"])
    return mongo_bw_odds_decision(payload, session_user)


def mongo_bw_clear_exposure(payload: dict, session_user: dict = None) -> dict:
    """decision/clearExposureByMarketId — exposure reset stub."""
    market_id = str((payload or {}).get("marketId") or "")
    db = get_db()
    if market_id:
        db.sports_bets.update_many(
            {"marketId": market_id, "status": "open"},
            {"$set": {"exposureCleared": True, "updatedAt": _now()}},
        )
    return _ok({"marketId": market_id}, "Exposure cleared successfully")


def mongo_bw_bets_delete(payload: dict, session_user: dict = None) -> dict:
    """decision/betsDelete — undeclare bet list delete / rollback."""
    payload = payload or {}
    session_user = session_user or {}
    password = str(payload.get("password") or "")
    if not password:
        return _err("Password required")

    if not _verify_staff_password(session_user, password):
        return _err("Invalid password")

    db = get_db()
    raw_ids = payload.get("_id") or payload.get("ids") or []
    if isinstance(raw_ids, (str, int)):
        raw_ids = [raw_ids]
    if not raw_ids:
        return _err("Bet id required")

    is_deleted = payload.get("isDeleted", True)
    if isinstance(is_deleted, str):
        is_deleted = is_deleted.lower() in ("true", "1", "yes")
    deleted_remark = str(payload.get("deletedRemark") or "byCompany")
    market_id = str(payload.get("marketId") or "")
    now = _now()
    updated = 0
    affected_users: set[str] = set()

    for raw_id in raw_ids:
        if raw_id in (None, ""):
            continue
        q: dict = {}
        sid = str(raw_id)
        if ObjectId.is_valid(sid):
            q["_id"] = ObjectId(sid)
        else:
            q["betId"] = sid
        if market_id:
            q["marketId"] = market_id

        bet = db.sports_bets.find_one(q)
        if not bet:
            continue

        uid = _reverse_settled_pl(db, bet, now)
        if uid:
            affected_users.add(uid)

        upd: dict[str, Any] = {
            "isDeleted": bool(is_deleted),
            "deletedRemark": deleted_remark,
            "updatedAt": now,
        }
        if is_deleted:
            upd["status"] = "deleted"
            upd["deletedAt"] = now
            upd["deletedBy"] = session_user.get("userId", "")
            upd["isDeclare"] = False
        else:
            upd["status"] = "open"
            upd["isDeclare"] = False
            upd["isDeleted"] = False
            upd["profitLoss"] = 0
            upd["decisionRun"] = None
            upd["settledAt"] = None
            upd["deletedAt"] = None
            upd["deletedBy"] = ""
            uid = str(bet.get("userId") or "")
            if uid:
                affected_users.add(uid)

        db.sports_bets.update_one({"_id": bet["_id"]}, {"$set": upd})
        updated += 1

    for uid in affected_users:
        new_exp = round(_calc_user_exposure(uid), 2)
        db.users.update_one({"userId": uid}, {"$set": {"exposure": new_exp, "updatedAt": now}})

    if not updated:
        return _err("Bet not found")

    msg = "Bet deleted successfully" if is_deleted else "Bet rollback successful"
    return _ok({"updated": updated}, msg)


def mongo_bw_automatic_betfair_decision(_payload: dict, session_user: dict = None) -> dict:
    """decision/automaticBetfairDecision — sync wnp9 declare state into local bets."""
    return run_auto_decision_sync(session_user)


def mongo_bw_betfair_market_decision(payload: dict, session_user: dict = None) -> dict:
    """decision/betfairMarketDecision — auto-decision for one eventId."""
    event_id = str((payload or {}).get("eventId") or "")
    if not event_id:
        return _err("eventId required")
    return run_auto_decision_sync(session_user, event_id=event_id)


def mongo_bw_stub_ok(_payload: dict, _session_user: dict = None) -> dict:
    return _ok({}, "OK")


BLUEWIN_DECISION_ROUTES: dict[str, tuple] = {
    "decision/oddsDecision": (mongo_bw_odds_decision, True),
    "decision/sessionDecision": (mongo_bw_session_decision, True),
    "decision/rollbackFancy": (mongo_bw_rollback_fancy, True),
    "decision/manualBetfairDecision": (mongo_bw_manual_betfair_decision, True),
    "decision/clearExposureByMarketId": (mongo_bw_clear_exposure, True),
    "decision/cancelFancy": (mongo_bw_cancel_fancy, True),
    "decision/betsDelete": (mongo_bw_bets_delete, True),
    "decision/automaticBetfairDecision": (mongo_bw_automatic_betfair_decision, True),
    "decision/autoDecisionMatchList": (mongo_auto_decision_match_list, True),
    "decision/updateAutoDecisionSetting": (mongo_update_auto_decision_setting, True),
    "decision/betfairMarketDecision": (mongo_bw_betfair_market_decision, True),
    "decision/dateWiseLedgerAndPosition": (mongo_bw_stub_ok, True),
    "decision/generateCasinoClientLedger": (mongo_bw_stub_ok, True),
}


def handle_bluewin_decision(endpoint: str, payload: dict, auth_header: str) -> bytes | None:
    """Return bytes if handled, else None."""
    import json

    route = BLUEWIN_DECISION_ROUTES.get(endpoint)
    if not route:
        return None

    handler, needs_session = route
    session_user: dict = {}
    if needs_session:
        session_user, err = _require_session(auth_header)
        if err:
            return json.dumps(err, default=str).encode("utf-8")
        tok = _extract_bearer(auth_header)
        if tok and session_user:
            session_user = dict(session_user)
            session_user["_token"] = tok

    body = handler(payload or {}, session_user)
    return json.dumps(body, default=str).encode("utf-8")
