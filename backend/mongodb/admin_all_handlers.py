"""Admin panel — baaki saare scraped API handlers (MongoDB collections se)."""

from __future__ import annotations

import copy
import uuid
from typing import Any, Optional

from mongodb.bet_logic import (
    calc_liability,
    is_fancy_market,
    merge_positions,
    normalize_match_for_api,
    parse_team_selections,
    remap_bookmaker_runners,
    remap_to_admin_display_ids,
    resolve_team_name,
    settle_fancy_bet,
    _fancy_rate,
)
from mongodb.bets import (
    _bet_to_fancy_record,
    _bet_to_odds_record,
    _iso_created_at,
    _market_label,
    _mybets_display_type,
)
from mongodb.matches_api import ADMIN_MONGO_ONLY, fetch_match_by_market_id, post_live_api, prepare_match_for_admin, _find_match_local
from mongodb.auth import _now
from mongodb.db import get_db
from mongodb.admin_compute import (
    compute_casino_bet_report,
    compute_casino_completed_list,
    compute_casino_plus_minus,
    compute_casino_profit_loss_pos,
    compute_casino_realtime_pos,
    compute_casino_report_by_user,
    compute_casino_result_by_round,
    compute_client_plus_minus,
    compute_day_wise_casino,
    compute_fancy_run_position_map,
    compute_fancy_session_positions,
    compute_matka_bet_list,
    compute_matka_list,
    compute_matka_profit_loss,
    compute_plus_minus_market,
    compute_plus_minus_user_wise,
    compute_session_list,
    compute_user_profit_loss,
    compute_lena_dena_list,
    compute_user_ledger,
    compute_user_statement,
    _empty_user_ledger,
    _find_match_doc,
)
from mongodb.admin_commission import (
    compute_commission_list_by_user,
    compute_reset_comm_list,
    compute_user_commission_report,
)


