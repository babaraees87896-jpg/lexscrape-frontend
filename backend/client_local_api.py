"""Client site local API — scraped JSON se casino / sports serve karo."""

from __future__ import annotations

import copy
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from config import OUTPUT_DIR
from mongodb.auth import _extract_bearer, mongo_client_update_password, resolve_client_user, validate_session
from mongodb.bets import BET_ENDPOINTS, POSITION_ENDPOINTS, handle_bet_endpoint, _get_match, _open_bets_query, _resolve_runner_name
from mongodb.bet_logic import position_info_for_client_bet
from mongodb.bet_logic import is_fancy_market
from mongodb.db import get_db, ping

ROOT = Path(__file__).resolve().parent
API_DIR = ROOT / OUTPUT_DIR / "api_data"

# endpoint -> scraped JSON file (output/api_data/)
SCRAPE_FILES: dict[str, str] = {}

# Scraped JS se betting endpoints — hamesha local MongoDB (live api.ons3.co nahi)
BET_LOCAL_ENDPOINTS = frozenset({
    "sports/oddBetPlaced",
    "sports/sessionBetPlaced",
    "sports/meterKhadoOddEvenCricketCassinoBetPlace",
    "sports/betsList",
    "sports/userPositionByMarketId",
    "sports/clientListByMarketId",
    "user/clientBetListByMarketId",
    "halkabhari/inplayOddsPositionHalkaBhari",
    "casino/casinoBetPlace",
    "casino/avaitorGamePlace",
    "casino/avaitorCashOut",
    "casino/avaitorRoundLost",
    "casino/diamondBetsList",
    "casino/syncLiveRoundResult",
    "casino/resultByRoundWise",
    "casino/roundWiseResult",
    "matka/matkaPlaceBet",
    "matka/matkaBetList",
    "matka/matkaReportByUser",
})

# Local MongoDB login ke baad in endpoints ko live API par mat bhejo
PUBLIC_LOCAL = ("website/domainSettingByDomainName",)


def _load_json(name: str) -> Optional[dict]:
    path = API_DIR / name
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return None


def _load_casino_games() -> list[dict]:
    """MongoDB first (staff edits), scraped JSON fallback."""
    try:
        from mongodb.casino_api import all_casino_games

        rows = all_casino_games()
        if rows:
            return rows
    except Exception:
        pass
    raw = _load_json("casino_data.json")
    if raw and isinstance(raw.get("data"), list):
        return raw["data"]
    return []


def _load_matches() -> list[dict]:
    """MongoDB matches → scraped JSON fallback."""
    if ping():
        rows = list(get_db().matches.find({}, {"_id": 0}))
        if rows:
            return rows
    raw = _load_json("match_list.json")
    if raw and isinstance(raw.get("data"), list):
        return raw["data"]
    return []


def _normalize_match_date(value: str) -> str:
    """Frontend moment.js format: DD-MM-YYYY HH:mm:ss A"""
    if not value:
        return value
    value = str(value).strip()
    if re.search(r"\s(AM|PM)$", value, re.I):
        return value
    # "28-03-2026 19:30:00" → add PM (7 PM)
    m = re.match(r"^(\d{2}-\d{2}-\d{4}\s+\d{2}:\d{2}:\d{2})$", value)
    if m:
        hour = int(value.split()[1].split(":")[0])
        suffix = "AM" if hour < 12 else "PM"
        return f"{value} {suffix}"
    return value


from mongodb.matches_api import (
    LIVE_MATCHES,
    _blocked_market_ids,
    get_match_list as _shared_match_list,
    is_match_blocked,
    normalize_match_row as _normalize_match,
    post_live_api,
    sync_live_matches_to_db,
)


def _ok(data: Any, message: str = "OK") -> dict:
    return {"message": message, "code": 0, "error": False, "data": data}


def _empty_list(message: str = "OK") -> dict:
    return _ok([], message)


def _empty_obj(message: str = "OK") -> dict:
    return _ok({}, message)


def _rewrite_casino_video_urls(obj: Any) -> Any:
    """Point casino iframe streams at local /casino-stream/ proxy (avoids upstream CSP)."""
    if isinstance(obj, dict):
        for key in ("videoUrl1", "videoUrl2", "videoUrl3"):
            val = obj.get(key)
            if isinstance(val, str) and val:
                obj[key] = (
                    val.replace("https://casinostream.tresting.com", "/casino-stream")
                    .replace("http://casinostream.tresting.com", "/casino-stream")
                    .replace("https://stream.1ex99.in", "/casino-stream/stream99")
                    .replace("http://stream.1ex99.in", "/casino-stream/stream99")
                )
        for value in obj.values():
            if isinstance(value, (dict, list)):
                _rewrite_casino_video_urls(value)
    elif isinstance(obj, list):
        for item in obj:
            _rewrite_casino_video_urls(item)
    return obj


def _rewrite_casino_api_body(endpoint: str, body: dict) -> dict:
    if not endpoint.startswith("casino/"):
        return body
    rewritten = copy.deepcopy(body)
    _rewrite_casino_video_urls(rewritten)
    return rewritten


