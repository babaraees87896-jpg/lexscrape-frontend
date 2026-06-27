"""MongoDB bet placement — sports + casino (real 1ex99 JS logic)."""

from __future__ import annotations

import json
import re
import secrets
import requests
import uuid
from datetime import datetime, timezone
from typing import Any, Optional, Tuple

from mongodb.auth import _extract_bearer, resolve_client_user, validate_session
from mongodb.bet_logic import (
    calc_casino_liability,
    calc_casino_position_delta,
    calc_liability,
    calc_net_exposure,
    calc_position_info,
    casino_runner_sids,
    fancy_bet_exposure,
    is_bookmaker_market,
    is_fancy_market,
    is_meter_khado_gtype,
    merge_positions,
    normalize_odds_type,
    position_info_for_client_bet,
    remap_bookmaker_runners,
    resolve_team_name,
    settle_casino_bet,
    settle_fancy_bet,
    settle_odds_bet,
)
from mongodb.db import get_db

BET_ENDPOINTS = frozenset({
    "sports/oddBetPlaced",
    "sports/sessionBetPlaced",
    "sports/meterKhadoOddEvenCricketCassinoBetPlace",
    "casino/casinoBetPlace",
    "casino/avaitorGamePlace",
    "casino/avaitorCashOut",
    "casino/avaitorRoundLost",
})

POSITION_ENDPOINTS = frozenset({
    "sports/userPositionByMarketId",
    "user/clientBetListByMarketId",
    "halkabhari/inplayOddsPositionHalkaBhari",
})


def _now():
    return datetime.now(timezone.utc)


def _bet_id(prefix: str = "bet") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _fail(message: str, code: int = 1) -> dict:
    return {"message": message, "code": code, "error": True, "data": {}}


def _require_user(auth_header: str, payload: Optional[dict] = None) -> Tuple[Optional[dict], Optional[dict]]:
    user, err = resolve_client_user(auth_header, payload, require_bet_access=True)
    if err:
        data = err.get("data")
        if isinstance(data, list):
            return None, _fail(err.get("message", "Error"), err.get("code", 1))
        return None, err if err.get("data") is not None else _fail(err.get("message", "Error"), err.get("code", 1))
    return user, None


def _session_user(auth_header: str, payload: Optional[dict] = None) -> Tuple[Optional[dict], Optional[dict]]:
    user, err = resolve_client_user(auth_header, payload)
    if err:
        if isinstance(err.get("data"), list):
            return None, _fail(err.get("message", "Error"), err.get("code", 401))
        return None, err
    return user, None


def _parse_amount(payload: dict) -> float:
    try:
        return float(payload.get("amount", payload.get("stake", 0)))
    except (TypeError, ValueError):
        return 0.0


def _parse_odds(payload: dict) -> float:
    try:
        return float(payload.get("odds", payload.get("rate", 0)))
    except (TypeError, ValueError):
        return 0.0


def _get_match(market_id: str, event_id: str = "") -> Optional[dict]:
    from mongodb.matches_api import _find_match_local, get_match_list

    mid = str(market_id or "")
    evt = str(event_id or "")
    db = get_db()
    m = db.matches.find_one({"marketId": mid}, {"_id": 0}) if mid else None
    if m:
        return m
    if mid:
        for row in db.matches.find({}, {"_id": 0}):
            if str(row.get("marketId")) == mid:
                return row
    found = _find_match_local(mid, evt)
    if found:
        return found
    try:
        for row in get_match_list({}, prefer_live=True):
            if mid and str(row.get("marketId")) == mid:
                return row
            if evt and str(row.get("eventId")) == evt:
                return row
    except Exception:
        pass
    return None


def _sync_event_position_from_bets(user_id: str, market_id: str, event_id: str = "") -> dict[str, float]:
    """Open bets se merged position — scraped site jaisa net P/L keys (1/2)."""
    evt = str(event_id or _event_id_for_market(market_id) or "")
    q: dict = {"userId": user_id, "status": "open", "marketId": str(market_id)}
    if evt:
        q["eventId"] = evt
    match = _get_match(str(market_id), evt)
    merged: dict[str, float] = {}
    for bet in get_db().sports_bets.find(q, {"_id": 0}):
        if is_fancy_market(
            str(bet.get("betFor") or ""),
            str(bet.get("oddsType") or ""),
            str(bet.get("gtype") or ""),
        ):
            continue
        merged = merge_positions(merged, position_info_for_client_bet(bet, match))
    pos_key: dict = {"userId": user_id}
    if evt:
        pos_key["eventId"] = evt
    else:
        pos_key["marketId"] = str(market_id)
    if not merged:
        get_db().positions.delete_one(pos_key)
        return merged

    get_db().positions.update_one(
        pos_key,
        {
            "$set": {
                "userId": user_id,
                "eventId": evt or None,
                "marketId": str(market_id),
                "runners": merged,
                "updatedAt": _now(),
            }
        },
        upsert=True,
    )
    return merged


def _odds_position_rows_from_bets(user_id: str) -> list[dict]:
    """Saari open odds/bookmaker bets → per-event merged runners (exposure ke liye)."""
    groups: dict[str, dict[str, float]] = {}
    for bet in get_db().sports_bets.find({"userId": user_id, "status": "open"}, {"_id": 0}):
        if is_fancy_market(
            str(bet.get("betFor") or ""),
            str(bet.get("oddsType") or ""),
            str(bet.get("gtype") or ""),
        ):
            continue
        mid = str(bet.get("marketId") or "")
        evt = str(bet.get("eventId") or _event_id_for_market(mid) or mid)
        match = _get_match(mid, evt)
        pi = position_info_for_client_bet(bet, match)
        if not pi:
            continue
        groups[evt] = merge_positions(groups.get(evt) or {}, pi)
    return [{"eventId": k, "runners": v} for k, v in groups.items()]


def _fetch_cache_team_data(market_id: str, match: Optional[dict] = None) -> list[dict]:
    """Live cache team_data — team_name ke liye (match detail bet list)."""
    import os

    m = match or _get_match(market_id)
    urls: list[str] = []
    if m:
        for key in ("cacheUrl", "checkOddsUrl"):
            val = m.get(key)
            if val:
                urls.append(str(val))
    if market_id:
        urls.append(f"https://1excache.tresting.com/v2/api/oddsDataNew?market_id={market_id}")
    port = os.environ.get("EX99_PORT", "8899")
    for url in dict.fromkeys(urls):
        try:
            fetch_url = url
            if fetch_url.startswith("/excache/"):
                fetch_url = f"http://127.0.0.1:{port}{fetch_url}"
            resp = requests.get(fetch_url, timeout=5)
            data = resp.json()
            result = data.get("result") if isinstance(data, dict) else None
            if not isinstance(result, dict):
                result = data if isinstance(data, dict) else {}
            rows = result.get("team_data") or result.get("teamData") or []
            if isinstance(rows, list) and rows:
                return rows
        except Exception:
            continue
    return []


def _resolve_runner_name(
    match: Optional[dict],
    selection_id: Any,
    payload_name: str = "",
    market_id: str = "",
) -> str:
    mid = market_id or str((match or {}).get("marketId") or "")
    cache_teams = _fetch_cache_team_data(mid, match) if mid else []
    return resolve_team_name(
        match,
        selection_id,
        payload_name=payload_name,
        cache_teams=cache_teams,
    )


