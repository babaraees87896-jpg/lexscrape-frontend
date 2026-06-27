"""Admin reports — MongoDB collections se compute (scraped site jaisa)."""

from __future__ import annotations

import copy
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from mongodb.bet_logic import (
    is_fancy_market,
    normalize_team_entry,
    parse_team_selections,
    resolve_team_name,
    settle_fancy_bet,
    team_label_from_row,
)
from mongodb.db import get_db

USER_TYPE_CHILD = {
    "owner": "subowner",
    "subowner": "superadmin",
    "superadmin": "admin",
    "admin": "subadmin",
    "subadmin": "master",
    "master": "superagent",
    "superagent": "agent",
    "agent": "client",
}

USER_TYPE_PRIORITY = {
    "owner": 9, "subowner": 8, "superadmin": 7, "admin": 6,
    "subadmin": 5, "master": 4, "superagent": 3, "agent": 2, "client": 1,
}

PLUS_MINUS_LEVELS = (
    "owner", "subowner", "superadmin", "admin", "subadmin", "master", "superagent", "agent",
)
PLUS_MINUS_PARENT_CHILD = tuple(
    zip(PLUS_MINUS_LEVELS, PLUS_MINUS_LEVELS[1:] + ("client",))
)
OBJECT_TOTAL_PREFIX = {
    "agent": "agentTotal",
    "superagent": "superagentTotal",
    "master": "masterTotal",
    "subadmin": "subadminIdTotal",
    "admin": "adminTotal",
    "superadmin": "superadminTotal",
    "subowner": "subownerTotal",
    "owner": "ownerTotal",
}
INNER_LEVELS_CAP = {
    "owner": "Owner",
    "subowner": "Subowner",
    "superadmin": "Superadmin",
    "admin": "Admin",
    "subadmin": "Subadmin",
    "master": "Master",
    "superagent": "Superagent",
    "agent": "Agent",
}