def casino_diamond_data(_payload: dict) -> dict:
    return _ok(_rewrite_casino_video_urls(copy.deepcopy(_load_casino_games())), "data fetched")


def casino_virtual_data(_payload: dict) -> dict:
    from mongodb.casino_api import virtual_casino_user_tiles

    return _ok(virtual_casino_user_tiles(), "data fetched")


def casino_by_event_id(payload: dict) -> dict:
    event_id = payload.get("eventId")
    try:
        from mongodb.casino_api import find_casino_game_by_event_id

        game = find_casino_game_by_event_id(event_id)
        if game:
            return _ok(_rewrite_casino_video_urls(copy.deepcopy(game)), "data fetched")
    except Exception:
        pass
    for game in _load_casino_games():
        if str(game.get("eventId")) == str(event_id):
            return _ok(_rewrite_casino_video_urls(copy.deepcopy(game)), "data fetched")
    return {"message": "Game not found", "code": 1, "error": True, "data": {}}


def match_list(payload: dict) -> dict:
    matches = _shared_match_list(payload, for_admin=False)
    return {"message": 0, "code": 0, "error": False, "data": matches}


def sport_by_market_id(payload: dict, auth_header: str = "") -> dict:
    market_id = str(payload.get("marketId", ""))
    event_id = str(payload.get("eventId") or "")
    if not market_id:
        return {"message": "marketId required", "code": 1, "error": True, "data": {}}

    match = None
    if LIVE_MATCHES:
        req_payload: dict = {"marketId": market_id}
        if event_id:
            req_payload["eventId"] = event_id
        live = post_live_api("sports/sportByMarketId", req_payload, _extract_bearer(auth_header) or "")
        if live and not live.get("error"):
            data = live.get("data")
            if isinstance(data, dict) and data:
                match = data
            elif isinstance(data, list) and data:
                match = data[0]
            if match:
                sync_live_matches_to_db([match])

    if not match:
        for row in _load_matches():
            if str(row.get("marketId")) == market_id:
                match = row
                break
            for sub in row.get("marketList") or []:
                if str(sub.get("marketId")) == market_id:
                    match = {**row, "marketList": [sub]}
                    break
            if match:
                break

    if not match:
        try:
            from scorecard_api import find_scrape_highlight_match

            match = find_scrape_highlight_match(market_id, event_id)
            if match:
                sync_live_matches_to_db([match])
        except Exception:
            pass

    if not match:
        return {"message": "Market not found", "code": 1, "error": True, "data": {}}
    if is_match_blocked(match, _blocked_market_ids()):
        return {"message": "Market is blocked", "code": 1, "error": True, "data": {}}
    try:
        from mongodb.wnp9_auto_decision import maybe_auto_settle_market

        maybe_auto_settle_market(
            str(match.get("marketId") or market_id),
            str(match.get("eventId") or event_id),
        )
    except Exception:
        pass
    return _ok(_normalize_match(copy.deepcopy(match)))


def _iso_dt(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value or "")


def _session_user(auth_header: str, payload: Optional[dict] = None) -> Optional[dict]:
    user, err = resolve_client_user(auth_header, payload)
    return user if not err else None


def _require_session(auth_header: str, payload: Optional[dict] = None) -> tuple[Optional[dict], Optional[dict]]:
    user, err = resolve_client_user(auth_header, payload)
    if err:
        return None, err
    return user, None


def _user_profile_data(user: dict) -> dict:
    """Logged-in user ka profile — scraped userDetails format."""
    data = {k: v for k, v in user.items() if k not in ("password", "_id")}
    for key in ("createdAt", "updatedAt", "lastLoginTime"):
        val = data.get(key)
        if hasattr(val, "isoformat"):
            data[key] = val.isoformat()
    data.setdefault("betChipsData", {})
    data.setdefault("betStatus", True)
    data.setdefault("matchStatus", True)
    data.setdefault("casinoStatus", True)
    data.setdefault("matkaStatus", True)
    return data


def _ledger_category_map(category: str) -> str:
    return {"sport": "cricket", "casino": "diamondCasino", "cash": "settle"}.get(category or "", category or "settle")


def _ledger_date_key(created_at: Any) -> str:
    dt = _parse_statement_date(created_at)
    return dt.strftime("%Y-%m-%d") if dt else ""


def _match_event_name(db, market_id: str, event_id: str) -> str:
    match = None
    if market_id:
        match = db.matches.find_one({"marketId": str(market_id)}, {"_id": 0, "eventName": 1, "matchName": 1})
    if not match and event_id:
        match = db.matches.find_one({"eventId": str(event_id)}, {"_id": 0, "eventName": 1, "matchName": 1})
    if match:
        return str(match.get("eventName") or match.get("matchName") or "")
    if event_id:
        return f"Event {event_id}"
    if market_id:
        return f"Market {market_id}"
    return "Sports"


_PASSBOOK_ONLY_TRANSFER_TYPES = frozenset({
    "deposit",
    "first_deposit",
    "withdraw",
    "user_create",
})