def _session_cache_rows(match: Optional[dict], market_id: str, event_id: str) -> list[dict]:
    urls: list[str] = []
    if match:
        for key in ("cacheUrl", "checkOddsUrl"):
            if match.get(key):
                urls.append(str(match[key]))
    if market_id:
        urls.append(f"https://1excache.tresting.com/v2/api/oddsDataNew?market_id={market_id}")

    for url in dict.fromkeys(urls):
        try:
            resp = requests.get(url, timeout=3)
            data = resp.json()
        except Exception:
            continue
        result = data.get("result") if isinstance(data, dict) else None
        rows = result.get("session") if isinstance(result, dict) else None
        if isinstance(rows, list) and rows:
            if event_id:
                scoped = [r for r in rows if str(r.get("eventId") or "") == str(event_id)]
                if scoped:
                    return scoped
            return rows
    return []


def _session_row_name(row: dict) -> str:
    return str(
        row.get("session_name")
        or row.get("sessionName")
        or row.get("fancyName")
        or row.get("runnerName")
        or row.get("nat")
        or ""
    ).strip()


def _resolve_session_name(
    match: Optional[dict],
    market_id: str,
    event_id: str,
    selection_id: Any,
    run: str,
    odds: float,
    bet_type: str,
) -> str:
    rows = _session_cache_rows(match, market_id, event_id)
    if not rows:
        return ""

    sid = str(selection_id or "")
    for row in rows:
        row_ids = {
            str(row.get("Selection_id") or ""),
            str(row.get("selectionId") or ""),
            str(row.get("selection_id") or ""),
            str(row.get("session_id") or ""),
        }
        if sid and sid in row_ids:
            return _session_row_name(row)

    try:
        run_num = int(float(run))
        odds_num = round(float(odds), 2)
    except (TypeError, ValueError):
        return ""

    bt = (bet_type or "").upper()
    candidates = []
    for row in rows:
        row_run = row.get("runsYes") if bt == "Y" else row.get("runsNo")
        row_odds = row.get("oddsYes") if bt == "Y" else row.get("oddsNo")
        try:
            if int(float(row_run)) == run_num and round(float(row_odds), 2) == odds_num:
                candidates.append(row)
        except (TypeError, ValueError):
            continue
    if len(candidates) == 1:
        return _session_row_name(candidates[0])
    return ""


def _parse_mmc(match: Optional[dict]) -> dict:
    if not match:
        return {}
    mmc = match.get("maxMinCoins") or {}
    if isinstance(mmc, dict):
        return mmc
    if not isinstance(mmc, str) or not mmc.strip():
        return {}
    try:
        return json.loads(mmc)
    except json.JSONDecodeError:
        fixed = re.sub(r"(\w+):", r'"\1":', mmc.strip())
        try:
            return json.loads(fixed)
        except json.JSONDecodeError:
            return {}


def _row_max_value(row: dict) -> float:
    for key in ("max", "maximumAmount", "maximum_amount"):
        try:
            val = float(row.get(key) or 0)
        except (TypeError, ValueError):
            val = 0.0
        if val > 0:
            return val
    return 0.0


def _session_row_ids(row: dict) -> set[str]:
    ids: set[str] = set()
    for key in (
        "Selection_id",
        "selectionId",
        "selection_id",
        "session_id",
        "fancyId",
        "diamondSelectionId",
    ):
        val = row.get(key)
        if val is not None and str(val).strip():
            ids.add(str(val))
    return ids


def _max_from_session_rows(
    rows: list,
    selection_id: Any,
    run: str = "",
    odds: float = 0,
    bet_type: str = "",
) -> float:
    if not rows:
        return 0.0
    sid = str(selection_id or "")
    for row in rows:
        if not isinstance(row, dict):
            continue
        if sid and sid in _session_row_ids(row):
            val = _row_max_value(row)
            if val > 0:
                return val

    try:
        run_num = int(float(run))
        odds_num = round(float(odds), 2)
    except (TypeError, ValueError):
        return 0.0

    bt = (bet_type or "").upper()
    candidates = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_run = row.get("runsYes") if bt == "Y" else row.get("runsNo")
        row_odds = row.get("oddsYes") if bt == "Y" else row.get("oddsNo")
        try:
            if int(float(row_run)) == run_num and round(float(row_odds), 2) == odds_num:
                candidates.append(row)
        except (TypeError, ValueError):
            continue
    if len(candidates) == 1:
        return _row_max_value(candidates[0])
    return 0.0


def _session_row_max(
    match: Optional[dict],
    selection_id: Any,
    *,
    market_id: str = "",
    event_id: str = "",
    run: str = "",
    odds: float = 0,
    bet_type: str = "",
    payload_max: Any = None,
) -> float:
    """Per-row session max — MongoDB match, live odds cache, phir UI payload."""
    if selection_id is None or selection_id == "":
        return 0.0

    try:
        client_max = float(payload_max or 0)
    except (TypeError, ValueError):
        client_max = 0.0
    if client_max > 0:
        return client_max

    row_sources: list[list] = []
    if match:
        for key in ("fancyList", "sessionList", "fancy", "session"):
            rows = match.get(key)
            if isinstance(rows, list) and rows:
                row_sources.append(rows)

    mid = market_id or str((match or {}).get("marketId") or "")
    evt = event_id or str((match or {}).get("eventId") or "")

    if mid:
        try:
            from mongodb.centerpanel_cache import fetch_odds_cache

            cache = fetch_odds_cache(mid)
            cache_rows = cache.get("session")
            if isinstance(cache_rows, list) and cache_rows:
                if evt:
                    scoped = [r for r in cache_rows if str(r.get("eventId") or "") == str(evt)]
                    row_sources.append(scoped if scoped else cache_rows)
                else:
                    row_sources.append(cache_rows)
        except Exception:
            pass

        live_rows = _session_cache_rows(match, mid, evt)
        if live_rows:
            row_sources.append(live_rows)

    for rows in row_sources:
        val = _max_from_session_rows(rows, selection_id, run, odds, bet_type)
        if val > 0:
            return val
    return 0.0


def _min_stake(match: Optional[dict], odds_type: str) -> float:
    mmc = _parse_mmc(match)
    if not mmc:
        return 100.0
    if odds_type == "fancy":
        return float(mmc.get("minimum_session_bet") or mmc.get("minimum_match_bet") or 100)
    return float(mmc.get("minimum_match_bet") or 100)


def _max_stake(
    match: Optional[dict],
    odds_type: str,
    bet_for: str,
    gtype: str = "",
    selection_id: Any = None,
    *,
    market_id: str = "",
    event_id: str = "",
    run: str = "",
    odds: float = 0,
    bet_type: str = "",
    payload_max: Any = None,
) -> Optional[float]:
    """Session/bookmaker max — scraped UI: row max + maxMinCoins."""
    mmc = _parse_mmc(match)
    bet_for = str(bet_for or "")
    odds_type = str(odds_type or "")

    if is_fancy_market(bet_for, odds_type, gtype):
        session_max = _session_row_max(
            match,
            selection_id,
            market_id=market_id,
            event_id=event_id,
            run=run,
            odds=odds,
            bet_type=bet_type,
            payload_max=payload_max,
        )
        global_max = float(mmc.get("maximum_session_bet") or 0)
        if session_max > 0:
            return session_max
        if global_max > 0:
            return global_max
        return None

    if odds_type == "bookmaker" or is_bookmaker_market(bet_for, odds_type):
        bm_max = float(mmc.get("maximum_bookmaker_coins") or 0)
        match_max = float(mmc.get("maximum_match_bet") or 0)
        if bm_max > 0:
            return bm_max
        if match_max > 0:
            return match_max
        return None

    return None


def _event_id_for_market(market_id: str) -> str:
    if not market_id:
        return ""
    match = get_db().matches.find_one({"marketId": str(market_id)}, {"eventId": 1, "_id": 0})
    if match and match.get("eventId") is not None:
        return str(match["eventId"])
    return ""


