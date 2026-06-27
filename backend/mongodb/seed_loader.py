"""Load scraped JSON + demo hierarchy into MongoDB seed documents."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

BET_CHIPS = {
    "100": 100, "500": 500, "1000": 1000, "2000": 2000,
    "5000": 5000, "10000": 10000, "25000": 25000, "50000": 50000,
    "100000": 100000, "200000": 200000, "300000": 300000, "500000": 500000,
}

HIERARCHY = [
    ("uid-owner", "OWNER001", "Owner Demo", "owner", 9, "", 10_000_000, "admin@123"),
    ("uid-subowner", "SUBOWNER001", "Sub Owner", "subowner", 8, "uid-owner", 5_000_000, "admin@123"),
    ("uid-subowner2", "SUBOWNER002", "Sub Owner Two", "subowner", 8, "uid-owner", 4_500_000, "admin@123"),
    ("uid-subowner3", "SUBOWNER003", "Sub Owner Three", "subowner", 8, "uid-owner", 4_000_000, "admin@123"),
    ("uid-superadmin", "SUPERADMIN001", "Super Admin", "superadmin", 7, "uid-subowner", 2_000_000, "admin@123"),
    ("uid-superadmin2", "SUPERADMIN002", "Super Admin Two", "superadmin", 7, "uid-subowner", 1_800_000, "admin@123"),
    ("uid-superadmin3", "SUPERADMIN003", "Super Admin Three", "superadmin", 7, "uid-subowner", 1_600_000, "admin@123"),
    ("uid-admin", "ADMIN001", "Admin Demo", "admin", 6, "uid-superadmin", 1_000_000, "admin@123"),
    ("uid-admin2", "ADMIN002", "Admin Two", "admin", 6, "uid-superadmin", 900_000, "admin@123"),
    ("uid-admin3", "ADMIN003", "Admin Three", "admin", 6, "uid-superadmin", 850_000, "admin@123"),
    ("uid-subadmin", "SUBADMIN001", "Sub Admin", "subadmin", 5, "uid-admin", 500_000, "admin@123"),
    ("uid-subadmin2", "SUBADMIN002", "Sub Admin Two", "subadmin", 5, "uid-admin", 450_000, "admin@123"),
    ("uid-subadmin3", "SUBADMIN003", "Sub Admin Three", "subadmin", 5, "uid-admin", 420_000, "admin@123"),
    ("uid-master", "MASTER001", "Master Demo", "master", 4, "uid-subadmin", 200_000, "admin@123"),
    ("uid-master2", "MASTER002", "Master Two", "master", 4, "uid-subadmin", 180_000, "admin@123"),
    ("uid-master3", "MASTER003", "Master Three", "master", 4, "uid-subadmin", 160_000, "admin@123"),
    ("uid-superagent", "SUPERAGENT001", "Super Agent", "superagent", 3, "uid-master", 100_000, "admin@123"),
    ("uid-superagent2", "SUPERAGENT002", "Super Agent Two", "superagent", 3, "uid-master", 80_000, "admin@123"),
    ("uid-superagent3", "SUPERAGENT003", "Super Agent Three", "superagent", 3, "uid-master", 70_000, "admin@123"),
    ("uid-agent", "AGENT001", "Agent Demo", "agent", 2, "uid-superagent", 50_000, "admin@123"),
    ("uid-agent2", "AGENT002", "Agent Two", "agent", 2, "uid-superagent", 40_000, "admin@123"),
    ("uid-client", "CLIENT001", "Client Demo", "client", 1, "uid-agent", 10_000, "admin@123"),
    ("6946a4bc25b6ee438db6ff4d", "C358167", "Sharma", "client", 1, "uid-agent", 0, "615849"),
    ("6a1bd2d7356a85a557003d5d", "C324001", "Demo User", "client", 1, "uid-agent", 1_000, "123456"),
]

CLIENT_ID = "6946a4bc25b6ee438db6ff4d"
ADMIN_ID = "uid-admin"

# userType -> (matchCommission, sessionCommission, casinoCommission) demo %
COMMISSION_BY_TYPE = {
    "owner": (2, 3, 2),
    "subowner": (2, 3, 2),
    "superadmin": (2, 3, 2),
    "admin": (2, 3, 2),
    "subadmin": (2, 3, 2),
    "master": (2, 3, 2),
    "superagent": (2, 3, 2),
    "agent": (2, 3, 2),
    "client": (1, 1, 1),
}


def _commission_for_type(utype: str) -> tuple[int, int, int]:
    return COMMISSION_BY_TYPE.get(str(utype or "").lower(), (0, 0, 0))


def _now():
    return datetime.now(timezone.utc)


def _load_json(path: Path) -> Any:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return None


def _load_api_list(relative: str) -> list:
    raw = _load_json(ROOT / relative)
    if not raw:
        return []
    data = raw.get("data")
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("list", "casinoBetData", "ledgerData", "rows"):
            if isinstance(data.get(key), list):
                return data[key]
    return []


def build_users() -> list[dict]:
    users = []
    now = _now()
    for uid, username, name, utype, priority, parent, coins, password in HIERARCHY:
        match_comm, session_comm, casino_comm = _commission_for_type(utype)
        users.append({
            "userId": uid,
            "username": username.upper() if not username.startswith("C") else username,
            "password": password,
            "name": name,
            "mobile": "9999999999",
            "userType": utype,
            "userPriority": priority,
            "parentId": parent or None,
            "creatorId": parent or "uid-owner",
            "coins": coins,
            "creditLimit": coins,
            "exposure": 0,
            "profitLoss": 0,
            "casinoStatus": True,
            "matkaStatus": True,
            "betStatus": True,
            "matchStatus": True,
            "intCasinoStatus": False,
            "matchShare": 100 if utype != "client" else 0,
            "casinoShare": 100 if utype != "client" else 0,
            "matchCommission": match_comm,
            "sessionCommission": session_comm,
            "casinoCommission": casino_comm,
            "commissionType": "BetByBet" if utype != "client" else "BetByBet",
            "betChipsData": BET_CHIPS,
            "isPasswordChanged": True,
            "status": 1,
            "isDeleted": False,
            "referralCode": f"{username}100100",
            "createdAt": now,
            "updatedAt": now,
        })

    details = _load_json(ROOT / "output" / "api_data" / "user_details.json")
    if details and details.get("data"):
        d = details["data"]
        for u in users:
            if u["userId"] == d.get("userId") or u["username"] == d.get("username"):
                u.update({k: v for k, v in d.items() if k not in ("passwordShow", "otp")})
                u["password"] = "615849"
                break
    return users


def build_domains() -> list[dict]:
    raw = _load_json(ROOT / "output" / "api_data" / "domain_settings.json")
    if raw and raw.get("data"):
        doc = dict(raw["data"])
        doc["domainName"] = doc.get("domainName") or "1ex99.in"
        return [doc]
    return [{
        "domainName": "1ex99.in",
        "domainUrl": "1ex99.in",
        "title": "Welcome to 1ex99.in",
        "userNotification": "Local MongoDB demo",
        "status": True,
    }]


def _merge_seed_file(rows: list[dict], filename: str, key: str = "marketId") -> list[dict]:
    path = Path(__file__).parent / "seed" / filename
    if not path.exists():
        return rows
    try:
        extra = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return rows
    if not isinstance(extra, list):
        return rows
    seen = {str(r.get(key)) for r in rows if r.get(key) is not None}
    for item in extra:
        item_key = item.get(key)
        if item_key is None or str(item_key) in seen:
            continue
        rows.append(item)
        seen.add(str(item_key))
    return rows


def build_matches() -> list[dict]:
    rows = _load_api_list("output/api_data/match_list.json")
    if not rows:
        rows = _load_api_list("admin/api_data/match_list.json")
    rows = _merge_seed_file(rows or [], "matches.json")
    return rows or [{
        "marketId": "1.245690241",
        "eventId": "28127348",
        "sportId": 4,
        "seriesId": 101480,
        "matchName": "Demo IPL Match",
        "isMatchOdds": True,
        "isFancy": True,
        "isBookmaker": True,
        "betPerm": True,
        "status": "INPLAY",
        "marketList": [],
        "betDelaySetting": {"matchOddsBetDelay": 2, "bookMakerBetDelay": 2},
        "createdAt": _now(),
    }]


def build_casino_games() -> list[dict]:
    rows = _load_api_list("output/api_data/casino_data.json")
    if not rows:
        rows = _load_api_list("admin/api_data/casino_data.json")
    return rows or [{
        "eventId": 3030,
        "name": "20-20 Teenpatti",
        "shortName": "teen20",
        "minStake": 100,
        "maxStake": 20000,
        "betStatus": True,
        "socketURL": "https://casinoapi.tresting.com",
        "cacheURL": "https://casinoapi.tresting.com/v2/api/casinoData?casinoType=teen20",
        "videoUrl1": "/casino-stream/route/?id3030",
        "setting": {"oddsDifference": "0.03"},
    }]


def build_user_ledger() -> list[dict]:
    return [{
        "userId": "uid-client",
        "totalCoins": 10000,
        "creditAmount": 5000,
        "debitAmount": 2500,
        "calAmount": 2500,
        "sportLedger": 200,
        "diamondCasinoLedger": 850,
        "intCasinoLedger": 0,
        "matkaLedger": 0,
        "cashLedger": 1450,
        "ledgerData": [],
    }, {
        "userId": CLIENT_ID,
        "totalCoins": 10000,
        "creditAmount": 5000,
        "debitAmount": 2500,
        "calAmount": 2500,
        "sportLedger": 200,
        "diamondCasinoLedger": 850,
        "intCasinoLedger": 0,
        "matkaLedger": 0,
        "cashLedger": 1450,
        "ledgerData": [],
    }, {
        "userId": "6946a4bc25b6ee438db6ff4d",
        "totalCoins": 0,
        "creditAmount": 1200,
        "debitAmount": 0,
        "calAmount": -1200,
        "sportLedger": -400,
        "diamondCasinoLedger": -800,
        "intCasinoLedger": 0,
        "matkaLedger": 0,
        "cashLedger": 0,
        "ledgerData": [],
    }, {
        "userId": "6a1bd2d7356a85a557003d5d",
        "totalCoins": 800,
        "creditAmount": 0,
        "debitAmount": 0,
        "calAmount": 0,
        "sportLedger": 0,
        "diamondCasinoLedger": 0,
        "intCasinoLedger": 0,
        "matkaLedger": 0,
        "cashLedger": 0,
        "ledgerData": [],
    }, {
        "userId": "85098e9c10864fcb8f65efdf",
        "totalCoins": 4508.04,
        "creditAmount": 0,
        "debitAmount": 0,
        "calAmount": 0,
        "sportLedger": 0,
        "diamondCasinoLedger": 0,
        "intCasinoLedger": 0,
        "matkaLedger": 0,
        "cashLedger": 0,
        "ledgerData": [],
    }, {
        "userId": "a6809605d6e74acc8b9323c9",
        "totalCoins": 2839,
        "creditAmount": 400,
        "debitAmount": 900,
        "calAmount": -500,
        "sportLedger": 100,
        "diamondCasinoLedger": 200,
        "intCasinoLedger": 0,
        "matkaLedger": 0,
        "cashLedger": 0,
        "ledgerData": [],
    }, {
        "userId": "5b8da4767d3c47c7bf5914fb",
        "totalCoins": 5000,
        "creditAmount": 1000,
        "debitAmount": 0,
        "calAmount": 1000,
        "sportLedger": 500,
        "diamondCasinoLedger": 500,
        "intCasinoLedger": 0,
        "matkaLedger": 0,
        "cashLedger": 0,
        "ledgerData": [],
    }, {
        "userId": "uid-agent",
        "totalCoins": 50000,
        "creditAmount": 10000,
        "debitAmount": 3000,
        "calAmount": 7000,
        "sportLedger": 2000,
        "diamondCasinoLedger": 5000,
        "intCasinoLedger": 0,
        "matkaLedger": 0,
        "cashLedger": 0,
        "ledgerData": [],
    }, {
        "userId": "902dea9738c944989b95638a",
        "totalCoins": 35000,
        "creditAmount": 500,
        "debitAmount": 3000,
        "calAmount": -2500,
        "sportLedger": -800,
        "diamondCasinoLedger": -1700,
        "intCasinoLedger": 0,
        "matkaLedger": 0,
        "cashLedger": 0,
        "ledgerData": [],
    }, {
        "userId": "uid-agent2",
        "totalCoins": 40000,
        "creditAmount": 2000,
        "debitAmount": 3500,
        "calAmount": -1500,
        "sportLedger": -500,
        "diamondCasinoLedger": -1000,
        "intCasinoLedger": 0,
        "matkaLedger": 0,
        "cashLedger": 0,
        "ledgerData": [],
    }, {
        "userId": "uid-superagent",
        "totalCoins": 100000,
        "creditAmount": 15000,
        "debitAmount": 3000,
        "calAmount": 12000,
        "sportLedger": 4000,
        "diamondCasinoLedger": 8000,
        "intCasinoLedger": 0,
        "matkaLedger": 0,
        "cashLedger": 0,
        "ledgerData": [],
    }, {
        "userId": "uid-superagent2",
        "totalCoins": 80000,
        "creditAmount": 500,
        "debitAmount": 4000,
        "calAmount": -3500,
        "sportLedger": -1200,
        "diamondCasinoLedger": -2300,
        "intCasinoLedger": 0,
        "matkaLedger": 0,
        "cashLedger": 0,
        "ledgerData": [],
    }, {
        "userId": "uid-superagent3",
        "totalCoins": 70000,
        "creditAmount": 0,
        "debitAmount": 0,
        "calAmount": 0,
        "sportLedger": 0,
        "diamondCasinoLedger": 0,
        "intCasinoLedger": 0,
        "matkaLedger": 0,
        "cashLedger": 0,
        "ledgerData": [],
    }, {
        "userId": "uid-master",
        "totalCoins": 200000,
        "creditAmount": 18000,
        "debitAmount": 4000,
        "calAmount": 14000,
        "sportLedger": 5000,
        "diamondCasinoLedger": 9000,
        "intCasinoLedger": 0,
        "matkaLedger": 0,
        "cashLedger": 0,
        "ledgerData": [],
    }, {
        "userId": "uid-master2",
        "totalCoins": 180000,
        "creditAmount": 800,
        "debitAmount": 5200,
        "calAmount": -4400,
        "sportLedger": -1400,
        "diamondCasinoLedger": -3000,
        "intCasinoLedger": 0,
        "matkaLedger": 0,
        "cashLedger": 0,
        "ledgerData": [],
    }, {
        "userId": "uid-master3",
        "totalCoins": 160000,
        "creditAmount": 0,
        "debitAmount": 0,
        "calAmount": 0,
        "sportLedger": 0,
        "diamondCasinoLedger": 0,
        "intCasinoLedger": 0,
        "matkaLedger": 0,
        "cashLedger": 0,
        "ledgerData": [],
    }, {
        "userId": "uid-subadmin",
        "totalCoins": 500000,
        "creditAmount": 22000,
        "debitAmount": 5000,
        "calAmount": 17000,
        "sportLedger": 7000,
        "diamondCasinoLedger": 10000,
        "intCasinoLedger": 0,
        "matkaLedger": 0,
        "cashLedger": 0,
        "ledgerData": [],
    }, {
        "userId": "uid-subadmin2",
        "totalCoins": 450000,
        "creditAmount": 1000,
        "debitAmount": 6500,
        "calAmount": -5500,
        "sportLedger": -2000,
        "diamondCasinoLedger": -3500,
        "intCasinoLedger": 0,
        "matkaLedger": 0,
        "cashLedger": 0,
        "ledgerData": [],
    }, {
        "userId": "uid-subadmin3",
        "totalCoins": 420000,
        "creditAmount": 0,
        "debitAmount": 0,
        "calAmount": 0,
        "sportLedger": 0,
        "diamondCasinoLedger": 0,
        "intCasinoLedger": 0,
        "matkaLedger": 0,
        "cashLedger": 0,
        "ledgerData": [],
    }, {
        "userId": "uid-admin",
        "totalCoins": 1000000,
        "creditAmount": 25000,
        "debitAmount": 6000,
        "calAmount": 19000,
        "sportLedger": 8000,
        "diamondCasinoLedger": 11000,
        "intCasinoLedger": 0,
        "matkaLedger": 0,
        "cashLedger": 0,
        "ledgerData": [],
    }, {
        "userId": "uid-admin2",
        "totalCoins": 900000,
        "creditAmount": 1200,
        "debitAmount": 8200,
        "calAmount": -7000,
        "sportLedger": -2500,
        "diamondCasinoLedger": -4500,
        "intCasinoLedger": 0,
        "matkaLedger": 0,
        "cashLedger": 0,
        "ledgerData": [],
    }, {
        "userId": "uid-admin3",
        "totalCoins": 850000,
        "creditAmount": 0,
        "debitAmount": 0,
        "calAmount": 0,
        "sportLedger": 0,
        "diamondCasinoLedger": 0,
        "intCasinoLedger": 0,
        "matkaLedger": 0,
        "cashLedger": 0,
        "ledgerData": [],
    }, {
        "userId": "uid-superadmin",
        "totalCoins": 2000000,
        "creditAmount": 30000,
        "debitAmount": 8000,
        "calAmount": 22000,
        "sportLedger": 9000,
        "diamondCasinoLedger": 13000,
        "intCasinoLedger": 0,
        "matkaLedger": 0,
        "cashLedger": 0,
        "ledgerData": [],
    }, {
        "userId": "uid-superadmin2",
        "totalCoins": 1800000,
        "creditAmount": 1500,
        "debitAmount": 10500,
        "calAmount": -9000,
        "sportLedger": -3000,
        "diamondCasinoLedger": -6000,
        "intCasinoLedger": 0,
        "matkaLedger": 0,
        "cashLedger": 0,
        "ledgerData": [],
    }, {
        "userId": "uid-superadmin3",
        "totalCoins": 1600000,
        "creditAmount": 0,
        "debitAmount": 0,
        "calAmount": 0,
        "sportLedger": 0,
        "diamondCasinoLedger": 0,
        "intCasinoLedger": 0,
        "matkaLedger": 0,
        "cashLedger": 0,
        "ledgerData": [],
    }, {
        "userId": "uid-subowner",
        "totalCoins": 5000000,
        "creditAmount": 35000,
        "debitAmount": 10000,
        "calAmount": 25000,
        "sportLedger": 10000,
        "diamondCasinoLedger": 15000,
        "intCasinoLedger": 0,
        "matkaLedger": 0,
        "cashLedger": 0,
        "ledgerData": [],
    }, {
        "userId": "uid-subowner2",
        "totalCoins": 4500000,
        "creditAmount": 2000,
        "debitAmount": 14000,
        "calAmount": -12000,
        "sportLedger": -4000,
        "diamondCasinoLedger": -8000,
        "intCasinoLedger": 0,
        "matkaLedger": 0,
        "cashLedger": 0,
        "ledgerData": [],
    }, {
        "userId": "uid-subowner3",
        "totalCoins": 4000000,
        "creditAmount": 0,
        "debitAmount": 0,
        "calAmount": 0,
        "sportLedger": 0,
        "diamondCasinoLedger": 0,
        "intCasinoLedger": 0,
        "matkaLedger": 0,
        "cashLedger": 0,
        "ledgerData": [],
    }]


def build_day_wise_casino() -> list[dict]:
    raw = _load_json(ROOT / "admin/api_data/day_wise_casino.json")
    if raw and isinstance(raw.get("data"), list):
        return raw["data"]
    return []


def build_profit_loss() -> list[dict]:
    raw = _load_json(ROOT / "admin/api_data/profit_loss.json")
    if raw and isinstance(raw.get("data"), list) and raw["data"]:
        return raw["data"]
    return [{"userId": ADMIN_ID, "profitLoss": 1250.5, "exposure": 0}]


def build_sports_bets() -> list[dict]:
    return [{
        "betId": "bet-sport-001",
        "userId": CLIENT_ID,
        "marketId": "1.245690241",
        "eventId": "28127348",
        "selectionId": 49749064,
        "runnerName": "Team A",
        "stake": 500,
        "odds": 1.85,
        "betType": "B",
        "betFor": "match",
        "oddsType": "match",
        "marketType": "Match Odds",
        "profitLoss": 0,
        "status": "open",
        "createdAt": _now(),
    }, {
        "betId": "bet-fancy-001",
        "userId": CLIENT_ID,
        "marketId": "1.245690241",
        "eventId": "28127348",
        "selectionId": 90001,
        "runnerName": "10 Over Runs",
        "stake": 200,
        "odds": 95,
        "betType": "N",
        "betFor": "fancy",
        "oddsType": "fancy",
        "marketType": "Session",
        "profitLoss": 0,
        "status": "open",
        "createdAt": _now(),
    }, {
        "betId": "bet-bm-nmp-001",
        "userId": CLIENT_ID,
        "marketId": "1.259072606",
        "eventId": "35706611",
        "selectionId": 1,
        "runnerName": "North Mumbai Panthers",
        "stake": 100,
        "odds": 157,
        "betType": "L",
        "betFor": "odds",
        "oddsType": "bookmaker",
        "marketType": "Bookmaker",
        "positionInfo": {"1": 157.0, "2": -100.0},
        "profitLoss": 0,
        "status": "open",
        "createdAt": _now(),
    }, {
        "betId": "bet-sport-uid-001",
        "userId": "uid-client",
        "marketId": "1.245690241",
        "eventId": "28127348",
        "selectionId": 49749064,
        "runnerName": "Team A",
        "stake": 500,
        "odds": 1.85,
        "betType": "B",
        "betFor": "match",
        "oddsType": "match",
        "marketType": "Match Odds",
        "profitLoss": 0,
        "status": "open",
        "createdAt": _now(),
    }, {
        "betId": "bet-fancy-uid-001",
        "userId": "uid-client",
        "marketId": "1.245690241",
        "eventId": "28127348",
        "selectionId": 90001,
        "runnerName": "10 Over Runs",
        "stake": 200,
        "odds": 95,
        "betType": "N",
        "betFor": "fancy",
        "oddsType": "fancy",
        "marketType": "Session",
        "profitLoss": 0,
        "status": "open",
        "createdAt": _now(),
    }, {
        "betId": "bet-bm-uid-001",
        "userId": "uid-client",
        "marketId": "1.259072606",
        "eventId": "35706611",
        "selectionId": 1,
        "runnerName": "North Mumbai Panthers",
        "stake": 100,
        "odds": 157,
        "betType": "L",
        "betFor": "odds",
        "oddsType": "bookmaker",
        "marketType": "Bookmaker",
        "positionInfo": {"1": 157.0, "2": -100.0},
        "profitLoss": 0,
        "status": "open",
        "createdAt": _now(),
    }]


def build_casino_bets() -> list[dict]:
    now = _now()
    return [{
        "betId": "bet-casino-001",
        "userId": CLIENT_ID,
        "eventId": 3030,
        "roundId": "R-12345",
        "stake": 1000,
        "odds": 1.85,
        "selection": "Player A",
        "playerName": "Player A",
        "profitLoss": 850,
        "gameType": "diamond",
        "status": "settled",
        "isDeclare": True,
        "showResult": "Player A",
        "resultDetails": {"winner": "Player A", "gtype": "teen20", "cards": ["H7", "D9", "C3"]},
        "createdAt": now,
    }, {
        "betId": "bet-casino-002",
        "userId": CLIENT_ID,
        "eventId": 3030,
        "roundId": "R-12346",
        "stake": 500,
        "odds": 2.0,
        "selection": "Player B",
        "playerName": "Player B",
        "profitLoss": 0,
        "gameType": "diamond",
        "status": "open",
        "isDeclare": False,
        "createdAt": now,
    }, {
        "betId": "bet-casino-003",
        "userId": CLIENT_ID,
        "eventId": 3031,
        "roundId": "R-3031-001",
        "stake": 200,
        "odds": 1.95,
        "selection": "Dragon",
        "playerName": "Dragon",
        "profitLoss": -200,
        "gameType": "diamond",
        "status": "settled",
        "isDeclare": True,
        "showResult": "Tiger",
        "resultDetails": {"winner": "Tiger", "gtype": "dt20"},
        "createdAt": now - timedelta(hours=2),
    }, {
        "betId": "bet-casino-004",
        "userId": "uid-client",
        "eventId": 3030,
        "roundId": "R-12347",
        "stake": 300,
        "odds": 1.9,
        "selection": "Player A",
        "playerName": "Player A",
        "profitLoss": 270,
        "gameType": "diamond",
        "status": "settled",
        "isDeclare": True,
        "showResult": "Player A",
        "createdAt": now,
    }, {
        "betId": "bet-casino-uid-open-001",
        "userId": "uid-client",
        "eventId": 3030,
        "roundId": "R-uid-open-001",
        "stake": 250,
        "odds": 2.1,
        "selection": "Player B",
        "playerName": "Player B",
        "profitLoss": 0,
        "gameType": "diamond",
        "status": "open",
        "isDeclare": False,
        "createdAt": now,
    }, {
        "betId": "bet-aviator-001",
        "userId": CLIENT_ID,
        "eventId": 9999,
        "roundId": "AV-001",
        "stake": 500,
        "selection": "cashout",
        "multiplier": 2.5,
        "profitLoss": 750,
        "gameType": "aviator",
        "status": "settled",
        "isDeclare": True,
        "showResult": "2.50x",
        "createdAt": now,
    }, {
        "betId": "bet-aviator-303031-001",
        "userId": CLIENT_ID,
        "eventId": 303031,
        "roundId": "AV303031-001",
        "stake": 200,
        "selection": "aviator",
        "multiplier": 3.2,
        "profitLoss": 440,
        "gameType": "aviator",
        "status": "settled",
        "isDeclare": True,
        "showResult": "3.20x",
        "createdAt": now,
    }, {
        "betId": "bet-casino-uid-002",
        "userId": "uid-client",
        "eventId": 3030,
        "roundId": "R-uid-002",
        "stake": 500,
        "odds": 2.0,
        "selection": "Player B",
        "playerName": "Player B",
        "profitLoss": 0,
        "gameType": "diamond",
        "status": "open",
        "isDeclare": False,
        "createdAt": now,
    }, {
        "betId": "bet-casino-uid-003",
        "userId": "uid-client",
        "eventId": 3031,
        "roundId": "R-uid-003",
        "stake": 200,
        "odds": 1.95,
        "selection": "Dragon",
        "playerName": "Dragon",
        "profitLoss": -200,
        "gameType": "diamond",
        "status": "settled",
        "isDeclare": True,
        "showResult": "Tiger",
        "resultDetails": {"winner": "Tiger", "gtype": "dt20"},
        "createdAt": now - timedelta(hours=2),
    }, {
        "betId": "bet-aviator-uid-001",
        "userId": "uid-client",
        "eventId": 9999,
        "roundId": "AV-uid-001",
        "stake": 500,
        "selection": "cashout",
        "multiplier": 2.5,
        "profitLoss": 750,
        "gameType": "aviator",
        "status": "settled",
        "isDeclare": True,
        "showResult": "2.50x",
        "createdAt": now - timedelta(hours=3),
    }, {
        "betId": "bet-aviator-303031-002",
        "userId": "uid-client",
        "eventId": 303031,
        "roundId": "AV303031-002",
        "stake": 100,
        "selection": "aviator",
        "multiplier": 0,
        "crashValue": 1.45,
        "profitLoss": -100,
        "gameType": "aviator",
        "status": "settled",
        "isDeclare": True,
        "showResult": "1.45",
        "createdAt": now - timedelta(hours=1),
    }, {
        "betId": "bet-aviator-303031-003",
        "userId": CLIENT_ID,
        "eventId": 303031,
        "roundId": "AV303031-003",
        "stake": 150,
        "selection": "aviator",
        "profitLoss": 0,
        "gameType": "aviator",
        "status": "open",
        "isDeclare": False,
        "createdAt": now,
    }]


def build_casino_rounds() -> list[dict]:
    return [{
        "eventId": 3030,
        "roundId": "R-12345",
        "gtype": "teen20",
        "result": {"winner": "Player A", "gtype": "teen20", "cards": ["H7", "D9", "C3"]},
        "createdAt": _now(),
    }]


def build_matka_events() -> list[dict]:
    return [{
        "matkaEventId": "matka-kalyan-001",
        "name": "Kalyan",
        "openTime": _now(),
        "closeTime": _now() + timedelta(hours=2),
        "result": "",
        "status": "open",
    }, {
        "matkaEventId": "matka-milan-001",
        "name": "Milan Day",
        "openTime": _now(),
        "closeTime": _now() + timedelta(hours=4),
        "result": "456",
        "status": "declared",
    }]


def build_matka_bets() -> list[dict]:
    return [{
        "betId": "bet-matka-001",
        "userId": CLIENT_ID,
        "matkaEventId": "matka-kalyan-001",
        "number": "123",
        "betNumber": "123",
        "gameType": "SINGLE",
        "betType": "OPEN",
        "priority": 1,
        "stake": 100,
        "profitLoss": 0,
        "status": "open",
        "createdAt": _now(),
    }, {
        "betId": "bet-matka-002",
        "userId": CLIENT_ID,
        "matkaEventId": "matka-milan-001",
        "number": "456",
        "betNumber": "456",
        "gameType": "JODI",
        "betType": "CLOSE",
        "priority": 2,
        "stake": 200,
        "profitLoss": 150,
        "status": "settled",
        "createdAt": _now(),
    }, {
        "betId": "bet-matka-uid-001",
        "userId": "uid-client",
        "matkaEventId": "matka-kalyan-001",
        "matkaName": "Kalyan",
        "number": "123",
        "betNumber": "123",
        "gameType": "SINGLE",
        "betType": "OPEN",
        "priority": 1,
        "stake": 100,
        "profitLoss": 0,
        "status": "open",
        "createdAt": _now(),
    }, {
        "betId": "bet-matka-uid-002",
        "userId": "uid-client",
        "matkaEventId": "matka-milan-001",
        "matkaName": "Milan Day",
        "number": "456",
        "betNumber": "456",
        "gameType": "JODI",
        "betType": "CLOSE",
        "priority": 2,
        "stake": 200,
        "profitLoss": 150,
        "status": "settled",
        "createdAt": _now(),
    }]


def build_ledger_entries() -> list[dict]:
    now = _now()
    return [{
        "ledgerId": "led-001",
        "userId": CLIENT_ID,
        "type": "credit",
        "amount": 5000,
        "description": "Opening balance",
        "category": "cash",
        "balanceAfter": 5000,
        "createdAt": now - timedelta(days=2),
    }, {
        "ledgerId": "led-002",
        "userId": CLIENT_ID,
        "type": "debit",
        "amount": 500,
        "description": "Sports bet placed",
        "category": "sport",
        "balanceAfter": 4500,
        "createdAt": now - timedelta(days=1),
    }, {
        "ledgerId": "led-client-open-001",
        "userId": "uid-client",
        "type": "credit",
        "amount": 5000,
        "description": "Opening balance",
        "remark": "Initial deposit from agent",
        "category": "cash",
        "balanceAfter": 5000,
        "createdAt": now - timedelta(days=3),
    }, {
        "ledgerId": "led-client-settle-001",
        "userId": "uid-client",
        "type": "credit",
        "amount": 5000,
        "description": "Limit credit from AGENT001",
        "remark": "Weekly settlement",
        "category": "cash",
        "transferType": "deposit",
        "balanceAfter": 10000,
        "createdAt": now - timedelta(days=1),
    }, {
        "ledgerId": "led-agent-open-001",
        "userId": "uid-agent",
        "type": "credit",
        "amount": 50000,
        "description": "Opening limit from SUPERAGENT001",
        "remark": "Agent opening balance",
        "category": "cash",
        "transferType": "first_deposit",
        "balanceAfter": 50000,
        "createdAt": now - timedelta(days=4),
    }, {
        "ledgerId": "led-agent-dep-001",
        "userId": "uid-agent",
        "toUserId": "uid-client",
        "type": "debit",
        "amount": 10000,
        "description": "Transfer to CLIENT001",
        "remark": "Client limit deposit",
        "category": "cash",
        "transferType": "deposit",
        "balanceAfter": 40000,
        "createdAt": now - timedelta(days=2),
    }, {
        "ledgerId": "led-agent2-open-001",
        "userId": "uid-agent2",
        "type": "credit",
        "amount": 40000,
        "description": "Opening limit from SUPERAGENT001",
        "remark": "Agent Two opening",
        "category": "cash",
        "transferType": "first_deposit",
        "balanceAfter": 40000,
        "createdAt": now - timedelta(days=3),
    }, {
        "ledgerId": "led-agent2-with-001",
        "userId": "uid-agent2",
        "fromUserId": "uid-client",
        "type": "debit",
        "amount": 5500,
        "description": "Settlement adjustment",
        "remark": "Weekly settlement short",
        "category": "cash",
        "transferType": "withdraw",
        "balanceAfter": 34500,
        "createdAt": now - timedelta(hours=4),
    }, {
        "ledgerId": "led-subowner-open-001",
        "userId": "uid-subowner",
        "type": "credit",
        "amount": 5000000,
        "description": "Opening limit from OWNER001",
        "remark": "Sub owner opening balance",
        "category": "cash",
        "transferType": "first_deposit",
        "balanceAfter": 5000000,
        "createdAt": now - timedelta(days=10),
    }, {
        "ledgerId": "led-subowner-dep-001",
        "userId": "uid-subowner",
        "toUserId": "uid-superadmin",
        "type": "debit",
        "amount": 45000,
        "description": "Transfer to SUPERADMIN001",
        "remark": "Super admin limit deposit",
        "category": "cash",
        "transferType": "deposit",
        "balanceAfter": 4955000,
        "createdAt": now - timedelta(days=6),
    }, {
        "ledgerId": "led-subowner2-open-001",
        "userId": "uid-subowner2",
        "type": "credit",
        "amount": 4500000,
        "description": "Opening limit from OWNER001",
        "remark": "Sub owner two opening",
        "category": "cash",
        "transferType": "first_deposit",
        "balanceAfter": 4500000,
        "createdAt": now - timedelta(days=9),
    }, {
        "ledgerId": "led-subowner2-settle-001",
        "userId": "uid-subowner2",
        "type": "debit",
        "amount": 16000,
        "description": "Settlement shortfall",
        "remark": "Weekly settlement adjustment",
        "category": "cash",
        "transferType": "withdraw",
        "balanceAfter": 4484000,
        "createdAt": now - timedelta(hours=1),
    }, {
        "ledgerId": "led-superadmin-open-001",
        "userId": "uid-superadmin",
        "type": "credit",
        "amount": 2000000,
        "description": "Opening limit from SUBOWNER001",
        "remark": "Super admin opening balance",
        "category": "cash",
        "transferType": "first_deposit",
        "balanceAfter": 2000000,
        "createdAt": now - timedelta(days=9),
    }, {
        "ledgerId": "led-superadmin-dep-001",
        "userId": "uid-superadmin",
        "toUserId": "uid-admin",
        "type": "debit",
        "amount": 40000,
        "description": "Transfer to ADMIN001",
        "remark": "Admin limit deposit",
        "category": "cash",
        "transferType": "deposit",
        "balanceAfter": 1960000,
        "createdAt": now - timedelta(days=5),
    }, {
        "ledgerId": "led-superadmin2-open-001",
        "userId": "uid-superadmin2",
        "type": "credit",
        "amount": 1800000,
        "description": "Opening limit from SUBOWNER001",
        "remark": "Super admin two opening",
        "category": "cash",
        "transferType": "first_deposit",
        "balanceAfter": 1800000,
        "createdAt": now - timedelta(days=8),
    }, {
        "ledgerId": "led-superadmin2-settle-001",
        "userId": "uid-superadmin2",
        "type": "debit",
        "amount": 14000,
        "description": "Settlement shortfall",
        "remark": "Weekly settlement adjustment",
        "category": "cash",
        "transferType": "withdraw",
        "balanceAfter": 1786000,
        "createdAt": now - timedelta(hours=1),
    }, {
        "ledgerId": "led-admin-open-001",
        "userId": "uid-admin",
        "type": "credit",
        "amount": 1000000,
        "description": "Opening limit from SUPERADMIN001",
        "remark": "Admin opening balance",
        "category": "cash",
        "transferType": "first_deposit",
        "balanceAfter": 1000000,
        "createdAt": now - timedelta(days=8),
    }, {
        "ledgerId": "led-admin-dep-001",
        "userId": "uid-admin",
        "toUserId": "uid-subadmin",
        "type": "debit",
        "amount": 35000,
        "description": "Transfer to SUBADMIN001",
        "remark": "Sub admin limit deposit",
        "category": "cash",
        "transferType": "deposit",
        "balanceAfter": 965000,
        "createdAt": now - timedelta(days=4),
    }, {
        "ledgerId": "led-admin2-open-001",
        "userId": "uid-admin2",
        "type": "credit",
        "amount": 900000,
        "description": "Opening limit from SUPERADMIN001",
        "remark": "Admin two opening",
        "category": "cash",
        "transferType": "first_deposit",
        "balanceAfter": 900000,
        "createdAt": now - timedelta(days=7),
    }, {
        "ledgerId": "led-admin2-settle-001",
        "userId": "uid-admin2",
        "type": "debit",
        "amount": 12500,
        "description": "Settlement shortfall",
        "remark": "Weekly settlement adjustment",
        "category": "cash",
        "transferType": "withdraw",
        "balanceAfter": 887500,
        "createdAt": now - timedelta(hours=2),
    }, {
        "ledgerId": "led-subadmin-open-001",
        "userId": "uid-subadmin",
        "type": "credit",
        "amount": 500000,
        "description": "Opening limit from ADMIN001",
        "remark": "Sub admin opening balance",
        "category": "cash",
        "transferType": "first_deposit",
        "balanceAfter": 500000,
        "createdAt": now - timedelta(days=7),
    }, {
        "ledgerId": "led-subadmin-dep-001",
        "userId": "uid-subadmin",
        "toUserId": "uid-master",
        "type": "debit",
        "amount": 30000,
        "description": "Transfer to MASTER001",
        "remark": "Master limit deposit",
        "category": "cash",
        "transferType": "deposit",
        "balanceAfter": 470000,
        "createdAt": now - timedelta(days=3),
    }, {
        "ledgerId": "led-subadmin2-open-001",
        "userId": "uid-subadmin2",
        "type": "credit",
        "amount": 450000,
        "description": "Opening limit from ADMIN001",
        "remark": "Sub admin two opening",
        "category": "cash",
        "transferType": "first_deposit",
        "balanceAfter": 450000,
        "createdAt": now - timedelta(days=6),
    }, {
        "ledgerId": "led-subadmin2-settle-001",
        "userId": "uid-subadmin2",
        "type": "debit",
        "amount": 11000,
        "description": "Settlement shortfall",
        "remark": "Weekly settlement adjustment",
        "category": "cash",
        "transferType": "withdraw",
        "balanceAfter": 439000,
        "createdAt": now - timedelta(hours=1),
    }, {
        "ledgerId": "led-master-open-001",
        "userId": "uid-master",
        "type": "credit",
        "amount": 200000,
        "description": "Opening limit from SUBADMIN001",
        "remark": "Master opening balance",
        "category": "cash",
        "transferType": "first_deposit",
        "balanceAfter": 200000,
        "createdAt": now - timedelta(days=6),
    }, {
        "ledgerId": "led-master-dep-001",
        "userId": "uid-master",
        "toUserId": "uid-superagent",
        "type": "debit",
        "amount": 25000,
        "description": "Transfer to SUPERAGENT001",
        "remark": "Super agent limit deposit",
        "category": "cash",
        "transferType": "deposit",
        "balanceAfter": 175000,
        "createdAt": now - timedelta(days=2),
    }, {
        "ledgerId": "led-master2-open-001",
        "userId": "uid-master2",
        "type": "credit",
        "amount": 180000,
        "description": "Opening limit from SUBADMIN001",
        "remark": "Master two opening",
        "category": "cash",
        "transferType": "first_deposit",
        "balanceAfter": 180000,
        "createdAt": now - timedelta(days=5),
    }, {
        "ledgerId": "led-master2-settle-001",
        "userId": "uid-master2",
        "type": "debit",
        "amount": 9200,
        "description": "Settlement shortfall",
        "remark": "Weekly settlement adjustment",
        "category": "cash",
        "transferType": "withdraw",
        "balanceAfter": 170800,
        "createdAt": now - timedelta(hours=2),
    }, {
        "ledgerId": "led-sa-open-001",
        "userId": "uid-superagent",
        "type": "credit",
        "amount": 100000,
        "description": "Opening limit from MASTER001",
        "remark": "Super agent opening balance",
        "category": "cash",
        "transferType": "first_deposit",
        "balanceAfter": 100000,
        "createdAt": now - timedelta(days=5),
    }, {
        "ledgerId": "led-sa-dep-001",
        "userId": "uid-superagent",
        "toUserId": "uid-agent",
        "type": "debit",
        "amount": 15000,
        "description": "Transfer to AGENT001",
        "remark": "Agent limit deposit",
        "category": "cash",
        "transferType": "deposit",
        "balanceAfter": 85000,
        "createdAt": now - timedelta(days=1),
    }, {
        "ledgerId": "led-sa2-open-001",
        "userId": "uid-superagent2",
        "type": "credit",
        "amount": 80000,
        "description": "Opening limit from MASTER001",
        "remark": "Super agent two opening",
        "category": "cash",
        "transferType": "first_deposit",
        "balanceAfter": 80000,
        "createdAt": now - timedelta(days=4),
    }, {
        "ledgerId": "led-sa2-settle-001",
        "userId": "uid-superagent2",
        "type": "debit",
        "amount": 8500,
        "description": "Settlement shortfall",
        "remark": "Weekly settlement adjustment",
        "category": "cash",
        "transferType": "withdraw",
        "balanceAfter": 71500,
        "createdAt": now - timedelta(hours=3),
    }, {
        "ledgerId": "led-comm-001",
        "userId": "uid-agent",
        "type": "credit",
        "amount": 120,
        "description": "Commission len den",
        "remark": "Monthly commission",
        "category": "commission",
        "meta": {"oddsComm": 25.0, "sessionComm": 10.0, "casinoComm": 85.0},
        "createdAt": now,
    }, {
        "ledgerId": "led-owner-open-001",
        "userId": "uid-owner",
        "type": "credit",
        "amount": 10_000_000,
        "description": "Opening balance credit",
        "remark": "Owner opening limit",
        "category": "cash",
        "transferType": "first_deposit",
        "balanceAfter": 10_000_000,
        "createdAt": now - timedelta(days=5),
    }, {
        "ledgerId": "led-owner-dep-001",
        "userId": "uid-owner",
        "toUserId": "uid-subowner",
        "type": "debit",
        "amount": 500_000,
        "description": "Transfer to SUBOWNER001",
        "remark": "Deposit to sub owner",
        "category": "cash",
        "transferType": "deposit",
        "balanceAfter": 9_500_000,
        "createdAt": now - timedelta(days=2),
    }, {
        "ledgerId": "led-owner-dep-002",
        "userId": "uid-owner",
        "toUserId": "uid-subowner",
        "type": "debit",
        "amount": 250_000,
        "description": "Transfer to SUBOWNER001",
        "remark": "Additional limit",
        "category": "cash",
        "transferType": "deposit",
        "balanceAfter": 9_250_000,
        "createdAt": now - timedelta(hours=6),
    }, {
        "ledgerId": "led-owner-comm-001",
        "userId": "uid-owner",
        "type": "credit",
        "amount": 15_000,
        "description": "Casino commission received",
        "remark": "Downline casino commission",
        "category": "commission",
        "transferType": "commission",
        "balanceAfter": 9_265_000,
        "createdAt": now - timedelta(hours=2),
    }, {
        "ledgerId": "led-owner-with-001",
        "userId": "uid-owner",
        "fromUserId": "uid-subowner",
        "type": "credit",
        "amount": 100_000,
        "description": "Withdrawal from SUBOWNER001",
        "remark": "Limit returned",
        "category": "cash",
        "transferType": "withdraw",
        "balanceAfter": 9_365_000,
        "createdAt": now - timedelta(hours=1),
    }]


def build_statements() -> list[dict]:
    return [{
        "statementId": "stmt-001",
        "userId": CLIENT_ID,
        "startDate": _now() - timedelta(days=7),
        "endDate": _now(),
        "rows": [
            {"date": "2026-05-25", "description": "Bet win", "credit": 1000, "debit": 0},
            {"date": "2026-05-26", "description": "Bet loss", "credit": 0, "debit": 500},
        ],
        "totalCredit": 1000,
        "totalDebit": 500,
    }]


def build_reports() -> list[dict]:
    casino_report = _load_json(ROOT / "admin/api_data/day_wise_casino.json")
    rows = []
    if casino_report and isinstance(casino_report.get("data"), list):
        for item in casino_report["data"]:
            rows.append({
                "reportType": "casino/dayWiseCasinoReport",
                "userId": ADMIN_ID,
                "marketId": None,
                "payload": item,
                "createdAt": _now(),
            })
    rows.extend([
        {
            "reportType": "reports/userProfitLoss",
            "userId": ADMIN_ID,
            "marketId": None,
            "payload": {"profitLoss": 1250.5, "exposure": 0},
            "createdAt": _now(),
        },
        {
            "reportType": "bluexchReports/clientPlusMinus",
            "userId": CLIENT_ID,
            "marketId": "1.245690241",
            "payload": {"plusMinus": 420.5, "commission": 12.5},
            "createdAt": _now(),
        },
        {
            "reportType": "decision/userCommissionReport",
            "userId": "uid-agent",
            "marketId": None,
            "payload": {
                "_id": "uid-agent",
                "userInfo": {"userId": "uid-agent", "username": "AGENT001", "name": "Agent Demo", "userType": "agent"},
                "oddsComm": 25.0,
                "sessionComm": 10.0,
                "casinoComm": 85.0,
                "downlineOddsComm": 12.5,
                "downlineSessionComm": 5.0,
                "downlineCasinoComm": 42.5,
            },
            "createdAt": _now(),
        },
    ])
    return rows


def build_positions() -> list[dict]:
    return [{
        "userId": CLIENT_ID,
        "marketId": "1.245690241",
        "selectionId": 49749064,
        "runnerName": "Team A",
        "position": 500,
        "exposure": -425,
    }, {
        "userId": CLIENT_ID,
        "marketId": "1.259072606",
        "eventId": "35706611",
        "runners": {"1": 157.0, "2": -100.0},
        "exposure": 100,
    }]


def build_bpexch_accounts() -> list[dict]:
    return [{
        "userId": CLIENT_ID,
        "balance": 2500,
        "statement": [
            {"date": "2026-05-28", "type": "credit", "amount": 1000},
            {"date": "2026-05-29", "type": "debit", "amount": 200},
        ],
    }]


def build_center_projects() -> list[dict]:
    raw = _load_json(ROOT / "centerpanel/api_data/project_list.json")
    if raw and isinstance(raw.get("data"), dict):
        items = raw["data"].get("list") or []
        rows = []
        for p in items:
            rows.append({
                "projectId": p.get("projectId") or f"proj-{len(rows)+1}",
                "name": p.get("projectName") or p.get("name") or "1ex99",
                "projectName": p.get("projectName") or p.get("name") or "1ex99",
                "domainUrl": p.get("domainUrl") or "1ex99.in",
                "status": p.get("status", True),
            })
        if rows:
            return rows
    return [
        {"projectId": "proj-1ex99", "name": "1ex99.in", "projectName": "1ex99 Demo", "domainUrl": "1ex99.in", "status": True},
        {"projectId": "proj-ons3", "name": "ons3.co", "projectName": "ons3.co", "domainUrl": "ons3.co", "status": True},
    ]


def build_center_events() -> list[dict]:
    raw = _load_json(ROOT / "centerpanel/api_data/all_events.json")
    if raw and isinstance(raw.get("data"), list):
        return raw["data"]
    return []


def build_center_series() -> list[dict]:
    raw = _load_json(ROOT / "centerpanel/api_data/series_list.json")
    if raw and isinstance(raw.get("data"), list):
        return raw["data"]
    return []


def build_center_manual_scores() -> list[dict]:
    return [{
        "eventId": "28127348",
        "marketId": "1.245690241",
        "score": {"team1": 120, "team2": 95, "overs": 20},
        "updatedAt": _now(),
    }]


def build_center_fancy_audit() -> list[dict]:
    return [{
        "fancyId": "mf-001",
        "marketId": "1.245690241",
        "action": "create",
        "userId": ADMIN_ID,
        "payload": {"sessionName": "10 Over Runs"},
        "createdAt": _now(),
    }]


def build_center_fancy_categories() -> list[dict]:
    return [
        {"categoryId": "fc-1", "name": "Normal Fancy", "status": True},
        {"categoryId": "fc-2", "name": "Khado", "status": True},
    ]


def build_center_domain_ips() -> list[dict]:
    return [
        {"domainUrl": "1ex99.in", "ip": "103.83.129.157", "userId": ADMIN_ID},
    ]


def build_decision_logs() -> list[dict]:
    return [{
        "logId": "dec-001",
        "marketId": "1.245690241",
        "eventId": "28127348",
        "action": "declare_result",
        "payload": {"winnerSelectionId": 49749064},
        "createdAt": _now(),
    }]


def build_auth_sessions() -> list[dict]:
    return [{
        "token": "local-demo-jwt-token",
        "userId": CLIENT_ID,
        "username": "C358167",
        "expiresAt": _now() + timedelta(days=1),
        "createdAt": _now(),
    }]


def build_user_activities() -> list[dict]:
    now = _now()
    demo_ips = [
        ("127.0.0.1", "Chrome/Windows"),
        ("192.168.1.10", "Mobile/Android"),
        ("103.21.45.88", "Chrome/Windows"),
        ("49.36.120.15", "Safari/iOS"),
        ("10.0.0.55", "Firefox/Linux"),
    ]
    rows: list[dict] = []

    def add_login(activity_id: str, user_id: str, hours_ago: float, ip_idx: int = 0) -> None:
        ip, device = demo_ips[ip_idx % len(demo_ips)]
        rows.append({
            "activityId": activity_id,
            "userId": user_id,
            "activityType": "login",
            "ip": ip,
            "device": device,
            "payload": {"panel": "admin.1ex99.in", "isp": device.split("/")[-1]},
            "createdAt": now - timedelta(hours=hours_ago),
        })

    for idx, (uid, username, *_rest) in enumerate(HIERARCHY):
        if uid == "uid-owner":
            continue
        add_login(f"act-login-{uid}-001", uid, 2 + idx * 0.5, idx)
        add_login(f"act-login-{uid}-002", uid, 24 + idx, (idx + 1) % len(demo_ips))

    for idx, uid in enumerate((CLIENT_ID, "uid-client", "6a1bd2d7356a85a557003d5d", "85098e9c10864fcb8f65efdf")):
        add_login(f"act-login-{uid}-001", uid, 1 + idx * 0.3, idx)
        add_login(f"act-login-{uid}-002", uid, 12 + idx, (idx + 2) % len(demo_ips))
        add_login(f"act-login-{uid}-003", uid, 48 + idx, (idx + 3) % len(demo_ips))

    rows.extend([
        {
            "activityId": "act-bet-uid-client-001",
            "userId": "uid-client",
            "activityType": "bet",
            "ip": "192.168.1.10",
            "device": "Mobile/Android",
            "payload": {"marketId": "1.245690241", "stake": 100},
            "createdAt": now - timedelta(minutes=30),
        },
    ])
    return rows


def build_sports_catalog() -> list[dict]:
    raw = _load_json(ROOT / "admin/api_data/sport_list.json")
    if raw and isinstance(raw.get("data"), list):
        rows = [x for x in raw["data"] if isinstance(x, dict)]
        if rows:
            return rows
    return [
        {"sportId": 4, "sportName": "Cricket", "status": True},
        {"sportId": 1, "sportName": "Soccer", "status": True},
    ]


def build_center_custom_series() -> list[dict]:
    return [{
        "seriesId": "cs-101480",
        "sportId": 4,
        "seriesName": "Indian Premier League",
        "status": "active",
        "source": "latiyal",
        "createdAt": _now(),
    }]


def build_center_racing_events() -> list[dict]:
    return [{
        "eventId": "race-001",
        "competitionId": "comp-001",
        "eventName": "Demo Horse Race",
        "venue": "Mumbai",
        "startTime": _now() + timedelta(hours=3),
        "status": "scheduled",
    }]


def build_center_manual_fancy() -> list[dict]:
    return [{
        "fancyId": "mf-001",
        "marketId": "1.245690241",
        "sessionName": "10 Over Runs",
        "runsYes": 45,
        "runsNo": 44,
        "oddsYes": 100,
        "oddsNo": 100,
        "status": "active",
    }]


def build_center_manual_bookmaker() -> list[dict]:
    return [{
        "bookmakerId": "bm-001",
        "marketId": "1.245690241",
        "runnerName": "Team A",
        "back": 1.85,
        "lay": 1.87,
        "status": "active",
    }]


def build_center_squad_templates() -> list[dict]:
    return [{
        "templateId": "sq-001",
        "sportId": 4,
        "name": "IPL Default Squad",
        "players": ["Player 1", "Player 2"],
    }]


def build_center_betfair_results() -> list[dict]:
    return [{
        "marketId": "1.245690241",
        "eventId": "28127348",
        "winnerSelectionId": 49749064,
        "resultStatus": "declared",
        "declaredAt": _now(),
    }]


def build_center_master_settings() -> list[dict]:
    return [{
        "settingKey": "betDelayGlobal",
        "value": 2,
        "description": "Global bet delay seconds",
        "updatedAt": _now(),
    }]


def build_all_seed_data() -> dict[str, list[dict]]:
    return {
        "users": build_users(),
        "domains": build_domains(),
        "matches": build_matches(),
        "sports_bets": build_sports_bets(),
        "casino_games": build_casino_games(),
        "casino_bets": build_casino_bets(),
        "casino_rounds": build_casino_rounds(),
        "matka_events": build_matka_events(),
        "matka_bets": build_matka_bets(),
        "ledger_entries": build_ledger_entries(),
        "statements": build_statements(),
        "reports": build_reports(),
        "positions": build_positions(),
        "bpexch_accounts": build_bpexch_accounts(),
        "center_projects": build_center_projects(),
        "center_fancy_categories": build_center_fancy_categories(),
        "center_domain_ips": build_center_domain_ips(),
        "decision_logs": build_decision_logs(),
        "auth_sessions": build_auth_sessions(),
        "user_activities": build_user_activities(),
        "sports_catalog": build_sports_catalog(),
        "center_custom_series": build_center_custom_series(),
        "center_racing_events": build_center_racing_events(),
        "center_manual_fancy": build_center_manual_fancy(),
        "center_manual_bookmaker": build_center_manual_bookmaker(),
        "center_squad_templates": build_center_squad_templates(),
        "center_betfair_results": build_center_betfair_results(),
        "center_master_settings": build_center_master_settings(),
        "center_events": build_center_events(),
        "center_series": build_center_series(),
        "center_manual_scores": build_center_manual_scores(),
        "center_fancy_audit": build_center_fancy_audit(),
        "user_ledger": build_user_ledger(),
        "day_wise_casino": build_day_wise_casino(),
        "profit_loss": build_profit_loss(),
    }


def clone_sports_bets_for_market(
    source_market_id: str,
    target_market_id: str,
    target_event_id: str = "",
) -> int:
    """Demo — ek market ki bets doosri market/event par copy karo."""
    from uuid import uuid4

    from mongodb.db import get_db

    db = get_db()
    target_market_id = str(target_market_id)
    target_event_id = str(target_event_id or "")
    match = db.matches.find_one({"marketId": target_market_id}, {"_id": 0, "eventId": 1})
    if not target_event_id and match:
        target_event_id = str(match.get("eventId") or "")

    source = list(db.sports_bets.find({"marketId": str(source_market_id)}, {"_id": 0}))
    if not source:
        return 0

    db.sports_bets.delete_many({"marketId": target_market_id})
    docs: list[dict] = []
    for bet in source:
        row = dict(bet)
        row["betId"] = f"sport-{uuid4().hex[:12]}"
        row["marketId"] = target_market_id
        if target_event_id:
            row["eventId"] = target_event_id
        docs.append(row)
    if docs:
        db.sports_bets.insert_many(docs)
    return len(docs)


def mark_sample_rejected_bets(market_id: str, limit: int = 2) -> int:
    """Demo — kuch open bets ko rejected/cancelled mark karo."""
    from mongodb.db import get_db

    db = get_db()
    market_id = str(market_id)
    bets = list(db.sports_bets.find({"marketId": market_id}, {"_id": 0, "betId": 1}).limit(limit))
    count = 0
    for bet in bets:
        db.sports_bets.update_one(
            {"betId": bet["betId"]},
            {"$set": {
                "isDeleted": True,
                "status": "rejected",
                "deletedRemark": "Rejected by admin",
            }},
        )
        count += 1
    return count


def mark_match_completed(
    market_id: str,
    won_team_name: str = "",
    won_selection_id: int | None = None,
) -> int:
    """Complete-game list — match ko declared/completed mark karo."""
    from mongodb.db import get_db

    db = get_db()
    market_id = str(market_id)
    upd: dict = {"isDeclare": True, "status": "COMPLETED"}
    if won_team_name:
        upd["wonTeamName"] = won_team_name
    if won_selection_id is not None:
        upd["wonTeamBookmakerSelectionId"] = won_selection_id
    res = db.matches.update_many({"marketId": market_id}, {"$set": upd})
    return int(res.modified_count)


def _demo_team_data(teams: list[tuple[str, int]]) -> str:
    return json.dumps([
        {
            "bookmakerSelectionId": sid,
            "selectionId": sid,
            "runner_name": name,
            "runnerName": name,
        }
        for name, sid in teams
    ])


DEMO_MATCH_META: dict[str, dict] = {
    "1.259049791": {
        "eventId": "35699389",
        "matchName": "West Indies v Sri Lanka (T-20)",
        "matchDate": "12-06-2026 06:00:00 AM",
        "matchType": "T20",
        "wonTeamName": "West Indies",
        "wonTeamBookmakerSelectionId": 1,
        "teams": [("West Indies", 1), ("Sri Lanka", 2)],
    },
    "1.259059770": {
        "eventId": "35705124",
        "matchName": "England W v Sri Lanka W (T-20)",
        "matchDate": "12-06-2026 23:00:00 PM",
        "matchType": "T20",
        "wonTeamName": "England W",
        "wonTeamBookmakerSelectionId": 1,
        "teams": [("England W", 1), ("Sri Lanka W", 2)],
    },
    "1.259072606": {
        "eventId": "35706611",
        "matchName": "Demo Completed Match",
        "matchDate": "10-06-2026 03:00:00 PM",
        "matchType": "T20",
        "wonTeamName": "Team A",
        "wonTeamBookmakerSelectionId": 1,
        "teams": [("Team A", 1), ("Team B", 2)],
    },
    "1.245690241": {
        "eventId": "28127348",
        "matchName": "Demo IPL Match",
        "matchDate": "08-06-2026 07:30:00 PM",
        "matchType": "T20",
        "wonTeamName": "Team A",
        "wonTeamBookmakerSelectionId": 1,
        "teams": [("Team A", 1), ("Team B", 2)],
    },
}


def upsert_demo_matches(*, declared: bool = True) -> int:
    """Sports bets wale markets ke liye matches collection mein docs banao."""
    from mongodb.db import get_db

    db = get_db()
    count = 0
    bet_markets = {str(m) for m in db.sports_bets.distinct("marketId") if m}
    target_ids = bet_markets | set(DEMO_MATCH_META)

    for mid in sorted(target_ids):
        meta = DEMO_MATCH_META.get(mid, {})
        doc = {
            "marketId": mid,
            "eventId": meta.get("eventId") or "",
            "sportId": 4,
            "matchName": meta.get("matchName") or f"Market {mid}",
            "matchDate": meta.get("matchDate") or "",
            "matchType": meta.get("matchType") or "T20",
            "isDeclare": declared,
            "status": "COMPLETED" if declared else "INPLAY",
            "wonTeamName": meta.get("wonTeamName") or "",
            "isBookmaker": True,
            "isFancy": True,
            "isMatchOdds": True,
            "betPerm": True,
        }
        if meta.get("wonTeamBookmakerSelectionId") is not None:
            doc["wonTeamBookmakerSelectionId"] = meta["wonTeamBookmakerSelectionId"]
        if meta.get("teams"):
            doc["teamData"] = _demo_team_data(meta["teams"])
        db.matches.update_one({"marketId": mid}, {"$set": doc}, upsert=True)
        count += 1
    return count


def upsert_demo_casino_bets() -> int:
    """Casino pages ke liye sample settled + open bets upsert."""
    from mongodb.db import get_db

    db = get_db()
    count = 0
    for bet in build_casino_bets():
        bid = bet.get("betId")
        if not bid:
            continue
        db.casino_bets.update_one({"betId": bid}, {"$set": bet}, upsert=True)
        count += 1
    return count


def upsert_demo_sports_bets() -> int:
    """Sports ledger rows ke liye demo bets upsert."""
    from mongodb.db import get_db

    db = get_db()
    count = 0
    for bet in build_sports_bets():
        bid = bet.get("betId")
        if not bid:
            continue
        db.sports_bets.update_one({"betId": bid}, {"$set": bet}, upsert=True)
        count += 1
    return count


def upsert_demo_matka_bets() -> int:
    """Matka ledger rows ke liye demo bets upsert."""
    from mongodb.db import get_db

    db = get_db()
    count = 0
    for bet in build_matka_bets():
        bid = bet.get("betId")
        if not bid:
            continue
        db.matka_bets.update_one({"betId": bid}, {"$set": bet}, upsert=True)
        count += 1
    return count


def upsert_demo_client_ledger() -> int:
    """Client ledger (/app/ledger/client) ke liye uid-client demo upsert."""
    return (
        upsert_demo_sports_bets()
        + upsert_demo_casino_bets()
        + upsert_demo_matka_bets()
        + upsert_demo_ledger_entries()
    )


def upsert_demo_ledger_pages() -> int:
    """Ledger all/cash-transaction pages ke liye user_ledger + hierarchy upsert."""
    return upsert_demo_hierarchy_users() + upsert_demo_user_ledgers()


def upsert_demo_ledger_entries() -> int:
    """Statement / account pages ke liye ledger rows upsert."""
    from mongodb.db import get_db

    db = get_db()
    count = 0
    for entry in build_ledger_entries():
        lid = entry.get("ledgerId")
        if not lid:
            continue
        db.ledger_entries.update_one({"ledgerId": lid}, {"$set": entry}, upsert=True)
        count += 1
    return count


def upsert_demo_user_ledgers() -> int:
    """Cash transaction (lena/dena) pages ke liye user_ledger upsert."""
    from mongodb.db import get_db

    db = get_db()
    count = 0
    for doc in build_user_ledger():
        uid = doc.get("userId")
        if not uid:
            continue
        db.user_ledger.update_one({"userId": uid}, {"$set": doc}, upsert=True)
        count += 1
    return count


def upsert_demo_user_activities() -> int:
    """Login / activity report pages ke liye user_activities upsert."""
    from mongodb.db import get_db

    db = get_db()
    count = 0
    for doc in build_user_activities():
        aid = doc.get("activityId")
        if not aid:
            continue
        db.user_activities.update_one({"activityId": aid}, {"$set": doc}, upsert=True)
        count += 1
    return count


def upsert_demo_login_report() -> int:
    """Login report pages (/app/login-report/*) — hierarchy users + login logs."""
    return upsert_demo_hierarchy_users() + upsert_demo_user_activities()


def upsert_demo_data_report() -> int:
    """Data report pages (/app/dataReport/*) — users, open bets, synced exposure."""
    from mongodb.bets import sync_user_balance

    count = upsert_demo_hierarchy_users()
    count += upsert_demo_sports_bets() + upsert_demo_casino_bets()
    for uid in ("uid-client", CLIENT_ID):
        try:
            sync_user_balance(uid)
            count += 1
        except Exception:
            pass
    return count


def upsert_demo_hierarchy_users() -> int:
    """Hierarchy users (e.g. AGENT002) upsert for cash-transaction pages."""
    from mongodb.db import get_db

    db = get_db()
    count = 0
    for user in build_users():
        uid = user.get("userId")
        if not uid:
            continue
        db.users.update_one({"userId": uid}, {"$set": user}, upsert=True)
        count += 1
    return count
