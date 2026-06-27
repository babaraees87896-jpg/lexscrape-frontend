"""
Admin panel (admin.1ex99.in) — scraped API endpoints ka structure + MongoDB mapping.

Source:
  admin/api_endpoints.json  — JS se extract (43 endpoints)
  admin/api_data/*.json     — live scrape samples

Run:
  python3 mongodb/admin_api_registry.py
  python3 main.py --setup-mongo
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ADMIN_DIR = ROOT / "admin"
ENDPOINTS_FILE = ADMIN_DIR / "api_endpoints.json"
API_DATA_DIR = ADMIN_DIR / "api_data"
OUT_FILE = Path(__file__).parent / "admin_api_structure.json"
TABLES_DIR = Path(__file__).parent / "tables" / "admin"

API_BASE = "https://api.ons3.co/v1/"

# endpoint -> (module, collection, operation, request_fields, scraped_sample_key)
ADMIN_API_MAP: dict[str, tuple] = {
    "user/login": ("Auth", "users", "auth", ["username", "password", "host", "isClient"], "login_info"),
    "logout": ("Auth", "auth_sessions", "delete", [], None),
    "user/userList": ("Users", "users", "read", ["downlineUserType", "parentId", "pageNo", "size"], "user_list"),
    "user/userSearch": ("Users", "users", "read", ["searchTerm", "downlineUserType"], "user_list"),
    "user/create": ("Users", "users", "create", ["username", "password", "name", "userType", "parentId"], None),
    "user/userUpdate": ("Users", "users", "update", ["userId"], None),
    "user/userupdate": ("Users", "users", "update", ["userId"], None),
    "user/updateUserPassword": ("Users", "users", "update", ["userId", "password"], None),
    "user/updateCoins": ("Users", "users", "update", ["userId", "coins"], None),
    "user/getUserShareData": ("Users", "users", "read", ["userId"], None),
    "user/userDetails": ("Users", "users", "read", ["userId"], "user_details"),
    "user/userBalance": ("Users", "users", "read", ["userId"], "balance"),
    "user/userActivity": ("Users", "user_activities", "read", ["userId", "fromDate", "toDate"], None),
    "user/userLoginActivity": ("Users", "user_activities", "read", ["userId", "fromDate", "toDate"], None),
    "user/userLedger": ("Ledger", "ledger_entries", "read", ["userId", "fromDate", "toDate"], "user_ledger"),
    "user/ledgerCreditDebit": ("Ledger", "ledger_entries", "create", ["userId", "amount", "type", "remark"], None),
    "user/deleteUserLedger": ("Ledger", "ledger_entries", "delete", ["ledgerId"], None),
    "user/lenaDena": ("Ledger", "ledger_entries", "create", ["fromUserId", "toUserId", "amount"], None),
    "user/domainList": ("Domain", "domains", "read", [], "domain_list"),
    "website/domainSettingByDomainName": ("Domain", "domains", "read", ["domainName"], None),
    "sports/matchList": ("Sports", "matches", "read", ["sportId", "pageNo", "size"], "match_list"),
    "sports/sportByMarketId": ("Sports", "matches", "read", ["marketId"], None),
    "sports/betsList": ("Sports", "sports_bets", "read", ["userId", "marketId"], "bets_list"),
    "sports/clientListByMarketId": ("Sports", "sports_bets", "read", ["marketId"], None),
    "sports/getOddsPosition": ("Sports", "positions", "read", ["marketId", "userId"], None),
    "sports/getSessionPositionBySelectionId": ("Sports", "positions", "read", ["selectionId", "marketId"], None),
    "casino/getDiamondCasinoData": ("Casino", "casino_games", "read", [], "casino_data"),
    "casino/getDiamondCasinoByEventId": ("Casino", "casino_games", "read", ["eventId"], None),
    "casino/diamondBetsList": ("Casino", "casino_bets", "read", ["userId", "eventId"], "casino_bets"),
    "casino/dayWiseCasinoReport": ("Casino", "reports", "read", ["fromDate", "toDate", "isCasino"], "day_wise_casino"),
    "casino/diamondCasinoReportByUser": ("Casino", "reports", "read", ["userId", "fromDate", "toDate"], None),
    "casino/getPlusMinusCasinoDetail": ("Casino", "reports", "read", ["userId", "eventId"], None),
    "casino/getProfitLossPos": ("Casino", "reports", "read", ["userId", "eventId"], None),
    "casino/realTimeDataPosDataDiamondCasino": ("Casino", "reports", "read", ["eventId", "userId"], None),
    "casino/roundWiseResult": ("Casino", "casino_rounds", "read", ["eventId", "roundId"], None),
    "matka/dayWiseMatkaReport": ("Matka", "reports", "read", ["fromDate", "toDate"], None),
    "matka/getProfitLossPosMatka": ("Matka", "reports", "read", ["userId"], None),
    "decision/completeSportList": ("Reports", "sports_catalog", "read", ["pageNo", "size"], "sport_list"),
    "decision/getPlusMinusByMarketId": ("Reports", "reports", "read", ["marketId"], None),
    "reports/userProfitLoss": ("Reports", "reports", "read", ["userId", "fromDate", "toDate"], "profit_loss"),
    "reports/getPlusMinusByMarketIdByUserWise": ("Reports", "reports", "read", ["marketId"], None),
    "reports/blockMarket": ("Reports", "matches", "update", ["marketId", "status"], None),
    "bluexchReports/clientPlusMinus": ("Reports", "reports", "read", ["userId", "marketId"], None),
}

# MongoDB collections jo admin panel use karta hai
ADMIN_COLLECTIONS: dict[str, dict] = {
    "users": {
        "description": "Poori user hierarchy — owner se client tak. Login, userList, userDetails, balance.",
        "indexes": [
            {"keys": {"username": 1}, "unique": True},
            {"keys": {"userId": 1}, "unique": True},
            {"keys": {"userType": 1}},
            {"keys": {"parentId": 1}},
        ],
        "fields": {
            "userId": {"type": "string", "required": True, "example": "6946a4bc25b6ee438db6ff4d"},
            "username": {"type": "string", "required": True, "example": "ADMIN001"},
            "password": {"type": "string", "required": True},
            "name": {"type": "string", "example": "Admin Demo"},
            "mobile": {"type": "string"},
            "userType": {"type": "enum", "values": ["owner", "subowner", "superadmin", "admin", "subadmin", "master", "superagent", "agent", "client"]},
            "userPriority": {"type": "number", "range": "1-9"},
            "parentId": {"type": "string"},
            "creatorId": {"type": "string"},
            "coins": {"type": "number", "note": "UI .toFixed() — hamesha number rakho"},
            "balance": {"type": "number", "note": "coins ke barabar hota hai list mein"},
            "exposure": {"type": "number", "default": 0},
            "profitLoss": {"type": "number", "default": 0},
            "creditLimit": {"type": "number"},
            "matchShare": {"type": "number"},
            "matchCommission": {"type": "number"},
            "sessionCommission": {"type": "number"},
            "casinoShare": {"type": "number"},
            "casinoCommission": {"type": "number"},
            "casinoStatus": {"type": "boolean"},
            "intCasinoStatus": {"type": "boolean"},
            "matkaStatus": {"type": "boolean"},
            "betStatus": {"type": "boolean"},
            "betChipsData": {"type": "object", "example": {"100": 100, "500": 500}},
            "status": {"type": "string|number", "example": "1"},
            "isDeleted": {"type": "boolean|string"},
            "referralCode": {"type": "string"},
            "createdAt": {"type": "date|timestamp"},
            "updatedAt": {"type": "date|timestamp"},
        },
    },
    "auth_sessions": {
        "description": "user/login token — JWT session store",
        "indexes": [{"keys": {"token": 1}}, {"keys": {"userId": 1}}],
        "fields": {
            "token": {"type": "string", "required": True},
            "userId": {"type": "string", "required": True},
            "username": {"type": "string"},
            "expiresAt": {"type": "date"},
            "createdAt": {"type": "date"},
        },
    },
    "ledger_entries": {
        "description": "user/userLedger, ledgerCreditDebit, lenaDena — paisa credit/debit history",
        "indexes": [{"keys": {"userId": 1, "createdAt": -1}}],
        "fields": {
            "ledgerId": {"type": "string"},
            "userId": {"type": "string", "required": True},
            "fromUserId": {"type": "string", "note": "lenaDena ke liye"},
            "toUserId": {"type": "string", "note": "lenaDena ke liye"},
            "type": {"type": "enum", "values": ["credit", "debit"]},
            "amount": {"type": "number", "required": True},
            "description": {"type": "string"},
            "remark": {"type": "string"},
            "category": {"type": "enum", "values": ["sport", "casino", "matka", "cash", "transfer"]},
            "balanceAfter": {"type": "number"},
            "createdAt": {"type": "date"},
        },
    },
    "user_activities": {
        "description": "user/userActivity, user/userLoginActivity — login aur action logs",
        "indexes": [{"keys": {"userId": 1, "createdAt": -1}}, {"keys": {"activityType": 1}}],
        "fields": {
            "userId": {"type": "string", "required": True},
            "activityType": {"type": "enum", "values": ["login", "logout", "bet", "update", "transfer"]},
            "ip": {"type": "string"},
            "device": {"type": "string"},
            "payload": {"type": "object"},
            "createdAt": {"type": "date"},
        },
    },
    "domains": {
        "description": "user/domainList, website/domainSettingByDomainName",
        "indexes": [{"keys": {"domainName": 1}, "unique": True}],
        "fields": {
            "domainName": {"type": "string", "required": True},
            "domainUrl": {"type": "string"},
            "title": {"type": "string"},
            "userNotification": {"type": "string"},
            "clientNotification": {"type": "string"},
            "themeSetting": {"type": "object"},
            "banner": {"type": "array"},
            "status": {"type": "boolean"},
        },
    },
    "matches": {
        "description": "sports/matchList, sportByMarketId, reports/blockMarket",
        "indexes": [{"keys": {"marketId": 1}}, {"keys": {"eventId": 1}}],
        "fields": {
            "marketId": {"type": "string"},
            "eventId": {"type": "string"},
            "sportId": {"type": "number"},
            "seriesId": {"type": "number"},
            "matchName": {"type": "string"},
            "matchDate": {"type": "string"},
            "marketList": {"type": "array", "note": "sub-markets: Match Odds, Fancy, Bookmaker"},
            "betDelaySetting": {"type": "object"},
            "maxMinCoins": {"type": "object"},
            "isMatchOdds": {"type": "boolean"},
            "isFancy": {"type": "boolean"},
            "isBookmaker": {"type": "boolean"},
            "isDeclare": {"type": "boolean"},
            "status": {"type": "string"},
            "createdAt": {"type": "date"},
        },
    },
    "sports_bets": {
        "description": "sports/betsList, clientListByMarketId",
        "indexes": [{"keys": {"userId": 1}}, {"keys": {"marketId": 1}}, {"keys": {"createdAt": -1}}],
        "fields": {
            "userId": {"type": "string", "required": True},
            "username": {"type": "string"},
            "marketId": {"type": "string"},
            "eventId": {"type": "string"},
            "selectionId": {"type": "number"},
            "stake": {"type": "number"},
            "odds": {"type": "number"},
            "betType": {"type": "string", "values": "B|K|L|N"},
            "betFor": {"type": "string"},
            "marketType": {"type": "string", "values": "odds|fancy|bookmaker"},
            "profitLoss": {"type": "number"},
            "status": {"type": "enum", "values": ["open", "settled", "void"]},
            "createdAt": {"type": "date"},
        },
    },
    "positions": {
        "description": "sports/getOddsPosition, getSessionPositionBySelectionId",
        "indexes": [{"keys": {"userId": 1, "marketId": 1}}],
        "fields": {
            "userId": {"type": "string"},
            "marketId": {"type": "string"},
            "selectionId": {"type": "number"},
            "position": {"type": "number"},
            "exposure": {"type": "number"},
        },
    },
    "casino_games": {
        "description": "casino/getDiamondCasinoData, getDiamondCasinoByEventId",
        "indexes": [{"keys": {"eventId": 1}, "unique": True}],
        "fields": {
            "eventId": {"type": "number", "required": True},
            "name": {"type": "string"},
            "shortName": {"type": "string"},
            "minStake": {"type": "number"},
            "maxStake": {"type": "number"},
            "betStatus": {"type": "boolean"},
            "socketURL": {"type": "string"},
            "cacheURL": {"type": "string"},
            "videoUrl1": {"type": "string"},
            "setting": {"type": "object"},
        },
    },
    "casino_bets": {
        "description": "casino/diamondBetsList",
        "indexes": [{"keys": {"userId": 1}}, {"keys": {"eventId": 1}}],
        "fields": {
            "userId": {"type": "string"},
            "eventId": {"type": "number"},
            "roundId": {"type": "string"},
            "stake": {"type": "number"},
            "selection": {"type": "string"},
            "profitLoss": {"type": "number"},
            "status": {"type": "string"},
            "createdAt": {"type": "date"},
        },
    },
    "casino_rounds": {
        "description": "casino/roundWiseResult",
        "indexes": [{"keys": {"eventId": 1, "roundId": 1}}],
        "fields": {
            "eventId": {"type": "number"},
            "roundId": {"type": "string"},
            "result": {"type": "object"},
            "createdAt": {"type": "date"},
        },
    },
    "reports": {
        "description": "casino/dayWiseCasinoReport, userProfitLoss, plus-minus reports — cached rows",
        "indexes": [{"keys": {"reportType": 1, "userId": 1}}, {"keys": {"marketId": 1}}],
        "fields": {
            "reportType": {"type": "string", "examples": ["dayWiseCasino", "userProfitLoss", "plusMinusMarket"]},
            "userId": {"type": "string"},
            "marketId": {"type": "string"},
            "eventId": {"type": "number"},
            "fromDate": {"type": "string"},
            "toDate": {"type": "string"},
            "userNetProfitLoss": {"type": "number"},
            "clientNetAmount": {"type": "number"},
            "payload": {"type": "object", "note": "full API row store"},
            "createdAt": {"type": "date"},
        },
    },
    "sports_catalog": {
        "description": "decision/completeSportList",
        "indexes": [{"keys": {"sportId": 1}, "unique": True}],
        "fields": {
            "sportId": {"type": "number"},
            "sportName": {"type": "string"},
            "status": {"type": "boolean"},
        },
    },
}


def _load_sample(key: str | None) -> dict | list | None:
    if not key:
        return None
    path = API_DATA_DIR / f"{key}.json"
    if not path.exists():
        alt = API_DATA_DIR / f"{key.replace('_', '')}.json"
        path = alt if alt.exists() else path
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return None


def _response_envelope(sample) -> dict:
    return {
        "message": "string",
        "code": "number (0=success)",
        "error": "boolean",
        "data": "object|array — sample neeche",
        "sample": sample,
    }


def build_admin_api_structure() -> dict:
    endpoints = json.loads(ENDPOINTS_FILE.read_text(encoding="utf-8"))
    apis = []
    modules: dict[str, list] = {}

    for ep in endpoints:
        meta = ADMIN_API_MAP.get(ep)
        if meta:
            module, collection, operation, req_fields, sample_key = meta
        else:
            module, collection, operation, req_fields, sample_key = (
                "Other", "reports", "read", [], None
            )
        sample = _load_sample(sample_key)
        row = {
            "endpoint": ep,
            "url": f"{API_BASE}{ep}",
            "local_url": f"http://localhost:8889/v1/{ep}",
            "method": "POST",
            "module": module,
            "mongodb_collection": collection,
            "operation": operation,
            "request_body": {f: "required" if f in req_fields[:3] else "optional" for f in req_fields} if req_fields else {},
            "request_fields": req_fields,
            "response": _response_envelope(sample),
        }
        apis.append(row)
        modules.setdefault(module, []).append(ep)

    collections_used = sorted({a["mongodb_collection"] for a in apis})
    coll_defs = {c: ADMIN_COLLECTIONS[c] for c in collections_used if c in ADMIN_COLLECTIONS}

    return {
        "panel": "admin.1ex99.in",
        "api_base": API_BASE,
        "local_server": "http://localhost:8889",
        "local_api_prefix": "/v1/",
        "database": "ex99_local",
        "mongo_uri": "mongodb://localhost:27017",
        "total_endpoints": len(apis),
        "modules": {k: {"count": len(v), "endpoints": v} for k, v in sorted(modules.items())},
        "mongodb_collections": coll_defs,
        "apis": apis,
    }


def write_admin_table_files(structure: dict):
    TABLES_DIR.mkdir(parents=True, exist_ok=True)
    apis_by_coll: dict[str, list] = {}
    for api in structure["apis"]:
        apis_by_coll.setdefault(api["mongodb_collection"], []).append(api["endpoint"])

    for coll_name, coll_def in structure["mongodb_collections"].items():
        table = {
            "collection": coll_name,
            "database": structure["database"],
            **coll_def,
            "admin_apis": sorted(apis_by_coll.get(coll_name, [])),
        }
        (TABLES_DIR / f"{coll_name}.json").write_text(
            json.dumps(table, indent=2, default=str), encoding="utf-8"
        )

    manifest = {
        "panel": structure["panel"],
        "database": structure["database"],
        "total_admin_apis": structure["total_endpoints"],
        "collections": list(structure["mongodb_collections"].keys()),
        "table_files": f"mongodb/tables/admin/*.json",
    }
    (TABLES_DIR / "_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def write_admin_api_structure() -> dict:
    structure = build_admin_api_structure()
    OUT_FILE.write_text(json.dumps(structure, indent=2, default=str), encoding="utf-8")
    write_admin_table_files(structure)
    return structure


if __name__ == "__main__":
    s = write_admin_api_structure()
    print(f"Wrote {OUT_FILE} — {s['total_endpoints']} admin APIs")
    print(f"Collections: {', '.join(s['mongodb_collections'].keys())}")
    print(f"Table files: mongodb/tables/admin/ ({len(s['mongodb_collections'])} files)")