def _open_bets_query(user_id: str, market_id: str, payload: Optional[dict] = None) -> dict:
    """Sirf isi event ki open bets — dusre event ka exposure mix na ho."""
    event_id = ""
    if payload:
        event_id = str(payload.get("eventId") or "")
    if not event_id:
        event_id = _event_id_for_market(market_id)
    q: dict = {"userId": user_id, "marketId": str(market_id), "status": "open"}
    if event_id:
        q["eventId"] = event_id
    return q


def _market_bets_query(user_id: str, market_id: str, payload: Optional[dict] = None) -> dict:
    """Open + settled bets for market bet list (fancy Complete Session table)."""
    event_id = ""
    if payload:
        event_id = str(payload.get("eventId") or "")
    if not event_id:
        event_id = _event_id_for_market(market_id)
    q: dict = {"userId": user_id, "marketId": str(market_id)}
    if event_id:
        q["eventId"] = event_id
    return q


def _bet_is_declared(bet: dict) -> bool:
    if bet.get("isDeclare") in (True, 1, "1"):
        return True
    if str(bet.get("status") or "").lower() in ("settled", "won", "lost", "declared"):
        return True
    return False


def _calc_user_exposure(
    user_id: str,
    pending_market_id: str = "",
    pending_event_id: str = "",
    pending_position_delta: Optional[dict] = None,
    pending_fancy_bet: Optional[dict] = None,
) -> float:
    db = get_db()
    positions = _odds_position_rows_from_bets(user_id)
    if pending_position_delta:
        evt = pending_event_id or _event_id_for_market(pending_market_id)
        found = False
        for pos in positions:
            if evt and str(pos.get("eventId")) == str(evt):
                pos["runners"] = merge_positions(pos.get("runners") or {}, pending_position_delta)
                found = True
                break
            if not evt and str(pos.get("marketId")) == str(pending_market_id):
                pos["runners"] = merge_positions(pos.get("runners") or {}, pending_position_delta)
                found = True
                break
        if not found:
            positions.append({"runners": pending_position_delta})
    fancy = list(db.sports_bets.find(
        {
            "userId": user_id,
            "status": "open",
            "$or": [
                {"marketKind": "fancy"},
                {"betFor": "fancy"},
                {"oddsType": {"$in": ["fancy", "session"]}},
            ],
        },
        {"_id": 0},
    ))
    sports_exp = calc_net_exposure(positions, fancy)
    if pending_fancy_bet:
        odds_exp = calc_net_exposure(positions, [])
        sports_exp = round(odds_exp + fancy_bet_exposure(fancy, pending_fancy_bet), 2)
    casino_exp = _total_casino_exposure(user_id)
    return round(sports_exp + casino_exp, 2)


def _open_casino_bets(user_id: str) -> list[dict]:
    return list(get_db().casino_bets.find(
        {"userId": user_id, "status": "open"},
        {"_id": 0},
    ))


def _casino_bet_exposure(bet: dict) -> float:
    return round(float(bet.get("liability") or bet.get("stake") or 0), 2)


def _total_casino_exposure(user_id: str, pending: float = 0) -> float:
    exp = sum(_casino_bet_exposure(b) for b in _open_casino_bets(user_id))
    return round(exp + pending, 2)


def _apply_exposure_delta(user_id: str, new_exposure: float) -> Tuple[Optional[float], Optional[str]]:
    """
    Real site model: coins = available, exposure = locked.
    total credit = coins + exposure (constant until P/L).
    """
    db = get_db()
    user = db.users.find_one({"userId": user_id})
    if not user:
        return None, "User not found"
    old_exposure = float(user.get("exposure") or 0)
    coins = float(user.get("coins") or 0)
    delta = round(new_exposure - old_exposure, 2)
    if delta > 0 and delta > coins + 0.001:
        return None, "Insufficient balance"
    new_coins = round(coins - delta, 2)
    db.users.update_one(
        {"userId": user_id},
        {"$set": {"coins": new_coins, "balance": new_coins, "exposure": new_exposure, "updatedAt": _now()}},
    )
    return new_coins, None


def _refresh_user_exposure(user_id: str) -> float:
    exposure = _calc_user_exposure(user_id)
    _apply_exposure_delta(user_id, exposure)
    return exposure


def _user_credit_limit(user: dict, computed_exposure: Optional[float] = None) -> float:
    """Total wallet credit — scraped site: CHIPS + EXP = fixed limit."""
    limit = user.get("creditLimit")
    if limit is not None and float(limit) > 0:
        return round(float(limit), 2)
    exp = computed_exposure
    if exp is None:
        exp = _calc_user_exposure(user["userId"])
    return round(float(user.get("coins") or 0) + float(exp), 2)


def sync_user_balance(user_id: str) -> Tuple[float, float]:
    """
    Positions se net exposure — MatchDetail JS position merge + worst-case loss.
    CHIPS = creditLimit - exposure
    """
    db = get_db()
    user = db.users.find_one({"userId": user_id})
    if not user:
        return 0.0, 0.0
    new_exposure = round(_calc_user_exposure(user_id), 2)
    credit = _user_credit_limit(user, new_exposure)
    new_coins = round(credit - new_exposure, 2)
    if new_coins < -0.001:
        new_coins = 0.0
        new_exposure = credit
    db.users.update_one(
        {"userId": user_id},
        {"$set": {"coins": new_coins, "balance": new_coins, "exposure": new_exposure, "updatedAt": _now()}},
    )
    return new_coins, new_exposure


def _update_market_position(user_id: str, market_id: str, event_id: str, delta: dict):
    """Bet ke baad poori event position open bets se rebuild — galat keys mix na hon."""
    _sync_event_position_from_bets(user_id, market_id, event_id)


def _ledger(user_id: str, amount: float, description: str, category: str, balance_after: float, *, entry_type: str = "debit"):
    get_db().ledger_entries.insert_one({
        "ledgerId": f"led-{secrets.token_hex(6)}",
        "userId": user_id,
        "type": entry_type,
        "amount": amount,
        "description": description,
        "category": category,
        "balanceAfter": balance_after,
        "createdAt": _now(),
    })


def _sports_success(user_id: str, message: str = "Bet placed successfully") -> dict:
    coins, exposure = sync_user_balance(user_id)
    return {
        "message": message,
        "code": 0,
        "error": False,
        "data": {
            "totalCoins": coins,
            "exposure": exposure,
        },
    }


def _casino_success(user_id: str, message: str = "Bet placed successfully") -> dict:
    coins, exposure = sync_user_balance(user_id)
    return {
        "message": message,
        "code": 0,
        "error": False,
        "data": {"totalCoins": coins, "exposure": exposure},
    }


def _aviator_success(user_id: str, bet_id: str, message: str = "Aviator Bet Placed Successfully") -> dict:
    """Scraped ha thunk expects o.user.data.betInsertId + balance fields."""
    user = get_db().users.find_one({"userId": user_id}) or {}
    coins = round(float(user.get("coins") or 0), 2)
    exposure = round(float(user.get("exposure") or 0), 2)
    return {
        "message": message,
        "code": 0,
        "error": False,
        "data": {
            "betInsertId": bet_id,
            "totalCoins": coins,
            "coins": str(coins),
            "exposure": str(exposure),
        },
    }


def _iso_created_at(value: Any) -> str:
    """MyBets moment.js — UTC ISO string with Z suffix."""
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.isoformat().replace("+00:00", "Z")
    text = str(value or "").strip()
    if text and not text.endswith("Z") and "+" not in text[-7:]:
        return text + "Z"
    return text