def _is_deposit_withdraw_entry(entry: dict) -> bool:
    """Deposit/withdraw — passbook only, client ledger se exclude."""
    transfer_type = str(entry.get("transferType") or "").lower()
    if transfer_type in _PASSBOOK_ONLY_TRANSFER_TYPES:
        return True
    desc = str(entry.get("description") or entry.get("remark") or "").lower()
    return any(k in desc for k in ("deposit", "withdrawal", "first deposit", "withdraw", "account created"))


def _ledger_entry_row(
    amount: float,
    event_name: str,
    remark: str,
    ledger_type: str,
    created_at: Any,
    *,
    market_id: Any = None,
    event_id: Any = None,
) -> dict:
    signed = round(float(amount), 2)
    return {
        "amount": signed,
        "remark": remark,
        "eventName": event_name,
        "ledgerType": ledger_type,
        "payment_type": "C" if signed >= 0 else "D",
        "marketId": str(market_id) if market_id not in (None, "") else None,
        "eventId": event_id,
        "date": _ledger_date_key(created_at),
        "createdAt": _iso_dt(created_at),
        "_sort": _parse_statement_date(created_at) or datetime.min.replace(tzinfo=timezone.utc),
    }


def _ledger_row_from_sports_bet(bet: dict, db) -> Optional[dict]:
    status = str(bet.get("status") or "open")
    stake = float(bet.get("stake") or 0)
    if stake <= 0:
        return None

    market_id = str(bet.get("marketId") or "")
    event_id = bet.get("eventId")
    event_name = _match_event_name(db, market_id, str(event_id or ""))
    remark = bet.get("runnerName") or bet.get("betFor") or bet.get("betType") or ""
    created = bet.get("createdAt")

    if status == "open":
        return _ledger_entry_row(-stake, event_name, f"{remark} (Open)", "cricket", created, market_id=market_id, event_id=event_id)

    pl = float(bet.get("profitLoss") or 0)
    settled = bet.get("settledAt") or created
    if pl > 0:
        return _ledger_entry_row(pl, event_name, remark, "cricket", settled, market_id=market_id, event_id=event_id)
    if pl < 0:
        return _ledger_entry_row(pl, event_name, remark, "cricket", settled, market_id=market_id, event_id=event_id)
    return _ledger_entry_row(-stake, event_name, f"{remark} (Lost)", "cricket", settled, market_id=market_id, event_id=event_id)


def _ledger_row_from_casino_bet(bet: dict) -> Optional[dict]:
    status = str(bet.get("status") or "open")
    stake = float(bet.get("stake") or 0)
    if stake <= 0:
        return None

    gt = str(bet.get("gameType") or "casino")
    event_id = bet.get("eventId")
    created = bet.get("createdAt")

    if gt == "aviator":
        event_name = "Aviator"
        remark = bet.get("roundId") or "Aviator"
        if bet.get("multiplier"):
            remark = f"{remark} @ {bet['multiplier']}x"
    else:
        event_name = str(bet.get("selection") or bet.get("casinoType") or "Diamond Casino")
        remark = str(bet.get("selection") or bet.get("roundId") or "Casino")

    if status == "open":
        return _ledger_entry_row(-stake, event_name, f"{remark} (Open)", "diamondCasino", created, event_id=event_id)

    pl = float(bet.get("profitLoss") or 0)
    settled = bet.get("settledAt") or created
    if pl > 0:
        return _ledger_entry_row(pl, event_name, remark, "diamondCasino", settled, event_id=event_id)
    if pl < 0:
        return _ledger_entry_row(pl, event_name, remark, "diamondCasino", settled, event_id=event_id)
    return _ledger_entry_row(-stake, event_name, f"{remark} (Lost)", "diamondCasino", settled, event_id=event_id)


def _ledger_row_from_matka_bet(bet: dict) -> Optional[dict]:
    status = str(bet.get("status") or "open")
    stake = float(bet.get("stake") or 0)
    if stake <= 0:
        return None

    event_id = bet.get("matkaEventId") or bet.get("eventId")
    num = bet.get("number") or bet.get("betNumber") or ""
    event_name = str(bet.get("matkaName") or bet.get("name") or f"Matka {event_id or ''}").strip()
    remark = str(num)
    created = bet.get("createdAt")

    if status == "open":
        return _ledger_entry_row(-stake, event_name, f"#{num} (Open)", "matka", created, event_id=event_id)

    pl = float(bet.get("profitLoss") or 0)
    settled = bet.get("settledAt") or created
    if pl > 0:
        return _ledger_entry_row(pl, event_name, remark, "matka", settled, event_id=event_id)
    if pl < 0:
        return _ledger_entry_row(pl, event_name, remark, "matka", settled, event_id=event_id)
    return _ledger_entry_row(-stake, event_name, f"#{num} (Lost)", "matka", settled, event_id=event_id)


def _apply_ledger_balances(rows: list[dict], current_coins: float) -> None:
    if not rows:
        return
    chrono = sorted(rows, key=lambda r: r.get("_sort") or datetime.min.replace(tzinfo=timezone.utc))
    total_pl = round(sum(float(r.get("amount") or 0) for r in chrono), 2)
    running = round(float(current_coins) - total_pl, 2)
    for row in chrono:
        running = round(running + float(row.get("amount") or 0), 2)
        row["balance"] = running


