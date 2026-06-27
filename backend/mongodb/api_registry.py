"""Build API -> MongoDB collection mapping table."""

from __future__ import annotations

import json
from pathlib import Path

SCHEMA_PATH = Path(__file__).parent / "collections_schema.json"
API_TABLE_PATH = Path(__file__).parent / "api_table.json"

# (endpoint, method, panel, collection, operation, request_fields)
CLIENT_APIS = [
    ("user/login", "POST", "client", "users", "auth", ["username", "password", "host", "isClient"]),
    ("user/userBalance", "POST", "client", "users", "read", ["userId"]),
    ("user/userDetails", "POST", "client", "users", "read", ["userId"]),
    ("user/userLedger", "POST", "client", "ledger_entries", "read", ["userId", "fromDate", "toDate"]),
    ("user/userStatement", "POST", "client", "statements", "read", ["userId", "startDate", "endDate"]),
    ("user/completeLedgerDetails", "POST", "client", "ledger_entries", "read", ["userId"]),
    ("user/userAccountDetails", "POST", "client", "users", "read", ["userId"]),
    ("user/clientBetListByMarketId", "POST", "client", "sports_bets", "read", ["userId", "marketId"]),
    ("user/casinoLoginUrl", "POST", "client", "users", "read", ["userId"]),
    ("user/userList", "POST", "admin", "users", "read", ["downlineUserType", "parentId"]),
    ("user/userSearch", "POST", "admin", "users", "read", ["searchTerm"]),
    ("user/create", "POST", "admin", "users", "create", ["username", "password", "userType", "parentId"]),
    ("user/userUpdate", "POST", "admin", "users", "update", ["userId"]),
    ("user/updateUserPassword", "POST", "admin", "users", "update", ["userId", "password"]),
    ("user/updateCoins", "POST", "admin", "users", "update", ["userId", "coins"]),
    ("user/updateBulkStatus", "POST", "admin", "users", "update", ["userIds", "status"]),
    ("user/updateRateReffrenece", "POST", "admin", "users", "update", ["userId", "rateReffrence"]),
    ("website/domainSettingByDomainName", "POST", "client", "domains", "read", ["domainName"]),
    ("sports/matchList", "POST", "client", "matches", "read", ["sportId"]),
    ("sports/sportByMarketId", "POST", "client", "matches", "read", ["marketId"]),
    ("sports/betsList", "POST", "client", "sports_bets", "read", ["userId"]),
    ("sports/clientListByMarketId", "POST", "admin", "sports_bets", "read", ["marketId"]),
    ("sports/userPositionByMarketId", "POST", "client", "positions", "read", ["marketId", "userId"]),
    ("sports/oddBetPlaced", "POST", "client", "sports_bets", "create", ["marketId", "selectionId", "stake", "odds", "betType"]),
    ("sports/sessionBetPlaced", "POST", "client", "sports_bets", "create", ["marketId", "selectionId", "stake", "session_id"]),
    ("sports/meterKhadoOddEvenCricketCassinoBetPlace", "POST", "client", "sports_bets", "create", ["marketId", "stake"]),
    ("casino/getDiamondCasinoData", "POST", "client", "casino_games", "read", []),
    ("casino/getDiamondCasinoByEventId", "POST", "client", "casino_games", "read", ["eventId"]),
    ("casino/casinoBetPlace", "POST", "client", "casino_bets", "create", ["eventId", "stake", "roundId"]),
    ("casino/diamondBetsList", "POST", "client", "casino_bets", "read", ["userId"]),
    ("casino/roundWiseResult", "POST", "client", "casino_rounds", "read", ["eventId", "roundId"]),
    ("casino/resultByRoundWise", "POST", "client", "casino_rounds", "read", ["eventId"]),
    ("casino/avaitorGamePlace", "POST", "client", "casino_bets", "create", ["stake", "multiplier"]),
    ("casino/avaitorCashOut", "POST", "client", "casino_bets", "update", ["betId"]),
    ("matka/getMatkaList", "POST", "client", "matka_events", "read", []),
    ("matka/getMatkaByMatkaEventId", "POST", "client", "matka_events", "read", ["matkaEventId"]),
    ("matka/matkaPlaceBet", "POST", "client", "matka_bets", "create", ["matkaEventId", "number", "stake"]),
    ("matka/matkaBetList", "POST", "client", "matka_bets", "read", ["userId"]),
    ("matka/matkaReportByUser", "POST", "client", "matka_bets", "read", ["userId"]),
    ("reports/casinoTransactionReport", "POST", "client", "reports", "read", ["userId"]),
    ("reports/getOddsPositionForAllexch", "POST", "client", "reports", "read", ["marketId"]),
    ("bluexchReports/clientPlusMinus", "POST", "client", "reports", "read", ["userId"]),
    ("bpexch/loadBalance", "POST", "client", "bpexch_accounts", "read", ["userId"]),
    ("bpexch/bpexchAccountStatement", "POST", "client", "bpexch_accounts", "read", ["userId"]),
    ("crick365/agentProfitLossCrick", "POST", "admin", "reports", "read", ["userId"]),
    ("halkabhari/inplayOddsPositionHalkaBhari", "POST", "client", "positions", "read", ["marketId"]),
]