def _mybets_display_type(bet_type: Any) -> str:
    bt = str(bet_type or "L").upper()
    if bt == "B":
        return "L"
    return bt


def _mybets_list_err(err: dict) -> dict:
    body = dict(err)
    body["data"] = []
    return body


def _unsettled_only(payload: dict) -> bool:
    if payload.get("isDeclare") is False:
        return True
    return str(payload.get("isDeclare", "")).lower() == "false"


def _unsettled_sports_query(user_id: str, payload: dict) -> dict:
    query: dict = {"userId": user_id}
    if _unsettled_only(payload):
        query["status"] = "open"
        query["isDeclare"] = {"$ne": True}
    return query


def _unsettled_casino_query(user_id: str, payload: dict) -> dict:
    query: dict = {"userId": user_id}
    if _unsettled_only(payload):
        query["status"] = "open"
    return query


def _bet_to_odds_record(bet: dict, match: Optional[dict] = None) -> dict:
    if match is None and bet.get("marketId"):
        match = _get_match(str(bet["marketId"]), str(bet.get("eventId") or ""))
    position_info = position_info_for_client_bet(bet, match)
    team_name = _resolve_runner_name(
        match,
        bet.get("selectionId"),
        str(bet.get("runnerName") or ""),
        str(bet.get("marketId") or ""),
    )
    odds_type = str(bet.get("oddsType") or bet.get("betFor") or "bookmaker")
    declared = _bet_is_declared(bet)
    return {
        "betId": bet.get("betId"),
        "amount": bet.get("stake"),
        "odds": bet.get("odds"),
        "type": bet.get("betType"),
        "selectionId": bet.get("selectionId"),
        "marketId": bet.get("marketId"),
        "eventId": bet.get("eventId"),
        "betFor": bet.get("betFor"),
        "oddsType": odds_type,
        "teamName": team_name,
        "runnerName": team_name,
        "positionInfo": position_info,
        "isDeclare": 1 if declared else 0,
        "createdAt": _iso_created_at(bet.get("createdAt")),
        "run": bet.get("run") or "0",
        "gtype": bet.get("gtype") or bet.get("fancyType"),
        "profitLoss": bet.get("profitLoss") or 0,
        "decisionRun": bet.get("decisionRun") if declared else None,
    }


def _bet_to_fancy_record(bet: dict) -> dict:
    session_name = str(
        bet.get("sessionName")
        or bet.get("fancyName")
        or bet.get("marketName")
        or bet.get("runnerName")
        or ""
    ).strip()
    if session_name.lower() in ("", "fancy", "normal", "session"):
        session_name = f"Run {bet.get('run')}" if bet.get("run") not in (None, "") else "Session"
    declared = _bet_is_declared(bet)
    return {
        "betId": bet.get("betId"),
        "session": session_name,
        "sessionName": session_name,
        "fancyName": session_name,
        "market": session_name,
        "marketName": session_name,
        "name": session_name,
        "runnerName": session_name,
        "amount": bet.get("stake"),
        "rate": bet.get("odds"),
        "odds": bet.get("odds"),
        "type": bet.get("betType"),
        "selectionId": bet.get("selectionId"),
        "marketId": bet.get("marketId"),
        "eventId": bet.get("eventId"),
        "run": bet.get("run"),
        "gtype": bet.get("gtype") or bet.get("fancyType"),
        "isDeclare": 1 if declared else 0,
        "decisionRun": bet.get("decisionRun") if declared else None,
        "profitLoss": bet.get("profitLoss") or 0,
        "createdAt": _iso_created_at(bet.get("createdAt")),
    }


def _parse_sports_payload(payload: dict) -> dict:
    """MatchDetail Fr=async() payload — scraped JS se."""
    bet_for = str(payload.get("betFor") or "")
    odds_type_raw = str(payload.get("oddsType") or bet_for or "match")
    gtype = str(payload.get("gtype") or payload.get("fancyType") or "")
    odds_type = normalize_odds_type(bet_for, odds_type_raw)
    name = next(
        (
            str(payload.get(key) or "").strip()
            for key in (
                "name",
                "runnerName",
                "sessionName",
                "fancyName",
                "marketName",
                "selectionName",
                "nat",
                "nation",
                "title",
            )
            if str(payload.get(key) or "").strip()
        ),
        "",
    )
    return {
        "amount": _parse_amount(payload),
        "odds": _parse_odds(payload),
        "market_id": str(payload.get("marketId") or ""),
        "selection_id": payload.get("selectionId"),
        "event_id": str(payload.get("eventId") or ""),
        "bet_type": str(payload.get("type") or payload.get("betType") or "L"),
        "bet_for": bet_for,
        "odds_type": odds_type,
        "odds_type_raw": odds_type_raw,
        "run": str(payload.get("run") if payload.get("run") is not None else "0"),
        "gtype": gtype,
        "betfair_market_id": str(payload.get("betfairMarketId") or ""),
        "name": name,
    }


def place_sports_bet(payload: dict, auth_header: str, endpoint: str = "") -> dict:
    user, err = _require_user(auth_header)
    if err:
        return err

    p = _parse_sports_payload(payload)
    amount = p["amount"]
    odds = p["odds"]
    market_id = p["market_id"]
    selection_id = p["selection_id"]
    event_id = p["event_id"]
    bet_type = p["bet_type"]
    odds_type = p["odds_type"]
    gtype = p["gtype"]

    if not market_id or not event_id or selection_id is None or selection_id == "":
        return _fail("Incomplete bet details. Please try again.")
    if odds <= 0:
        return _fail("Odds Are Equal to Zero")

    match = _get_match(market_id)
    if match and (match.get("isBlocked") or match.get("betPerm") is False):
        return _fail("Market is blocked")
    min_stake = _min_stake(match, odds_type)
    if amount < min_stake:
        return _fail(f"Please enter minimum amount: {int(min_stake)}")
    max_stake = _max_stake(
        match,
        odds_type,
        p["bet_for"],
        gtype,
        selection_id,
        market_id=market_id,
        event_id=event_id,
        run=p["run"],
        odds=odds,
        bet_type=bet_type,
        payload_max=payload.get("max"),
    )
    if max_stake is not None and amount > max_stake + 0.001:
        return _fail(f"Please enter maximum amount: {int(max_stake)}")

    liability = calc_liability(amount, odds, bet_type, odds_type, gtype, bet_for=p["bet_for"])
    user_id = user["userId"]
    resolved_name = _resolve_runner_name(match, selection_id, p["name"], market_id)
    if odds_type == "fancy" and resolved_name.lower() in ("", "fancy", "normal", "session"):
        resolved_name = _resolve_session_name(match, market_id, event_id, selection_id, p["run"], odds, bet_type)

    position_info = {}
    if not is_fancy_market(p["bet_for"], odds_type, gtype):
        position_info = calc_position_info(
            amount, odds, bet_type, odds_type, selection_id, match, bet_for=p["bet_for"],
        )

    pending_fancy_bet = None
    if odds_type == "fancy":
        pending_fancy_bet = {
            "marketId": market_id,
            "eventId": event_id,
            "selectionId": selection_id,
            "runnerName": resolved_name or p["bet_for"],
            "stake": amount,
            "odds": odds,
            "betType": bet_type,
            "betFor": p["bet_for"],
            "oddsType": p["odds_type_raw"] or odds_type,
            "run": p["run"],
            "gtype": gtype,
            "fancyType": gtype,
            "liability": liability,
        }
    new_exposure = _calc_user_exposure(
        user_id,
        pending_market_id=market_id,
        pending_event_id=event_id,
        pending_position_delta=position_info or None,
        pending_fancy_bet=pending_fancy_bet,
    )
    credit = _user_credit_limit(user, _calc_user_exposure(user_id))
    if new_exposure > credit + 0.001:
        return _fail("Insufficient balance")

    if position_info:
        _update_market_position(user_id, market_id, event_id, position_info)

    sel_id: Any = int(selection_id) if str(selection_id).isdigit() else selection_id
    created = _now()
    bet = {
        "betId": _bet_id("sport"),
        "userId": user_id,
        "marketId": market_id,
        "eventId": event_id,
        "selectionId": sel_id,
        "runnerName": resolved_name or p["bet_for"],
        "sessionName": resolved_name or None,
        "fancyName": resolved_name or None,
        "marketName": resolved_name or None,
        "stake": amount,
        "odds": odds,
        "betType": bet_type,
        "betFor": p["bet_for"],
        "oddsType": p["odds_type_raw"] or odds_type,
        "marketKind": odds_type,
        "marketType": p["odds_type_raw"] or odds_type,
        "run": p["run"],
        "gtype": gtype,
        "fancyType": gtype,
        "betfairMarketId": p["betfair_market_id"] or None,
        "liability": liability,
        "positionInfo": position_info,
        "profitLoss": 0,
        "status": "open",
        "isDeclare": False,
        "createdAt": created,
    }
    get_db().sports_bets.insert_one(bet)
    new_coins, _ = sync_user_balance(user_id)
    _ledger(user_id, liability, f"Sports bet — {bet_type} @ {odds}", "sport", new_coins)

    msg = "Bet placed successfully"
    if endpoint == "sports/oddBetPlaced":
        msg = "Odds Bet placed successfully"
    elif endpoint == "sports/sessionBetPlaced":
        msg = "Session Bet placed successfully"
    elif "meterKhado" in endpoint:
        msg = "Bet placed successfully"

    return _sports_success(user_id, msg)


