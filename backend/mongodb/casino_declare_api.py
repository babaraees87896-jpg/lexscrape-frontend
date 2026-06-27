"""Diamond Casino — declare result (staff panel)."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Optional

from mongodb.admin_compute import (
    _bet_ist_date_str,
    _casino_bet_is_declared,
    _casino_bets_in_scope,
    _casino_game_name,
    _casino_games_map,
)
from mongodb.bet_logic import casino_runner_sids, settle_casino_bet
from mongodb.bets import _casino_declared_result_sid
from mongodb.bets import _ledger, sync_user_balance, _now
from mongodb.casino_api import all_casino_games, find_casino_game_by_event_id
from mongodb.db import get_db

# Cache URL fetch fail hone par — declare modal ke liye minimum options.
_FALLBACK_RESULT_JSON: dict[str, dict[str, dict[str, str]]] = {
    "teen20": {
        "Player A": {"1": "Player A"},
        "Player B": {"3": "Player B"},
        "Pair plus A": {"2": "Pair plus A"},
        "Pair plus B": {"4": "Pair plus B"},
    },
    "dt20": {
        "Dragon": {"1": "Dragon"},
        "Tiger": {"2": "Tiger"},
        "Tie": {"3": "Tie"},
        "Pair": {"4": "Pair"},
    },
    "lucky7eu": {
        "Low Card": {"1": "Low Card"},
        "High Card": {"2": "High Card"},
        "Even": {"3": "Even"},
        "Odd": {"4": "Odd"},
    },
    "Teen": {
        "Player A": {"1": "Player A"},
        "Player B": {"2": "Player B"},
    },
    "abj": {
        "Andar": {"1": "Andar"},
        "Bahar": {"4": "Bahar"},
    },
}


def _iso(val: Any) -> str:
    if isinstance(val, datetime):
        if val.tzinfo is None:
            val = val.replace(tzinfo=timezone.utc)
        return val.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(val, (int, float)) and val > 1_000_000_000:
        return datetime.fromtimestamp(val / 1000 if val > 1e12 else val, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    return str(val or "")


def _runner_label(row: dict) -> str:
    return str(row.get("nat") or row.get("nation") or row.get("name") or "").strip()


def _markets_to_categories(markets: list[dict]) -> dict[str, dict[str, str]]:
    """Declare modal shape: category -> {sid: label}."""
    out: dict[str, dict[str, str]] = {}
    for row in markets:
        if not isinstance(row, dict):
            continue
        sid = str(row.get("sid") or "").strip()
        label = _runner_label(row)
        if not sid or not label:
            continue
        out.setdefault(label, {})[sid] = label
    if not out:
        return {"Main": {"1": "Winner"}}
    return out


def _fetch_cache_markets(cache_url: str) -> list[dict]:
    if not cache_url:
        return []
    try:
        req = urllib.request.Request(
            cache_url,
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = json.loads(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError, OSError):
        return []

    data = raw.get("data") if isinstance(raw, dict) else raw
    if isinstance(data, dict):
        inner = data.get("data")
        if isinstance(inner, dict) and isinstance(inner.get("t2"), list):
            return inner["t2"]
        if isinstance(data.get("t2"), list):
            return data["t2"]
    return []


def _casino_type_from_game(game: dict) -> str:
    short = str(game.get("shortName") or "").strip()
    if short:
        return short
    cache_url = str(game.get("cacheURL") or "")
    m = re.search(r"casinoType=?([^&\"']+)", cache_url)
    return m.group(1).strip() if m else ""


def build_casino_result_json(*, force_refresh: bool = False) -> dict[str, dict[str, dict[str, str]]]:
    """casino/sendCasinoResultjson — declare modal categories per game type."""
    db = get_db()
    if not force_refresh:
        cached = db.center_master_settings.find_one({"key": "casinoResultJson"})
        value = cached.get("value") if isinstance(cached, dict) else None
        if isinstance(value, dict) and value:
            return value

    out: dict[str, dict[str, dict[str, str]]] = {}
    for game in all_casino_games():
        ctype = _casino_type_from_game(game)
        if not ctype or ctype in out:
            continue
        markets = _fetch_cache_markets(str(game.get("cacheURL") or ""))
        if markets:
            out[ctype] = _markets_to_categories(markets)
        elif ctype in _FALLBACK_RESULT_JSON:
            out[ctype] = _FALLBACK_RESULT_JSON[ctype]
    for ctype, cfg in _FALLBACK_RESULT_JSON.items():
        out.setdefault(ctype, cfg)
    return out


def _round_row_from_bets(round_id: str, bets: list[dict]) -> dict:
    first = bets[0]
    event_id = first.get("eventId")
    casino_type = str(first.get("casinoType") or "").strip()
    if not casino_type:
        game = find_casino_game_by_event_id(event_id)
        casino_type = _casino_type_from_game(game or {})
    created = first.get("createdAt")
    for bet in bets[1:]:
        c = bet.get("createdAt")
        if c and created and c < created:
            created = c
    return {
        "_id": str(round_id),
        "eventId": event_id,
        "casinoType": casino_type,
        "gtype": casino_type,
        "roundId": str(round_id),
        "createdAt": _iso(created),
    }


def list_undeclared_rounds(payload: dict) -> list[dict]:
    """casino/undeclaredRoundId — open diamond bets grouped by roundId."""
    payload = dict(payload or {})
    payload.setdefault("isDeclare", False)

    db = get_db()
    rows: list[dict] = []
    seen: set[str] = set()

    open_bets = [
        b for b in _casino_bets_in_scope(db, payload)
        if str(b.get("status") or "open").lower() == "open"
        and str(b.get("gameType") or "diamond").lower() != "aviator"
        and not b.get("isDeclare")
    ]

    by_round: dict[str, list[dict]] = {}
    for bet in open_bets:
        rid = str(bet.get("roundId") or "").strip()
        if not rid:
            continue
        by_round.setdefault(rid, []).append(bet)

    for rid, bets in sorted(by_round.items(), key=lambda x: x[1][0].get("createdAt") or "", reverse=True):
        rows.append(_round_row_from_bets(rid, bets))
        seen.add(rid)

    for rnd in db.casino_rounds.find({"isDeclare": {"$ne": True}}):
        rid = str(rnd.get("roundId") or rnd.get("_id") or "").strip()
        if not rid or rid in seen:
            continue
        if payload.get("eventId") is not None and rnd.get("eventId") != payload.get("eventId"):
            try:
                if int(rnd.get("eventId")) != int(payload.get("eventId")):
                    continue
            except (TypeError, ValueError):
                continue
        day = _bet_ist_date_str({"createdAt": rnd.get("createdAt")})
        from_str = str(payload.get("fromDate") or "")[:10]
        to_str = str(payload.get("toDate") or "")[:10]
        if from_str and day and day < from_str:
            continue
        if to_str and day and day > to_str:
            continue
        ctype = str(rnd.get("gtype") or rnd.get("casinoType") or "").strip()
        rows.append({
            "_id": rid,
            "eventId": rnd.get("eventId"),
            "casinoType": ctype,
            "gtype": ctype,
            "roundId": rid,
            "createdAt": _iso(rnd.get("createdAt")),
        })
        seen.add(rid)

    rows.sort(key=lambda r: r.get("createdAt") or "", reverse=True)
    return rows


# Socket result code -> (winning sid, label) for main market bets.
_MAIN_RESULT_SID: dict[str, dict[str, tuple[str, str]]] = {
    "teen20": {"1": ("1", "Player A"), "3": ("3", "Player B")},
    "teen9": {"1": ("1", "Player A"), "2": ("2", "Player B")},
    "teen8": {"1": ("1", "Player A"), "2": ("2", "Player B")},
    "Teen": {"1": ("1", "Player A"), "2": ("2", "Player B")},
    "dt20": {"1": ("1", "Dragon"), "2": ("2", "Tiger"), "3": ("3", "Tie")},
    "dt202": {"1": ("1", "Dragon"), "2": ("2", "Tiger"), "3": ("3", "Tie")},
    "dt6": {"1": ("1", "Dragon"), "2": ("2", "Tiger"), "3": ("3", "Tie")},
    "dtl20": {"1": ("1", "Dragon"), "2": ("2", "Tiger"), "3": ("3", "Tie")},
    "lucky7eu": {"1": ("1", "Low Card"), "2": ("2", "High Card"), "3": ("3", "Even"), "4": ("4", "Odd")},
    "lucky7": {"1": ("1", "Low Card"), "2": ("2", "High Card")},
    "abj": {"1": ("1", "Andar"), "4": ("4", "Bahar")},
}


def _winner_from_result_code(casino_type: str, result_code: str) -> tuple[str, str]:
    code = str(result_code or "").strip()
    ctype = str(casino_type or "").strip()
    mapped = (_MAIN_RESULT_SID.get(ctype) or {}).get(code)
    if mapped:
        return mapped
    # Fallback: result code often equals winning sid on main market.
    if code.isdigit():
        return code, code
    return "", str(result_code or "")


def _credit_casino_settlement(user_id: str, pl: float) -> None:
    """Declared bet P/L wallet mein post karo; exposure sync_user_balance se release."""
    db = get_db()
    user = db.users.find_one({"userId": user_id}) or {}
    new_credit = round(float(user.get("creditLimit") or 0) + pl, 2)
    db.users.update_one(
        {"userId": user_id},
        {"$set": {"creditLimit": new_credit, "updatedAt": _now()}},
    )
    sync_user_balance(user_id)


def _casino_settle_pos_array(bet: dict, profit_loss: float) -> dict[str, float]:
    """Settled bet — posArray sirf declare result sid par (open scenario nahi)."""
    pl = round(float(profit_loss or 0), 2)
    if abs(pl) < 0.001:
        return {}
    result_sid = _casino_declared_result_sid(bet)
    if not result_sid:
        details = bet.get("resultDetails")
        if isinstance(details, dict) and details.get("sid"):
            result_sid = str(details["sid"])
    if result_sid:
        return {result_sid: pl}
    sid = bet.get("sid")
    return {str(sid): pl} if sid is not None else {}


def settle_diamond_round_bets(
    round_id: str,
    win_sid: str,
    winner_label: str,
    *,
    event_id: Any = None,
    casino_type: str = "",
    result_details: Optional[dict] = None,
    bet_for: str = "",
) -> int:
    """Open diamond bets settle karo — balance/exposure update ke saath."""
    round_id = _casino_round_id(round_id)
    if not round_id:
        return 0

    db = get_db()
    q: dict = {"roundId": round_id, "status": "open", "gameType": {"$ne": "aviator"}}
    if event_id is not None:
        q["eventId"] = int(event_id) if str(event_id).isdigit() else event_id

    bets = list(db.casino_bets.find(q))
    if not bets:
        return 0

    result_json = build_casino_result_json()
    if not casino_type:
        casino_type = str(bets[0].get("casinoType") or "").strip()
    if not casino_type:
        game = find_casino_game_by_event_id(bets[0].get("eventId"))
        casino_type = _casino_type_from_game(game or {})

    show_result = str(winner_label or win_sid or "").strip()
    details = dict(result_details or {})
    if show_result and "winner" not in details:
        details["winner"] = show_result
    if casino_type and "gtype" not in details:
        details["gtype"] = casino_type
    if win_sid and "sid" not in details:
        details["sid"] = str(win_sid)

    user_pl: dict[str, float] = {}
    settled = 0
    for bet in bets:
        won = _bet_wins(bet, win_sid, bet_for, result_json, casino_type)
        pl = settle_casino_bet(bet, won)
        settle_row = {
            **bet,
            "profitLoss": pl,
            "resultDetails": details,
            "result": show_result,
            "showResult": show_result,
        }
        settled_pos = _casino_settle_pos_array(settle_row, pl)
        db.casino_bets.update_one(
            {"betId": bet["betId"]},
            {"$set": {
                "status": "settled",
                "profitLoss": pl,
                "isDeclare": True,
                "showResult": show_result,
                "result": show_result,
                "resultDetails": details,
                "declaredAt": _now(),
                "settledAt": _now(),
                "posArray": settled_pos,
                "runnerSids": casino_runner_sids(str(bet.get("casinoType") or ""), bet.get("sid")),
            }},
        )
        uid = str(bet.get("userId") or "")
        if uid:
            user_pl[uid] = round(user_pl.get(uid, 0) + pl, 2)
        settled += 1

    for uid, total_pl in user_pl.items():
        _credit_casino_settlement(uid, total_pl)

    db.casino_rounds.update_one(
        {"roundId": round_id},
        {"$set": {
            "roundId": round_id,
            "eventId": bets[0].get("eventId"),
            "gtype": casino_type,
            "casinoType": casino_type,
            "isDeclare": True,
            "result": details,
            "showResult": show_result,
            "declaredAt": _now(),
        }},
        upsert=True,
    )
    return settled


def _casino_round_id(value: Any) -> str:
    if value is None or value == "":
        return ""
    return str(value).strip()


_live_results_cache: dict[str, tuple[float, dict[str, str]]] = {}


def _fetch_live_results_by_mid(casino_type: str) -> dict[str, str]:
    """Original tresting feed — open bets ke liye declared round lookup."""
    ctype = str(casino_type or "").strip()
    if not ctype:
        return {}
    import time

    now = time.time()
    cached = _live_results_cache.get(ctype)
    if cached and now - cached[0] < 1.5:
        return cached[1]

    url = f"https://casinoapi.tresting.com/v2/api/casinoData?casinoType={ctype}"
    by_mid: dict[str, str] = {}
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ex99-local/1"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
        data = payload.get("data") if isinstance(payload, dict) else {}
        if isinstance(data, dict):
            rows = data.get("result")
            if rows is None and isinstance(data.get("data"), dict):
                rows = data["data"].get("result")
            if isinstance(rows, list):
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    mid = _casino_round_id(row.get("mid"))
                    code = str(row.get("result") or row.get("win") or "").strip()
                    if mid and code:
                        by_mid[mid] = code
    except Exception:
        pass

    _live_results_cache[ctype] = (now, by_mid)
    return by_mid


def sync_open_bets_from_live_results(user_id: str, event_id: Any = None) -> None:
    """Open bets jinka result live feed mein aa chuka ho — turant settle (original site jaisa)."""
    db = get_db()
    q: dict = {"userId": user_id, "status": "open", "gameType": {"$ne": "aviator"}}
    if event_id not in (None, ""):
        q["eventId"] = int(event_id) if str(event_id).isdigit() else event_id

    pending: dict[str, dict] = {}
    for bet in db.casino_bets.find(q, {"roundId": 1, "eventId": 1, "casinoType": 1}):
        rid = _casino_round_id(bet.get("roundId"))
        if not rid or rid in pending:
            continue
        ctype = str(bet.get("casinoType") or "").strip()
        if not ctype:
            game = find_casino_game_by_event_id(bet.get("eventId"))
            ctype = _casino_type_from_game(game or {})
        pending[rid] = {"casinoType": ctype, "eventId": bet.get("eventId")}

    if not pending:
        return

    live_by_type: dict[str, dict[str, str]] = {}
    for rid, meta in pending.items():
        ctype = str(meta.get("casinoType") or "").strip()
        if not ctype:
            continue
        if ctype not in live_by_type:
            live_by_type[ctype] = _fetch_live_results_by_mid(ctype)
        code = live_by_type[ctype].get(rid)
        if not code:
            continue
        declare_live_casino_result({
            "roundId": rid,
            "result": code,
            "casinoType": ctype,
            "eventId": meta.get("eventId"),
        })


def auto_settle_user_pending_rounds(user_id: str, event_id: Any = None) -> None:
    """Live feed + casino_rounds — pending open bets settle."""
    sync_open_bets_from_live_results(user_id, event_id)
    db = get_db()
    q: dict = {"userId": user_id, "status": "open", "gameType": {"$ne": "aviator"}}
    if event_id not in (None, ""):
        q["eventId"] = int(event_id) if str(event_id).isdigit() else event_id

    seen: set[str] = set()
    for bet in db.casino_bets.find(q, {"roundId": 1, "eventId": 1}):
        rid = _casino_round_id(bet.get("roundId"))
        if not rid or rid in seen:
            continue
        seen.add(rid)
        rnd = db.casino_rounds.find_one({"roundId": rid, "isDeclare": True})
        if not rnd:
            continue
        result = rnd.get("result") or {}
        if not isinstance(result, dict):
            result = {"winner": str(result)}
        win_sid = str(result.get("sid") or rnd.get("winSid") or "").strip()
        winner = str(result.get("winner") or rnd.get("showResult") or "").strip()
        if not win_sid and winner:
            win_sid = winner
        settle_diamond_round_bets(
            rid,
            win_sid,
            winner,
            event_id=bet.get("eventId"),
            casino_type=str(rnd.get("gtype") or rnd.get("casinoType") or ""),
            result_details=result,
        )


def declare_live_casino_result(payload: dict) -> dict:
    """casino/syncLiveRoundResult — live socket result se round declare + settle."""
    payload = payload or {}
    round_id = _casino_round_id(payload.get("roundId") or payload.get("mid") or "")
    result_code = str(payload.get("result") or payload.get("win") or "").strip()
    casino_type = str(payload.get("casinoType") or payload.get("gtype") or "").strip()
    event_id = payload.get("eventId")

    if not round_id or not result_code:
        return {"message": "roundId and result required", "code": 1, "error": True, "data": {}}

    win_sid, winner_label = _winner_from_result_code(casino_type, result_code)
    if not win_sid:
        return {"message": "Unknown result code", "code": 1, "error": True, "data": {}}

    settled = settle_diamond_round_bets(
        round_id,
        win_sid,
        winner_label,
        event_id=event_id,
        casino_type=casino_type,
        result_details={"winner": winner_label, "sid": win_sid, "gtype": casino_type, "result": result_code},
    )
    if settled == 0:
        get_db().casino_rounds.update_one(
            {"roundId": round_id},
            {"$set": {
                "roundId": round_id,
                "eventId": event_id,
                "gtype": casino_type,
                "casinoType": casino_type,
                "isDeclare": True,
                "result": {"winner": winner_label, "sid": win_sid, "gtype": casino_type, "result": result_code},
                "showResult": winner_label,
                "declaredAt": _now(),
            }},
            upsert=True,
        )
    return {
        "message": "Casino round settled" if settled else "Round already settled",
        "code": 0,
        "error": False,
        "data": {"roundId": round_id, "settledBets": settled, "winner": winner_label},
    }


def casino_result_by_round_wise(payload: dict, auth_header: str = "") -> dict:
    """casino/resultByRoundWise — result cards + pending bets settle."""
    from mongodb.admin_compute import compute_casino_result_by_round

    payload = payload or {}
    round_id = _casino_round_id(payload.get("roundId") or payload.get("mid") or "")
    if round_id:
        rnd = get_db().casino_rounds.find_one({"roundId": round_id, "isDeclare": True})
        if rnd:
            result = rnd.get("result") or {}
            if not isinstance(result, dict):
                result = {"winner": str(result)}
            settle_diamond_round_bets(
                round_id,
                str(result.get("sid") or ""),
                str(result.get("winner") or rnd.get("showResult") or ""),
                event_id=payload.get("eventId") or rnd.get("eventId"),
                casino_type=str(rnd.get("gtype") or rnd.get("casinoType") or ""),
                result_details=result if isinstance(result, dict) else {},
            )

    rows = compute_casino_result_by_round(payload)
    return {
        "message": "Round result fetched",
        "code": 0,
        "error": False,
        "data": {"data": rows},
    }


def casino_round_wise_result(payload: dict) -> dict:
    """casino/roundWiseResult — admin/client round result list."""
    payload = payload or {}
    db = get_db()
    q: dict = {}
    if payload.get("eventId") is not None:
        q["eventId"] = payload["eventId"]
    if payload.get("roundId"):
        q["roundId"] = _casino_round_id(payload["roundId"])

    rows = []
    for doc in db.casino_rounds.find(q, {"_id": 0}).sort("declaredAt", -1).limit(50):
        result = doc.get("result") or {}
        if not isinstance(result, dict):
            result = {"winner": str(result)}
        rows.append({
            "eventId": doc.get("eventId"),
            "roundId": doc.get("roundId"),
            "gtype": doc.get("gtype") or doc.get("casinoType"),
            "result": result,
            "createdAt": _iso(doc.get("declaredAt") or doc.get("createdAt")),
        })
    return {
        "message": "Round result fetched",
        "code": 0,
        "error": False,
        "data": rows,
    }


def _bet_wins(bet: dict, win_sid: str, bet_for: str, result_json: dict, casino_type: str) -> bool:
    sid = str(bet.get("sid") or "").strip()
    selection = str(bet.get("selection") or "").strip()
    win_sid = str(win_sid or "").strip()
    bet_for = str(bet_for or "").strip()

    if sid and win_sid and sid == win_sid:
        return True

    game_cfg = result_json.get(casino_type) or {}
    category = game_cfg.get(bet_for) if isinstance(game_cfg, dict) else None
    winner_label = ""
    if isinstance(category, dict):
        winner_label = str(category.get(win_sid) or "").strip()

    if winner_label and selection and selection.lower() == winner_label.lower():
        return True
    if bet_for and selection and bet_for.lower() == selection.lower() and sid == win_sid:
        return True
    return False


def declare_manual_casino_result(payload: dict) -> dict:
    """casino/doManaualResult — round declare + bet settlement."""
    payload = payload or {}
    round_id = _casino_round_id(payload.get("roundId") or "")
    win_sid = str(payload.get("win") or "").strip()
    bet_for = str(payload.get("betFor") or "").strip()
    event_id = payload.get("eventId")

    if not round_id:
        return {"message": "roundId required", "code": 1, "error": True, "data": {}}
    if not win_sid:
        return {"message": "Result selection required", "code": 1, "error": True, "data": {}}

    db = get_db()
    q: dict = {"roundId": round_id, "status": "open"}
    if event_id is not None:
        q["eventId"] = event_id
    bets = list(db.casino_bets.find(q))
    if not bets:
        return {"message": "No open bets found for this round", "code": 1, "error": True, "data": {}}

    result_json = build_casino_result_json()
    casino_type = str(bets[0].get("casinoType") or "").strip()
    if not casino_type:
        game = find_casino_game_by_event_id(bets[0].get("eventId"))
        casino_type = _casino_type_from_game(game or {})

    winner_label = ""
    cat = (result_json.get(casino_type) or {}).get(bet_for, {})
    if isinstance(cat, dict):
        winner_label = str(cat.get(win_sid) or "").strip()

    settled = settle_diamond_round_bets(
        round_id,
        win_sid,
        winner_label or win_sid,
        event_id=event_id,
        casino_type=casino_type,
        result_details={"winner": winner_label or win_sid, "sid": win_sid, "betFor": bet_for, "gtype": casino_type},
        bet_for=bet_for,
    )

    return {
        "message": "Casino result declared successfully",
        "code": 0,
        "error": False,
        "data": {"roundId": round_id, "settledBets": settled, "winner": winner_label or win_sid},
    }


def _bet_ledger_date_str(bet: dict) -> str:
    ts = bet.get("ledgerDate") or bet.get("declaredAt") or bet.get("settledAt") or bet.get("createdAt")
    return _bet_ist_date_str({"createdAt": ts})


def _apply_casino_pl_to_user(user_id: str, pl: float) -> float:
    """Declared bet ka P/L user balance mein post karo."""
    db = get_db()
    user = db.users.find_one({"userId": user_id}) or {}
    coins = round(float(user.get("coins") or 0) + pl, 2)
    credit = round(float(user.get("creditLimit") or 0) + pl, 2)
    db.users.update_one(
        {"userId": user_id},
        {"$set": {"coins": coins, "creditLimit": credit, "updatedAt": _now()}},
    )
    return coins


def _upsert_day_wise_casino(date_str: str, bets: list[dict], games: dict) -> None:
    db = get_db()
    by_event: dict[Any, dict] = {}
    for bet in bets:
        if not _casino_bet_is_declared(bet):
            continue
        eid = bet.get("eventId")
        if eid is None:
            continue
        game = games.get(int(eid)) if str(eid).isdigit() else games.get(eid)
        bucket = by_event.setdefault(eid, {
            "date": date_str,
            "eventId": eid,
            "eventName": _casino_game_name(game, eid),
            "userNetProfitLoss": 0.0,
            "userOddsComm": 0.0,
            "clientOddsAmount": 0.0,
            "clientNetAmount": 0.0,
        })
        pl = float(bet.get("profitLoss") or 0)
        stake = float(bet.get("stake") or bet.get("amount") or 0)
        bucket["userNetProfitLoss"] = round(bucket["userNetProfitLoss"] + pl, 2)
        bucket["clientNetAmount"] = round(bucket["clientNetAmount"] + pl, 2)
        bucket["clientOddsAmount"] = round(bucket["clientOddsAmount"] + stake, 2)

    for eid, row in by_event.items():
        db.day_wise_casino.update_one(
            {"date": date_str, "eventId": eid},
            {"$set": row},
            upsert=True,
        )


def generate_diamond_ledger(payload: dict) -> dict:
    """casino/generateDiamondLedger — date ke declared diamond bets ka ledger post."""
    payload = payload or {}
    date_str = str(payload.get("date") or payload.get("fromDate") or "")[:10]
    if not date_str:
        return {"message": "Date required", "code": 1, "error": True, "data": {}}

    db = get_db()
    games = _casino_games_map(db)
    scoped = _casino_bets_in_scope(db, {"fromDate": date_str, "toDate": date_str})
    posted = 0
    touched_users: set[str] = set()
    day_bets: list[dict] = []

    for bet in scoped:
        if str(bet.get("gameType") or "diamond").lower() == "aviator":
            continue
        if not _casino_bet_is_declared(bet):
            continue
        if _bet_ledger_date_str(bet) != date_str:
            continue
        day_bets.append(bet)
        if bet.get("ledgerPosted"):
            continue

        user_id = str(bet.get("userId") or "")
        if not user_id:
            continue

        pl = float(bet.get("profitLoss") or 0)
        stake = float(bet.get("stake") or bet.get("amount") or 0)
        selection = str(bet.get("selection") or bet.get("playerName") or "Casino")
        round_id = str(bet.get("roundId") or "")
        game = games.get(int(bet.get("eventId"))) if bet.get("eventId") is not None and str(bet.get("eventId")).isdigit() else None
        game_name = _casino_game_name(game, bet.get("eventId"))

        coins = _apply_casino_pl_to_user(user_id, pl)
        desc = f"{game_name} — {selection}"
        if round_id:
            desc += f" (Round {round_id})"
        desc += f" P/L {pl:+.2f}"
        entry_type = "credit" if pl >= 0 else "debit"
        _ledger(user_id, round(abs(pl) if pl else stake, 2), desc, "casino", coins, entry_type=entry_type)

        db.casino_bets.update_one(
            {"betId": bet["betId"]},
            {"$set": {"ledgerPosted": True, "ledgerPostedAt": _now(), "ledgerDate": date_str}},
        )
        touched_users.add(user_id)
        posted += 1

    for uid in touched_users:
        sync_user_balance(uid)

    if day_bets:
        _upsert_day_wise_casino(date_str, day_bets, games)

    if posted:
        msg = f"Diamond casino ledger generated for {date_str} ({posted} entries)"
    else:
        msg = f"No pending diamond ledger entries for {date_str}"

    return {"message": msg, "code": 0, "error": False, "data": {"date": date_str, "posted": posted}}


def save_casino_result(payload: dict) -> dict:
    """casino/saveResult — casino result JSON refresh + persist (original site jaisa)."""
    payload = payload or {}
    result_json = build_casino_result_json(force_refresh=True)
    db = get_db()
    db.center_master_settings.update_one(
        {"key": "casinoResultJson"},
        {"$set": {
            "key": "casinoResultJson",
            "value": result_json,
            "isVankyResult": bool(payload.get("isVankyResult")),
            "updatedAt": _now(),
        }},
        upsert=True,
    )
    return {
        "message": "Casino result saved successfully",
        "code": 0,
        "error": False,
        "data": {"gameTypes": len(result_json)},
    }