ADMIN_EXTRA_APIS = [
    ("user/userActivity", "POST", "admin", "user_activities", "read", ["userId"]),
    ("user/userLoginActivity", "POST", "admin", "user_activities", "read", ["userId"]),
    ("user/lenaDena", "POST", "admin", "ledger_entries", "create", ["fromUserId", "toUserId", "amount"]),
    ("user/domainList", "POST", "admin", "domains", "read", []),
    ("user/getUserShareData", "POST", "admin", "users", "read", ["userId"]),
    ("user/ledgerCreditDebit", "POST", "admin", "ledger_entries", "create", ["userId", "amount", "type"]),
    ("user/deleteUserLedger", "POST", "admin", "ledger_entries", "delete", ["ledgerId"]),
    ("sports/getOddsPosition", "POST", "admin", "positions", "read", ["marketId"]),
    ("sports/getSessionPositionBySelectionId", "POST", "admin", "positions", "read", ["selectionId"]),
    ("casino/dayWiseCasinoReport", "POST", "admin", "reports", "read", ["fromDate", "toDate"]),
    ("casino/diamondCasinoReportByUser", "POST", "admin", "reports", "read", ["userId"]),
    ("casino/getPlusMinusCasinoDetail", "POST", "admin", "reports", "read", ["userId"]),
    ("casino/getProfitLossPos", "POST", "admin", "reports", "read", ["userId"]),
    ("casino/realTimeDataPosDataDiamondCasino", "POST", "admin", "reports", "read", ["eventId"]),
    ("decision/completeSportList", "POST", "admin", "sports_catalog", "read", []),
    ("decision/getPlusMinusByMarketId", "POST", "admin", "reports", "read", ["marketId"]),
    ("matka/dayWiseMatkaReport", "POST", "admin", "reports", "read", ["fromDate", "toDate"]),
    ("matka/getProfitLossPosMatka", "POST", "admin", "reports", "read", ["userId"]),
    ("reports/userProfitLoss", "POST", "admin", "reports", "read", ["userId"]),
    ("reports/blockMarket", "POST", "admin", "matches", "update", ["marketId"]),
    ("reports/getPlusMinusByMarketIdByUserWise", "POST", "admin", "reports", "read", ["marketId"]),
    ("logout", "POST", "admin", "auth_sessions", "delete", []),
]

CENTER_PANEL_PREFIX = "centerPanel/"


def _center_panel_apis() -> list[tuple]:
    ep_file = Path(__file__).resolve().parent.parent / "centerpanel" / "api_endpoints.json"
    if not ep_file.exists():
        return []
    endpoints = json.loads(ep_file.read_text(encoding="utf-8"))
    mapping = {
        "userLogin": ("users", "auth"),
        "userList": ("users", "read"),
        "userCreate": ("users", "create"),
        "createCustomer": ("users", "create"),
        "updateUser": ("users", "update"),
        "getProjectList": ("center_projects", "read"),
        "createUpdateProject": ("center_projects", "create"),
        "getFancyCategoryList": ("center_fancy_categories", "read"),
        "createFancyCategory": ("center_fancy_categories", "create"),
        "getDomainIpList": ("center_domain_ips", "read"),
        "assignDomainIpToUser": ("center_domain_ips", "update"),
        "getDecisionLogs": ("decision_logs", "read"),
        "getSportsMatchList": ("matches", "read"),
        "diamondMatchList": ("matches", "read"),
        "getAllEvents": ("matches", "read"),
        "getAllCustomSeries": ("center_custom_series", "read"),
        "createCustomSeries": ("center_custom_series", "create"),
        "getRacingEvents": ("center_racing_events", "read"),
        "saveRacingEventsList": ("center_racing_events", "create"),
        "getManualFancyList": ("center_manual_fancy", "read"),
        "saveManualFancy": ("center_manual_fancy", "create"),
        "getManualBookmakerList": ("center_manual_bookmaker", "read"),
        "saveManualBookmaker": ("center_manual_bookmaker", "create"),
        "getSquadTemplates": ("center_squad_templates", "read"),
        "saveSquadTemplate": ("center_squad_templates", "create"),
        "getBetfairResults": ("center_betfair_results", "read"),
        "getMasterSettingData": ("center_master_settings", "read"),
        "masterSettingUpdate": ("center_master_settings", "update"),
    }
    rows = []
    for ep in endpoints:
        if ep == "logout":
            rows.append(("logout", "POST", "centerpanel", "auth_sessions", "delete", []))
            continue
        if not ep.startswith(CENTER_PANEL_PREFIX):
            rows.append((ep, "POST", "centerpanel", "reports", "read", []))
            continue
        action = ep.split("/", 1)[1]
        coll, op = mapping.get(action, ("reports", "read"))
        rows.append((ep, "POST", "centerpanel", coll, op, []))
    return rows


def build_api_table() -> dict:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    apis = []
    seen = set()

    for row in CLIENT_APIS + ADMIN_EXTRA_APIS + _center_panel_apis():
        if row[0] in seen:
            continue
        seen.add(row[0])
        endpoint, method, panel, collection, operation, req = row
        apis.append({
            "endpoint": endpoint,
            "method": method,
            "panel": panel,
            "collection": collection,
            "operation": operation,
            "request_fields": req,
            "response_source": collection if operation != "auth" else "users+auth_sessions",
        })

    return {
        "database": schema["database"] if "database" in schema else "ex99_local",
        "mongo_uri": "mongodb://localhost:27017",
        "total_apis": len(apis),
        "collections_used": sorted({a["collection"] for a in apis}),
        "apis": sorted(apis, key=lambda x: x["endpoint"]),
    }


def write_api_table():
    table = build_api_table()
    API_TABLE_PATH.write_text(json.dumps(table, indent=2), encoding="utf-8")
    return table


if __name__ == "__main__":
    t = write_api_table()
    print(f"Wrote {API_TABLE_PATH} — {t['total_apis']} APIs")