def _casino_round_id(value: Any) -> str:
    """Live socket mid string hota hai — frontend roundId===w strict match."""
    if value is None or value == "":
        return ""
    return str(value).strip()


def _casino_selection(payload_or_bet: dict) -> str:
    """t2 rows nation use karti hain; place payload kabhi betFor='undefined' bhejta hai."""
    for key in ("playerName", "betFor", "nat", "nation", "selection"):
        raw = payload_or_bet.get(key)
        if raw is None:
            continue
        text = str(raw).strip()
        if text and text.lower() != "undefined":
            return text
    return ""


def _casino_bet_created_ms(value: Any) -> int:
    if hasattr(value, "timestamp"):
        return int(value.timestamp() * 1000)
    if isinstance(value, (int, float)):
        return int(value)
    return 0


def _casino_declared_result_sid(bet: dict) -> str:
    """Declare ke baad posArray key — je runner/card result aaya."""
    details = bet.get("resultDetails")
    if isinstance(details, dict):
        for key in ("sid", "result", "winner"):
            val = details.get(key)
            if val is not None and str(val).strip().isdigit():
                return str(val).strip()
    for key in ("result", "showResult"):
        val = bet.get(key)
        if val is not None and str(val).strip().isdigit():
            return str(val).strip()
    return ""


def _casino_pos_array(bet: dict) -> dict[str, float]:
    """Scraped casino games posArray — open = scenario map; settled = result sid par final P/L."""
    sid = bet.get("sid")
    stake = float(bet.get("stake") or bet.get("amount") or 0)
    rate = float(bet.get("rate") or bet.get("odds") or 0)
    bet_type = str(bet.get("betType") or bet.get("type") or "Yes")
    status = str(bet.get("status") or "open").lower()
    casino_type = str(bet.get("casinoType") or "")

    if status == "open" and sid is not None and stake > 0 and rate > 0:
        runner_sids = casino_runner_sids(casino_type, sid)
        if runner_sids:
            return calc_casino_position_delta(stake, rate, bet_type, sid, runner_sids, casino_type)

    if status in ("settled", "won", "lost"):
        pl = round(float(bet.get("profitLoss") or 0), 2)
        if abs(pl) < 0.001:
            return {}
        result_sid = _casino_declared_result_sid(bet)
        if result_sid:
            return {result_sid: pl}
        if sid is not None:
            return {str(sid): pl}
        return {}

    existing = bet.get("posArray")
    if isinstance(existing, dict) and existing:
        return {str(k): round(float(v), 2) for k, v in existing.items()}
    if sid is None:
        return {}
    return {str(sid): round(float(bet.get("profitLoss") or 0), 2)}


def _format_diamond_casino_bet_client(bet: dict) -> dict:
    row = {k: v for k, v in bet.items() if k != "_id"}
    stake = float(bet.get("stake") or bet.get("amount") or 0)
    odds = float(bet.get("odds") or bet.get("rate") or 0)
    selection = _casino_selection(bet)
    status = str(bet.get("status") or "open").lower()
    is_declare = bool(bet.get("isDeclare")) or status in ("settled", "won", "lost")

    row["roundId"] = _casino_round_id(bet.get("roundId"))
    row["posArray"] = _casino_pos_array(bet)
    row["amount"] = stake
    row["odds"] = odds
    row["rate"] = odds
    row["playerName"] = selection
    row["nat"] = selection
    row["betType"] = str(bet.get("betType") or bet.get("type") or "Yes")
    if bet.get("sid") is not None:
        row["sid"] = str(bet.get("sid"))
    row["isDeclare"] = 1 if is_declare else 0
    row["profitLoss"] = float(bet.get("profitLoss") or 0)
    show = bet.get("showResult") or bet.get("result") or ""
    if not show and isinstance(bet.get("resultDetails"), dict):
        show = bet["resultDetails"].get("winner") or ""
    if is_declare:
        row["showResult"] = str(show or "")
    elif show:
        row["showResult"] = str(show)
    created_ms = _casino_bet_created_ms(bet.get("createdAt"))
    if created_ms:
        row["createdAt"] = created_ms
    return row


def _casino_stake_limits(event_id: Any, payload: Optional[dict] = None) -> tuple[float, float]:
    """Per-game minStake/maxStake — casino_games MongoDB (scraped UI jaisa)."""
    payload = payload or {}
    try:
        payload_min = float(payload.get("minStake") or 0)
    except (TypeError, ValueError):
        payload_min = 0.0
    try:
        payload_max = float(payload.get("maxStake") or 0)
    except (TypeError, ValueError):
        payload_max = 0.0
    if payload_min > 0 and payload_max > 0:
        return payload_min, payload_max

    from mongodb.casino_api import find_casino_game_by_event_id

    game = find_casino_game_by_event_id(event_id)
    if not game:
        return 10.0, 0.0

    try:
        min_stake = float(game.get("minStake") or 10)
    except (TypeError, ValueError):
        min_stake = 10.0
    try:
        max_stake = float(game.get("maxStake") or 0)
    except (TypeError, ValueError):
        max_stake = 0.0
    if payload_min > 0:
        min_stake = payload_min
    if payload_max > 0:
        max_stake = payload_max
    return min_stake, max_stake


def _validate_casino_stake(amount: float, event_id: Any, payload: Optional[dict] = None) -> Optional[dict]:
    min_stake, max_stake = _casino_stake_limits(event_id, payload)
    if amount < min_stake - 0.001:
        return _fail(f"Please enter minimum amount: {int(min_stake)}")
    if max_stake > 0 and amount > max_stake + 0.001:
        return _fail(f"Please enter maximum amount: {int(max_stake)}")
    return None