def _build_user_ledger_data(user_id: str, user: dict, payload: dict) -> dict:
    db = get_db()
    ledger_type = str(payload.get("ledgerType") or payload.get("type") or "all").strip()
    start = _parse_statement_date(payload.get("fromDate") or payload.get("startDate"))
    end = _parse_statement_date(payload.get("toDate") or payload.get("endDate"))
    if end and end.hour == 0 and end.minute == 0:
        end = end + timedelta(days=1) - timedelta(microseconds=1)

    rows: list[dict] = []

    for bet in db.sports_bets.find({"userId": user_id}, {"_id": 0}):
        created = bet.get("settledAt") or bet.get("createdAt")
        if not _statement_in_range(created, start, end):
            continue
        row = _ledger_row_from_sports_bet(bet, db)
        if row:
            rows.append(row)

    for bet in db.casino_bets.find({"userId": user_id}, {"_id": 0}):
        created = bet.get("settledAt") or bet.get("createdAt")
        if not _statement_in_range(created, start, end):
            continue
        row = _ledger_row_from_casino_bet(bet)
        if row:
            rows.append(row)

    for bet in db.matka_bets.find({"userId": user_id}, {"_id": 0}):
        created = bet.get("settledAt") or bet.get("createdAt")
        if not _statement_in_range(created, start, end):
            continue
        row = _ledger_row_from_matka_bet(bet)
        if row:
            rows.append(row)

    for entry in db.ledger_entries.find({"userId": user_id, "category": "cash"}, {"_id": 0}):
        if not _statement_in_range(entry.get("createdAt"), start, end):
            continue
        if _is_deposit_withdraw_entry(entry):
            continue
        amount = float(entry.get("amount") or 0)
        signed = amount if entry.get("type") == "credit" else -amount
        from mongodb.admin_compute import _cash_settle_remark
        rows.append(_ledger_entry_row(
            signed,
            "Cash",
            _cash_settle_remark(entry),
            "settle",
            entry.get("createdAt"),
            ledger_id=entry.get("ledgerId"),
        ))

    if ledger_type and ledger_type not in ("all", ""):
        rows = [r for r in rows if r.get("ledgerType") == ledger_type]

    _apply_ledger_balances(rows, float(user.get("coins") or 0))

    rows.sort(key=lambda r: r.get("_sort") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    for row in rows:
        row.pop("_sort", None)

    credit = debit = 0.0
    sport_ledger = diamond_ledger = matka_ledger = cash_ledger = 0.0
    for row in rows:
        amt = float(row.get("amount") or 0)
        lt = row.get("ledgerType") or ""
        if amt >= 0:
            credit += amt
        else:
            debit += abs(amt)
        if lt == "cricket":
            sport_ledger += amt
        elif lt == "diamondCasino":
            diamond_ledger += amt
        elif lt == "matka":
            matka_ledger += amt
        elif lt == "settle":
            cash_ledger += amt

    return {
        "totalCoins": round(float(user.get("coins") or 0), 2),
        "creditAmount": round(credit, 2),
        "debitAmount": round(debit, 2),
        "calAmount": round(credit - debit, 2),
        "sportLedger": round(sport_ledger, 2),
        "diamondCasinoLedger": round(diamond_ledger, 2),
        "intCasinoLedger": 0,
        "matkaLedger": round(matka_ledger, 2),
        "cashLedger": round(cash_ledger, 2),
        "ledgerData": rows,
    }


def _parse_statement_date(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    text = str(value).strip()
    for fmt in (
        "%Y-%m-%d %H:%M:%S.%f%z",
        "%Y-%m-%d %H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(text.replace("+00:00", "+0000"), fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _statement_in_range(created_at: Any, start: Optional[datetime], end: Optional[datetime]) -> bool:
    dt = _parse_statement_date(created_at)
    if not dt:
        return True
    if start and dt < start:
        return False
    if end and dt > end:
        return False
    return True


def _statement_row(
    amount: float,
    remark: str,
    created_at: Any,
    game_type: str,
    *,
    for_bet: int = 1,
    statement_for: str = "BET",
) -> dict:
    return {
        "amount": round(float(amount), 2),
        "gameType": game_type,
        "remark": remark,
        "userRemark": remark,
        "isComm": 0,
        "isCommission": 0,
        "forBet": for_bet,
        "statementFor": statement_for,
        "createdAt": _iso_dt(created_at),
    }


def _sports_statement_remark(bet: dict) -> str:
    parts = []
    name = bet.get("runnerName") or bet.get("betFor") or bet.get("marketKind") or "Sports"
    parts.append(str(name))
    bet_type = bet.get("betType")
    if bet_type:
        parts.append(str(bet_type))
    odds = bet.get("odds")
    if odds:
        parts.append(f"@ {odds}")
    run = bet.get("run")
    if run not in (None, "", "0"):
        parts.append(f"Run {run}")
    stake = bet.get("stake")
    if stake:
        parts.append(f"Stake {stake}")
    return " | ".join(parts)


def _casino_statement_remark(bet: dict) -> str:
    gt = str(bet.get("gameType") or "casino")
    if gt == "aviator":
        parts = ["Aviator"]
        if bet.get("roundId"):
            parts.append(f"Round {bet['roundId']}")
        if bet.get("multiplier"):
            parts.append(f"@{bet['multiplier']}x")
        parts.append(f"Stake {bet.get('stake', 0)}")
        return " | ".join(parts)
    sel = bet.get("selection") or bet.get("casinoType") or "Casino"
    return f"{sel} | Stake {bet.get('stake', 0)}"


def _rows_from_sports_bet(bet: dict) -> list[dict]:
    stake = float(bet.get("stake") or 0)
    if stake <= 0:
        return []
    remark = _sports_statement_remark(bet)
    created = bet.get("createdAt")
    settled = bet.get("settledAt") or created
    status = str(bet.get("status") or "open")
    pl = float(bet.get("profitLoss") or 0)
    rows = []

    if status == "open":
        rows.append(_statement_row(-stake, f"{remark} (Open)", created, "cricket"))
        return rows

    rows.append(_statement_row(-stake, f"{remark} (Bet)", created, "cricket"))
    if pl > 0:
        rows.append(_statement_row(stake + pl, f"{remark} (Win)", settled, "cricket"))
    elif pl < 0 and abs(pl + stake) > 0.001:
        rows.append(_statement_row(pl, f"{remark} (Loss)", settled, "cricket"))
    return rows


def _rows_from_casino_bet(bet: dict) -> list[dict]:
    stake = float(bet.get("stake") or 0)
    if stake <= 0:
        return []
    remark = _casino_statement_remark(bet)
    created = bet.get("createdAt")
    settled = bet.get("settledAt") or created
    status = str(bet.get("status") or "open")
    pl = float(bet.get("profitLoss") or 0)
    win_amount = float(bet.get("winAmount") or 0)
    rows = []

    if status == "open":
        rows.append(_statement_row(-stake, f"{remark} (Open)", created, "diamondCasino"))
        return rows

    rows.append(_statement_row(-stake, f"{remark} (Bet)", created, "diamondCasino"))
    if pl > 0:
        credit = win_amount if win_amount > 0 else round(stake + pl, 2)
        rows.append(_statement_row(credit, f"{remark} (Win)", settled, "diamondCasino"))
    elif pl < 0 and abs(pl + stake) > 0.001:
        rows.append(_statement_row(pl, f"{remark} (Loss)", settled, "diamondCasino"))
    return rows


def _rows_from_matka_bet(bet: dict) -> list[dict]:
    stake = float(bet.get("stake") or 0)
    if stake <= 0:
        return []
    num = bet.get("number") or bet.get("betNumber") or ""
    remark = f"Matka {bet.get('matkaEventId') or ''} #{num} | Stake {stake}".strip()
    created = bet.get("createdAt")
    settled = bet.get("settledAt") or created
    status = str(bet.get("status") or "open")
    pl = float(bet.get("profitLoss") or 0)
    rows = []

    if status == "open":
        rows.append(_statement_row(-stake, f"{remark} (Open)", created, "matka"))
        return rows

    rows.append(_statement_row(-stake, f"{remark} (Bet)", created, "matka"))
    if pl > 0:
        rows.append(_statement_row(stake + pl, f"{remark} (Win)", settled, "matka"))
    elif pl < 0 and abs(pl + stake) > 0.001:
        rows.append(_statement_row(pl, f"{remark} (Loss)", settled, "matka"))
    return rows


def _build_user_statement_rows(user_id: str, payload: dict, user: dict) -> list[dict]:
    db = get_db()
    start = _parse_statement_date(payload.get("startDate") or payload.get("fromDate"))
    end = _parse_statement_date(payload.get("endDate") or payload.get("toDate"))
    if end and end.hour == 0 and end.minute == 0:
        end = end + timedelta(days=1) - timedelta(microseconds=1)

    rows: list[dict] = []

    for bet in db.sports_bets.find({"userId": user_id}, {"_id": 0}).sort("createdAt", -1):
        if not _statement_in_range(bet.get("createdAt"), start, end):
            continue
        rows.extend(_rows_from_sports_bet(bet))

    for bet in db.casino_bets.find({"userId": user_id}, {"_id": 0}).sort("createdAt", -1):
        if not _statement_in_range(bet.get("createdAt"), start, end):
            continue
        rows.extend(_rows_from_casino_bet(bet))

    for bet in db.matka_bets.find({"userId": user_id}, {"_id": 0}).sort("createdAt", -1):
        if not _statement_in_range(bet.get("createdAt"), start, end):
            continue
        rows.extend(_rows_from_matka_bet(bet))

    for row in db.ledger_entries.find({"userId": user_id, "category": "cash"}, {"_id": 0}).sort("createdAt", -1):
        if not _statement_in_range(row.get("createdAt"), start, end):
            continue
        amount = float(row.get("amount") or 0)
        signed = amount if row.get("type") == "credit" else -amount
        rows.append(_statement_row(
            signed,
            row.get("description") or "Account entry",
            row.get("createdAt"),
            _ledger_category_map(row.get("category") or "cash"),
            for_bet=0,
            statement_for="ACCOUNT_STATEMENT",
        ))

    for row in rows:
        row["_sort"] = _parse_statement_date(row.get("createdAt")) or datetime.min.replace(tzinfo=timezone.utc)
    _apply_ledger_balances(rows, float(user.get("coins") or 0))
    for row in rows:
        row.pop("_sort", None)

    rows.sort(key=lambda r: _parse_statement_date(r.get("createdAt")) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return rows


def _is_winpro_statement_request(payload: dict) -> bool:
    return any(
        payload.get(key) not in (None, "")
        for key in ("pageNo", "size", "fromDate", "toDate", "startDate", "endDate", "statementFor")
    )


def _filter_statement_rows(rows: list[dict], payload: dict) -> list[dict]:
    statement_for = str(payload.get("statementFor") or "").strip()
    if statement_for == "ACCOUNT_STATEMENT":
        return [row for row in rows if row.get("statementFor") == "ACCOUNT_STATEMENT"]
    if statement_for == "profitLoss":
        return [row for row in rows if row.get("statementFor") != "ACCOUNT_STATEMENT"]
    return rows


def _statement_balance_before(rows: list[dict], current_coins: float) -> float:
    chrono = sorted(
        rows,
        key=lambda r: _parse_statement_date(r.get("createdAt")) or datetime.min.replace(tzinfo=timezone.utc),
    )
    total_pl = round(sum(float(r.get("amount") or 0) for r in chrono), 2)
    return round(float(current_coins) - total_pl, 2)


def _is_winpro_date_only_request(payload: dict) -> bool:
    """Winpro Submit button — sirf fromDate/toDate, bina pageNo/size/statementFor."""
    has_dates = any(
        payload.get(key) not in (None, "")
        for key in ("fromDate", "toDate", "startDate", "endDate")
    )
    has_paging = any(
        payload.get(key) not in (None, "")
        for key in ("pageNo", "size", "statementFor")
    )
    return has_dates and not has_paging


def _winpro_statement_page(payload: dict) -> dict:
    """Winpro MY ACCOUNT STATEMENT — scraped API jaisa paginated envelope."""
    return {
        "statementData": [],
        "totalCount": 0,
        "balanceAmount": "0.00",
    }


def _format_winpro_statement_response(rows: list[dict], current_coins: float, payload: dict) -> dict:
    """Winpro accountstatement — original API jaisa statementData + balanceAmount."""
    balance_before = _statement_balance_before(rows, current_coins)
    for row in rows:
        row["newAmount"] = row.get("balance", 0)

    if _is_winpro_date_only_request(payload):
        return {
            "message": f"{balance_before:.2f}",
            "code": 0,
            "error": False,
            "data": rows,
        }

    page_no = max(int(payload.get("pageNo") or 1), 1)
    size = max(int(payload.get("size") or 10), 1)
    total_count = len(rows)
    start_idx = (page_no - 1) * size
    page_rows = rows[start_idx:start_idx + size]

    return {
        "message": 0,
        "code": 0,
        "error": False,
        "data": {
            "statementData": page_rows,
            "totalCount": total_count,
            "balanceAmount": f"{balance_before:.2f}",
        },
    }


def user_statement(payload: dict, auth_header: str) -> dict:
    """user/userStatement — ClientStatement.js: logged-in user ke saare bets (scraped passbook)."""
    winpro_req = _is_winpro_statement_request(payload)
    user = _session_user(auth_header)
    if not user:
        empty: Any = _winpro_statement_page(payload) if winpro_req else []
        return {"message": "Session expired", "code": 401, "error": True, "data": empty}

    user_id = user["userId"]
    if payload.get("userId") and str(payload.get("userId")) != str(user_id):
        empty = _winpro_statement_page(payload) if winpro_req else []
        return {"message": "Unauthorized", "code": 403, "error": True, "data": empty}

    rows = _build_user_statement_rows(user_id, payload, user)
    rows = _filter_statement_rows(rows, payload)
    current_coins = float(user.get("coins") or 0)

    if _is_winpro_statement_request(payload):
        return _format_winpro_statement_response(rows, current_coins, payload)

    return _ok(rows, "Passbook List Fetched Successfully")


def user_ledger(payload: dict, auth_header: str) -> dict:
    """user/userLedger — match-wise overall rows (scraped /app/ledger jaisa)."""
    user = _session_user(auth_header)
    if not user:
        return {
            "message": "Session expired",
            "code": 401,
            "error": True,
            "data": {"ledgerData": [], "totalCoins": 0, "creditAmount": 0, "debitAmount": 0, "calAmount": 0},
        }

    user_id = user["userId"]
    if payload.get("userId") and str(payload.get("userId")) != str(user_id):
        return {
            "message": "Unauthorized",
            "code": 403,
            "error": True,
            "data": {"ledgerData": [], "totalCoins": 0, "creditAmount": 0, "debitAmount": 0, "calAmount": 0},
        }

    from mongodb.admin_compute import compute_user_ledger

    req = dict(payload or {})
    req["userId"] = user_id
    data = compute_user_ledger(req, session_user=user)
    return _ok(data, "Ledger List fetched Successfully")


def complete_ledger_details(payload: dict, auth_header: str) -> dict:
    """user/completeLedgerDetails — ViewStatement page (marketId) ya full ledger."""
    payload = dict(payload or {})
    if str(payload.get("marketId") or "").strip():
        return client_plus_minus(payload, auth_header)
    body = user_ledger({**payload, "ledgerType": "all"}, auth_header)
    if body.get("error"):
        return body
    return _ok(body.get("data") or {}, "Ledger details fetched")


def _empty_plus_minus() -> dict:
    return _ok({
        "oddsBetsData": [],
        "sessionBetsData": [],
        "completeData": {
            "clientOddsAmount": 0,
            "clientSessionAmount": 0,
            "clientOddsComm": 0,
            "clientSessionComm": 0,
            "clientNetAmount": 0,
        },
    }, "Plus Minus fetched successfully")


def client_plus_minus(payload: dict, auth_header: str) -> dict:
    """bluexchReports/clientPlusMinus — ViewStatement-C_6_jLLe.js"""
    user, err = _require_session(auth_header, payload)
    if err:
        return err

    market_id = str(payload.get("marketId") or "")
    user_id = user["userId"]
    if not market_id:
        return _empty_plus_minus()

    db = get_db()
    bet_query = _open_bets_query(user_id, market_id, payload)
    bet_query.pop("status", None)
    bets = list(db.sports_bets.find(bet_query, {"_id": 0}).sort("createdAt", 1))

    odds_bets = []
    session_bets = []
    odds_pl = session_pl = 0.0
    open_odds_positions: dict[str, float] = {}

    for bet in bets:
        if is_fancy_market(str(bet.get("betFor") or ""), str(bet.get("oddsType") or ""), str(bet.get("gtype") or "")):
            pl = float(bet.get("profitLoss") or 0)
            session_pl += pl
            session_bets.append({
                "sessionName": bet.get("runnerName") or bet.get("gtype") or "Session",
                "odds": bet.get("odds"),
                "amount": bet.get("stake"),
                "run": bet.get("run"),
                "type": bet.get("betType"),
                "decisionRun": bet.get("decisionRun") or 0,
                "profitLoss": pl,
                "isDeleted": 1 if bet.get("status") == "deleted" else 0,
                "gtype": bet.get("gtype") or bet.get("fancyType") or "fancy",
                "commPerm": 1,
            })
        else:
            status = str(bet.get("status") or "open").lower()
            pl = float(bet.get("profitLoss") or 0)
            match_row = _get_match(market_id, str(bet.get("eventId") or ""))
            pos_info = position_info_for_client_bet(bet, match_row)
            if status == "open":
                for k, v in pos_info.items():
                    open_odds_positions[str(k)] = round(open_odds_positions.get(str(k), 0) + float(v), 2)
            else:
                odds_pl += pl
            odds_bets.append({
                "positionInfo": pos_info,
                "odds": bet.get("odds"),
                "amount": bet.get("stake"),
                "type": str(bet.get("betType") or "L").upper(),
                "teamName": _resolve_runner_name(
                    match_row,
                    bet.get("selectionId"),
                    str(bet.get("runnerName") or ""),
                    market_id,
                ),
                "oddsType": bet.get("oddsType") or "bookmaker",
                "profitLoss": pl,
                "isDeclare": 1 if bet.get("isDeclare") else 0,
            })

    if open_odds_positions and odds_pl == 0:
        odds_pl = round(min(open_odds_positions.values()), 2)

    return _ok({
        "oddsBetsData": odds_bets,
        "sessionBetsData": session_bets,
        "completeData": {
            "clientOddsAmount": round(odds_pl, 2),
            "clientSessionAmount": round(session_pl, 2),
            "clientOddsComm": 0,
            "clientSessionComm": 0,
            "clientNetAmount": round(odds_pl + session_pl, 2),
        },
    }, "Plus Minus fetched successfully")


def domain_setting_by_domain_name(payload: dict) -> dict:
    from config import HOST
    from mongodb.domains_api import domain_setting_response

    payload = dict(payload or {})
    if not payload.get("domainName") and not payload.get("domainUrl"):
        payload["domainName"] = HOST
    return domain_setting_response(payload)


def user_profit_loss(payload: dict, auth_header: str) -> dict:
    """reports/userProfitLoss — dashboard P/L."""
    user = _session_user(auth_header)
    if not user:
        return {"message": "Session expired", "code": 401, "error": True, "data": {}}
    return _ok({
        "profitLoss": float(user.get("profitLoss") or 0),
        "exposure": float(user.get("exposure") or 0),
        "coins": float(user.get("coins") or 0),
    })


def user_details(payload: dict, auth_header: str) -> dict:
    user, err = _require_session(auth_header, payload)
    if err:
        return err
    return _ok(_user_profile_data(user))


def user_account_details(payload: dict, auth_header: str) -> dict:
    """user/userAccountDetails — sirf logged-in user ka account."""
    user, err = _require_session(auth_header, payload)
    if err:
        return err
    profile = _user_profile_data(user)
    return _ok({
        "userId": profile.get("userId"),
        "username": profile.get("username"),
        "name": profile.get("name"),
        "mobile": profile.get("mobile", ""),
        "coins": profile.get("coins", 0),
        "exposure": profile.get("exposure", 0),
        "creditLimit": profile.get("creditLimit", profile.get("coins", 0)),
        "profitLoss": profile.get("profitLoss", 0),
        "betStatus": profile.get("betStatus", True),
        "matchStatus": profile.get("matchStatus", True),
        "casinoStatus": profile.get("casinoStatus", True),
        "matkaStatus": profile.get("matkaStatus", True),
        "betChipsData": profile.get("betChipsData", {}),
        "isOneClickBet": profile.get("isOneClickBet", False),
        "oneClickBetAmount": profile.get("oneClickBetAmount", 10),
    })


HANDLERS = {
    "casino/getDiamondCasinoData": casino_diamond_data,
    "casino/getDiamondCasinoByEventId": casino_by_event_id,
    "casino/getVirtualCasinoData": casino_virtual_data,
    "sports/matchList": match_list,
    "sports/sportByMarketId": sport_by_market_id,
    "website/domainSettingByDomainName": domain_setting_by_domain_name,
    "user/userDetails": user_details,
    "user/userAccountDetails": user_account_details,
    "user/updateUserPassword": mongo_client_update_password,
    "user/userStatement": user_statement,
    "user/userLedger": user_ledger,
    "user/completeLedgerDetails": complete_ledger_details,
    "bluexchReports/clientPlusMinus": client_plus_minus,
    "reports/userProfitLoss": user_profit_loss,
}


def _has_valid_session(auth_header: str) -> bool:
    token = _extract_bearer(auth_header)
    return bool(token and validate_session(token))


def should_serve_local(endpoint: str, auth_header: str, use_mongo_auth: bool) -> bool:
    if not use_mongo_auth:
        return False
    endpoint = endpoint.lstrip("/").split("?")[0]
    if endpoint in PUBLIC_LOCAL:
        return True
    if endpoint in BET_LOCAL_ENDPOINTS or endpoint in BET_ENDPOINTS or endpoint in POSITION_ENDPOINTS:
        return True
    if endpoint in HANDLERS:
        return True
    if endpoint.startswith(("sports/", "casino/", "matka/", "halkabhari/", "user/client")):
        return True
    if endpoint.startswith(("user/", "reports/", "bpexch/", "bluexchReports/")):
        return endpoint not in ("user/login", "user/logout")
    return False


def build_local_api_response(
    endpoint: str,
    payload: dict | None = None,
    auth_header: str = "",
) -> Optional[bytes]:
    endpoint = endpoint.lstrip("/").split("?")[0]
    payload = payload or {}

    bet_body = handle_bet_endpoint(endpoint, payload, auth_header)
    if bet_body is not None:
        return json.dumps(bet_body, default=str).encode("utf-8")

    handler = HANDLERS.get(endpoint)
    if handler:
        if endpoint.startswith(("user/", "bluexchReports/", "reports/", "bpexch/")) or endpoint == "sports/sportByMarketId":
            body = handler(payload, auth_header)
        else:
            body = handler(payload)
        body = _rewrite_casino_api_body(endpoint, body)
        return json.dumps(body, default=str).encode("utf-8")

    rel = SCRAPE_FILES.get(endpoint)
    if rel:
        scraped = _load_json(rel)
        if scraped:
            scraped = _rewrite_casino_api_body(endpoint, scraped)
            return json.dumps(scraped, default=str).encode("utf-8")

    # Safe defaults — live API par mat jao
    if endpoint.startswith("casino/"):
        if "List" in endpoint or "Result" in endpoint or "Report" in endpoint:
            body = _empty_list()
        elif endpoint.endswith("getDiamondCasinoData"):
            body = _ok(_rewrite_casino_video_urls(copy.deepcopy(_load_casino_games())), "data fetched")
        else:
            body = _empty_obj()
    elif endpoint.startswith("sports/"):
        body = _empty_list() if "List" in endpoint or "Position" in endpoint else _empty_obj()
    elif endpoint.startswith("matka/"):
        body = _empty_list() if "List" in endpoint or "Report" in endpoint else _empty_obj()
    elif endpoint.startswith("reports/") or endpoint.startswith("bpexch/"):
        if not _has_valid_session(auth_header):
            body = {"message": "Session expired", "code": 401, "error": True, "data": []}
        else:
            body = _empty_list()
    elif endpoint.startswith("bluexchReports/"):
        if not _has_valid_session(auth_header):
            body = {"message": "Session expired", "code": 401, "error": True, "data": {}}
        else:
            body = _empty_plus_minus()
    elif endpoint.startswith("user/"):
        if not _has_valid_session(auth_header):
            body = {"message": "Session expired", "code": 401, "error": True, "data": {}}
        elif "Statement" in endpoint:
            body = _empty_list("Passbook List Fetched Successfully")
        elif "Ledger" in endpoint:
            body = user_ledger(payload, auth_header)
        else:
            body = _empty_obj()
    else:
        return None

    return json.dumps(body, default=str).encode("utf-8")