def _num(val, default=0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _parse_date(val: Any) -> Optional[datetime]:
    if val is None or val == "":
        return None
    if isinstance(val, datetime):
        return val if val.tzinfo else val.replace(tzinfo=timezone.utc)
    s = str(val).strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(s[:19], fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


IST = timezone(timedelta(hours=5, minutes=30))


def _bet_created_at(bet: dict) -> Optional[datetime]:
    created = bet.get("createdAt")
    if hasattr(created, "timestamp"):
        return created if getattr(created, "tzinfo", None) else created.replace(tzinfo=timezone.utc)
    return _parse_date(created)


def _bet_ist_date_str(bet: dict) -> str:
    created = _bet_created_at(bet)
    if not created:
        return ""
    if not created.tzinfo:
        created = created.replace(tzinfo=timezone.utc)
    return created.astimezone(IST).strftime("%Y-%m-%d")


def _bet_net_pl(bet: dict) -> float:
    if bet.get("isDeclare") or str(bet.get("status", "")).lower() in ("settled", "won", "lost"):
        return _num(bet.get("profitLoss", 0))
    pos = bet.get("positionInfo") or {}
    if pos:
        vals = [_num(v) for v in pos.values()]
        return min(vals) if vals else -_num(bet.get("stake", 0))
    return -_num(bet.get("stake", bet.get("amount", 0)))


def _sport_bet_is_declared(bet: dict) -> bool:
    status = str(bet.get("status") or "open").lower()
    return bool(bet.get("isDeclare")) or status in ("settled", "won", "lost")


def _find_user(db, user_id: str) -> dict | None:
    if not user_id:
        return None
    user = db.users.find_one({"userId": user_id, "isDeleted": {"$ne": True}})
    if user:
        return user
    return db.users.find_one({"username": str(user_id).upper(), "isDeleted": {"$ne": True}})


def _user_data_row(user: dict) -> dict:
    uid = user.get("userId")
    ut = str(user.get("userType") or "")
    return {
        "userId": uid,
        "id": uid,
        "username": user.get("username"),
        "name": user.get("name"),
        "userType": ut,
        "parentId": user.get("parentId"),
        "creatorId": user.get("creatorId") or user.get("parentId") or "",
        "userPriority": user.get("userPriority", USER_TYPE_PRIORITY.get(ut, 0)),
        "coins": user.get("coins", 0),
        "exposure": user.get("exposure", 0),
    }


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


def _orphan_scraped_client_ids(db) -> set[str]:
    """Live scrape se aaye clients jinka parent local DB mein nahi hai."""
    orphans: set[str] = set()
    for user in db.users.find(
        {"userType": "client", "isDeleted": {"$ne": True}},
        {"userId": 1, "parentId": 1},
    ):
        pid = user.get("parentId")
        if not pid:
            continue
        if not db.users.find_one({"userId": str(pid)}, {"_id": 1}):
            orphans.add(str(user["userId"]))
    return orphans


def _session_downline_ids(db, session_user: dict) -> set[str]:
    """Logged-in user ke saare downline userId — BlueWin OW1000 seed tree se link."""
    root_id = str(session_user.get("userId") or "")
    if not root_id:
        return set()
    downline = _collect_descendant_ids(db, root_id)
    if not downline:
        uname = str(session_user.get("username") or "").upper()
        if uname == "OW1000" or root_id == "uid-ow1000":
            seed = db.users.find_one(
                {"username": "OWNER001", "isDeleted": {"$ne": True}},
                {"userId": 1},
            )
            if seed:
                downline = _collect_descendant_ids(db, str(seed["userId"]))
    viewer_type = str(session_user.get("userType") or "").lower()
    if viewer_type in ("owner", "subowner", "superadmin"):
        downline |= _orphan_scraped_client_ids(db)
    return downline


def _session_allowed_ids(db, session_user: dict) -> set[str]:
    """Logged-in user + unki poori downline — hierarchy scope checks."""
    sid = str(session_user.get("userId") or "")
    if not sid:
        return set()
    return _session_downline_ids(db, session_user) | {sid}


def _session_may_access_user(db, session_user: dict | None, target_id: str) -> bool:
    if not session_user or not target_id:
        return True
    return str(target_id) in _session_allowed_ids(db, session_user)


def _client_ids_in_subtree(db, root_id: str) -> set[str]:
    if not root_id:
        return set()
    subtree = _collect_descendant_ids(db, root_id)
    subtree.add(root_id)
    clients: set[str] = set()
    for uid in subtree:
        user = _find_user(db, uid)
        if user and str(user.get("userType") or "").lower() == "client":
            clients.add(uid)
    return clients


def _profit_loss_category(source: str, bet: dict) -> str:
    if source in ("sport", "matka"):
        return "event"
    if source == "casino":
        if str(bet.get("gameType") or "").lower() == "aviator":
            return "live_casino"
        return "casino"
    return "all"


def _profit_loss_sport_event_name(db, bet: dict) -> str:
    market_id = str(bet.get("marketId") or "")
    event_id = str(bet.get("eventId") or "")
    match = _find_match_doc(db, market_id)
    if not match and event_id:
        match = db.matches.find_one({"eventId": event_id}, {"_id": 0, "eventName": 1, "matchName": 1})
    if match:
        return str(match.get("eventName") or match.get("matchName") or f"Event {event_id}")
    label = bet.get("marketType") or bet.get("runnerName") or "Sports"
    return str(label)


def _profit_loss_casino_event_name(bet: dict, games: dict) -> str:
    event_id = bet.get("eventId")
    if str(bet.get("gameType") or "").lower() == "aviator":
        return "Aviator"
    game = None
    if event_id is not None:
        try:
            game = games.get(int(event_id))
        except (TypeError, ValueError):
            game = games.get(event_id)
    name = _casino_game_name(game, event_id)
    if not name or name.startswith("Casino "):
        name = str(bet.get("selection") or bet.get("casinoType") or name)
    return name


def _profit_loss_matka_event_name(bet: dict) -> str:
    return str(
        bet.get("matkaName")
        or bet.get("name")
        or f"Matka {bet.get('matkaEventId') or bet.get('eventId') or ''}"
    ).strip()


def _bets_for_market(db, market_id: str, user_ids: set[str] | None = None) -> list[dict]:
    """Market + same eventId par saari sports bets (bookmaker/fancy alag marketId)."""
    market_id = str(market_id)
    match = _find_match_doc(db, market_id)
    event_id = str(match.get("eventId") or "") if match else ""

    clauses: list[dict] = [{"marketId": market_id}]
    if event_id:
        clauses.append({"eventId": event_id})
    q: dict = {"$or": clauses} if len(clauses) > 1 else clauses[0]
    if user_ids is not None:
        q["userId"] = {"$in": list(user_ids)}

    seen: set[str] = set()
    rows: list[dict] = []
    for bet in db.sports_bets.find(q, {"_id": 0}):
        if bet.get("isDeleted"):
            continue
        bid = str(bet.get("betId") or "")
        if bid and bid in seen:
            continue
        if bid:
            seen.add(bid)
        rows.append(bet)
    return rows


def _declared_bets_for_market(
    db,
    market_id: str,
    user_ids: set[str] | None = None,
) -> list[dict]:
    """Company report / owner P&L — sirf declared bets (scrape jaisa, open exposure nahi)."""
    return [
        b for b in _bets_for_market(db, market_id, user_ids)
        if _sport_bet_is_declared(b) and not b.get("isDeleted")
    ]


def _aggregate_pl_by_user(bets: list[dict]) -> dict[str, float]:
    totals: dict[str, float] = {}
    for bet in bets:
        uid = str(bet.get("userId") or "")
        if not uid:
            continue
        totals[uid] = round(totals.get(uid, 0) + _bet_net_pl(bet), 2)
    return totals


def _aggregate_downline_company_row(
    child_user: dict,
    client_rows: list[dict],
    viewer_type: str,
    child_type: str,
    market_id: str,
) -> dict:
    """Company report row — subtree ke saare clients ka aggregate."""
    odds_amt = round(sum(_num(r.get("clientOddsAmount")) for r in client_rows), 2)
    session_amt = round(sum(_num(r.get("clientSessionAmount")) for r in client_rows), 2)
    client_net = round(sum(_num(r.get("clientNetAmount")) for r in client_rows), 2)
    odds_comm = round(sum(_num(r.get(f"{viewer_type}OddsComm")) for r in client_rows), 2)
    session_comm = round(sum(_num(r.get(f"{viewer_type}SessionComm")) for r in client_rows), 2)
    ledger_amt = _drill_ledger_amount(client_rows, viewer_type, child_type)
    return {
        "_id": child_user["userId"],
        "userData": _user_data_row(child_user),
        "downlineUserType": child_type,
        "marketId": market_id,
        "clientOddsAmount": odds_amt,
        "clientSessionAmount": session_amt,
        "clientNetAmount": client_net,
        "userOddsComm": odds_comm,
        "userSessionComm": session_comm,
        "userLedgerAmt": ledger_amt,
        "userNetProfitLoss": ledger_amt,
    }


def _client_plus_rows_for_subtree(
    bets_by_client: dict[str, list[dict]],
    subtree: set[str],
    users_by_id: dict[str, dict],
) -> list[dict]:
    rows: list[dict] = []
    for client_id, client_bets in bets_by_client.items():
        if client_id not in subtree:
            continue
        client = users_by_id.get(client_id)
        if not client or client.get("userType") != "client":
            continue
        chain_users = _chain_users(_chain_ids_for_client(client, users_by_id), users_by_id)
        rows.append(_build_client_plus_row(client, chain_users, client_bets))
    return rows


def compute_plus_minus_user_wise(payload: dict) -> list[dict]:
    """reports/getPlusMinusByMarketIdByUserWise — company report downline rows."""
    payload = payload or {}
    market_id = str(payload.get("marketId") or "")
    user_id = str(payload.get("userId") or "")
    viewer_type = str(payload.get("userType") or "").lower()
    if not market_id or not user_id or not viewer_type:
        return []

    db = get_db()
    child_type = USER_TYPE_CHILD.get(viewer_type, "client")
    users_by_id = _users_index(db)

    direct_children = list(db.users.find({
        "parentId": user_id,
        "userType": child_type,
        "isDeleted": {"$ne": True},
    }, {"_id": 0}))

    downline_ids = _collect_descendant_ids(db, user_id) | {user_id}
    bets = _declared_bets_for_market(db, market_id, downline_ids)
    if not bets:
        return []

    bets_by_client: dict[str, list[dict]] = {}
    for bet in bets:
        uid = str(bet.get("userId") or "")
        if uid:
            bets_by_client.setdefault(uid, []).append(bet)

    rows: list[dict] = []
    for child in direct_children:
        cid = str(child["userId"])
        subtree = _collect_descendant_ids(db, cid) | {cid}
        client_rows = _client_plus_rows_for_subtree(bets_by_client, subtree, users_by_id)
        if not client_rows and child_type == "client" and cid in bets_by_client:
            client = users_by_id.get(cid) or child
            chain_users = _chain_users(_chain_ids_for_client(client, users_by_id), users_by_id)
            client_rows = [_build_client_plus_row(client, chain_users, bets_by_client[cid])]
        if not client_rows:
            continue
        rows.append(_aggregate_downline_company_row(
            child, client_rows, viewer_type, child_type, market_id
        ))

    if not rows and child_type == "client":
        user = users_by_id.get(user_id)
        if user and user.get("userType") == "client" and user_id in bets_by_client:
            chain_users = _chain_users(_chain_ids_for_client(user, users_by_id), users_by_id)
            client_rows = [_build_client_plus_row(user, chain_users, bets_by_client[user_id])]
            rows.append(_aggregate_downline_company_row(
                user, client_rows, viewer_type, child_type, market_id
            ))

    return rows


def _comm_rate(user: dict, kind: str) -> float:
    key = {
        "odds": "matchCommission",
        "session": "sessionCommission",
        "casino": "casinoCommission",
    }.get(kind, "matchCommission")
    return _num(user.get(key, 0)) / 100.0


def _commission_from_stake(stake: float, rate: float) -> float:
    return round(stake * rate, 2)


def _commission_diff_rate(parent: dict, child: dict, kind: str) -> float:
    """Scrape — upline commission = parent% minus immediate downline%."""
    return max(_comm_rate(parent, kind) - _comm_rate(child, kind), 0.0)


def _share_amount(client_net: float, parent_share: float, child_share: float) -> float:
    diff = max(_num(parent_share) - _num(child_share), 0)
    return round(-client_net * diff / 100.0, 2)


def _owner_chain_user(
    chain_users: dict[str, dict],
    client: dict | None = None,
    users_by_id: dict[str, dict] | None = None,
) -> dict:
    """Owner — chain_users se, ya client parent walk (broken/missing upline par)."""
    owner = chain_users.get("owner")
    if owner:
        return owner
    if client and users_by_id:
        cur: dict | None = client
        while cur:
            if str(cur.get("userType") or "") == "owner":
                return cur
            pid = str(cur.get("parentId") or "")
            cur = users_by_id.get(pid) if pid else None
    return {}


def _plus_minus_owner_odds_comm(
    odds_stake: float,
    chain_users: dict[str, dict],
    client: dict | None = None,
    users_by_id: dict[str, dict] | None = None,
) -> float:
    """Scrape — har upline column par owner ka match commission (M Comm)."""
    owner = _owner_chain_user(chain_users, client, users_by_id)
    if owner:
        return _commission_from_stake(odds_stake, _comm_rate(owner, "odds"))
    return 0.0


def _plus_minus_owner_session_comm(
    session_stake: float,
    chain_users: dict[str, dict],
    client: dict | None = None,
    users_by_id: dict[str, dict] | None = None,
) -> float:
    owner = _owner_chain_user(chain_users, client, users_by_id)
    if owner:
        return _commission_from_stake(session_stake, _comm_rate(owner, "session"))
    return 0.0


def _plus_minus_level_amounts(
    lvl: str,
    client_net: float,
    parent_share: float,
    child_share: float,
    odds_comm: float,
    session_comm: float,
) -> tuple[float, float, float]:
    """Scrape plus-minus row — (ShareAmount, NetAmount, FinalAmount)."""
    total_comm = round(odds_comm + session_comm, 2)
    marginal = _share_amount(client_net, parent_share, child_share)

    if lvl == "agent":
        if marginal <= total_comm:
            return 0.0, total_comm, total_comm
        return marginal, round(marginal + total_comm, 2), round(marginal + total_comm, 2)

    if marginal <= total_comm:
        return 0.0, total_comm, total_comm
    share_amt = round(marginal - total_comm, 2)
    return share_amt, marginal, marginal


def _plus_minus_casino_level_amounts(
    lvl: str,
    client_net: float,
    parent_share: float,
    child_share: float,
    odds_comm: float,
) -> tuple[float, float, float]:
    return _plus_minus_level_amounts(lvl, client_net, parent_share, child_share, odds_comm, 0.0)


def _plus_minus_child_share(
    child: dict,
    child_level: str,
    viewer_level: str,
    chain_users: dict[str, dict],
    share_field: str = "matchShare",
) -> float:
    """Scrape — client 0%; agent ko upline par 0% jab agent share <= owner commission."""
    if child_level == "client":
        return 0.0
    if child_level == "agent":
        agent_share = _num(child.get(share_field, 0))
        owner = chain_users.get("owner") or {}
        owner_comm = _num(owner.get("matchCommission" if share_field == "matchShare" else "casinoCommission", 0))
        if agent_share <= owner_comm:
            return 0.0
        return agent_share
    return _num(child.get(share_field, 0))


def _plus_minus_casino_child_share(
    child: dict, child_level: str, viewer_level: str, chain_users: dict[str, dict]
) -> float:
    return _plus_minus_child_share(child, child_level, viewer_level, chain_users, "casinoShare")


def _drill_ledger_amount(
    client_rows: list[dict],
    viewer_type: str,
    child_type: str,
) -> float:
    """Agent plus minus drill — zero-share upline rows par bhi net amount dikhe."""
    total = sum(_num(r.get(f"{viewer_type}FinalAmount")) for r in client_rows)
    if total != 0:
        return round(total, 2)

    child_idx = (
        PLUS_MINUS_LEVELS.index(child_type)
        if child_type in PLUS_MINUS_LEVELS
        else len(PLUS_MINUS_LEVELS)
    )
    viewer_idx = (
        PLUS_MINUS_LEVELS.index(viewer_type)
        if viewer_type in PLUS_MINUS_LEVELS
        else -1
    )

    for idx in range(child_idx, len(PLUS_MINUS_LEVELS)):
        lvl = PLUS_MINUS_LEVELS[idx]
        total = sum(_num(r.get(f"{lvl}FinalAmount")) for r in client_rows)
        if total != 0:
            return round(total, 2)

    for idx in range(viewer_idx - 1, -1, -1):
        lvl = PLUS_MINUS_LEVELS[idx]
        total = sum(_num(r.get(f"{lvl}FinalAmount")) for r in client_rows)
        if total != 0:
            return round(total, 2)
    return 0.0


def _bet_is_fancy(bet: dict) -> bool:
    return is_fancy_market(
        str(bet.get("betFor") or ""),
        str(bet.get("oddsType") or ""),
        str(bet.get("gtype") or ""),
    )


def _find_match_doc(db, market_id: str) -> dict | None:
    if not market_id:
        return None
    match = db.matches.find_one({"marketId": market_id}, {"_id": 0})
    if match:
        return match
    return db.matches.find_one({"marketList.marketId": market_id}, {"_id": 0})


def _match_completed_for_ledger(db, market_id: str, event_id: str = "") -> bool:
    """Ledger row sirf jab match declare/complete ho — in-play/open match skip."""
    from mongodb.matches_api import _match_is_declared

    match = _find_match_doc(db, str(market_id or ""))
    if not match and event_id:
        match = db.matches.find_one({"eventId": str(event_id)}, {"_id": 0})
    return _match_is_declared(match) if match else False


def _user_has_bets_in_range(db, uid: str, coll_name: str, from_dt, to_dt) -> bool:
    coll = getattr(db, coll_name)
    for bet in coll.find({"userId": uid}, {"_id": 1, "createdAt": 1, "settledAt": 1}):
        created = bet.get("settledAt") or bet.get("createdAt")
        if _statement_in_range(created, from_dt, to_dt):
            return True
    return False


def _sport_name_from_match(match: dict | None) -> str:
    if not match:
        return ""
    for key in ("sportName", "sport", "eventTypeName"):
        val = match.get(key)
        if val:
            return str(val)
    sport_id = match.get("sportId")
    if sport_id == 4:
        return "Cricket"
    if sport_id == 1:
        return "Soccer"
    if sport_id == 2:
        return "Tennis"
    return ""


def _users_index(db) -> dict[str, dict]:
    return {
        str(u["userId"]): u
        for u in db.users.find({"isDeleted": {"$ne": True}}, {"_id": 0})
    }


def _chain_ids_for_client(client: dict, users_by_id: dict[str, dict]) -> dict[str, str]:
    """Actual upline user id per level (empty string if level missing in path)."""
    by_type: dict[str, str] = {}
    cur = client
    while cur:
        by_type[str(cur.get("userType") or "")] = str(cur["userId"])
        pid = cur.get("parentId")
        cur = users_by_id.get(str(pid or "")) if pid else None

    chain = {lvl: by_type.get(lvl, "") for lvl in PLUS_MINUS_LEVELS}
    chain["client"] = str(client.get("userId") or "")
    return chain


def _chain_users(chain: dict[str, str], users_by_id: dict[str, dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for lvl in (*PLUS_MINUS_LEVELS, "client"):
        uid = chain.get(lvl) or ""
        if uid and uid in users_by_id:
            out[lvl] = users_by_id[uid]
    return out


def _is_descendant(ancestor_id: str, client_id: str, users_by_id: dict[str, dict]) -> bool:
    if not ancestor_id or not client_id:
        return False
    cur = users_by_id.get(client_id)
    while cur:
        if str(cur.get("userId") or "") == str(ancestor_id):
            return True
        cur = users_by_id.get(str(cur.get("parentId") or ""))
    return False


def _sum_field(rows: list[dict], field: str) -> float:
    return round(sum(_num(r.get(field, 0)) for r in rows), 2)


def _object_totals(object_level: str, rows: list[dict]) -> dict[str, float]:
    prefix = OBJECT_TOTAL_PREFIX[object_level]
    totals: dict[str, float] = {
        f"{prefix}ClientNetAmount": _sum_field(rows, "clientNetAmount"),
        f"{prefix}ClientOddsAmount": _sum_field(rows, "clientOddsAmount"),
        f"{prefix}ClientSessionAmount": _sum_field(rows, "clientSessionAmount"),
    }
    if object_level == "agent":
        totals[f"{prefix}ClientOddsComm"] = _sum_field(rows, "clientOddsComm")
        totals[f"{prefix}ClientSessionComm"] = _sum_field(rows, "clientSessionComm")
    for inner in PLUS_MINUS_LEVELS:
        cap = INNER_LEVELS_CAP[inner]
        for suffix in ("OddsComm", "SessionComm", "ShareAmount", "NetAmount", "FinalAmount"):
            totals[f"{prefix}{cap}{suffix}"] = _sum_field(rows, f"{inner}{suffix}")
    return totals


def _object_entry(user: dict, object_level: str, rows: list[dict]) -> dict[str, Any]:
    entry: dict[str, Any] = {
        f"{object_level}Username": user.get("username", ""),
        f"{object_level}Name": user.get("name", ""),
    }
    if object_level == "agent":
        entry["agentUsername"] = user.get("username", "")
        entry["agentName"] = user.get("name", "")
    entry.update(_object_totals(object_level, rows))
    return entry


def _empty_plus_minus_market(market_id: str, match: dict | None = None) -> dict[str, Any]:
    return {
        "marketId": market_id,
        "sportName": _sport_name_from_match(match),
        "ownerClientObject": {},
    }


def _apply_level_identity(entry: dict[str, Any], lvl: str, user: dict) -> None:
    """UI hierarchy bars — har level par username/name."""
    username = str(user.get("username") or "")
    name = str(user.get("name") or "")
    entry[f"{lvl}Username"] = username
    entry[f"{lvl}Name"] = name
    if lvl == "agent":
        entry["agentUsername"] = username
        entry["agentName"] = name


def _sync_plus_minus_object_names(
    result: dict[str, Any],
    chains_by_client: dict[str, dict[str, str]],
    users_by_id: dict[str, dict],
) -> None:
    """Har *Object key par username/name set — nested UI ke liye."""
    level_ids: dict[str, set[str]] = {lvl: set() for lvl in PLUS_MINUS_LEVELS}
    for chain in chains_by_client.values():
        for lvl in PLUS_MINUS_LEVELS:
            uid = str(chain.get(lvl) or "")
            if uid:
                level_ids[lvl].add(uid)
    for lvl in PLUS_MINUS_LEVELS:
        client_obj_key = f"{lvl}ClientObject"
        for parent_id in (result.get(client_obj_key) or {}):
            if parent_id:
                level_ids[lvl].add(str(parent_id))

    for lvl in PLUS_MINUS_LEVELS:
        obj_key = f"{lvl}Object"
        bucket = result.setdefault(obj_key, {})
        for uid in level_ids[lvl]:
            user = users_by_id.get(uid) or {}
            entry = bucket.get(uid)
            if not entry:
                entry = _object_entry(user, lvl, [])
                bucket[uid] = entry
            elif user:
                _apply_level_identity(entry, lvl, user)


def _build_client_plus_row(
    client: dict,
    chain_users: dict[str, dict],
    bets: list[dict],
) -> dict:
    odds_amt = session_amt = 0.0
    odds_stake = session_stake = 0.0
    for bet in bets:
        pl = _bet_net_pl(bet)
        stake = _num(bet.get("stake", bet.get("amount", 0)))
        if _bet_is_fancy(bet):
            session_amt = round(session_amt + pl, 2)
            session_stake = round(session_stake + stake, 2)
        else:
            odds_amt = round(odds_amt + pl, 2)
            odds_stake = round(odds_stake + stake, 2)

    client_net = round(odds_amt + session_amt, 2)
    row: dict[str, Any] = {
        "username": client.get("username", ""),
        "name": client.get("name", ""),
        "clientOddsAmount": odds_amt,
        "clientSessionAmount": session_amt,
        "clientNetAmount": client_net,
        "clientOddsComm": _commission_from_stake(odds_stake, _comm_rate(client, "odds")),
        "clientSessionComm": _commission_from_stake(session_stake, _comm_rate(client, "session")),
    }

    ordered = [*PLUS_MINUS_LEVELS, "client"]
    chain_users = dict(chain_users)
    users_by_id = {str(u.get("userId") or ""): u for u in chain_users.values() if u.get("userId")}
    users_by_id[str(client.get("userId") or "")] = client
    owner_user = _owner_chain_user(chain_users, client, users_by_id)
    if owner_user and "owner" not in chain_users:
        chain_users["owner"] = owner_user
    owner_odds_comm = _plus_minus_owner_odds_comm(odds_stake, chain_users, client, users_by_id)
    owner_session_comm = _plus_minus_owner_session_comm(session_stake, chain_users, client, users_by_id)
    for idx, lvl in enumerate(PLUS_MINUS_LEVELS):
        user = chain_users.get(lvl) or {}
        child = client
        child_level = "client"
        for j in range(idx + 1, len(ordered)):
            if ordered[j] in chain_users:
                child = chain_users[ordered[j]]
                child_level = ordered[j]
                break
        share_amt, net_amt, final_amt = _plus_minus_level_amounts(
            lvl,
            client_net,
            user.get("matchShare", 0),
            _plus_minus_child_share(child, child_level, lvl, chain_users),
            owner_odds_comm,
            owner_session_comm,
        )
        row[f"{lvl}OddsComm"] = owner_odds_comm
        row[f"{lvl}SessionComm"] = owner_session_comm
        row[f"{lvl}ShareAmount"] = share_amt
        row[f"{lvl}NetAmount"] = net_amt
        row[f"{lvl}FinalAmount"] = final_amt

    if not chain_users.get("agent") and chain_users.get("admin"):
        for suffix in ("OddsComm", "SessionComm", "ShareAmount", "NetAmount", "FinalAmount"):
            row[f"agent{suffix}"] = row.get(f"admin{suffix}", 0)
            row[f"admin{suffix}"] = 0.0
    return row


def _build_casino_client_plus_row(
    client: dict,
    chain_users: dict[str, dict],
    bets: list[dict],
) -> dict:
    """Casino plus-minus — declared bets, casinoCommission + casinoShare (scrape jaisa)."""
    odds_amt = 0.0
    odds_stake = 0.0
    for bet in bets:
        if not _casino_bet_is_declared(bet) or _casino_bet_is_deleted(bet):
            continue
        pl = _num(bet.get("profitLoss", 0))
        stake = _num(bet.get("stake", bet.get("amount", 0)))
        odds_amt = round(odds_amt + pl, 2)
        odds_stake = round(odds_stake + stake, 2)

    client_net = odds_amt
    row: dict[str, Any] = {
        "username": client.get("username", ""),
        "name": client.get("name", ""),
        "clientOddsAmount": odds_amt,
        "clientSessionAmount": 0.0,
        "clientNetAmount": client_net,
        "clientOddsComm": _commission_from_stake(odds_stake, _comm_rate(client, "casino")),
        "clientSessionComm": 0.0,
    }

    ordered = [*PLUS_MINUS_LEVELS, "client"]
    owner_casino_comm = _commission_from_stake(
        odds_stake, _comm_rate(_owner_chain_user(chain_users), "casino")
    )
    for idx, lvl in enumerate(PLUS_MINUS_LEVELS):
        user = chain_users.get(lvl) or {}
        child = client
        child_level = "client"
        for j in range(idx + 1, len(ordered)):
            if ordered[j] in chain_users:
                child = chain_users[ordered[j]]
                child_level = ordered[j]
                break
        share_amt, net_amt, final_amt = _plus_minus_casino_level_amounts(
            lvl,
            client_net,
            user.get("casinoShare", 0),
            _plus_minus_casino_child_share(child, child_level, lvl, chain_users),
            owner_casino_comm,
        )
        row[f"{lvl}OddsComm"] = owner_casino_comm
        row[f"{lvl}SessionComm"] = 0.0
        row[f"{lvl}ShareAmount"] = share_amt
        row[f"{lvl}NetAmount"] = net_amt
        row[f"{lvl}FinalAmount"] = final_amt

    if not chain_users.get("agent") and chain_users.get("admin"):
        for suffix in ("OddsComm", "SessionComm", "ShareAmount", "NetAmount", "FinalAmount"):
            row[f"agent{suffix}"] = row.get(f"admin{suffix}", 0)
            row[f"admin{suffix}"] = 0.0
    return row


def _nest_client_objects(result: dict, chain: dict[str, str], client_row: dict) -> None:
    for parent_lvl, child_lvl in PLUS_MINUS_PARENT_CHILD:
        if child_lvl == "client":
            continue
        parent_id = chain.get(parent_lvl) or ""
        child_id = chain.get(child_lvl) or ""
        if not parent_id or not child_id or parent_id == child_id:
            continue
        obj_key = f"{parent_lvl}ClientObject"
        bucket = result.setdefault(obj_key, {})
        bucket.setdefault(parent_id, {})
        bucket[parent_id].setdefault(child_id, {})

    agent_id = chain.get("agent") or chain.get("admin") or ""
    client_id = chain.get("client") or ""
    if agent_id:
        result.setdefault("agentClientObject", {})
        result["agentClientObject"].setdefault(agent_id, {})
        if client_id and client_row:
            result["agentClientObject"][agent_id][client_id] = client_row


def compute_plus_minus_market(payload: dict) -> dict:
    """decision/getPlusMinusByMarketId — nested hierarchy object (chunk 950)."""
    market_id = str((payload or {}).get("marketId") or "")
    if not market_id:
        return {}

    db = get_db()
    users_by_id = _users_index(db)
    match = _find_match_doc(db, market_id)
    bets = [
        b for b in _bets_for_market(db, market_id)
        if _sport_bet_is_declared(b) and not b.get("isDeleted")
    ]
    if not bets:
        return _empty_plus_minus_market(market_id, match)

    result: dict[str, Any] = {
        "marketId": market_id,
        "sportName": _sport_name_from_match(match),
    }

    bets_by_client: dict[str, list[dict]] = {}
    for bet in bets:
        uid = str(bet.get("userId") or "")
        if uid:
            bets_by_client.setdefault(uid, []).append(bet)

    rows_by_client: dict[str, dict] = {}
    chains_by_client: dict[str, dict[str, str]] = {}

    for client_id, client_bets in bets_by_client.items():
        client = users_by_id.get(client_id)
        if not client or client.get("userType") != "client":
            continue
        chain = _chain_ids_for_client(client, users_by_id)
        chain_users = _chain_users(chain, users_by_id)
        client_row = _build_client_plus_row(client, chain_users, client_bets)
        _nest_client_objects(result, chain, client_row)
        rows_by_client[client_id] = client_row
        chains_by_client[client_id] = chain

    if not rows_by_client:
        return _empty_plus_minus_market(market_id, match)

    for lvl in PLUS_MINUS_LEVELS:
        obj_key = f"{lvl}Object"
        result[obj_key] = {}
        level_user_ids: set[str] = set()
        for chain in chains_by_client.values():
            uid = chain.get(lvl) or ""
            if uid:
                level_user_ids.add(uid)
        for uid in level_user_ids:
            user = users_by_id.get(uid) or {}
            rows = [
                rows_by_client[cid]
                for cid in rows_by_client
                if _is_descendant(uid, cid, users_by_id)
            ]
            result[obj_key][uid] = _object_entry(user, lvl, rows)

    result.setdefault("agentObject", {})
    for agent_id, clients in result.get("agentClientObject", {}).items():
        user = users_by_id.get(agent_id) or {}
        rows = [r for r in clients.values() if isinstance(r, dict) and r.get("username")]
        result["agentObject"][agent_id] = _object_entry(user, "agent", rows)

    _sync_plus_minus_object_names(result, chains_by_client, users_by_id)
    return result


def compute_session_list(market_id: str) -> list[dict]:
    """sports/getSessionList — fancy sessions for plus-minus select page."""
    market_id = str(market_id or "")
    if not market_id:
        return []

    db = get_db()
    sessions: dict[str, dict] = {}

    def _add(selection_id: Any, name: str, decision_run: Any = None, post_date: str = "") -> None:
        sid = str(selection_id or "")
        if not sid:
            return
        label = (name or f"Session {sid}").strip()
        if post_date and post_date not in label:
            label = f"{label} ({post_date})"
        existing = sessions.get(sid, {})
        sessions[sid] = {
            "selectionId": selection_id if isinstance(selection_id, int) else sid,
            "sessionNames": label,
            "sessionName": label,
            "decisionRun": decision_run if decision_run is not None else existing.get("decisionRun", ""),
            "marketId": market_id,
        }

    for bet in _bets_for_market(db, market_id):
        if not _bet_is_fancy(bet):
            continue
        _add(
            bet.get("selectionId"),
            str(bet.get("runnerName") or bet.get("sessionName") or bet.get("gtype") or "Session"),
            bet.get("decisionRun"),
        )

    for doc in db.center_manual_fancy.find({"marketId": market_id}, {"_id": 0}):
        _add(
            doc.get("selectionId") or doc.get("fancyId"),
            str(doc.get("sessionName") or doc.get("session_name") or doc.get("fancyName") or "Session"),
            doc.get("decisionRun"),
        )

    match = _find_match_doc(db, market_id)
    if match:
        for key in ("sessionList", "fancyList", "fancyData", "marketList"):
            block = match.get(key)
            if isinstance(block, list):
                for item in block:
                    if not isinstance(item, dict):
                        continue
                    mid = str(item.get("marketId") or market_id)
                    if mid != market_id and key == "marketList":
                        continue
                    _add(
                        item.get("selectionId") or item.get("selectionid"),
                        str(item.get("sessionNames") or item.get("sessionName") or item.get("runnerName") or ""),
                        item.get("decisionRun"),
                        str(item.get("postDate") or item.get("createdAt") or "")[:10],
                    )

    rows = list(sessions.values())
    rows.sort(key=lambda r: str(r.get("sessionNames") or ""))
    return rows


def _market_owner_profit(db, market_id: str) -> float:
    """Games list — owner level final amount (scraped totalProfit jaisa)."""
    bets = _declared_bets_for_market(db, market_id)
    if not bets:
        return 0.0
    users_by_id = _users_index(db)
    total = 0.0
    seen: set[str] = set()
    for bet in bets:
        cid = str(bet.get("userId") or "")
        if not cid or cid in seen:
            continue
        seen.add(cid)
        client = users_by_id.get(cid)
        if not client or client.get("userType") != "client":
            continue
        client_bets = [b for b in bets if str(b.get("userId") or "") == cid]
        chain_users = _chain_users(_chain_ids_for_client(client, users_by_id), users_by_id)
        row = _build_client_plus_row(client, chain_users, client_bets)
        total += _num(row.get("ownerFinalAmount", 0))
    return round(total, 2)


def _match_sort_dt(match: dict) -> datetime:
    raw = match.get("matchDate") or match.get("date") or ""
    if isinstance(raw, (int, float)) or (isinstance(raw, str) and raw.isdigit()):
        try:
            ts = int(raw)
            if ts > 1_000_000_000_000:
                ts //= 1000
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (ValueError, OSError):
            pass
    parsed = _parse_date(raw)
    return parsed or datetime.min.replace(tzinfo=timezone.utc)


def _match_won_team_name(doc: dict) -> str:
    won = str(doc.get("wonTeamName") or "").strip()
    if won:
        return won
    sid = doc.get("wonTeamBookmakerSelectionId")
    if sid is not None:
        return resolve_team_name(doc, sid) or ""
    return ""


def _decision_won_team_name(db, market_id: str) -> str:
    log = db.decision_logs.find_one(
        {
            "marketId": str(market_id),
            "wonTeamName": {"$exists": True, "$nin": ["", None]},
        },
        {"_id": 0, "wonTeamName": 1},
        sort=[("createdAt", -1)],
    )
    return str((log or {}).get("wonTeamName") or "").strip()


def _resolve_completed_match_row(db, market_id: str) -> dict:
    """Bets-only markets — scrape matchList + decision_logs se poora label."""
    from mongodb.matches_api import _find_match_local, enrich_match_metadata, normalize_match_row

    market_id = str(market_id or "")
    bet = db.sports_bets.find_one({"marketId": market_id}, {"eventId": 1, "_id": 0})
    event_id = str((bet or {}).get("eventId") or "")
    resolved = _find_match_local(market_id, event_id)
    if resolved:
        row = copy.deepcopy(resolved)
        row.setdefault("marketId", market_id)
        if event_id and not row.get("eventId"):
            row["eventId"] = event_id
        row.setdefault("isDeclare", True)
        row.setdefault("status", "COMPLETED")
        if not row.get("wonTeamName"):
            row["wonTeamName"] = _decision_won_team_name(db, market_id)
        return normalize_match_row(enrich_match_metadata(row))

    label = _match_teams_display_name(db, market_id, event_id) or f"Market {market_id}"
    return normalize_match_row({
        "marketId": market_id,
        "eventId": event_id,
        "matchName": label,
        "sportId": 4,
        "isDeclare": True,
        "status": "COMPLETED",
        "wonTeamName": _decision_won_team_name(db, market_id),
    })


def _enrich_completed_match_row(db, row: dict) -> dict:
    from mongodb.matches_api import _find_match_local, enrich_match_metadata, normalize_match_row

    doc = copy.deepcopy(row or {})
    mid = str(doc.get("marketId") or "")
    if not mid:
        return doc
    name = str(doc.get("matchName") or doc.get("eventName") or "").strip()
    needs_lookup = (
        not name
        or name.startswith("Market ")
        or not doc.get("matchDate")
        or not doc.get("eventId")
    )
    if needs_lookup:
        resolved = _find_match_local(mid, str(doc.get("eventId") or ""))
        if resolved:
            for key, val in resolved.items():
                if val in (None, "") and doc.get(key) not in (None, ""):
                    continue
                if val not in (None, ""):
                    doc[key] = val
    if not doc.get("wonTeamName"):
        doc["wonTeamName"] = _decision_won_team_name(db, mid)
    return normalize_match_row(enrich_match_metadata(doc))


def compute_complete_sport_list(payload: dict) -> dict:
    """decision/completeSportList — completed games list (non-live only)."""
    from mongodb.matches_api import _match_is_live, normalize_match_row

    payload = payload or {}
    page = max(int(payload.get("pageNo") or 1), 1)
    size = max(int(payload.get("size") or 15), 1)
    sport_filter = payload.get("sportId")

    db = get_db()
    bet_markets = {str(m) for m in db.sports_bets.distinct("marketId") if m}

    by_market: dict[str, dict] = {}
    for doc in db.matches.find({}, {"_id": 0}):
        mid = str(doc.get("marketId") or "")
        if not mid:
            continue
        norm = _enrich_completed_match_row(db, doc)
        if _match_is_live(norm):
            continue
        if mid in bet_markets or norm.get("isDeclare") or str(norm.get("status") or "").upper() == "COMPLETED":
            by_market[mid] = norm

    for mid in bet_markets:
        if mid not in by_market:
            by_market[mid] = _resolve_completed_match_row(db, mid)
        else:
            by_market[mid] = _enrich_completed_match_row(db, by_market[mid])

    rows = list(by_market.values())

    if sport_filter is not None and sport_filter != "":
        if isinstance(sport_filter, (list, tuple)):
            allowed = {str(s) for s in sport_filter}
            rows = [m for m in rows if str(m.get("sportId", "")) in allowed]
        else:
            rows = [m for m in rows if str(m.get("sportId", "")) == str(sport_filter)]

    rows.sort(key=_match_sort_dt, reverse=True)
    total = len(rows)
    page_rows = rows[(page - 1) * size: page * size]

    match_data: dict[str, dict] = {}
    for idx, doc in enumerate(page_rows):
        mid = str(doc.get("marketId") or "")
        declared = bool(doc.get("isDeclare")) or bool(_decision_won_team_name(db, mid))
        match_data[str(idx)] = {
            "marketId": mid,
            "matchName": (doc.get("matchName") or doc.get("eventName") or "").strip(),
            "matchDate": doc.get("matchDate") or "",
            "matchType": doc.get("matchType") or doc.get("sportType") or "",
            "sportId": doc.get("sportId", 4),
            "eventId": doc.get("eventId", ""),
            "isDeclare": declared,
            "wonTeamName": _match_won_team_name(doc) or _decision_won_team_name(db, mid),
            "totalProfit": _market_owner_profit(db, mid),
        }

    return {"matchData": match_data, "totalCount": total}


def compute_client_plus_minus(payload: dict) -> list[dict]:
    """bluexchReports/clientPlusMinus."""
    payload = payload or {}
    market_id = str(payload.get("marketId") or "")
    user_id = str(payload.get("userId") or payload.get("downlineUserId") or "")
    db = get_db()
    q: dict = {}
    if market_id:
        q["marketId"] = market_id
    if user_id:
        q["userId"] = user_id
    bets = list(db.sports_bets.find(q, {"_id": 0}))
    pl = round(sum(_bet_net_pl(b) for b in bets), 2)
    return [{
        "_id": user_id or market_id or uuid.uuid4().hex[:12],
        "userId": user_id,
        "marketId": market_id,
        "plusMinus": pl,
        "commission": 0,
    }]


def compute_user_profit_loss(payload: dict, session_user: dict | None = None) -> list[dict]:
    """reports/userProfitLoss — /app/profit-loss client report (chunk 2350)."""
    payload = payload or {}
    session_user = session_user or {}
    root_id = str(payload.get("userId") or session_user.get("userId") or "")
    pl_type = str(payload.get("profitLossType") or payload.get("transactionType") or "all").lower()
    from_dt = _parse_date(payload.get("fromDate"))
    to_dt = _parse_date(payload.get("toDate"))
    if to_dt:
        to_dt = to_dt.replace(hour=23, minute=59, second=59)

    db = get_db()
    if root_id and not _session_may_access_user(db, session_user, root_id):
        return []
    client_ids = _client_ids_in_subtree(db, root_id)
    if not client_ids:
        return []

    games = _casino_games_map(db)
    buckets: dict[str, dict] = {}

    def _add_row(day_key: str, event_key: str, event_name: str, created_at: Any, pl: float, category: str) -> None:
        if pl_type not in ("", "all") and category != pl_type:
            return
        if not day_key:
            return
        key = f"{day_key}|{event_key}"
        ts = _statement_ts(created_at)
        row = buckets.get(key)
        if not row:
            row = {
                "eventName": event_name,
                "userNetProfitLoss": 0.0,
                "createdAt": ts,
                "_id": event_key,
            }
            buckets[key] = row
        row["userNetProfitLoss"] = round(row["userNetProfitLoss"] + pl, 2)
        if ts >= row["createdAt"]:
            row["createdAt"] = ts
            row["eventName"] = event_name

    for bet in db.sports_bets.find({"userId": {"$in": list(client_ids)}}, {"_id": 0}):
        created = _bet_created_at(bet)
        if not _statement_in_range(created, from_dt, to_dt):
            continue
        day = _bet_ist_date_str(bet)
        event_key = str(bet.get("eventId") or bet.get("marketId") or "sport")
        _add_row(day, event_key, _profit_loss_sport_event_name(db, bet), created, _bet_net_pl(bet), "event")

    for bet in db.casino_bets.find({"userId": {"$in": list(client_ids)}}, {"_id": 0}):
        settled = bet.get("settledAt") or bet.get("createdAt")
        if not _statement_in_range(settled, from_dt, to_dt):
            continue
        day = _bet_ist_date_str({"createdAt": settled})
        event_id = bet.get("eventId", 0)
        status = str(bet.get("status") or "open").lower()
        pl = -_num(bet.get("stake", 0)) if status == "open" else _num(bet.get("profitLoss", 0))
        _add_row(
            day,
            str(event_id),
            _profit_loss_casino_event_name(bet, games),
            settled,
            pl,
            _profit_loss_category("casino", bet),
        )

    for bet in db.matka_bets.find({"userId": {"$in": list(client_ids)}}, {"_id": 0}):
        settled = bet.get("settledAt") or bet.get("createdAt")
        if not _statement_in_range(settled, from_dt, to_dt):
            continue
        day = _bet_ist_date_str({"createdAt": settled})
        event_key = str(bet.get("matkaEventId") or bet.get("eventId") or "matka")
        status = str(bet.get("status") or "open").lower()
        pl = -_num(bet.get("stake", 0)) if status == "open" else _num(bet.get("profitLoss", 0))
        _add_row(day, event_key, _profit_loss_matka_event_name(bet), settled, pl, "event")

    rows = list(buckets.values())
    rows.sort(key=lambda r: r.get("createdAt", 0), reverse=True)
    return rows


def compute_fancy_run_position_map(payload: dict) -> dict[str, float]:
    """sports/getSessionPositionBySelectionId — run → net P/L (sidebar ladder)."""
    payload = payload or {}
    market_id = str(payload.get("marketId") or "")
    selection_id = payload.get("selectionId")
    user_id = str(payload.get("userId") or payload.get("downlineUserId") or "")
    if not market_id or selection_id in (None, "", "null"):
        return {}

    db = get_db()
    bet_q: dict = {"marketId": market_id}
    try:
        sid_int = int(selection_id)
        bet_q["selectionId"] = {"$in": [sid_int, str(sid_int), selection_id]}
    except (TypeError, ValueError):
        bet_q["selectionId"] = selection_id
    if user_id:
        bet_q["userId"] = user_id
    bet_q["status"] = "open"
    bet_q["isDeclare"] = {"$ne": True}

    bets: list[dict] = []
    for bet in db.sports_bets.find(bet_q, {"_id": 0}):
        if not is_fancy_market(
            str(bet.get("betFor") or ""),
            str(bet.get("oddsType") or ""),
            str(bet.get("gtype") or ""),
        ):
            continue
        bets.append(bet)

    if not bets:
        return {}

    runs = sorted({int(_num(b.get("run"))) for b in bets})
    lo, hi = runs[0] - 1, runs[-1] + 1
    out: dict[str, float] = {}
    for run in range(lo, hi + 1):
        pl = round(sum(settle_fancy_bet(b, run) for b in bets), 2)
        out[str(run)] = pl
    return out


def compute_fancy_session_positions(payload: dict) -> list[dict]:
    """sports/getSessionPositionBySelectionId — fancy/session positions."""
    payload = payload or {}
    market_id = str(payload.get("marketId") or "")
    selection_id = payload.get("selectionId")
    db = get_db()

    q: dict = {"marketId": market_id} if market_id else {}
    if selection_id is not None:
        q["selectionId"] = selection_id

    rows: list[dict] = []
    seen: set[tuple] = set()

    for pos in db.positions.find(q, {"_id": 0}):
        if pos.get("runners"):
            continue
        key = (pos.get("userId"), pos.get("selectionId"), pos.get("marketId"))
        if key in seen:
            continue
        seen.add(key)
        user = _find_user(db, str(pos.get("userId") or "")) or {}
        rows.append({
            "userId": pos.get("userId"),
            "username": user.get("username", ""),
            "name": user.get("name", ""),
            "marketId": pos.get("marketId"),
            "selectionId": pos.get("selectionId"),
            "runnerName": pos.get("runnerName", ""),
            "position": _num(pos.get("position", pos.get("exposure", 0))),
            "exposure": _num(pos.get("exposure", 0)),
        })

    bet_q = {"marketId": market_id} if market_id else {}
    for bet in db.sports_bets.find(bet_q, {"_id": 0}):
        if not is_fancy_market(
            str(bet.get("betFor") or ""),
            str(bet.get("oddsType") or ""),
            str(bet.get("gtype") or ""),
        ):
            continue
        sid = bet.get("selectionId")
        if selection_id is not None and str(sid) != str(selection_id):
            continue
        uid = bet.get("userId")
        key = (uid, sid, market_id)
        if key in seen:
            continue
        seen.add(key)
        user = _find_user(db, str(uid or "")) or {}
        pl = _bet_net_pl(bet)
        rows.append({
            "userId": uid,
            "username": user.get("username", ""),
            "name": user.get("name", ""),
            "marketId": market_id,
            "selectionId": sid,
            "runnerName": bet.get("runnerName") or bet.get("sessionName") or "",
            "position": pl,
            "exposure": abs(pl) if pl < 0 else 0,
            "run": bet.get("run", "0"),
        })
    return rows


def compute_day_wise_casino(payload: dict) -> list[dict]:
    """casino/dayWiseCasinoReport — casino_bets se aggregate."""
    payload = payload or {}
    from_dt = _parse_date(payload.get("fromDate"))
    to_dt = _parse_date(payload.get("toDate"))
    if to_dt:
        to_dt = to_dt.replace(hour=23, minute=59, second=59)

    db = get_db()
    by_day: dict[str, dict] = {}

    for bet in db.casino_bets.find({}, {"_id": 0}):
        created = _bet_created_at(bet)
        if from_dt and created and created < from_dt:
            continue
        if to_dt and created and created > to_dt:
            continue
        if not created:
            continue
        day_key = created.strftime("%Y-%m-%d")
        ts = int(created.timestamp() * 1000)
        event_id = bet.get("eventId", 0)
        bucket = by_day.setdefault(day_key, {})
        evt = bucket.setdefault(str(event_id), {
            "_id": {"date": str(ts)},
            "eventId": event_id,
            "eventName": bet.get("eventName") or f"Casino {event_id}",
            "userNetProfitLoss": 0.0,
            "userOddsComm": 0.0,
            "clientOddsAmount": 0.0,
            "clientNetAmount": 0.0,
            "createdAt": ts,
        })
        pl = _num(bet.get("profitLoss", 0))
        stake = _num(bet.get("stake", 0))
        evt["userNetProfitLoss"] = round(evt["userNetProfitLoss"] + pl, 2)
        evt["clientNetAmount"] = round(evt["clientNetAmount"] + pl, 2)
        evt["clientOddsAmount"] = round(evt["clientOddsAmount"] + stake, 2)

    rows: list[dict] = []
    for day_events in by_day.values():
        rows.extend(day_events.values())
    rows.sort(key=lambda r: r.get("createdAt", 0), reverse=True)
    return rows


def _coerce_event_id(event_id) -> int | str | None:
    if event_id is None or event_id == "":
        return None
    try:
        return int(event_id)
    except (TypeError, ValueError):
        return event_id


def _event_id_lookup(event_id) -> dict:
    eid = _coerce_event_id(event_id)
    if eid is None:
        return {}
    if isinstance(eid, int):
        return {"eventId": {"$in": [eid, str(eid)]}}
    return {"eventId": eid}


def _strip_casino_doc(doc: dict) -> dict:
    row = copy.deepcopy(doc)
    oid = doc.get("_id")
    if oid is not None:
        row["id"] = str(oid)
    row.pop("_id", None)
    return row


def normalize_casino_game(doc: dict) -> dict:
    """Casino Ledger (/app/casino/ledger) + game report pages."""
    row = _strip_casino_doc(doc)
    if row.get("eventId") is not None:
        try:
            row["eventId"] = int(row["eventId"])
        except (TypeError, ValueError):
            pass
    if "cashinoStatus" not in row:
        row["cashinoStatus"] = bool(row.get("casinoStatus", row.get("betStatus", False)))
    row["cashinoStatus"] = bool(row.get("cashinoStatus"))
    row["betStatus"] = bool(row.get("betStatus", row["cashinoStatus"]))
    if not row.get("shortName") and row.get("name"):
        row["shortName"] = str(row["name"]).lower().replace(" ", "").replace("-", "")[:20]
    created = row.get("createdAt") or row.get("time") or row.get("date")
    if hasattr(created, "timestamp"):
        row["createdAt"] = int(created.timestamp() * 1000)
    elif isinstance(created, (int, float)):
        row["createdAt"] = int(created)
    row.setdefault("fetchData", "socket")
    row.setdefault("isDisable", False)
    row.setdefault("isVirtual", False)
    return row


def _casino_games_map(db) -> dict[int, dict]:
    out: dict[int, dict] = {}
    for game in db.casino_games.find({}, {"_id": 0}):
        eid = game.get("eventId")
        if eid is not None:
            try:
                out[int(eid)] = game
            except (TypeError, ValueError):
                out[eid] = game
    return out


def _casino_game_name(game: dict | None, event_id: Any) -> str:
    if game:
        return str(game.get("name") or game.get("shortName") or f"Casino {event_id}")
    return f"Casino {event_id}"


def _casino_bets_in_scope(db, payload: dict) -> list[dict]:
    """Filter casino_bets by event, users, dates (admin casino pages)."""
    payload = payload or {}
    from_str = str(payload.get("fromDate") or "")[:10] if payload.get("fromDate") else ""
    to_str = str(payload.get("toDate") or "")[:10] if payload.get("toDate") else ""

    q: dict = {}
    event_id = payload.get("eventId")
    if event_id is not None:
        try:
            q["eventId"] = int(event_id)
        except (TypeError, ValueError):
            q["eventId"] = event_id
    elif payload.get("eventIdArray"):
        ids = payload.get("eventIdArray") or []
        if isinstance(ids, list) and ids:
            norm = []
            for i in ids:
                try:
                    norm.append(int(i))
                except (TypeError, ValueError):
                    norm.append(i)
            q["eventId"] = {"$in": norm}

    if payload.get("roundId"):
        q["roundId"] = str(payload["roundId"])
    if payload.get("username"):
        user = _find_user(db, str(payload["username"]))
        if user:
            q["userId"] = user.get("userId")

    downline_ids: set[str] | None = None
    if payload.get("downlineUserIdArray"):
        arr = payload.get("downlineUserIdArray") or []
        if isinstance(arr, list) and arr:
            downline_ids = {str(u) for u in arr}
    elif payload.get("userId") and payload.get("eventId") is None and not payload.get("eventIdArray"):
        uid = str(payload["userId"])
        downline_ids = _collect_descendant_ids(db, uid) | {uid}

    rows: list[dict] = []
    for bet in db.casino_bets.find(q, {"_id": 0}):
        uid = str(bet.get("userId") or "")
        if downline_ids is not None and uid not in downline_ids:
            continue
        bet_day = _bet_ist_date_str(bet)
        if from_str and bet_day and bet_day < from_str:
            continue
        if to_str and bet_day and bet_day > to_str:
            continue
        if payload.get("date"):
            day_str = str(payload["date"])[:10]
            if bet_day != day_str:
                continue

        declare_param = payload.get("isDeclare")
        if declare_param is not None and declare_param != "":
            is_declare = _casino_bet_is_declared(bet)
            if _is_falsey_flag(declare_param) and is_declare:
                continue
            if _is_truthy_flag(declare_param) and not is_declare:
                continue

        deleted_param = payload.get("isDeleted")
        if deleted_param is not None and deleted_param != "":
            is_deleted = _casino_bet_is_deleted(bet)
            if _is_falsey_flag(deleted_param) and is_deleted:
                continue
            if _is_truthy_flag(deleted_param) and not is_deleted:
                continue

        rows.append(bet)
    return rows


def _is_truthy_flag(val: Any) -> bool:
    return val is True or val == 1 or str(val).lower() in ("true", "1")


def _is_falsey_flag(val: Any) -> bool:
    return val is False or val == 0 or str(val).lower() in ("false", "0")


def _casino_bet_is_declared(bet: dict) -> bool:
    status = str(bet.get("status") or "open").lower()
    return bool(bet.get("isDeclare")) or status in ("settled", "won", "lost")


def _casino_bet_is_deleted(bet: dict) -> bool:
    status = str(bet.get("status") or "").lower()
    return bool(bet.get("isDeleted")) or status == "deleted"


def _casino_bet_ts(bet: dict) -> int:
    created = bet.get("createdAt")
    if hasattr(created, "timestamp"):
        return int(created.timestamp() * 1000)
    if isinstance(created, (int, float)):
        return int(created)
    parsed = _parse_date(created)
    return int(parsed.timestamp() * 1000) if parsed else 0


def _format_casino_bet_row(bet: dict, user: dict, game: dict | None = None) -> dict:
    pl = _num(bet.get("profitLoss", 0))
    stake = _num(bet.get("stake", bet.get("amount", 0)))
    settled = str(bet.get("status", "")).lower() in ("settled", "won", "lost")
    result = bet.get("resultDetails") or bet.get("result") or {}
    if not isinstance(result, dict):
        result = {}
    is_aviator = str(bet.get("gameType") or "").lower() == "aviator"
    multiplier = _num(bet.get("multiplier", 0))
    crash = bet.get("crashValue")
    show = bet.get("showResult") or result.get("winner") or bet.get("selection") or ""
    if is_aviator and settled:
        if multiplier:
            show = show or f"{multiplier}x"
        elif crash is not None:
            show = show or str(crash)
    player = bet.get("playerName") or bet.get("selection") or ""
    if is_aviator and not player:
        player = "Aviator"
    odds = _num(bet.get("odds", 0))
    if is_aviator and not odds:
        if multiplier:
            odds = multiplier
        elif crash is not None:
            odds = _num(crash)
    mongo_id = bet.get("_id")
    row_id = str(mongo_id) if mongo_id is not None else str(bet.get("betId") or "")
    return {
        "betId": bet.get("betId"),
        "_id": row_id,
        "id": row_id,
        "roundId": bet.get("roundId"),
        "eventId": bet.get("eventId"),
        "playerName": player,
        "odds": odds,
        "amount": stake,
        "profitLoss": pl,
        "isDeclare": _casino_bet_is_declared(bet),
        "isDeleted": _casino_bet_is_deleted(bet),
        "showResult": show,
        "resultDetails": result,
        "gameName": _casino_game_name(game, bet.get("eventId")),
        "userInfo": {
            "userId": user.get("userId"),
            "username": user.get("username", ""),
            "name": user.get("name", ""),
        },
        "createdAt": _casino_bet_ts(bet),
    }


def compute_casino_bet_report(payload: dict) -> tuple[list[dict], int]:
    """casino/diamondCasinoReportByUser — eventId + date (chunk 3350 bet list)."""
    db = get_db()
    games = _casino_games_map(db)
    bets = _casino_bets_in_scope(db, payload)
    rows = []
    for bet in bets:
        user = _find_user(db, str(bet.get("userId") or "")) or {}
        game = games.get(int(bet.get("eventId"))) if bet.get("eventId") is not None else None
        rows.append(_format_casino_bet_row(bet, user, game))
    rows.sort(key=lambda r: r.get("createdAt") or 0, reverse=True)
    page = max(int((payload or {}).get("pageNo") or 1), 1)
    size = max(int((payload or {}).get("size") or 50), 1)
    total = len(rows)
    return rows[(page - 1) * size: page * size], total


def compute_casino_completed_list(payload: dict) -> list[dict]:
    """Completed casino hub — aggregate by eventId (chunk 3967 / E4)."""
    db = get_db()
    games = _casino_games_map(db)
    bets = _casino_bets_in_scope(db, payload)
    by_event: dict[Any, dict] = {}

    for bet in bets:
        eid = bet.get("eventId")
        if eid is None:
            continue
        game = games.get(int(eid)) if str(eid).isdigit() else games.get(eid)
        entry = by_event.setdefault(eid, {
            "_id": eid,
            "gameName": _casino_game_name(game, eid),
            "exposure": 0.0,
            "profitLoss": 0.0,
            "clientProfitLoss": 0.0,
            "amount": 0.0,
            "createdAt": 0,
        })
        stake = _num(bet.get("stake", bet.get("amount", 0)))
        pl = _num(bet.get("profitLoss", 0))
        entry["amount"] = round(entry["amount"] + stake, 2)
        entry["profitLoss"] = round(entry["profitLoss"] + pl, 2)
        entry["clientProfitLoss"] = entry["profitLoss"]
        if str(bet.get("status", "")).lower() not in ("settled", "won", "lost"):
            entry["exposure"] = round(entry["exposure"] + stake, 2)
        ts = _casino_bet_ts(bet)
        if ts > entry["createdAt"]:
            entry["createdAt"] = ts

    rows = list(by_event.values())
    rows.sort(key=lambda r: r.get("createdAt") or 0, reverse=True)
    return rows


def compute_casino_report_by_user(payload: dict) -> list[dict]:
    """Legacy list shape — prefer compute_casino_completed_list / compute_casino_bet_report."""
    if (payload or {}).get("eventId") is not None:
        rows, _ = compute_casino_bet_report(payload or {})
        return rows
    return compute_casino_completed_list(payload or {})


def compute_casino_realtime_pos(payload: dict) -> list[dict]:
    """casino/realTimeDataPosDataDiamondCasino — open casino exposure."""
    payload = payload or {}
    db = get_db()
    q: dict = {"status": {"$nin": ["settled", "won", "lost"]}}
    if payload.get("eventId") is not None:
        try:
            q["eventId"] = int(payload["eventId"])
        except (TypeError, ValueError):
            q["eventId"] = payload["eventId"]
    if payload.get("downlineUserId"):
        q["userId"] = payload["downlineUserId"]

    by_user: dict[str, float] = {}
    for bet in db.casino_bets.find(q, {"_id": 0}):
        uid = str(bet.get("userId") or "")
        by_user[uid] = round(by_user.get(uid, 0) + _num(bet.get("stake", bet.get("amount", 0))), 2)

    rows = []
    for uid, exp in by_user.items():
        user = _find_user(db, uid) or {}
        rows.append({
            "userId": uid,
            "username": user.get("username", ""),
            "name": user.get("name", ""),
            "exposure": exp,
            "position": -exp,
        })
    return rows


def compute_casino_profit_loss_pos(payload: dict) -> list[dict]:
    """casino/getProfitLossPos — company report downline (chunk 8683)."""
    payload = payload or {}
    event_id = payload.get("eventId")
    user_id = str(payload.get("userId") or "")
    viewer_type = str(payload.get("userType") or "").lower()
    if event_id is None or not user_id or not viewer_type:
        return []

    db = get_db()
    child_type = USER_TYPE_CHILD.get(viewer_type, "client")
    users_by_id = _users_index(db)
    downline_ids = _collect_descendant_ids(db, user_id) | {user_id}

    scope = {**payload, "userId": None}
    bets = [b for b in _casino_bets_in_scope(db, scope) if str(b.get("userId") or "") in downline_ids]
    if not bets:
        return []

    bets_by_client: dict[str, list[dict]] = {}
    for bet in bets:
        uid = str(bet.get("userId") or "")
        if uid:
            bets_by_client.setdefault(uid, []).append(bet)

    direct_children = list(db.users.find({
        "parentId": user_id,
        "userType": child_type,
        "isDeleted": {"$ne": True},
    }, {"_id": 0}))

    rows: list[dict] = []
    market_key = str(event_id)
    for child in direct_children:
        cid = str(child["userId"])
        subtree = _collect_descendant_ids(db, cid) | {cid}
        client_rows = _client_plus_rows_for_subtree(bets_by_client, subtree, users_by_id)
        if not client_rows and child_type == "client" and cid in bets_by_client:
            client = users_by_id.get(cid) or child
            chain_users = _chain_users(_chain_ids_for_client(client, users_by_id), users_by_id)
            client_rows = [_build_client_plus_row(client, chain_users, bets_by_client[cid])]
        if not client_rows:
            continue
        rows.append(_aggregate_downline_company_row(
            child, client_rows, viewer_type, child_type, market_key
        ))

    if not rows and child_type == "client":
        user = users_by_id.get(user_id)
        if user and user.get("userType") == "client" and user_id in bets_by_client:
            chain_users = _chain_users(_chain_ids_for_client(user, users_by_id), users_by_id)
            client_rows = [_build_client_plus_row(user, chain_users, bets_by_client[user_id])]
            rows.append(_aggregate_downline_company_row(
                user, client_rows, viewer_type, child_type, market_key
            ))

    return rows


def compute_casino_plus_minus(payload: dict) -> dict:
    """casino/getPlusMinusCasinoDetail — nested hierarchy (chunk 98)."""
    payload = payload or {}
    db = get_db()
    bets = [
        b for b in _casino_bets_in_scope(db, payload)
        if _casino_bet_is_declared(b) and not _casino_bet_is_deleted(b)
    ]
    if not bets:
        return {}

    users_by_id = _users_index(db)
    allowed_clients = {str(u) for u in (payload.get("downlineUserIdArray") or [])}

    bets_by_client: dict[str, list[dict]] = {}
    for bet in bets:
        uid = str(bet.get("userId") or "")
        if allowed_clients and uid not in allowed_clients:
            continue
        if uid:
            bets_by_client.setdefault(uid, []).append(bet)

    result: dict[str, Any] = {}
    rows_by_client: dict[str, dict] = {}
    chains_by_client: dict[str, dict[str, str]] = {}

    for client_id, client_bets in bets_by_client.items():
        client = users_by_id.get(client_id)
        if not client or client.get("userType") != "client":
            continue
        chain = _chain_ids_for_client(client, users_by_id)
        chain_users = _chain_users(chain, users_by_id)
        client_row = _build_casino_client_plus_row(client, chain_users, client_bets)
        _nest_client_objects(result, chain, client_row)
        rows_by_client[client_id] = client_row
        chains_by_client[client_id] = chain

    if not rows_by_client:
        return {}

    for lvl in PLUS_MINUS_LEVELS:
        obj_key = f"{lvl}Object"
        result[obj_key] = {}
        level_user_ids: set[str] = set()
        for chain in chains_by_client.values():
            uid = chain.get(lvl) or ""
            if uid:
                level_user_ids.add(uid)
        for uid in level_user_ids:
            user = users_by_id.get(uid) or {}
            agg_rows = [
                rows_by_client[cid]
                for cid in rows_by_client
                if _is_descendant(uid, cid, users_by_id)
            ]
            result[obj_key][uid] = _object_entry(user, lvl, agg_rows)

    result.setdefault("agentObject", {})
    for agent_id, clients in result.get("agentClientObject", {}).items():
        user = users_by_id.get(agent_id) or {}
        result["agentObject"][agent_id] = _object_entry(user, "agent", list(clients.values()))

    return result


def compute_matka_profit_loss(payload: dict) -> list[dict]:
    payload = payload or {}
    db = get_db()
    q: dict = {}
    if payload.get("eventId") is not None:
        q["eventId"] = payload["eventId"]
    if payload.get("userId"):
        q["userId"] = payload["userId"]
    rows = []
    for bet in db.matka_bets.find(q, {"_id": 0}):
        user = _find_user(db, str(bet.get("userId") or "")) or {}
        rows.append({
            **copy.deepcopy(bet),
            "username": user.get("username", ""),
            "userNetProfitLoss": _num(bet.get("profitLoss", 0)),
        })
    return rows


def _matka_event_map(db) -> dict[str, dict]:
    return {str(e.get("matkaEventId")): e for e in db.matka_events.find({}, {"_id": 0})}


def _format_matka_bet_row(bet: dict, event: dict, user: dict) -> dict:
    stake = _num(bet.get("stake"))
    pl = _num(bet.get("profitLoss", 0))
    created = bet.get("createdAt")
    ts = int(created.timestamp() * 1000) if hasattr(created, "timestamp") else created
    return {
        "betId": bet.get("betId"),
        "matkaEventId": bet.get("matkaEventId"),
        "matkaName": event.get("name") or bet.get("matkaName") or "Matka",
        "gameType": bet.get("gameType") or "SINGLE",
        "betNumber": bet.get("number") or bet.get("betNumber") or "",
        "betType": bet.get("betType") or "OPEN",
        "amount": stake,
        "profit": max(pl, 0),
        "loss": abs(min(pl, 0)),
        "profitLoss": pl,
        "priority": int(bet.get("priority") or 1),
        "isDeclare": bet.get("status") not in ("open", None) or bool(event.get("result")),
        "isDeleted": bool(bet.get("isDeleted", False)),
        "ip": bet.get("ip") or "",
        "date": bet.get("date") or "",
        "result": event.get("result") or bet.get("result") or "",
        "oddEvenResult": bet.get("oddEvenResult") or "",
        "userInfo": {
            "userId": user.get("userId"),
            "username": user.get("username"),
            "name": user.get("name"),
        },
        "createdAt": ts,
    }


def compute_matka_list(payload: dict) -> list[dict]:
    """matka/getMatkaList — bets + event info (admin matka page)."""
    payload = payload or {}
    db = get_db()
    events = _matka_event_map(db)
    from_dt = _parse_date(payload.get("fromDate"))
    to_dt = _parse_date(payload.get("toDate"))
    if to_dt:
        to_dt = to_dt.replace(hour=23, minute=59, second=59)

    q: dict = {}
    if payload.get("matkaEventId"):
        q["matkaEventId"] = payload["matkaEventId"]
    if payload.get("isDeleted") is False:
        q["isDeleted"] = {"$ne": True}

    rows: list[dict] = []
    for bet in db.matka_bets.find(q, {"_id": 0}).sort("createdAt", -1).limit(500):
        created = _bet_created_at(bet)
        if from_dt and created and created < from_dt:
            continue
        if to_dt and created and created > to_dt:
            continue
        event = events.get(str(bet.get("matkaEventId")), {})
        user = _find_user(db, str(bet.get("userId") or "")) or {}
        rows.append(_format_matka_bet_row(bet, event, user))

    if not rows:
        for ev in events.values():
            rows.append({
                "matkaEventId": ev.get("matkaEventId"),
                "matkaName": ev.get("name"),
                "gameType": "SINGLE",
                "priority": 1,
                "profitLoss": 0,
                "amount": 0,
                "isDeclare": ev.get("status") == "declared",
                "isDeleted": False,
                "result": ev.get("result") or "",
            })
    return rows


def compute_matka_bet_list(payload: dict) -> list[dict]:
    """matka/matkaBetList — admin filtered bet list."""
    return compute_matka_list(payload)


def _statement_ts(created_at: Any) -> int:
    if isinstance(created_at, (int, float)):
        return int(created_at)
    parsed = _parse_date(created_at)
    if parsed and hasattr(parsed, "timestamp"):
        return int(parsed.timestamp() * 1000)
    return 0


def _statement_in_range(created_at: Any, from_dt: datetime | None, to_dt: datetime | None) -> bool:
    if not from_dt and not to_dt:
        return True
    day = _bet_ist_date_str({"createdAt": created_at})
    if not day:
        return True
    from_str = from_dt.strftime("%Y-%m-%d") if from_dt else ""
    to_str = to_dt.strftime("%Y-%m-%d") if to_dt else ""
    if from_str and day < from_str:
        return False
    if to_str and day > to_str:
        return False
    return True


def _statement_row(
    amount: float,
    remark: str,
    created_at: Any,
    game_type: str,
    *,
    statement_for: str = "BET",
    user_remark: str = "",
    is_comm: bool = False,
    market_id: str = "",
) -> dict:
    return {
        "amount": round(_num(amount), 2),
        "gameType": game_type,
        "remark": remark,
        "userRemark": user_remark or remark,
        "statementFor": statement_for,
        "isComm": is_comm,
        "marketId": market_id,
        "createdAt": _statement_ts(created_at),
    }


def _sports_statement_remark(bet: dict) -> str:
    parts = [str(bet.get("runnerName") or bet.get("betFor") or "Sport")]
    if bet.get("betType"):
        parts.append(str(bet["betType"]))
    if bet.get("odds"):
        parts.append(f"@ {bet['odds']}")
    if bet.get("stake"):
        parts.append(f"Stake {bet['stake']}")
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


def _rows_from_sports_bet_statement(bet: dict) -> list[dict]:
    stake = _num(bet.get("stake", 0))
    if stake <= 0:
        return []
    remark = _sports_statement_remark(bet)
    created = bet.get("createdAt")
    settled = bet.get("settledAt") or created
    status = str(bet.get("status") or "open").lower()
    pl = _num(bet.get("profitLoss", 0))
    rows: list[dict] = []
    if status == "open":
        rows.append(_statement_row(-stake, f"{remark} (Open)", created, "cricket"))
        return rows
    rows.append(_statement_row(-stake, f"{remark} (Bet)", created, "cricket"))
    if pl > 0:
        rows.append(_statement_row(stake + pl, f"{remark} (Win)", settled, "cricket"))
    elif pl < 0:
        rows.append(_statement_row(pl, f"{remark} (Loss)", settled, "cricket"))
    return rows


def _rows_from_casino_bet_statement(bet: dict) -> list[dict]:
    stake = _num(bet.get("stake", bet.get("amount", 0)))
    if stake <= 0:
        return []
    remark = _casino_statement_remark(bet)
    created = bet.get("createdAt")
    settled = bet.get("settledAt") or created
    status = str(bet.get("status") or "open").lower()
    pl = _num(bet.get("profitLoss", 0))
    game_type = "diamondCasino" if str(bet.get("gameType") or "") != "aviator" else "diamondCasino"
    rows: list[dict] = []
    if status == "open":
        rows.append(_statement_row(-stake, f"{remark} (Open)", created, game_type))
        return rows
    rows.append(_statement_row(-stake, f"{remark} (Bet)", created, game_type))
    if pl > 0:
        win_amount = _num(bet.get("winAmount", 0))
        credit = win_amount if win_amount > 0 else round(stake + pl, 2)
        rows.append(_statement_row(credit, f"{remark} (Win)", settled, game_type))
    elif pl < 0:
        rows.append(_statement_row(pl, f"{remark} (Loss)", settled, game_type))
    return rows


def _rows_from_matka_bet_statement(bet: dict) -> list[dict]:
    stake = _num(bet.get("stake", 0))
    if stake <= 0:
        return []
    num = bet.get("number") or bet.get("betNumber") or ""
    remark = f"Matka {bet.get('matkaEventId') or ''} #{num} | Stake {stake}".strip()
    created = bet.get("createdAt")
    settled = bet.get("settledAt") or created
    status = str(bet.get("status") or "open").lower()
    pl = _num(bet.get("profitLoss", 0))
    rows: list[dict] = []
    if status == "open":
        rows.append(_statement_row(-stake, f"{remark} (Open)", created, "matka"))
        return rows
    rows.append(_statement_row(-stake, f"{remark} (Bet)", created, "matka"))
    if pl > 0:
        rows.append(_statement_row(stake + pl, f"{remark} (Win)", settled, "matka"))
    elif pl < 0:
        rows.append(_statement_row(pl, f"{remark} (Loss)", settled, "matka"))
    return rows


def _rows_from_cached_statement(doc: dict) -> list[dict]:
    rows: list[dict] = []
    for row in doc.get("rows") or []:
        amount = _num(row.get("credit", 0)) - _num(row.get("debit", 0))
        rows.append(_statement_row(
            amount,
            row.get("description") or "",
            row.get("date") or row.get("createdAt"),
            row.get("gameType") or "cash",
            statement_for="ACCOUNT_STATEMENT",
            user_remark=row.get("userRemark") or "",
            is_comm=bool(row.get("isComm", False)),
            market_id=str(row.get("marketId") or ""),
        ))
    return rows


def _filter_statement_rows(rows: list[dict], statement_for: str | None) -> list[dict]:
    if not statement_for:
        return rows
    if statement_for == "ACCOUNT_STATEMENT":
        return [r for r in rows if r.get("statementFor") == "ACCOUNT_STATEMENT"]
    if statement_for == "profitLoss":
        return [r for r in rows if r.get("statementFor") != "ACCOUNT_STATEMENT"]
    return rows


def _user_settlement_balance(db, user_id: str) -> float:
    """Cash settlement balance — positive = dena (payable), negative = lena (receivable)."""
    data = compute_user_ledger({"downlineUserId": user_id})
    cal = _num(data.get("calAmount", 0))
    if cal != 0 or data.get("ledgerData"):
        return round(cal, 2)
    return 0.0


def compute_lena_dena_list(payload: dict, session_user: dict | None = None) -> list[dict]:
    """user/lenaDena read — /app/ledger/all/{type} + /app/cash-transction/{type}."""
    payload = payload or {}
    session_user = session_user or {}
    db = get_db()
    fetch_type = str(payload.get("fetchUserType") or "client").lower()

    if payload.get("downlineUserId"):
        parent_id = str(payload["downlineUserId"])
        if not _session_may_access_user(db, session_user, parent_id):
            return []
        users = list(db.users.find({
            "userType": fetch_type,
            "isDeleted": {"$ne": True},
            "$or": [{"parentId": parent_id}, {"creatorId": parent_id}],
        }, {"_id": 0}))
    else:
        root_id = str(session_user.get("userId") or "")
        viewer_type = str(session_user.get("userType") or "").lower()
        direct_child = USER_TYPE_CHILD.get(viewer_type, "")

        if root_id and fetch_type == direct_child:
            users = list(db.users.find({
                "userType": fetch_type,
                "isDeleted": {"$ne": True},
                "$or": [{"parentId": root_id}, {"creatorId": root_id}],
            }, {"_id": 0}))
        elif root_id:
            downline = _session_downline_ids(db, session_user)
            users = list(db.users.find({
                "userId": {"$in": list(downline)},
                "userType": fetch_type,
                "isDeleted": {"$ne": True},
            }, {"_id": 0}))
        else:
            users = list(db.users.find({
                "userType": fetch_type,
                "isDeleted": {"$ne": True},
            }, {"_id": 0}))

    rows: list[dict] = []
    for user in users:
        uid = str(user.get("userId") or "")
        if not uid:
            continue
        rows.append({
            "userId": uid,
            "username": user.get("username", ""),
            "name": str(user.get("name") or user.get("username") or "").strip(),
            "userType": user.get("userType", fetch_type),
            "balance": _user_settlement_balance(db, uid),
            "coins": _num(user.get("coins", 0)),
        })
    rows.sort(key=lambda r: abs(_num(r.get("balance", 0))), reverse=True)
    return rows


def _empty_user_ledger() -> dict:
    return {
        "totalCoins": 0,
        "creditAmount": 0,
        "debitAmount": 0,
        "calAmount": 0,
        "sportLedger": 0,
        "diamondCasinoLedger": 0,
        "intCasinoLedger": 0,
        "matkaLedger": 0,
        "cashLedger": 0,
        "ledgerData": [],
    }


def _ledger_sort_dt(created_at: Any) -> datetime:
    parsed = _parse_date(created_at)
    if parsed:
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return datetime.min.replace(tzinfo=timezone.utc)


def _match_teams_display_name(db, market_id: str = "", event_id: str = "") -> str:
    """Poora match label — eventName/matchName ya teamData se 'Team1 v Team2'."""
    match = _find_match_doc(db, str(market_id or ""))
    if not match and event_id:
        match = db.matches.find_one(
            {"eventId": str(event_id)},
            {"_id": 0, "eventName": 1, "matchName": 1, "teamData": 1},
        )
    if not match:
        return ""
    label = str(match.get("eventName") or match.get("matchName") or "").strip()
    if label:
        return label
    names: list[str] = []
    for team in parse_team_selections(match)[:2]:
        name = team_label_from_row(normalize_team_entry(team))
        if name and name not in names:
            names.append(name)
    if len(names) >= 2:
        return f"{names[0]} v {names[1]}"
    return names[0] if names else ""


def _ledger_match_event_name(db, market_id: str, event_id: str) -> str:
    label = _match_teams_display_name(db, market_id, event_id)
    if label:
        return label
    if event_id:
        return f"Event {event_id}"
    if market_id:
        return f"Market {market_id}"
    return "Sports"


_LEDGER_COLLECTION_LABEL = {
    "cricket": "Cricket",
    "settle": "Cash",
    "diamondCasino": "Diamond Casino",
    "internationalCasino": "International Casino",
    "matka": "Matka",
}


def _ledger_collection_label(ledger_type: str, *, game_hint: str = "", category: str = "") -> str:
    cat = str(category or "").lower()
    if cat == "commission":
        return "Commission"
    if str(game_hint or "").lower() == "aviator":
        return "Aviator"
    return _LEDGER_COLLECTION_LABEL.get(ledger_type, game_hint or ledger_type or "Other")


def _ledger_date_key(created_at: Any) -> str:
    parsed = _ledger_sort_dt(created_at)
    if parsed and parsed != datetime.min.replace(tzinfo=timezone.utc):
        return parsed.strftime("%Y-%m-%d")
    return ""


def _ledger_entry_row(
    amount: float,
    event_name: str,
    remark: str,
    ledger_type: str,
    created_at: Any,
    *,
    market_id: Any = None,
    event_id: Any = None,
    ledger_id: Any = None,
    bet_id: Any = None,
    user_type: str = "",
) -> dict:
    signed = round(_num(amount), 2)
    collection = event_name or _ledger_collection_label(ledger_type)
    row = {
        "amount": signed,
        "remark": remark or collection,
        "eventName": collection,
        "ledgerType": ledger_type,
        "marketId": str(market_id) if market_id not in (None, "") else None,
        "eventId": event_id,
        "createdAt": _statement_ts(created_at),
        "date": _ledger_date_key(created_at),
        "userType": user_type or "client",
        "isDeleted": "false",
        "_sort": _ledger_sort_dt(created_at),
    }
    if ledger_id not in (None, ""):
        row["ledgerId"] = str(ledger_id)
    if bet_id not in (None, ""):
        row["betId"] = str(bet_id)
    return row


_LEDGER_CATEGORY_TYPE = {
    "sport": "cricket",
    "casino": "diamondCasino",
    "matka": "matka",
}


def _parse_ledger_entry_desc(desc: str) -> tuple[str, str]:
    text = str(desc or "").strip()
    if not text:
        return "Sports", ""
    sep = "—" if "—" in text else (" - " if " - " in text else "")
    if sep:
        head, tail = text.split(sep, 1)
        head = head.strip()
        tail = tail.strip()
        remark = tail.split("(P/L")[0].strip()
        head_l = head.lower()
        if "casino" in head_l:
            return "Diamond Casino", remark or tail
        if "matka" in head_l:
            return "Matka", remark or tail
        if any(x in head_l for x in ("sport", "fancy", "bet settled", "match")):
            return "Cricket", remark or tail
        return head, remark or tail
    return text, text


def _ledger_row_from_ledger_entry(entry: dict, db) -> Optional[dict]:
    """ledger_entries se bet-by-bet row — scraped live site jaisa."""
    category = str(entry.get("category") or "").lower()
    ledger_type = _LEDGER_CATEGORY_TYPE.get(category)
    if not ledger_type:
        return None
    amt = _num(entry.get("amount", 0))
    if amt == 0:
        return None
    signed = amt if str(entry.get("type") or "").lower() == "credit" else -amt
    desc = str(entry.get("description") or entry.get("remark") or "")
    _, remark = _parse_ledger_entry_desc(desc)
    market_id = entry.get("marketId")
    event_id = entry.get("eventId")
    collection = _ledger_collection_label(ledger_type)
    if ledger_type == "cricket" and (market_id or event_id):
        match_name = _ledger_match_event_name(db, str(market_id or ""), str(event_id or ""))
        if match_name and not match_name.startswith(("Market ", "Event ")):
            collection = match_name
    return _ledger_entry_row(
        signed,
        collection,
        remark or desc,
        ledger_type,
        entry.get("createdAt"),
        market_id=market_id,
        event_id=event_id,
        ledger_id=entry.get("ledgerId"),
    )


def _ledger_row_from_sports_bet(bet: dict, db) -> Optional[dict]:
    stake = _num(bet.get("stake", 0))
    if stake <= 0:
        return None
    market_id = str(bet.get("marketId") or "")
    event_id = bet.get("eventId")
    match_name = _ledger_match_event_name(db, market_id, str(event_id or ""))
    remark = bet.get("runnerName") or bet.get("betFor") or bet.get("betType") or ""
    if not remark and bet.get("betType") is not None and bet.get("odds") is not None:
        remark = f"{bet.get('betType')} @ {bet.get('odds')}"
    event_name = match_name or _ledger_collection_label("cricket")
    created = bet.get("createdAt")
    status = str(bet.get("status") or "open").lower()
    bet_id = bet.get("betId")
    if status == "open":
        open_remark = f"{remark} (Open)" if remark else "(Open)"
        return _ledger_entry_row(-stake, event_name, open_remark, "cricket", created, market_id=market_id, event_id=event_id, bet_id=bet_id)
    pl = _num(bet.get("profitLoss", 0))
    settled = bet.get("settledAt") or created
    row_remark = remark or ""
    if pl > 0:
        return _ledger_entry_row(pl, event_name, row_remark, "cricket", settled, market_id=market_id, event_id=event_id, bet_id=bet_id)
    if pl < 0:
        return _ledger_entry_row(pl, event_name, row_remark, "cricket", settled, market_id=market_id, event_id=event_id, bet_id=bet_id)
    return _ledger_entry_row(-stake, event_name, f"{row_remark} (Lost)" if row_remark else "(Lost)", "cricket", settled, market_id=market_id, event_id=event_id, bet_id=bet_id)


def _ledger_row_from_casino_bet(bet: dict) -> Optional[dict]:
    stake = _num(bet.get("stake", 0))
    if stake <= 0:
        return None
    gt = str(bet.get("gameType") or "casino")
    event_id = bet.get("eventId")
    created = bet.get("createdAt")
    if gt == "aviator":
        collection = _ledger_collection_label("diamondCasino", game_hint="aviator")
        remark = bet.get("roundId") or "Aviator"
        if bet.get("multiplier"):
            remark = f"{remark} @ {bet['multiplier']}x"
    else:
        collection = _ledger_collection_label("diamondCasino")
        remark = str(bet.get("selection") or bet.get("roundId") or bet.get("casinoType") or "Casino")
    bet_id = bet.get("betId")
    status = str(bet.get("status") or "open").lower()
    if status == "open":
        return _ledger_entry_row(-stake, collection, f"{remark} (Open)", "diamondCasino", created, event_id=event_id, bet_id=bet_id)
    pl = _num(bet.get("profitLoss", 0))
    settled = bet.get("settledAt") or created
    if pl > 0:
        return _ledger_entry_row(pl, collection, remark, "diamondCasino", settled, event_id=event_id, bet_id=bet_id)
    if pl < 0:
        return _ledger_entry_row(pl, collection, remark, "diamondCasino", settled, event_id=event_id, bet_id=bet_id)
    return _ledger_entry_row(-stake, collection, f"{remark} (Lost)", "diamondCasino", settled, event_id=event_id, bet_id=bet_id)


def _ledger_row_from_matka_bet(bet: dict) -> Optional[dict]:
    stake = _num(bet.get("stake", 0))
    if stake <= 0:
        return None
    event_id = bet.get("matkaEventId") or bet.get("eventId")
    num = bet.get("number") or bet.get("betNumber") or ""
    matka_label = str(bet.get("matkaName") or bet.get("name") or f"Matka {event_id or ''}").strip()
    collection = _ledger_collection_label("matka")
    remark = matka_label or str(num)
    created = bet.get("createdAt")
    bet_id = bet.get("betId")
    status = str(bet.get("status") or "open").lower()
    if status == "open":
        return _ledger_entry_row(-stake, collection, f"#{num} (Open)" if num else f"{remark} (Open)", "matka", created, event_id=event_id, bet_id=bet_id)
    pl = _num(bet.get("profitLoss", 0))
    settled = bet.get("settledAt") or created
    if pl > 0:
        return _ledger_entry_row(pl, collection, remark, "matka", settled, event_id=event_id, bet_id=bet_id)
    if pl < 0:
        return _ledger_entry_row(pl, collection, remark, "matka", settled, event_id=event_id, bet_id=bet_id)
    return _ledger_entry_row(-stake, collection, f"#{num} (Lost)" if num else f"{remark} (Lost)", "matka", settled, event_id=event_id, bet_id=bet_id)


def _remark_from_ledger_entries(entries: list[dict]) -> str:
    """ledger_entries description se scraped-style remark (e.g. 'L @ 0.04', 'ENGLAND W')."""
    if not entries:
        return ""
    latest = max(entries, key=lambda e: _ledger_sort_dt(e.get("createdAt")))
    _, remark = _parse_ledger_entry_desc(str(latest.get("description") or latest.get("remark") or ""))
    return remark or str(latest.get("remark") or "").strip()


def _won_by_from_sports_bets(bets: list[dict], open_count: int = 0) -> str:
    """Won-by / selection remark — match naam ke bina."""
    if not bets:
        return ""
    settled = [b for b in bets if str(b.get("status") or "").lower() != "open"]
    pool = settled or bets
    latest = max(pool, key=lambda b: _ledger_sort_dt(b.get("settledAt") or b.get("createdAt")))
    remark = str(
        latest.get("runnerName")
        or latest.get("sessionName")
        or latest.get("marketName")
        or ""
    ).strip()
    if not remark:
        bet_type = latest.get("betType")
        odds = latest.get("odds")
        if bet_type is not None and odds is not None:
            remark = f"{bet_type} @ {odds}"
    if open_count:
        open_note = f"{open_count} open"
        remark = f"{remark} ({open_note})" if remark else open_note
    return remark


def _remark_from_sports_bets(
    bets: list[dict],
    open_count: int = 0,
    *,
    db=None,
    market_id: str = "",
    event_id: str = "",
) -> str:
    """Match overall row — poora team/match naam (scraped site jaisa)."""
    if db is not None:
        teams_label = _match_teams_display_name(db, str(market_id or ""), str(event_id or ""))
        if teams_label:
            if open_count:
                return f"{teams_label} ({open_count} open)"
            return teams_label
    return _won_by_from_sports_bets(bets, open_count)


def _remark_from_casino_bets(bets: list[dict]) -> str:
    if not bets:
        return ""
    latest = max(bets, key=lambda b: _ledger_sort_dt(b.get("settledAt") or b.get("createdAt")))
    if str(latest.get("gameType") or "") == "aviator":
        remark = str(latest.get("roundId") or "Aviator")
        if latest.get("multiplier"):
            remark = f"{remark} @ {latest['multiplier']}x"
        return remark
    return str(
        latest.get("selection") or latest.get("roundId") or latest.get("casinoType") or ""
    ).strip()


def _overall_rows_from_sports_bets(db, uid: str, from_dt, to_dt) -> list[dict]:
    """Match-wise overall — ek row per marketId (scraped site overall jaisa)."""
    groups: dict[str, list[dict]] = {}
    for bet in db.sports_bets.find({"userId": uid}, {"_id": 0}):
        created = bet.get("settledAt") or bet.get("createdAt")
        if not _statement_in_range(created, from_dt, to_dt):
            continue
        key = str(bet.get("marketId") or bet.get("eventId") or "sports")
        groups.setdefault(key, []).append(bet)

    rows: list[dict] = []
    for _key, bets in groups.items():
        market_id = bets[0].get("marketId")
        event_id = bets[0].get("eventId")
        if not _match_completed_for_ledger(db, str(market_id or ""), str(event_id or "")):
            continue
        match_name = _ledger_match_event_name(db, str(market_id or ""), str(event_id or ""))
        event_name = match_name or _ledger_collection_label("cricket")
        net = 0.0
        latest = bets[0].get("createdAt")
        settled_bets: list[dict] = []
        for bet in bets:
            status = str(bet.get("status") or "open").lower()
            if status == "open" and not bet.get("isDeclare"):
                continue
            settled_bets.append(bet)
            net += _num(bet.get("profitLoss", 0))
            ts = bet.get("settledAt") or bet.get("createdAt")
            if _ledger_sort_dt(ts) >= _ledger_sort_dt(latest):
                latest = ts
        if not settled_bets:
            continue
        remark = _won_by_from_sports_bets(settled_bets)
        rows.append(_ledger_entry_row(
            round(net, 2), event_name, remark, "cricket", latest,
            market_id=market_id, event_id=event_id,
        ))
    return rows


def _overall_rows_from_sport_entries(db, uid: str, from_dt, to_dt) -> list[dict]:
    """Sports bets na hon to ledger_entries ka total — ek overall cricket row."""
    total = 0.0
    latest = None
    entries: list[dict] = []
    for entry in db.ledger_entries.find({"userId": uid, "category": "sport"}, {"_id": 0}):
        if not _statement_in_range(entry.get("createdAt"), from_dt, to_dt):
            continue
        entries.append(entry)
        amt = _num(entry.get("amount", 0))
        total += amt if str(entry.get("type") or "").lower() == "credit" else -amt
        ts = entry.get("createdAt")
        if latest is None or _ledger_sort_dt(ts) > _ledger_sort_dt(latest):
            latest = ts
    if total == 0:
        return []
    remark = _remark_from_ledger_entries(entries)
    sample_bet = db.sports_bets.find_one({"userId": uid}, {"_id": 0, "marketId": 1, "eventId": 1})
    match_name = ""
    if sample_bet:
        match_name = _match_teams_display_name(
            db, str(sample_bet.get("marketId") or ""), str(sample_bet.get("eventId") or "")
        )
    event_name = match_name or _ledger_collection_label("cricket")
    if not remark and match_name:
        remark = _won_by_from_sports_bets(
            list(db.sports_bets.find({"userId": uid}, {"_id": 0}).limit(20))
        )
    return [_ledger_entry_row(round(total, 2), event_name, remark, "cricket", latest)]


def _overall_rows_from_casino_bets(db, uid: str, from_dt, to_dt) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    for bet in db.casino_bets.find({"userId": uid}, {"_id": 0}):
        created = bet.get("settledAt") or bet.get("createdAt")
        if not _statement_in_range(created, from_dt, to_dt):
            continue
        key = str(bet.get("eventId") or bet.get("roundId") or bet.get("casinoType") or "casino")
        groups.setdefault(key, []).append(bet)

    rows: list[dict] = []
    for _key, bets in groups.items():
        if any(
            str(b.get("status") or "open").lower() == "open" and not b.get("isDeclare")
            for b in bets
        ):
            continue
        gt = str(bets[0].get("gameType") or "casino")
        collection = _ledger_collection_label("diamondCasino", game_hint=gt)
        net = 0.0
        latest = bets[0].get("createdAt")
        for bet in bets:
            net += _num(bet.get("profitLoss", 0))
            ts = bet.get("settledAt") or bet.get("createdAt")
            if _ledger_sort_dt(ts) >= _ledger_sort_dt(latest):
                latest = ts
        rows.append(_ledger_entry_row(
            round(net, 2), collection, _remark_from_casino_bets(bets), "diamondCasino", latest,
            event_id=bets[0].get("eventId"),
        ))
    return rows


def _overall_rows_from_casino_entries(db, uid: str, from_dt, to_dt) -> list[dict]:
    total = 0.0
    latest = None
    entries: list[dict] = []
    for entry in db.ledger_entries.find({"userId": uid, "category": "casino"}, {"_id": 0}):
        if not _statement_in_range(entry.get("createdAt"), from_dt, to_dt):
            continue
        entries.append(entry)
        amt = _num(entry.get("amount", 0))
        total += amt if str(entry.get("type") or "").lower() == "credit" else -amt
        ts = entry.get("createdAt")
        if latest is None or _ledger_sort_dt(ts) > _ledger_sort_dt(latest):
            latest = ts
    if total == 0:
        return []
    remark = _remark_from_ledger_entries(entries)
    return [_ledger_entry_row(round(total, 2), _ledger_collection_label("diamondCasino"), remark, "diamondCasino", latest)]


def _overall_rows_from_matka_bets(db, uid: str, from_dt, to_dt) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    for bet in db.matka_bets.find({"userId": uid}, {"_id": 0}):
        created = bet.get("settledAt") or bet.get("createdAt")
        if not _statement_in_range(created, from_dt, to_dt):
            continue
        key = str(bet.get("matkaEventId") or bet.get("eventId") or "matka")
        groups.setdefault(key, []).append(bet)

    rows: list[dict] = []
    for _key, bets in groups.items():
        collection = _ledger_collection_label("matka")
        net = sum(_num(b.get("profitLoss", 0)) for b in bets if str(b.get("status", "")).lower() != "open")
        net -= sum(_num(b.get("stake", 0)) for b in bets if str(b.get("status", "")).lower() == "open")
        latest = max((b.get("settledAt") or b.get("createdAt") for b in bets), key=_ledger_sort_dt)
        remark = str(bets[0].get("matkaName") or bets[0].get("name") or "").strip()
        if not remark:
            nums = [str(b.get("number") or b.get("betNumber") or "").strip() for b in bets]
            nums = [n for n in nums if n]
            remark = nums[-1] if nums else ""
        rows.append(_ledger_entry_row(round(net, 2), collection, remark, "matka", latest))
    return rows


def _bet_by_bet_ledger_rows(db, uid: str, from_dt, to_dt) -> list[dict]:
    """BetByBet — har bet alag row (owner Bet Delete ke liye betId)."""
    rows: list[dict] = []
    for _cat, bet_coll, bet_row_fn in (
        ("sport", db.sports_bets, lambda b: _ledger_row_from_sports_bet(b, db)),
        ("casino", db.casino_bets, _ledger_row_from_casino_bet),
        ("matka", db.matka_bets, _ledger_row_from_matka_bet),
    ):
        for bet in bet_coll.find({"userId": uid, "isDeleted": {"$ne": True}}, {"_id": 0}):
            created = bet.get("settledAt") or bet.get("createdAt")
            if not _statement_in_range(created, from_dt, to_dt):
                continue
            row = bet_row_fn(bet)
            if row:
                rows.append(row)
    return rows


def _descendant_client_ids(db, uid: str) -> list[str]:
    """Agent+ ke neeche saare client userId — cash-transaction downline rollup."""
    desc = _collect_descendant_ids(db, uid)
    if not desc:
        return []
    return [
        u["userId"]
        for u in db.users.find(
            {"userId": {"$in": list(desc)}, "userType": "client", "isDeleted": {"$ne": True}},
            {"userId": 1},
        )
    ]


def _cash_settle_remark(entry: dict) -> str:
    """Deposit/withdraw/cash settle — scraped ledger jaisa clear label."""
    transfer = str(entry.get("transferType") or "").lower()
    desc = str(entry.get("description") or "").strip()
    remark = str(entry.get("remark") or "").strip()
    if transfer == "deposit":
        return "Deposit"
    if transfer in ("first_deposit", "user_create"):
        return "First Deposit"
    if transfer == "withdraw":
        return "Withdrawal"
    desc_l = desc.lower()
    if "first deposit" in desc_l:
        return "First Deposit"
    if "withdraw" in desc_l:
        return "Withdrawal"
    if "deposit" in desc_l:
        return "Deposit"
    return remark or desc or "Cash"


def _cash_settle_ledger_rows(db, uid: str, from_dt, to_dt) -> list[dict]:
    rows: list[dict] = []
    for entry in db.ledger_entries.find({"userId": uid}, {"_id": 0}):
        category = str(entry.get("category") or "cash")
        if category in ("sport", "casino", "matka"):
            continue
        if not _statement_in_range(entry.get("createdAt"), from_dt, to_dt):
            continue
        amt = _num(entry.get("amount", 0))
        signed = amt if str(entry.get("type") or "").lower() == "credit" else -amt
        remark = _cash_settle_remark(entry)
        rows.append(_ledger_entry_row(
            signed,
            _ledger_collection_label("settle", category=category),
            remark,
            "settle",
            entry.get("createdAt"),
            ledger_id=entry.get("ledgerId"),
        ))
    return rows


_PASSBOOK_ONLY_TRANSFER_TYPES = frozenset({
    "deposit",
    "first_deposit",
    "withdraw",
    "user_create",
})


def _is_passbook_only_cash_entry(entry: dict) -> bool:
    """Deposit/withdraw/account create — passbook only, client ledger se exclude."""
    transfer_type = str(entry.get("transferType") or "").lower()
    if transfer_type in _PASSBOOK_ONLY_TRANSFER_TYPES:
        return True
    desc = str(entry.get("description") or entry.get("remark") or "").lower()
    return any(k in desc for k in ("deposit", "withdrawal", "first deposit", "withdraw", "account created"))


def _overall_client_ledger_rows(db, uid: str, from_dt, to_dt) -> list[dict]:
    """Ek client ka overall sport/casino/matka rows — cash/deposit passbook only."""
    rows: list[dict] = []
    sport_rows = _overall_rows_from_sports_bets(db, uid, from_dt, to_dt)
    if not sport_rows and not _user_has_bets_in_range(db, uid, "sports_bets", from_dt, to_dt):
        sport_rows = _overall_rows_from_sport_entries(db, uid, from_dt, to_dt)
    rows.extend(sport_rows)

    casino_rows = _overall_rows_from_casino_bets(db, uid, from_dt, to_dt)
    if not casino_rows and not _user_has_bets_in_range(db, uid, "casino_bets", from_dt, to_dt):
        casino_rows = _overall_rows_from_casino_entries(db, uid, from_dt, to_dt)
    rows.extend(casino_rows)

    rows.extend(_overall_rows_from_matka_bets(db, uid, from_dt, to_dt))
    return rows


def _overall_ledger_rows(db, uid: str, from_dt, to_dt) -> list[dict]:
    """Overall ledger — client apna; agent+ apna cash + downline clients ka rollup."""
    user = _find_user(db, uid) or {}
    user_type = str(user.get("userType") or "client").lower()

    if user_type == "client":
        return _overall_client_ledger_rows(db, uid, from_dt, to_dt)

    rows: list[dict] = []
    for client_id in _descendant_client_ids(db, uid):
        rows.extend(_overall_client_ledger_rows(db, client_id, from_dt, to_dt))
    rows.extend(_cash_settle_ledger_rows(db, uid, from_dt, to_dt))
    return rows


def _ledger_is_bet_by_bet(user: dict, payload: dict) -> bool:
    mode = str(payload.get("ledgerMode") or payload.get("ledgerView") or "").lower()
    if mode in ("betbybet", "bet", "detail"):
        return True
    if mode in ("overall", "summary"):
        return False
    # Admin cash-transaction ledger — default overall (scraped site jaisa)
    return False


def _apply_ledger_balances(rows: list[dict], current_coins: float) -> None:
    if not rows:
        return
    chrono = sorted(rows, key=lambda r: r.get("_sort") or datetime.min.replace(tzinfo=timezone.utc))
    total_pl = round(sum(_num(r.get("amount", 0)) for r in chrono), 2)
    running = round(float(current_coins) - total_pl, 2)
    for row in chrono:
        running = round(running + _num(row.get("amount", 0)), 2)
        row["balance"] = running


def compute_user_ledger(payload: dict, session_user: dict | None = None) -> dict:
    """user/userLedger — admin /app/ledger/{userType} (My Ledger)."""
    payload = payload or {}
    session_user = session_user or {}
    uid = str(
        payload.get("userId")
        or payload.get("downlineUserId")
        or session_user.get("userId")
        or ""
    )
    if not uid:
        return _empty_user_ledger()

    db = get_db()
    user = _find_user(db, uid) or {}
    user_type = str(user.get("userType") or "client")
    ledger_type = str(payload.get("ledgerType") or payload.get("type") or "all").strip()
    from_dt = _parse_date(payload.get("fromDate") or payload.get("startDate"))
    to_dt = _parse_date(payload.get("toDate") or payload.get("endDate"))
    if to_dt and to_dt.hour == 0 and to_dt.minute == 0:
        to_dt = to_dt + timedelta(days=1) - timedelta(microseconds=1)

    rows: list[dict] = []
    if _ledger_is_bet_by_bet(user, payload):
        rows = _bet_by_bet_ledger_rows(db, uid, from_dt, to_dt)
        if user_type != "client":
            for entry in db.ledger_entries.find({"userId": uid}, {"_id": 0}):
                category = str(entry.get("category") or "cash")
                if category in ("sport", "casino", "matka"):
                    continue
                if not _statement_in_range(entry.get("createdAt"), from_dt, to_dt):
                    continue
                amt = _num(entry.get("amount", 0))
                signed = amt if entry.get("type") == "credit" else -amt
                rows.append(_ledger_entry_row(
                    signed,
                    _ledger_collection_label("settle", category=category),
                    _cash_settle_remark(entry),
                    "settle",
                    entry.get("createdAt"),
                    ledger_id=entry.get("ledgerId"),
                ))
    else:
        rows = _overall_ledger_rows(db, uid, from_dt, to_dt)

    if ledger_type and ledger_type not in ("all", ""):
        if ledger_type == "internationalCasino":
            rows = [r for r in rows if r.get("ledgerType") in ("diamondCasino", "internationalCasino")]
        else:
            rows = [r for r in rows if r.get("ledgerType") == ledger_type]

    _apply_ledger_balances(rows, _num(user.get("coins", 0)))

    rows.sort(key=lambda r: r.get("_sort") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    for row in rows:
        row.pop("_sort", None)
        row["userType"] = user_type
        row["isDeleted"] = "false"

    credit = debit = 0.0
    sport_ledger = diamond_ledger = matka_ledger = cash_ledger = 0.0
    for row in rows:
        amt = _num(row.get("amount", 0))
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
        "totalCoins": round(_num(user.get("coins", 0)), 2),
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


def compute_user_statement(payload: dict) -> list[dict]:
    """user/userStatement — admin /app/statement/{userId} (chunk 4503)."""
    payload = payload or {}
    db = get_db()
    uid = str(payload.get("userId") or "")
    if not uid:
        return []
    from_dt = _parse_date(payload.get("startDate") or payload.get("fromDate"))
    to_dt = _parse_date(payload.get("endDate") or payload.get("toDate"))
    filter_for = payload.get("statementFor") or None

    rows: list[dict] = []
    for doc in db.statements.find({"userId": uid}, {"_id": 0}):
        rows.extend(_rows_from_cached_statement(doc))

    for entry in db.ledger_entries.find({"userId": uid}, {"_id": 0}).sort("createdAt", 1):
        if not _statement_in_range(entry.get("createdAt"), from_dt, to_dt):
            continue
        category = str(entry.get("category") or "cash")
        amt = _num(entry.get("amount", 0))
        if entry.get("type") == "debit":
            amt = -amt
        is_comm = category == "commission"
        stmt_for = "BET" if is_comm else "ACCOUNT_STATEMENT"
        game_type = "settle" if category in ("cash", "transfer") else category
        rows.append(_statement_row(
            amt,
            entry.get("description") or "",
            entry.get("createdAt"),
            game_type,
            statement_for=stmt_for,
            user_remark=entry.get("remark") or "",
            is_comm=is_comm,
        ))

    for bet in db.sports_bets.find({"userId": uid}, {"_id": 0}).sort("createdAt", 1):
        if not _statement_in_range(bet.get("createdAt"), from_dt, to_dt):
            continue
        rows.extend(_rows_from_sports_bet_statement(bet))

    for bet in db.casino_bets.find({"userId": uid}, {"_id": 0}).sort("createdAt", 1):
        if not _statement_in_range(bet.get("createdAt"), from_dt, to_dt):
            continue
        rows.extend(_rows_from_casino_bet_statement(bet))

    for bet in db.matka_bets.find({"userId": uid}, {"_id": 0}).sort("createdAt", 1):
        if not _statement_in_range(bet.get("createdAt"), from_dt, to_dt):
            continue
        rows.extend(_rows_from_matka_bet_statement(bet))

    rows = _filter_statement_rows(rows, filter_for)
    # Chunk 4503: running balance = total - prefixSum — newest row top par (scraped site jaisa)
    rows.sort(key=lambda r: r.get("createdAt") or 0, reverse=True)
    return rows


def compute_casino_result_by_round(payload: dict) -> list[dict]:
    """casino/resultByRoundWise — roundId se result cards."""
    payload = payload or {}
    db = get_db()
    round_id = str(payload.get("roundId") or payload.get("mid") or "")
    q: dict = {}
    if round_id:
        q["roundId"] = round_id
    if payload.get("eventId") is not None:
        q["eventId"] = payload["eventId"]

    rows: list[dict] = []
    for doc in db.casino_rounds.find(q, {"_id": 0}):
        result = doc.get("result") or {}
        cards = result.get("cards") or []
        if isinstance(cards, list):
            cards_str = ",".join(str(c) for c in cards)
        else:
            cards_str = str(cards)
        rows.append({
            "mid": doc.get("roundId"),
            "roundId": doc.get("roundId"),
            "eventId": doc.get("eventId"),
            "gtype": doc.get("gtype") or result.get("gtype") or "teen20",
            "cards": cards_str,
            "winner": result.get("winner") or "",
        })

    if not rows and round_id:
        bet = db.casino_bets.find_one({"roundId": round_id}, {"_id": 0})
        if bet:
            rows.append({
                "mid": round_id,
                "roundId": round_id,
                "eventId": bet.get("eventId"),
                "gtype": bet.get("gameType") or "teen20",
                "cards": bet.get("cards") or "",
                "winner": bet.get("selection") or "",
            })
    return rows