def _casino_bet_allowed(event_id: Any) -> Optional[dict]:
    from mongodb.casino_api import find_casino_game_by_event_id

    game = find_casino_game_by_event_id(event_id)
    if not game:
        return None
    if game.get("betStatus") is False or game.get("isDisable"):
        setting = game.get("setting") if isinstance(game.get("setting"), dict) else {}
        msg = str(setting.get("errorMessage") or "Betting is disabled for this game")
        return _fail(msg)
    return None


def place_casino_bet(payload: dict, auth_header: str) -> dict:
    user, err = _require_user(auth_header)
    if err:
        return err

    amount = _parse_amount(payload)
    odds = _parse_odds(payload)
    event_id = payload.get("eventId")
    round_id = _casino_round_id(payload.get("roundId") or payload.get("mid") or "")
    bet_for = str(payload.get("betFor") or payload.get("nat") or "").strip()
    selection = _casino_selection(payload) or bet_for
    sid = payload.get("sid")
    bet_type = str(payload.get("type") or payload.get("betType") or ("No" if payload.get("isLay") else "Yes"))

    if not event_id or not round_id:
        return _fail("Incomplete bet details")

    blocked = _casino_bet_allowed(event_id)
    if blocked:
        return blocked
    stake_err = _validate_casino_stake(amount, event_id, payload)
    if stake_err:
        return stake_err

    user_id = user["userId"]
    casino_type = str(payload.get("casinoType") or "")
    payload_sids = payload.get("runnerSids") or payload.get("allSids")
    runner_sids = casino_runner_sids(casino_type, sid, payload_sids if isinstance(payload_sids, list) else None)
    liability = calc_casino_liability(amount, odds, bet_type, casino_type, sid)
    new_exposure = _calc_user_exposure(user_id) + liability
    credit = _user_credit_limit(user, _calc_user_exposure(user_id))
    if new_exposure > credit + 0.001:
        return _fail("Insufficient balance")

    bet = {
        "betId": _bet_id("casino"),
        "userId": user_id,
        "eventId": int(event_id) if str(event_id).isdigit() else event_id,
        "roundId": round_id,
        "sid": str(sid) if sid is not None else sid,
        "stake": amount,
        "rate": odds,
        "odds": odds,
        "selection": selection,
        "playerName": selection,
        "betFor": bet_for or selection,
        "betType": bet_type,
        "casinoType": casino_type,
        "runnerSids": runner_sids,
        "liability": liability,
        "profitLoss": 0,
        "gameType": "diamond",
        "status": "open",
        "isDeclare": False,
        "posArray": _casino_pos_array({
            "sid": sid,
            "stake": amount,
            "rate": odds,
            "betType": bet_type,
            "casinoType": casino_type,
            "runnerSids": runner_sids,
            "status": "open",
        }),
        "createdAt": _now(),
    }
    get_db().casino_bets.insert_one(bet)
    new_coins, _ = sync_user_balance(user_id)
    _ledger(user_id, liability, f"Casino bet — {selection}", "casino", new_coins)
    return _casino_success(user_id)


def _aviator_settlement_response(
    bet_id: str,
    pl: float,
    coins: float,
    exposure: float,
    message: str = "Settled",
    **extra: Any,
) -> dict:
    data = {
        "betInsertId": bet_id,
        "profitLoss": pl,
        "totalCoins": coins,
        "coins": str(coins),
        "exposure": str(exposure),
    }
    data.update(extra)
    return {
        "message": message,
        "code": 0,
        "error": False,
        "data": data,
    }


def _find_open_aviator_bet(user_id: str, bet_id: str = "", round_id: str = "") -> Optional[dict]:
    q: dict = {"userId": user_id, "status": "open", "gameType": "aviator"}
    if bet_id:
        q["betId"] = bet_id
    elif round_id:
        q["roundId"] = round_id
    else:
        return None
    return get_db().casino_bets.find_one(q)


def _apply_aviator_settlement(
    user_id: str,
    bet: dict,
    pl: float,
    *,
    multiplier: Optional[float] = None,
    win_amount: Optional[float] = None,
    crash_value: Optional[float] = None,
) -> Tuple[float, float]:
    db = get_db()
    bet_id = bet["betId"]
    user_row = db.users.find_one({"userId": user_id}) or {}
    coins = float(user_row.get("coins") or 0)
    old_exposure = float(user_row.get("exposure") or 0)
    credit = float(user_row.get("creditLimit") or (coins + old_exposure))

    settle_fields: dict = {
        "status": "settled",
        "profitLoss": pl,
        "settledAt": _now(),
    }
    if multiplier is not None:
        settle_fields["multiplier"] = multiplier
    if win_amount is not None:
        settle_fields["winAmount"] = win_amount
    if crash_value is not None:
        settle_fields["crashValue"] = crash_value

    db.casino_bets.update_one({"betId": bet_id}, {"$set": settle_fields})

    new_exposure = round(_calc_user_exposure(user_id), 2)
    released = round(old_exposure - new_exposure, 2)
    new_coins = round(coins + released + pl, 2)
    new_credit = round(credit + pl, 2)
    db.users.update_one(
        {"userId": user_id},
        {"$set": {
            "coins": new_coins,
            "exposure": new_exposure,
            "creditLimit": new_credit,
            "updatedAt": _now(),
        }},
    )
    return new_coins, new_exposure


def place_aviator_bet(payload: dict, auth_header: str) -> dict:
    user, err = _require_user(auth_header)
    if err:
        return err

    amount = _parse_amount(payload)
    round_id = str(payload.get("roundId") or "")
    event_id = payload.get("eventId") or 303031
    if not round_id:
        return _fail("Incomplete bet details")

    blocked = _casino_bet_allowed(event_id)
    if blocked:
        return blocked
    stake_err = _validate_casino_stake(amount, event_id, payload)
    if stake_err:
        return stake_err

    user_id = user["userId"]
    liability = round(amount, 2)
    new_exposure = _calc_user_exposure(user_id) + liability
    credit = _user_credit_limit(user, _calc_user_exposure(user_id))
    if new_exposure > credit + 0.001:
        return _fail("Insufficient balance")

    bet_id = _bet_id("aviator")
    bet = {
        "betId": bet_id,
        "userId": user_id,
        "eventId": int(event_id) if str(event_id).isdigit() else event_id,
        "roundId": round_id,
        "stake": amount,
        "multiplier": payload.get("multiplier"),
        "selection": "aviator",
        "casinoType": str(payload.get("casinoType") or "aviator"),
        "liability": liability,
        "profitLoss": 0,
        "gameType": "aviator",
        "status": "open",
        "createdAt": _now(),
    }
    get_db().casino_bets.insert_one(bet)
    new_coins, new_exp = sync_user_balance(user_id)
    _ledger(user_id, liability, "Aviator bet placed", "casino", new_coins)
    return _aviator_success(user_id, bet_id)


def cashout_aviator_bet(payload: dict, auth_header: str) -> dict:
    user, err = _require_user(auth_header)
    if err:
        return err

    bet_id = str(payload.get("betId") or payload.get("betInsertId") or "")
    if not bet_id:
        return _fail("Bet not found")

    db = get_db()
    bet = db.casino_bets.find_one({
        "betId": bet_id,
        "userId": user["userId"],
        "status": "open",
        "gameType": "aviator",
    })
    if not bet:
        return _fail("Bet not found")

    try:
        multiplier = float(payload.get("multiplier") or payload.get("cashoutMultiplier") or 0)
    except (TypeError, ValueError):
        multiplier = 0.0
    if multiplier <= 1:
        return _fail("Cashout not available")

    stake = float(bet.get("stake") or 0)
    win_amount = round(stake * multiplier, 2)
    pl = round(win_amount - stake, 2)
    user_id = user["userId"]

    new_coins, new_exposure = _apply_aviator_settlement(
        user_id, bet, pl, multiplier=multiplier, win_amount=win_amount,
    )
    _ledger(user_id, win_amount, f"Aviator cashout @ {multiplier}x", "casino", new_coins, entry_type="credit")

    return _aviator_settlement_response(
        bet_id, pl, new_coins, new_exposure,
        message="Cashout successful",
        winAmount=win_amount,
        multiplier=multiplier,
    )