def _num(val, default=0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _bet_display_profit_loss(bet: dict, position_info: dict | None = None) -> tuple[float, float]:
    """Undeclare bet list UI — profit / loss columns (original site shape)."""
    pi = position_info if position_info is not None else (bet.get("positionInfo") or {})
    if pi:
        vals = [_num(v) for v in pi.values()]
        if vals:
            return round(max(vals), 2), round(abs(min(vals)), 2)

    amount = _num(bet.get("stake") or bet.get("amount"))
    odds = _num(bet.get("odds"))
    bt = str(bet.get("betType") or bet.get("type") or "L").upper()
    bet_for = str(bet.get("betFor") or "")
    odds_type = str(bet.get("oddsType") or bet_for or "")
    gtype = str(bet.get("gtype") or bet.get("fancyType") or "")

    pl = _num(bet.get("profitLoss", 0))
    if str(bet.get("status") or "").lower() not in ("open", "") and pl != 0:
        return round(max(pl, 0), 2), round(abs(min(pl, 0)), 2)

    if is_fancy_market(bet_for, odds_type, gtype):
        rate = _fancy_rate(odds)
        if bt == "N":
            return round(amount * rate, 2), round(amount, 2)
        if bt == "Y":
            return round(amount, 2), round(amount * rate, 2)
        loss = calc_liability(amount, odds, bt, odds_type, gtype, bet_for)
        return round(amount, 2), round(loss, 2)

    loss = calc_liability(amount, odds, bt, odds_type, gtype, bet_for)
    ot = str(odds_type or bet_for).lower()
    if ot in ("bookmaker", "toss", "odds"):
        profit = round(amount * odds, 2) if bt == "L" else round(amount, 2)
    elif ot == "match":
        o = odds if odds > 1 else odds + 1
        profit = round(amount * max(o - 1, 0), 2) if bt in ("L", "B") else round(amount, 2)
    elif bt in ("L", "B", "Y"):
        profit = round(amount, 2)
    else:
        o = odds if odds > 1 else odds + 1
        profit = round(amount * max(o - 1, 0), 2)
    return profit, round(loss, 2)


def _attach_bet_row_meta(row: dict, bet: dict, position_info: dict | None = None) -> None:
    profit, loss = _bet_display_profit_loss(bet, position_info)
    row["profit"] = profit
    row["loss"] = loss
    if bet.get("_id") is not None:
        row["_id"] = str(bet["_id"])
    if bet.get("ip"):
        row["ip"] = str(bet.get("ip"))
    if bet.get("isDeleted") is not None:
        row["isDeleted"] = 1 if bet.get("isDeleted") else 0


def _is_owner_user(user: dict | None) -> bool:
    return str((user or {}).get("userType") or "").lower() == "owner"


def _owner_mints_coins(user: dict | None) -> bool:
    return _is_owner_user(user)


def _strip_mongo(doc: dict) -> dict:
    row = copy.deepcopy(doc)
    row.pop("_id", None)
    row.pop("password", None)
    return row


def _find_user(db, user_id: str) -> dict | None:
    if not user_id:
        return None
    user = db.users.find_one({"userId": user_id, "isDeleted": {"$ne": True}})
    if user:
        return user
    return db.users.find_one({"username": str(user_id).upper(), "isDeleted": {"$ne": True}})


def _admin_client_user_info(user: dict, db=None) -> dict:
    """Admin bet rows — clientName, clientCode, creatorName."""
    if db is None:
        db = get_db()
    uid = str(user.get("userId") or "")
    username = str(user.get("username") or uid)
    name = str(user.get("name") or "")
    creator = _find_user(db, str(user.get("creatorId") or "")) or {}
    creator_name = str(creator.get("name") or creator.get("username") or "")
    return {
        "userId": uid,
        "username": username,
        "name": name,
        "clientName": name,
        "clientCode": username,
        "creatorName": creator_name,
    }


def _is_truthy_flag(val: Any) -> bool:
    return val is True or val == 1 or str(val).lower() in ("true", "1")


def _is_falsey_flag(val: Any) -> bool:
    return val is False or val == 0 or str(val).lower() in ("false", "0")


def _open_odds_bets_query(market_id: str, extra: dict | None = None) -> dict:
    """Open bookmaker/match odds bets — same eventId scope as betsList."""
    filters: dict = {
        "status": "open",
        "isDeclare": {"$ne": True},
        "isDeleted": {"$ne": True},
    }
    if extra:
        filters.update(extra)
    return _merge_bets_query(market_id, filters)


def _stake_totals_by_selection(
    market_id: str,
    match: Optional[dict] = None,
    user_id: str | None = None,
) -> dict[str, float]:
    """Sum open odds stakes per selectionId (match-bets totalPosition footer)."""
    db = get_db()
    extra: dict = {}
    if user_id:
        extra["userId"] = user_id
    bet_q = _open_odds_bets_query(market_id, extra or None)
    totals: dict[str, float] = {}
    for bet in db.sports_bets.find(bet_q, {"_id": 0}):
        if is_fancy_market(
            str(bet.get("betFor") or ""),
            str(bet.get("oddsType") or ""),
            str(bet.get("gtype") or ""),
        ):
            continue
        sid = bet.get("selectionId")
        if sid is None:
            continue
        mapped = remap_to_admin_display_ids(match, {str(sid): _num(bet.get("stake", 0))})
        for key, val in mapped.items():
            totals[key] = totals.get(key, 0.0) + _num(val)
    return totals


def _enrich_odds_position_rows(
    rows: list[dict],
    stake_by_sel: dict[str, float],
    match: Optional[dict] = None,
) -> list[dict]:
    """Add totalPosition; ensure every team runner has a row."""
    by_id = {str(r.get("_id") or r.get("selectionId")): r for r in rows}
    teams = parse_team_selections(match) if match else []
    team_ids = [
        str(t.get("bookmakerSelectionId") or t.get("selection_id") or t.get("selectionId"))
        for t in teams
        if t.get("bookmakerSelectionId") or t.get("selection_id") or t.get("selectionId") is not None
    ]
    if not team_ids:
        team_ids = sorted(by_id.keys(), key=lambda k: int(k) if str(k).isdigit() else k)
    out: list[dict] = []
    for key in team_ids:
        row = by_id.get(key) or {
            "_id": key,
            "selectionId": int(key) if str(key).isdigit() else key,
            "Position": 0.0,
        }
        sid = str(row.get("_id") or row.get("selectionId"))
        out.append({
            **row,
            "_id": sid,
            "selectionId": int(sid) if sid.isdigit() else sid,
            "Position": round(_num(row.get("Position", 0)), 2),
            "totalPosition": round(_num(stake_by_sel.get(sid, 0)), 2),
        })
    for key, row in by_id.items():
        if key not in team_ids:
            sid = str(row.get("_id") or row.get("selectionId"))
            out.append({
                **row,
                "_id": sid,
                "selectionId": int(sid) if sid.isdigit() else sid,
                "Position": round(_num(row.get("Position", 0)), 2),
                "totalPosition": round(_num(stake_by_sel.get(sid, 0)), 2),
            })
    return out


def _user_has_credit_history(db, user_id: str) -> bool:
    return bool(db.ledger_entries.find_one({"userId": user_id, "type": "credit"}))


def record_coin_transfer(
    db,
    *,
    debit_user: dict,
    credit_user: dict,
    amount: float,
    transfer_type: str,
    debit_description: str,
    credit_description: str,
    remark: str = "",
    category: str = "cash",
    created_at=None,
) -> tuple[str, str]:
    """Parent/child coin move — dono users ke ledger me transaction."""
    if amount <= 0:
        return "", ""
    now = created_at or _now()
    debit_id = uuid.uuid4().hex[:24]
    credit_id = uuid.uuid4().hex[:24]
    db.ledger_entries.insert_many([
        {
            "ledgerId": debit_id,
            "userId": debit_user["userId"],
            "toUserId": credit_user["userId"],
            "type": "debit",
            "amount": amount,
            "description": debit_description,
            "remark": remark,
            "category": category,
            "transferType": transfer_type,
            "balanceAfter": _num(debit_user.get("coins", 0)),
            "createdAt": now,
        },
        {
            "ledgerId": credit_id,
            "userId": credit_user["userId"],
            "fromUserId": debit_user["userId"],
            "type": "credit",
            "amount": amount,
            "description": credit_description,
            "remark": remark,
            "category": category,
            "transferType": transfer_type,
            "balanceAfter": _num(credit_user.get("coins", 0)),
            "createdAt": now,
        },
    ])
    return debit_id, credit_id


def _collect_descendant_ids(db, root_id: str) -> set[str]:
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


def _can_manage_user(session_user: dict, target_user_id: str) -> bool:
    sid = str(session_user.get("userId") or "")
    tid = str(target_user_id or "")
    if not sid:
        return False
    if not tid or tid == sid:
        return True
    from mongodb.admin_compute import _session_downline_ids
    return tid in _session_downline_ids(get_db(), session_user)


def _ok_list(rows: list, msg: str = "OK") -> dict:
    return {"message": msg, "code": 0, "error": False, "data": rows}


def _ok_data(data: Any, msg: str = "OK") -> dict:
    return {"message": msg, "code": 0, "error": False, "data": data}


def _ok_obj(msg: str = "OK") -> dict:
    return {"message": msg, "code": 0, "error": False, "data": {}}


def _reports_payload(report_type: str, payload: dict) -> list:
    db = get_db()
    q: dict = {"reportType": report_type}
    if payload.get("userId"):
        q["userId"] = payload["userId"]
    if payload.get("marketId"):
        q["marketId"] = payload["marketId"]
    if payload.get("eventId"):
        q["eventId"] = payload["eventId"]
    rows = []
    for doc in db.reports.find(q):
        row = copy.deepcopy(doc.get("payload") or {})
        row.setdefault("userId", doc.get("userId"))
        row.setdefault("marketId", doc.get("marketId"))
        rows.append(_strip_mongo(row) if isinstance(row, dict) else row)
    return rows


def _computed_or_reports(report_type: str, payload: dict, compute_fn) -> list:
    rows = compute_fn(payload or {})
    if rows:
        return rows
    return _reports_payload(report_type, payload or {})


def mongo_admin_match_by_market(payload: dict, session_user: dict = None) -> dict:
    market_id = str(payload.get("marketId") or "")
    event_id = str(payload.get("eventId") or "")
    if ADMIN_MONGO_ONLY:
        match = _find_match_local(market_id, event_id)
        if match:
            return {"message": 0, "code": 0, "error": False, "data": prepare_match_for_admin(match)}
        return {"message": 0, "code": 0, "error": False, "data": {}}
    token = (session_user or {}).get("_token") or ""
    match = fetch_match_by_market_id(market_id, event_id, token, prefer_live=False)
    if match:
        return {"message": 0, "code": 0, "error": False, "data": match}
    return {"message": 0, "code": 0, "error": False, "data": {}}


def _aggregate_admin_odds_positions(market_id: str, match: Optional[dict]) -> list[dict]:
    """Admin getOddsPosition — selection_id (1/2) par total Position."""
    db = get_db()
    totals: dict[str, float] = {}
    positions = list(db.positions.find({"marketId": market_id}, {"_id": 0}))

    if positions:
        for pos in positions:
            runners = remap_to_admin_display_ids(match, pos.get("runners") or {})
            totals = merge_positions(totals, runners)
    else:
        bet_q = _open_odds_bets_query(market_id)
        for bet in db.sports_bets.find(bet_q, {"_id": 0}):
            if is_fancy_market(
                str(bet.get("betFor") or ""),
                str(bet.get("oddsType") or ""),
                str(bet.get("gtype") or ""),
            ):
                continue
            pos_info = bet.get("positionInfo") or {}
            if match and pos_info:
                pos_info = remap_bookmaker_runners(match, pos_info)
            pos_info = remap_to_admin_display_ids(match, pos_info)
            totals = merge_positions(totals, pos_info)

    rows: list[dict] = []
    for key, val in sorted(totals.items(), key=lambda kv: int(kv[0]) if str(kv[0]).isdigit() else kv[0]):
        sid = int(key) if str(key).isdigit() else key
        rows.append({"_id": str(key), "selectionId": sid, "Position": round(_num(val), 2)})
    stake_by_sel = _stake_totals_by_selection(market_id, match)
    return _enrich_odds_position_rows(rows, stake_by_sel, match)


def _client_runners_for_market(match: Optional[dict], runners: dict) -> list[dict]:
    teams = parse_team_selections(match)
    mapped = remap_to_admin_display_ids(match, runners or {})
    if teams:
        out: list[dict] = []
        for team in teams:
            sid = team.get("bookmakerSelectionId") or team.get("selection_id") or team.get("selectionId")
            if sid is None:
                continue
            name = team.get("runner_name") or team.get("runnerName") or ""
            out.append({
                "selectionId": sid,
                "selectionName": name,
                "position": mapped.get(str(sid), 0),
            })
        return out
    return [
        {"selectionId": k, "selectionName": "", "position": v}
        for k, v in mapped.items()
    ]


def _admin_odds_bet_row(bet: dict, match: Optional[dict] = None) -> dict:
    """Match-session-bets — odds/bookmaker rows (teamName, positionInfo, ...)."""
    db = get_db()
    uid = bet.get("userId") or ""
    user = _find_user(db, uid) or {}
    row = _bet_to_odds_record(bet, match)
    row["userId"] = uid
    row["username"] = user.get("username") or uid
    row["name"] = user.get("name") or ""
    row["userInfo"] = _admin_client_user_info(user, db)
    is_declared = bool(bet.get("isDeclare") or bet.get("status") != "open")
    row["isDeclare"] = 1 if is_declared else 0
    if is_declared:
        row["decisionSelectionId"] = bet.get("decisionSelectionId") or bet.get("selectionId")
    row["profitLoss"] = _num(bet.get("profitLoss", 0))
    if row.get("positionInfo"):
        row["positionInfo"] = {str(k): _num(v) for k, v in row["positionInfo"].items()}
    if not row.get("teamName"):
        row["teamName"] = resolve_team_name(
            match,
            bet.get("selectionId"),
            payload_name=str(bet.get("runnerName") or ""),
        )
    if bet.get("wonTeamName"):
        row["wonTeamName"] = str(bet.get("wonTeamName"))
    elif bet.get("decisionSelectionId") is not None and match:
        row["wonTeamName"] = resolve_team_name(match, bet.get("decisionSelectionId"))
    elif str(bet.get("status") or "").lower() in ("rejected", "cancelled", "void", "deleted"):
        row["wonTeamName"] = str(bet.get("status")).title()
    if bet.get("deletedRemark"):
        row["deletedRemark"] = bet.get("deletedRemark")
    elif bet.get("rejectReason"):
        row["deletedRemark"] = bet.get("rejectReason")
    _attach_bet_row_meta(row, bet, row.get("positionInfo"))
    return row


def _admin_fancy_bet_row(bet: dict) -> dict:
    """Match-session-bets — fancy/session rows (sessionName, run, gtype, ...)."""
    db = get_db()
    uid = bet.get("userId") or ""
    user = _find_user(db, uid) or {}
    row = _bet_to_fancy_record(bet)
    row["userId"] = uid
    row["username"] = user.get("username") or uid
    row["name"] = user.get("name") or ""
    row["userInfo"] = _admin_client_user_info(user, db)
    is_declared = bool(bet.get("isDeclare") or bet.get("status") != "open")
    row["isDeclare"] = 1 if is_declared else 0
    if bet.get("decisionRun") is not None:
        row["decisionRun"] = bet.get("decisionRun")
    if bet.get("deletedRemark"):
        row["deletedRemark"] = bet.get("deletedRemark")
    if is_declared and bet.get("decisionRun") is not None:
        row["profitLoss"] = settle_fancy_bet(bet, int(_num(bet.get("decisionRun"))))
    else:
        row["profitLoss"] = _num(bet.get("profitLoss", 0))
    _attach_bet_row_meta(row, bet)
    return row


def _selection_id_query(selection_id: Any) -> Any:
    if selection_id in (None, "", "null"):
        return None
    try:
        sid = int(selection_id)
        return {"$in": [sid, str(sid), selection_id]}
    except (TypeError, ValueError):
        return selection_id


def _market_scope_filter(market_id: str) -> dict:
    """MarketId + same eventId ki bets (bookmaker alag market par ho sakti hain)."""
    market_id = str(market_id or "")
    if not market_id:
        return {}
    match = _find_match_doc(get_db(), market_id)
    event_id = str(match.get("eventId") or "") if match else ""
    if event_id:
        return {"$or": [{"marketId": market_id}, {"eventId": event_id}]}
    return {"marketId": market_id}


def _merge_bets_query(market_id: str, filters: dict) -> dict:
    scope = _market_scope_filter(market_id)
    if not scope:
        return dict(filters)
    if not filters:
        return scope
    return {"$and": [scope, filters]}


def _apply_bets_list_filters(q: dict, payload: dict) -> None:
    """betsList — isDeleted / isDeclare (inplay vs completed fancy pages)."""
    is_del = payload.get("isDeleted")
    del_false = _is_falsey_flag(is_del)
    del_true = _is_truthy_flag(is_del)

    declare_param = payload.get("isDeclare")
    declare_true = _is_truthy_flag(declare_param)
    declare_false = _is_falsey_flag(declare_param)
    paginate = payload.get("pageNo") is not None or payload.get("size") is not None
    fancy_on = _is_truthy_flag(payload.get("fancyBet"))
    odds_on = _is_truthy_flag(payload.get("oddsBet"))
    only_fancy = fancy_on and not odds_on

    if del_true:
        q["$or"] = [
            {"isDeleted": True},
            {"status": {"$in": ["rejected", "cancelled", "deleted", "void"]}},
        ]
        return

    if del_false:
        q["isDeleted"] = {"$ne": True}

    # Completed fancy (4312): isDeclare true, or fancy-only View without pagination
    if declare_true or (only_fancy and del_false and not declare_false and not paginate):
        q["$or"] = [
            {"isDeclare": True},
            {"status": {"$in": ["settled", "won", "lost", "declared"]}},
            {"decisionRun": {"$exists": True, "$ne": None}},
        ]
    elif declare_false or (del_false and paginate and not only_fancy):
        # Inplay match odds (2717) — open undeclared only
        q["status"] = "open"
        q["isDeclare"] = {"$ne": True}
    elif only_fancy and del_false and paginate:
        # Inplay display session bets (1349) — open + settled, non-deleted
        q["status"] = {"$nin": ["rejected", "cancelled", "deleted", "void"]}


def _admin_bet_row(bet: dict, match: Optional[dict] = None) -> dict:
    if is_fancy_market(
        str(bet.get("betFor") or ""),
        str(bet.get("oddsType") or ""),
        str(bet.get("gtype") or ""),
    ):
        return _admin_fancy_bet_row(bet)
    return _admin_odds_bet_row(bet, match)


def _admin_exposure_bet_list(payload: dict) -> list[dict]:
    """dataReport exposure modal — flat unsettled bet rows (sports + casino)."""
    user_id = payload.get("downlineUserId") or payload.get("userId")
    if not user_id:
        return []

    db = get_db()
    include_odds = payload.get("oddsBet", True) is not False
    include_fancy = payload.get("fancyBet", True) is not False
    include_diamond = payload.get("diamondBet", True) is not False
    rows: list[dict] = []

    sports_q: dict = {
        "userId": user_id,
        "status": "open",
        "isDeclare": {"$ne": True},
    }
    if include_odds or include_fancy:
        for bet in db.sports_bets.find(sports_q, {"_id": 0}).sort("createdAt", -1).limit(500):
            is_fancy = is_fancy_market(
                str(bet.get("betFor") or ""),
                str(bet.get("oddsType") or ""),
                str(bet.get("gtype") or ""),
            )
            if is_fancy and not include_fancy:
                continue
            if not is_fancy and not include_odds:
                continue
            market_type = "fancy" if is_fancy else "odds"
            market_name = _market_label(
                db,
                str(bet.get("marketId") or ""),
                bet.get("eventId"),
                bet,
            )
            if is_fancy:
                market_name = str(
                    bet.get("runnerName") or bet.get("gtype") or bet.get("betFor") or market_name
                ).strip()
            else:
                market_name = str(market_name).strip()
                team = str(bet.get("runnerName") or "").strip()
                if team and team not in market_name:
                    market_name = f"{market_name} — {team}" if market_name else team
            row = {
                "marketType": market_type,
                "market": market_name,
                "rate": bet.get("odds"),
                "type": _mybets_display_type(bet.get("betType")),
                "amount": bet.get("stake"),
                "time": _iso_created_at(bet.get("createdAt")),
            }
            if is_fancy:
                row["odds"] = bet.get("run") if bet.get("run") not in (None, "") else bet.get("odds")
            else:
                row["odds"] = bet.get("odds")
            rows.append(row)

    if include_diamond:
        for bet in db.casino_bets.find(
            {"userId": user_id, "status": "open"},
            {"_id": 0},
        ).sort("createdAt", -1).limit(200):
            gt = str(bet.get("gameType") or "casino")
            if gt == "aviator":
                market = "Aviator"
                if bet.get("roundId"):
                    market = f"Aviator — Round {bet['roundId']}"
            else:
                market = str(bet.get("selection") or bet.get("casinoType") or "Casino").strip()
            rows.append({
                "marketType": "diamondCasino",
                "market": market,
                "odds": bet.get("rate") or bet.get("multiplier") or bet.get("odds") or 0,
                "type": "L",
                "amount": bet.get("stake"),
                "time": _iso_created_at(bet.get("createdAt")),
            })

    return rows


def mongo_admin_bets_list(payload: dict, session_user: dict = None) -> dict:
    payload = payload or {}
    if payload.get("isClientExposure") and (payload.get("downlineUserId") or payload.get("userId")):
        return {
            "message": "Fetch List Successfuly",
            "code": 0,
            "error": False,
            "data": _admin_exposure_bet_list(payload),
        }

    market_id = str(payload.get("marketId") or "")
    token = (session_user or {}).get("_token") or ""
    if not ADMIN_MONGO_ONLY and market_id:
        live = post_live_api("sports/betsList", payload, token)
        if live and not live.get("error") and isinstance(live.get("data"), dict):
            return {
                "message": live.get("message") or "Fetch List Successfuly",
                "code": 0,
                "error": False,
                "data": live["data"],
            }

    db = get_db()
    filters: dict = {}
    if payload.get("userId"):
        filters["userId"] = payload["userId"]
    if payload.get("downlineUserId"):
        filters["userId"] = payload["downlineUserId"]
    sel_q = _selection_id_query(payload.get("selectionId"))
    if sel_q is not None:
        filters["selectionId"] = sel_q
    _apply_bets_list_filters(filters, payload)
    q = _merge_bets_query(market_id, filters)

    prefer_live = False if ADMIN_MONGO_ONLY else None
    match = fetch_match_by_market_id(
        market_id, str(payload.get("eventId") or ""), token, prefer_live=prefer_live
    )
    odds_rows: list[dict] = []
    fancy_rows: list[dict] = []
    for bet in db.sports_bets.find(q, {"_id": 0}).sort("createdAt", -1):
        row = _admin_bet_row(bet, match)
        if is_fancy_market(
            str(bet.get("betFor") or ""),
            str(bet.get("oddsType") or ""),
            str(bet.get("gtype") or ""),
        ):
            fancy_rows.append(row)
        else:
            odds_rows.append(row)

    fancy_on = _is_truthy_flag(payload.get("fancyBet"))
    odds_on = _is_truthy_flag(payload.get("oddsBet"))
    only_fancy = fancy_on and not odds_on
    only_odds = odds_on and not fancy_on
    if _is_falsey_flag(payload.get("fancyBet")):
        fancy_rows = []
    if _is_falsey_flag(payload.get("oddsBet")):
        odds_rows = []
    if only_fancy:
        odds_rows = []
    if only_odds:
        fancy_rows = []

    total_odds = len(odds_rows)
    total_fancy = len(fancy_rows)
    page_no = max(int(payload.get("pageNo") or 1), 1)
    size = max(int(payload.get("size") or 50), 1)
    paginate = payload.get("pageNo") is not None or payload.get("size") is not None
    start = (page_no - 1) * size
    if paginate and only_odds:
        odds_rows = odds_rows[start:start + size]
    elif paginate and only_fancy:
        fancy_rows = fancy_rows[start:start + size]
    elif paginate and fancy_on and odds_on:
        odds_rows = odds_rows[start:start + size]
        fancy_rows = fancy_rows[start:start + size]

    return {
        "message": "Fetch List Successfuly",
        "code": 0,
        "error": False,
        "data": {
            "oddsBetData": odds_rows,
            "fancyBetData": fancy_rows,
            "totalOddsCount": total_odds,
            "totalFancyCount": total_fancy,
        },
    }


def mongo_admin_client_list_by_market(payload: dict, session_user: dict = None) -> dict:
    market_id = str(payload.get("marketId") or "")
    token = (session_user or {}).get("_token") or ""
    if not ADMIN_MONGO_ONLY and market_id:
        live = post_live_api("sports/clientListByMarketId", payload, token)
        if live and not live.get("error") and live.get("data") is not None:
            data = live["data"]
            if isinstance(data, dict) and "total" not in data and isinstance(data.get("list"), list):
                data = {**data, "total": len(data["list"])}
            return {
                "message": live.get("message") or "Client list fetched",
                "code": 0,
                "error": False,
                "data": data,
            }

    db = get_db()
    bet_q: dict = _market_scope_filter(market_id) if market_id else {}
    user_ids = db.sports_bets.distinct("userId", bet_q)
    clients: list[dict] = []
    for uid in user_ids:
        if not uid:
            continue
        user = _find_user(db, str(uid)) or {}
        if user.get("userType") != "client":
            continue
        clients.append({
            "clientId": str(uid),
            "userInfo": {
                "userId": str(uid),
                "username": user.get("username") or str(uid),
                "name": user.get("name") or "",
                "userType": user.get("userType") or "client",
            },
        })
    clients.sort(key=lambda c: str(c.get("userInfo", {}).get("username") or ""))
    return {
        "message": "Client list fetched",
        "code": 0,
        "error": False,
        "data": clients,
    }


def mongo_admin_odds_position(payload: dict, session_user: dict = None) -> dict:
    market_id = str(payload.get("marketId") or "")
    token = (session_user or {}).get("_token") or ""
    if not ADMIN_MONGO_ONLY and market_id:
        live = post_live_api("sports/getOddsPosition", payload, token)
        if live and not live.get("error") and isinstance(live.get("data"), list):
            return _ok_list(live["data"], live.get("message") or "Position fetched")

    db = get_db()
    prefer_live = False if ADMIN_MONGO_ONLY else None
    match = fetch_match_by_market_id(
        market_id, str(payload.get("eventId") or ""), token, prefer_live=prefer_live
    )

    if payload.get("userId"):
        uid = str(payload["userId"])
        pos = db.positions.find_one({"userId": uid, "marketId": market_id}, {"_id": 0}) or {}
        runners = remap_to_admin_display_ids(match, pos.get("runners") or {})
        if not runners:
            bet_q = _open_odds_bets_query(market_id, {"userId": uid})
            for bet in db.sports_bets.find(bet_q, {"_id": 0}):
                if is_fancy_market(
                    str(bet.get("betFor") or ""),
                    str(bet.get("oddsType") or ""),
                    str(bet.get("gtype") or ""),
                ):
                    continue
                pos_info = bet.get("positionInfo") or {}
                if match and pos_info:
                    pos_info = remap_bookmaker_runners(match, pos_info)
                pos_info = remap_to_admin_display_ids(match, pos_info)
                runners = merge_positions(runners, pos_info)
        rows = [
            {"_id": k, "selectionId": int(k) if str(k).isdigit() else k, "Position": round(_num(v), 2)}
            for k, v in runners.items()
        ]
        stake_by_sel = _stake_totals_by_selection(market_id, match, uid)
        rows = _enrich_odds_position_rows(rows, stake_by_sel, match)
        return _ok_list(rows, "Position fetched")

    rows = _aggregate_admin_odds_positions(market_id, match)
    return _ok_list(rows, "Position fetched")


def mongo_admin_session_position(payload: dict, _session_user: dict = None) -> dict:
    pos_map = compute_fancy_run_position_map(payload or {})
    if pos_map:
        return _ok_data(pos_map, "Session position fetched")

    rows = compute_fancy_session_positions(payload or {})
    if rows:
        return _ok_list(rows, "Session position fetched")

    db = get_db()
    q: dict = {}
    if payload.get("marketId"):
        q["marketId"] = str(payload["marketId"])
    if payload.get("selectionId") is not None:
        q["selectionId"] = payload["selectionId"]
    legacy = [_strip_mongo(p) for p in db.positions.find(q)]
    if legacy and isinstance(legacy[0], dict) and legacy[0].get("runners"):
        return _ok_data(legacy[0].get("runners") or {}, "Session position fetched")
    return _ok_data({}, "Session position fetched")


def mongo_admin_casino_round_result(payload: dict, _session_user: dict = None) -> dict:
    db = get_db()
    q: dict = {}
    if payload.get("eventId") is not None:
        q["eventId"] = payload["eventId"]
    if payload.get("roundId"):
        q["roundId"] = payload["roundId"]
    rows = [_strip_mongo(r) for r in db.casino_rounds.find(q)]
    return _ok_list(rows, "Round result fetched")


def mongo_admin_casino_by_event(payload: dict, _session_user: dict = None) -> dict:
    from mongodb.casino_api import find_casino_game_by_event_id, staff_diamond_casino_games

    event_id = (payload or {}).get("eventId")
    if event_id is not None:
        game = find_casino_game_by_event_id(event_id)
        if game:
            return _ok_data(game, "data fetched")
        return {"message": "Game not found", "code": 1, "error": True, "data": {}}
    return _ok_data(staff_diamond_casino_games(), "data fetched")


def mongo_admin_day_wise_casino_report(payload: dict, _session_user: dict = None) -> dict:
    rows = _computed_or_reports("casino/dayWiseCasinoReport", payload, compute_day_wise_casino)
    if not rows:
        db = get_db()
        rows = [_strip_mongo(d) for d in db.day_wise_casino.find({})]
    return _ok_list(rows, "Casino report fetched")


def mongo_admin_casino_report_by_user(payload: dict, _session_user: dict = None) -> dict:
    payload = payload or {}
    if payload.get("eventId") is not None:
        rows, total = compute_casino_bet_report(payload)
        if not rows:
            rows = _reports_payload("casino/diamondCasinoReportByUser", payload)
            total = len(rows)
        return {
            "message": "Diamond Casino Bet List Fetch Successfully",
            "code": 0,
            "error": False,
            "data": {"casinoBetData": rows, "totalCasinoCount": total},
        }
    rows = compute_casino_completed_list(payload)
    if not rows:
        rows = _computed_or_reports("casino/diamondCasinoReportByUser", payload, compute_casino_report_by_user)
    page = max(int(payload.get("pageNo") or 1), 1)
    size = max(int(payload.get("size") or 100), 1)
    total = len(rows)
    page_rows = rows[(page - 1) * size: page * size]
    return {
        "message": "Casino user report fetched",
        "code": 0,
        "error": False,
        "data": page_rows,
        "total": total,
    }


def mongo_admin_casino_plus_minus(payload: dict, _session_user: dict = None) -> dict:
    data = compute_casino_plus_minus(payload or {})
    if not data:
        rows = _reports_payload("casino/getPlusMinusCasinoDetail", payload or {})
        if rows and isinstance(rows[0], dict):
            data = rows[0]
    if isinstance(data, dict):
        return _ok_data(data, "Plus minus fetched")
    return _ok_list(data if isinstance(data, list) else [], "Plus minus fetched")


def mongo_admin_casino_profit_loss_pos(payload: dict, _session_user: dict = None) -> dict:
    rows = _computed_or_reports("casino/getProfitLossPos", payload, compute_casino_profit_loss_pos)
    return _ok_list(rows, "Profit loss position fetched")


def mongo_admin_casino_realtime_pos(payload: dict, _session_user: dict = None) -> dict:
    rows = _computed_or_reports("casino/realTimeDataPosDataDiamondCasino", payload, compute_casino_realtime_pos)
    if rows:
        return _ok_list(rows, "Realtime data fetched")
    return _ok_obj("Realtime data fetched")


def mongo_admin_matka_day_wise(_payload: dict, _session_user: dict = None) -> dict:
    db = get_db()
    rows = [_strip_mongo(e) for e in db.matka_events.find({})]
    return _ok_list(rows, "Matka report fetched")


def mongo_admin_matka_profit_loss(payload: dict, _session_user: dict = None) -> dict:
    rows = _computed_or_reports("matka/getProfitLossPosMatka", payload, compute_matka_profit_loss)
    return _ok_list(rows, "Matka P/L fetched")


def _computed_or_object(report_type: str, payload: dict, compute_fn) -> dict:
    data = compute_fn(payload or {})
    if data:
        return data if isinstance(data, dict) else {}
    rows = _reports_payload(report_type, payload or {})
    if rows and isinstance(rows[0], dict):
        return rows[0]
    return {}


def mongo_admin_session_list(payload: dict, _session_user: dict = None) -> dict:
    market_id = str((payload or {}).get("marketId") or "")
    rows = compute_session_list(market_id)
    return _ok_list(rows, "Session list fetched")


def mongo_admin_plus_minus_market(payload: dict, _session_user: dict = None) -> dict:
    data = _computed_or_object("decision/getPlusMinusByMarketId", payload, compute_plus_minus_market)
    return _ok_data(data, "Plus minus by market fetched")


def mongo_admin_plus_minus_user_wise(payload: dict, _session_user: dict = None) -> dict:
    rows = _computed_or_reports(
        "reports/getPlusMinusByMarketIdByUserWise", payload, compute_plus_minus_user_wise
    )
    return _ok_list(rows, "Plus minus user wise fetched")


def mongo_admin_client_plus_minus(payload: dict, _session_user: dict = None) -> dict:
    rows = _computed_or_reports("bluexchReports/clientPlusMinus", payload, compute_client_plus_minus)
    return _ok_list(rows, "Client plus minus fetched")


def mongo_admin_profit_loss_report(payload: dict, session_user: dict = None) -> dict:
    payload = payload or {}
    session_user = session_user or {}
    rows = compute_user_profit_loss(payload, session_user)
    if not rows:
        cached = _reports_payload("reports/userProfitLoss", payload)
        if cached and isinstance(cached[0], dict) and "userNetProfitLoss" in cached[0]:
            rows = cached
    return {"message": False, "code": 0, "error": False, "data": rows}


def mongo_admin_block_market(payload: dict, session_user: dict = None) -> dict:
    payload = payload or {}
    market_id = str(payload.get("marketId") or "")
    db = get_db()

    # List blocked markets (dashboard / inplay) — sirf matches collection (source of truth)
    if not market_id:
        blocked: dict[str, bool] = {}
        for doc in db.matches.find(
            {"$or": [{"isBlocked": True}, {"betPerm": False}]},
            {"_id": 0, "marketId": 1, "isBlocked": 1, "betPerm": 1},
        ):
            mid = str(doc.get("marketId") or "")
            if not mid:
                continue
            if doc.get("isBlocked") is True or doc.get("betPerm") is False:
                blocked[mid] = True
        return _ok_data(blocked, "Block market list fetched")

    status = bool(payload.get("status", payload.get("blockStatus", False)))
    res = db.matches.update_one(
        {"marketId": market_id},
        {"$set": {
            "betPerm": not status,
            "isBlocked": status,
            "updatedAt": _now(),
        }},
    )
    if res.matched_count == 0:
        db.matches.update_one(
            {"marketList.marketId": market_id},
            {"$set": {
                "betPerm": not status,
                "isBlocked": status,
                "updatedAt": _now(),
            }},
        )
    if status:
        db.reports.update_one(
            {"reportType": "reports/blockMarket", "marketId": market_id},
            {"$set": {
                "reportType": "reports/blockMarket",
                "marketId": market_id,
                "status": True,
                "payload": {"marketId": market_id, "status": True},
                "updatedAt": _now(),
            }},
            upsert=True,
        )
    else:
        db.reports.delete_one({"reportType": "reports/blockMarket", "marketId": market_id})
    return _ok_obj("Market status updated")


def mongo_admin_block_market_list(_payload: dict, _session_user: dict = None) -> dict:
    """reports/userWiseBlockMarketList — blocked markets list."""
    return mongo_admin_block_market({}, _session_user)


def mongo_admin_ledger_from_entries(payload: dict, session_user: dict = None) -> dict:
    """user/userLedger — bets + ledger_entries se My Ledger build."""
    payload = payload or {}
    session_user = session_user or {}
    view_uid = str(
        payload.get("userId")
        or payload.get("downlineUserId")
        or session_user.get("userId")
        or ""
    )
    if view_uid and session_user and not _can_manage_user(session_user, view_uid):
        return {"message": "Ledger List fetched Successfully", "code": 0, "error": False, "data": _empty_user_ledger()}
    data = compute_user_ledger(payload, session_user)
    return {"message": "Ledger List fetched Successfully", "code": 0, "error": False, "data": data}


def mongo_admin_ledger_credit_debit(payload: dict, session_user: dict) -> dict:
    payload = payload or {}
    uid = str(payload.get("userId") or payload.get("downlineUserId") or "")
    amount = _num(payload.get("amount", payload.get("coins", 0)))
    entry_type = str(payload.get("type") or payload.get("paymentType") or "credit").lower()
    if entry_type in ("dena", "debit", "withdraw"):
        entry_type = "debit"
    else:
        entry_type = "credit"
    if not uid or amount <= 0:
        return {"message": "userId and amount required", "code": 1, "error": True, "data": {}}

    db = get_db()
    target = _find_user(db, uid)
    if not target:
        return {"message": "User not found", "code": 1, "error": True, "data": {}}
    if session_user and not _can_manage_user(session_user, target["userId"]):
        return {"message": "Not authorised", "code": 1, "error": True, "data": {}}

    now = _now()
    ledger_id = uuid.uuid4().hex[:24]
    cat = payload.get("category") or "cash"
    coins_delta = amount if entry_type == "credit" else -amount
    new_balance = _num(target.get("coins", 0)) + coins_delta
    if new_balance < 0:
        return {"message": "Insufficient balance", "code": 1, "error": True, "data": {}}

    db.ledger_entries.insert_one({
        "ledgerId": ledger_id,
        "userId": target["userId"],
        "type": entry_type,
        "amount": amount,
        "description": payload.get("description") or payload.get("remark") or "Ledger entry",
        "remark": payload.get("remark", ""),
        "category": cat,
        "balanceAfter": new_balance,
        "createdAt": now,
    })
    db.users.update_one(
        {"userId": target["userId"]},
        {"$inc": {"coins": coins_delta, "balance": coins_delta}, "$set": {"updatedAt": now}},
    )
    return _ok_data({"ledgerId": ledger_id, "message": "Ledger updated"}, "Ledger updated successfully")


def mongo_admin_delete_ledger(payload: dict, session_user: dict) -> dict:
    payload = payload or {}
    if not _is_owner_user(session_user):
        return {"message": "Only owner can delete bets", "code": 1, "error": True, "data": {}}

    bet_id = str(payload.get("betId") or "")
    downline_uid = str(payload.get("downlineUserId") or "")
    if bet_id:
        db = get_db()
        for coll in ("sports_bets", "casino_bets", "matka_bets"):
            bet = db[coll].find_one({"betId": bet_id})
            if not bet:
                continue
            bet_uid = str(bet.get("userId") or "")
            if downline_uid and bet_uid != downline_uid:
                return {"message": "Bet does not belong to selected user", "code": 1, "error": True, "data": {}}
            if session_user and bet_uid and not _can_manage_user(session_user, bet_uid):
                return {"message": "Not authorised", "code": 1, "error": True, "data": {}}
            db[coll].update_one(
                {"betId": bet_id},
                {"$set": {"isDeleted": True, "deletedAt": _now(), "deletedBy": session_user.get("userId", "")}},
            )
            return _ok_obj("Bet deleted")
        return {"message": "Bet not found", "code": 1, "error": True, "data": {}}

    ledger_id = payload.get("ledgerId") or payload.get("_id")
    if not ledger_id:
        return {"message": "ledgerId or betId required", "code": 1, "error": True, "data": {}}
    db = get_db()
    entry = db.ledger_entries.find_one({"ledgerId": ledger_id})
    if not entry:
        return {"message": "Ledger not found", "code": 1, "error": True, "data": {}}
    if session_user and not _can_manage_user(session_user, entry.get("userId", "")):
        return {"message": "Not authorised", "code": 1, "error": True, "data": {}}
    db.ledger_entries.delete_one({"ledgerId": ledger_id})
    return _ok_obj("Ledger deleted")


def mongo_admin_lena_dena(payload: dict, session_user: dict) -> dict:
    """Lena/Dena — fetch list (cash-transaction) ya do users ke beech transfer."""
    payload = payload or {}
    if payload.get("fetchUserType"):
        rows = compute_lena_dena_list(payload, session_user)
        return _ok_list(rows, "Lena dena fetched")

    from_id = str(payload.get("fromUserId") or payload.get("userId") or "")
    to_id = str(payload.get("toUserId") or payload.get("downlineUserId") or "")
    amount = _num(payload.get("amount", payload.get("coins", 0)))
    if not from_id or not to_id or amount <= 0:
        return {"message": "fromUserId, toUserId, amount required", "code": 1, "error": True, "data": {}}

    db = get_db()
    sender = _find_user(db, from_id)
    receiver = _find_user(db, to_id)
    if not sender or not receiver:
        return {"message": "User not found", "code": 1, "error": True, "data": {}}
    if session_user and not _can_manage_user(session_user, sender["userId"]):
        return {"message": "Not authorised", "code": 1, "error": True, "data": {}}
    owner_unlimited = _is_owner_user(sender) or _is_owner_user(session_user)
    if not owner_unlimited and _num(sender.get("coins", 0)) < amount:
        return {"message": "Insufficient balance", "code": 1, "error": True, "data": {}}

    now = _now()
    sender_mints = _owner_mints_coins(sender)
    sender_coins = _num(sender.get("coins", 0))
    if not sender_mints:
        db.users.update_one({"userId": sender["userId"]}, {"$inc": {"coins": -amount, "balance": -amount}, "$set": {"updatedAt": now}})
    db.users.update_one({"userId": receiver["userId"]}, {"$inc": {"coins": amount, "balance": amount}, "$set": {"updatedAt": now}})
    record_coin_transfer(
        db,
        debit_user={**sender, "coins": sender_coins if sender_mints else sender_coins - amount},
        credit_user={**receiver, "coins": _num(receiver.get("coins", 0)) + amount},
        amount=amount,
        transfer_type="transfer",
        debit_description=f"Transfer to {receiver.get('username', to_id)}",
        credit_description=f"Transfer from {sender.get('username', from_id)}",
        category="transfer",
        created_at=now,
    )
    return _ok_list([], "Transfer successful")


def mongo_admin_user_commission_report(payload: dict, session_user: dict = None) -> dict:
    rows = compute_user_commission_report(payload, session_user)
    return _ok_list(rows, "Commission report fetched")


def mongo_admin_commission_list_by_user(payload: dict, session_user: dict = None) -> dict:
    rows = compute_commission_list_by_user(payload, session_user)
    return _ok_list(rows, "Commission list fetched")


def mongo_admin_reset_comm_list(payload: dict, _session_user: dict = None) -> dict:
    rows = compute_reset_comm_list(payload)
    return _ok_list(rows, "Reset commission history fetched")


def mongo_admin_reset_comm(payload: dict, session_user: dict = None) -> dict:
    from mongodb.admin_commission import record_commission_reset
    return record_commission_reset(payload or {}, session_user or {})


def mongo_admin_user_statement(payload: dict, _session_user: dict = None) -> dict:
    rows = compute_user_statement(payload)
    return _ok_list(rows, "Statement fetched")


def mongo_admin_matka_list(payload: dict, _session_user: dict = None) -> dict:
    rows = compute_matka_list(payload)
    return _ok_list(rows, "Matka list fetched")


def mongo_admin_matka_bet_list(payload: dict, _session_user: dict = None) -> dict:
    rows = compute_matka_bet_list(payload)
    return _ok_list(rows, "Matka bet list fetched")


def mongo_admin_casino_result_by_round(payload: dict, _session_user: dict = None) -> dict:
    rows = compute_casino_result_by_round(payload)
    return _ok_list(rows, "Round result fetched")