def settle_aviator_loss_bet(payload: dict, auth_header: str) -> dict:
    """Plane crash — open bet loss settle karke exposure clear karo."""
    user, err = _require_user(auth_header)
    if err:
        return err

    bet_id = str(payload.get("betId") or payload.get("betInsertId") or "")
    round_id = str(payload.get("roundId") or "")
    user_id = user["userId"]

    bet = _find_open_aviator_bet(user_id, bet_id, round_id)
    if not bet:
        user_row = get_db().users.find_one({"userId": user_id}) or {}
        coins = round(float(user_row.get("coins") or 0), 2)
        exposure = round(float(user_row.get("exposure") or 0), 2)
        return _aviator_settlement_response(
            bet_id, 0, coins, exposure, message="No open bet",
        )

    try:
        crash_value = float(payload.get("crashValue") or payload.get("multiplier") or 0)
        if crash_value <= 0:
            crash_value = None
    except (TypeError, ValueError):
        crash_value = None

    stake = float(bet.get("stake") or 0)
    pl = round(-stake, 2)
    new_coins, new_exposure = _apply_aviator_settlement(
        user_id, bet, pl, crash_value=crash_value,
    )
    _ledger(user_id, stake, f"Aviator loss{f' @ {crash_value}x' if crash_value else ''}", "casino", new_coins)

    return _aviator_settlement_response(
        bet["betId"], pl, new_coins, new_exposure,
        message="Bet settled",
        crashValue=crash_value,
    )


def client_bets_by_market(payload: dict, auth_header: str) -> dict:
    user, err = _session_user(auth_header, payload)
    if err:
        return err

    market_id = str(payload.get("marketId") or "")
    user_id = user["userId"]
    try:
        from mongodb.wnp9_auto_decision import maybe_auto_settle_market

        maybe_auto_settle_market(market_id, str(payload.get("eventId") or ""))
    except Exception:
        pass
    match = _get_match(market_id)
    bets = list(get_db().sports_bets.find(
        _market_bets_query(user_id, market_id, payload),
        {"_id": 0},
    ).sort("createdAt", -1))

    odds_bets = []
    fancy_bets = []
    for b in bets:
        if is_fancy_market(str(b.get("betFor") or ""), str(b.get("oddsType") or ""), str(b.get("gtype") or "")):
            fancy_bets.append(_bet_to_fancy_record(b))
        elif str(b.get("status") or "").lower() == "open":
            odds_bets.append(_bet_to_odds_record(b, match))

    return {
        "message": "data get successfully",
        "code": 0,
        "error": False,
        "data": {"oddsBetData": odds_bets, "fancyBetData": fancy_bets},
    }


def user_position_by_market(payload: dict, auth_header: str) -> dict:
    user, err = _session_user(auth_header, payload)
    if err:
        return err

    market_id = str(payload.get("marketId") or "")
    user_id = user["userId"]
    event_id = str(payload.get("eventId") or _event_id_for_market(market_id) or "")

    # Rebuild from this user's open bets before reading positions, so old seed
    # rows or closed-market leftovers never leak into current exposure display.
    if market_id:
        _sync_event_position_from_bets(user_id, market_id, event_id)

    pos_key: dict = {"userId": user_id}
    if event_id:
        pos_key["eventId"] = event_id
    else:
        pos_key["marketId"] = market_id
    pos = get_db().positions.find_one(pos_key, {"_id": 0})
    runners = (pos or {}).get("runners") or {}
    match = _get_match(market_id) if market_id else None
    if match and runners:
        runners = remap_bookmaker_runners(match, runners)

    odds_position = [
        {"selectionId": int(k) if str(k).isdigit() else k, "position": v}
        for k, v in runners.items()
    ]

    session_query = _open_bets_query(user_id, market_id, payload)
    session_bets = [
        b for b in get_db().sports_bets.find(session_query, {"_id": 0})
        if is_fancy_market(
            str(b.get("betFor") or ""),
            str(b.get("oddsType") or ""),
            str(b.get("gtype") or ""),
        )
    ]

    session_position = [
        {
            "selectionId": b.get("selectionId"),
            "run": b.get("run"),
            "type": b.get("betType"),
            "amount": b.get("stake"),
            "odds": b.get("odds"),
        }
        for b in session_bets
    ]

    return {
        "message": "Inplay Matches Odds Position Fetch Successfully",
        "code": 0,
        "error": False,
        "data": {"oddsPosition": odds_position, "sessionPosition": session_position},
    }


def _market_label(db, market_id: str, event_id: Any, bet: dict) -> str:
    match = _get_match(str(market_id)) if market_id else None
    if not match and event_id:
        match = db.matches.find_one({"eventId": str(event_id)}, {"_id": 0, "eventName": 1, "matchName": 1})
    if match:
        return str(match.get("eventName") or match.get("matchName") or market_id or event_id or "")
    return str(bet.get("runnerName") or bet.get("betFor") or market_id or event_id or "Sports")


def sports_bets_list(payload: dict, auth_header: str) -> dict:
    """sports/betsList — MyBets unsettled list (scraped flat array)."""
    user, err = _session_user(auth_header, payload)
    if err:
        return _mybets_list_err(err)

    user_id = user["userId"]
    db = get_db()
    include_odds = payload.get("oddsBet", True) is not False
    include_fancy = payload.get("fancyBet", True) is not False
    include_diamond = payload.get("diamondBet", True) is not False

    rows: list[dict] = []

    if include_odds or include_fancy:
        for bet in db.sports_bets.find(
            _unsettled_sports_query(user_id, payload),
            {"_id": 0},
        ).sort("createdAt", -1).limit(500):
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
            market_name = _market_label(db, str(bet.get("marketId") or ""), bet.get("eventId"), bet)
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
            _unsettled_casino_query(user_id, payload),
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
                "odds": bet.get("rate") or bet.get("multiplier") or 0,
                "type": "L",
                "amount": bet.get("stake"),
                "time": _iso_created_at(bet.get("createdAt")),
            })

    return {"message": "Fetch List Successfuly", "code": 0, "error": False, "data": rows}


def casino_bets_list(payload: dict, auth_header: str) -> dict:
    user, err = _session_user(auth_header, payload)
    if err:
        return err

    user_id = user["userId"]
    event_id = payload.get("eventId")
    from mongodb.casino_declare_api import auto_settle_user_pending_rounds

    auto_settle_user_pending_rounds(user_id, event_id)

    query: dict = {"userId": user_id}
    if event_id not in (None, ""):
        query["eventId"] = int(event_id) if str(event_id).isdigit() else event_id

    bets = list(get_db().casino_bets.find(query, {"_id": 0}).sort("createdAt", -1).limit(200))
    rows = [_format_diamond_casino_bet_client(b) for b in bets]
    return {
        "message": "Diamond Casino Bet List Fetch Successfully",
        "code": 0,
        "error": False,
        "data": {"casinoBetData": rows},
    }


def matka_bets_list(payload: dict, auth_header: str) -> dict:
    user, err = _session_user(auth_header, payload)
    if err:
        return err

    user_id = user["userId"]
    query: dict = {"userId": user_id}
    if payload.get("isDeclare") is False:
        query["status"] = "open"
    bets = list(get_db().matka_bets.find(query, {"_id": 0}).sort("createdAt", -1).limit(200))
    return {
        "message": "Matka bet list fetched",
        "code": 0,
        "error": False,
        "data": bets,
    }


def matka_report_by_user(payload: dict, auth_header: str) -> dict:
    user, err = _session_user(auth_header, payload)
    if err:
        return err

    user_id = user["userId"]
    bets = list(get_db().matka_bets.find({"userId": user_id}, {"_id": 0}).sort("createdAt", -1).limit(200))
    return {
        "message": "Matka report fetched",
        "code": 0,
        "error": False,
        "data": bets,
    }


def _lock_balance(user_id: str, liability: float) -> Tuple[Optional[float], Optional[str]]:
    db = get_db()
    user = db.users.find_one({"userId": user_id})
    if not user:
        return None, "User not found"
    coins = float(user.get("coins") or 0)
    if liability <= 0:
        return None, "Invalid bet amount"
    if liability > coins:
        return None, "Insufficient balance"
    new_coins = round(coins - liability, 2)
    db.users.update_one({"userId": user_id}, {"$set": {"coins": new_coins, "updatedAt": _now()}})
    return new_coins, None


def _apply_sports_settlement_balance(
    user_id: str,
    bet: dict,
    pl: float,
    *,
    decision_run: Any = None,
    won_selection_id: Any = None,
) -> Tuple[float, float]:
    """Scraped site model — declare/settle par coins, exposure, creditLimit + ledger."""
    db = get_db()
    user_row = db.users.find_one({"userId": user_id}) or {}
    coins = float(user_row.get("coins") or 0)
    old_exposure = float(user_row.get("exposure") or 0)
    credit = float(user_row.get("creditLimit") or (coins + old_exposure))

    settle_fields: dict = {
        "status": "settled",
        "profitLoss": round(float(pl), 2),
        "isDeclare": True,
        "settledAt": _now(),
    }
    if decision_run is not None:
        settle_fields["decisionRun"] = decision_run
    if won_selection_id is not None:
        settle_fields["wonSelectionId"] = won_selection_id
        settle_fields["decisionSelectionId"] = won_selection_id

    db.sports_bets.update_one({"betId": bet["betId"]}, {"$set": settle_fields})

    new_exposure = round(_calc_user_exposure(user_id), 2)
    released = round(old_exposure - new_exposure, 2)
    new_coins = round(coins + released + pl, 2)
    new_credit = round(credit + pl, 2)
    db.users.update_one(
        {"userId": user_id},
        {"$set": {
            "coins": new_coins,
            "exposure": new_exposure,
            "creditLimit": new_credit,
            "updatedAt": _now(),
        }},
    )

    runner = (
        bet.get("runnerName")
        or bet.get("sessionName")
        or bet.get("fancyName")
        or bet.get("marketName")
        or "Sports"
    )
    if decision_run is not None:
        desc = f"Fancy declare — {runner} @ {decision_run} (P/L {pl:+.2f})"
    else:
        desc = f"Bet settled — {runner} (P/L {pl:+.2f})"
    _ledger(
        user_id,
        round(abs(pl), 2),
        desc,
        "sport",
        new_coins,
        entry_type="credit" if pl >= 0 else "debit",
    )
    return new_coins, new_exposure


def settle_sports_bet(bet_id: str, result: dict) -> dict:
    """Admin/demo — bet settle karke P/L apply karo."""
    db = get_db()
    bet = db.sports_bets.find_one({"betId": bet_id, "status": "open"})
    if not bet:
        return _fail("Bet not found")

    user_id = bet["userId"]
    market_id = str(bet.get("marketId") or "")
    event_id = str(bet.get("eventId") or "")

    if is_fancy_market(str(bet.get("betFor") or ""), str(bet.get("oddsType") or ""), str(bet.get("gtype") or "")):
        pl = settle_fancy_bet(bet, int(result.get("decisionRun", 0)))
        new_coins, new_exposure = _apply_sports_settlement_balance(
            user_id,
            bet,
            pl,
            decision_run=result.get("decisionRun"),
        )
    else:
        match = _get_match(market_id, event_id)
        pl = settle_odds_bet(bet, result.get("wonSelectionId"), match)
        _sync_event_position_from_bets(user_id, market_id, event_id)
        new_coins, new_exposure = _apply_sports_settlement_balance(
            user_id,
            bet,
            pl,
            won_selection_id=result.get("wonSelectionId"),
        )

    return {
        "message": "Settled",
        "code": 0,
        "error": False,
        "data": {
            "betId": bet_id,
            "profitLoss": pl,
            "totalCoins": new_coins,
            "coins": new_coins,
            "exposure": new_exposure,
        },
    }


def settle_casino_bet_by_id(bet_id: str, won: bool, payout_rate: Optional[float] = None) -> dict:
    db = get_db()
    bet = db.casino_bets.find_one({"betId": bet_id, "status": "open"})
    if not bet:
        return _fail("Bet not found")

    user_id = bet["userId"]
    liability = float(bet.get("liability") or bet.get("stake") or 0)
    pl = settle_casino_bet(bet, won, payout_rate)

    db.casino_bets.update_one({"betId": bet_id}, {"$set": {"status": "settled", "profitLoss": pl}})
    user = db.users.find_one({"userId": user_id}) or {}
    coins = float(user.get("coins") or 0) + liability + pl
    db.users.update_one({"userId": user_id}, {"$set": {"coins": round(coins, 2), "updatedAt": _now()}})

    return _ok_settle(bet_id, pl, coins)


def _ok_settle(bet_id: str, pl: float, coins: float) -> dict:
    return {"message": "Settled", "code": 0, "error": False, "data": {"betId": bet_id, "profitLoss": pl, "totalCoins": coins}}


def handle_bet_endpoint(endpoint: str, payload: dict, auth_header: str) -> Optional[dict]:
    endpoint = endpoint.lstrip("/").split("?")[0]

    if endpoint in ("sports/oddBetPlaced", "sports/sessionBetPlaced",
                    "sports/meterKhadoOddEvenCricketCassinoBetPlace"):
        return place_sports_bet(payload, auth_header, endpoint)
    if endpoint == "casino/casinoBetPlace":
        return place_casino_bet(payload, auth_header)
    if endpoint == "casino/avaitorGamePlace":
        return place_aviator_bet(payload, auth_header)
    if endpoint == "casino/avaitorCashOut":
        return cashout_aviator_bet(payload, auth_header)
    if endpoint == "casino/avaitorRoundLost":
        return settle_aviator_loss_bet(payload, auth_header)
    if endpoint == "sports/betsList":
        return sports_bets_list(payload, auth_header)
    if endpoint == "casino/diamondBetsList":
        return casino_bets_list(payload, auth_header)
    if endpoint == "casino/syncLiveRoundResult":
        from mongodb.casino_declare_api import declare_live_casino_result
        return declare_live_casino_result(payload)
    if endpoint == "casino/resultByRoundWise":
        from mongodb.casino_declare_api import casino_result_by_round_wise
        return casino_result_by_round_wise(payload, auth_header)
    if endpoint == "casino/roundWiseResult":
        from mongodb.casino_declare_api import casino_round_wise_result
        return casino_round_wise_result(payload)
    if endpoint == "matka/matkaBetList":
        return matka_bets_list(payload, auth_header)
    if endpoint == "matka/matkaReportByUser":
        return matka_report_by_user(payload, auth_header)
    if endpoint in ("user/clientBetListByMarketId", "halkabhari/inplayOddsPositionHalkaBhari"):
        return client_bets_by_market(payload, auth_header)
    if endpoint == "sports/userPositionByMarketId":
        return user_position_by_market(payload, auth_header)
    return None
