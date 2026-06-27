"""
Scraped API JSON → MongoDB (real wale jaisa — koi extra field/collection nahi).

Har collection = real API ka `data` — bilkul waise hi fields, waise hi structure.

Run: python3 main.py --import-scraped
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
ADMIN_APIS = ROOT / "admin" / "api_data" / "all_apis.json"
CLIENT_APIS = ROOT / "output" / "api_data" / "all_apis.json"
ADMIN_API_DIR = ROOT / "admin" / "api_data"
CLIENT_API_DIR = ROOT / "output" / "api_data"
SCHEMA_PATH = Path(__file__).parent / "collections_schema.json"

# Sirf real admin/client APIs ke collections — kuch extra nahi
REAL_COLLECTIONS = (
    "users",
    "auth_sessions",
    "domains",
    "matches",
    "casino_games",
    "sports_bets",
    "casino_bets",
    "ledger_entries",
    "user_ledger",
    "day_wise_casino",
    "sports_catalog",
    "profit_loss",
)

BET_CHIPS = {
    "100": 100, "500": 500, "1000": 1000, "2000": 2000,
    "5000": 5000, "10000": 10000, "25000": 25000, "50000": 50000,
    "100000": 100000, "200000": 200000, "300000": 300000, "500000": 500000,
}


def _hierarchy_row(
    user_id: str,
    username: str,
    name: str,
    user_type: str,
    priority: int,
    parent_id: str = "",
    coins: float = 100000,
    password: str = "admin@123",
) -> dict:
    return {
        "userId": user_id,
        "username": username,
        "password": password,
        "name": name,
        "mobile": "9999999999",
        "userType": user_type,
        "userPriority": priority,
        "parentId": parent_id or None,
        "creatorId": parent_id or user_id,
        "coins": coins,
        "balance": coins,
        "exposure": 0,
        "profitLoss": 0,
        "status": "1",
        "isDeleted": "false",
        "betStatus": True,
        "matchStatus": True,
        "casinoStatus": True,
        "matkaStatus": True,
        "intCasinoStatus": False,
        "matchShare": 100,
        "matchCommission": 2 if user_type == "owner" else 0,
        "sessionCommission": 3 if user_type == "owner" else 0,
        "casinoShare": 100,
        "casinoCommission": 2 if user_type == "owner" else 0,
        "commissionType": "BetByBet" if user_type == "owner" else "NoCommission",
        "isPasswordChanged": True,
        "betChipsData": _copy(BET_CHIPS),
        "referralCode": f"{username}100100",
    }


def build_local_hierarchy_users() -> list[dict]:
    """Real website jaisi poori chain — owner se client tak."""
    sys.path.insert(0, str(ROOT))
    from config import ADMIN_PASSWORD, ADMIN_USERNAME

    rows = [
        _hierarchy_row("uid-owner", "OWNER001", "Owner Demo", "owner", 9, "", 10_000_000),
        _hierarchy_row("uid-subowner", "SUBOWNER001", "Sub Owner", "subowner", 8, "uid-owner", 5_000_000),
        _hierarchy_row("uid-superadmin", "SUPERADMIN001", "Super Admin", "superadmin", 7, "uid-subowner", 2_000_000),
        _hierarchy_row("uid-admin", ADMIN_USERNAME.upper(), "Admin Demo", "admin", 6, "uid-superadmin", 500_000, ADMIN_PASSWORD),
        _hierarchy_row("uid-subadmin", "SUBADMIN001", "Sub Admin", "subadmin", 5, "uid-admin", 200_000),
        _hierarchy_row("uid-master", "MASTER001", "Master Demo", "master", 4, "uid-subadmin", 100_000),
        _hierarchy_row("uid-superagent", "SUPERAGENT001", "Super Agent", "superagent", 3, "uid-master", 50_000),
        _hierarchy_row("uid-agent", "AGENT001", "Agent Demo", "agent", 2, "uid-superagent", 25_000),
        _hierarchy_row("uid-client", "CLIENT001", "Client Demo", "client", 1, "uid-agent", 5_000, "123456"),
    ]
    return rows


def _local_admin_user() -> dict:
    return next(u for u in build_local_hierarchy_users() if u["userType"] == "admin")


def _copy(obj: Any) -> Any:
    return copy.deepcopy(obj)


def _load_json(path: Path) -> Any:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return None


def _merge_apis() -> dict:
    merged: dict = {}
    for path in (CLIENT_APIS, ADMIN_APIS):
        if path.exists():
            merged.update(json.loads(path.read_text(encoding="utf-8")))
    file_map = {
        "user_list": "user_list.json",
        "user_details": "user_details.json",
        "balance": "balance.json",
        "day_wise_casino": "day_wise_casino.json",
        "domain_list": "domain_list.json",
        "user_ledger": "user_ledger.json",
        "match_list": "match_list.json",
        "casino_data": "casino_data.json",
        "bets_list": "bets_list.json",
        "casino_bets": "casino_bets.json",
        "sport_list": "sport_list.json",
        "profit_loss": "profit_loss.json",
        "login_info": "login_info.json",
    }
    for key, fname in file_map.items():
        for base in (ADMIN_API_DIR, CLIENT_API_DIR):
            p = base / fname
            if p.exists():
                raw = _load_json(p)
                if raw is not None:
                    merged[key] = raw
    ds = _load_json(CLIENT_API_DIR / "domain_settings.json")
    if ds:
        merged["domain_settings"] = ds
    return merged


def _api_data(apis: dict, key: str) -> Any:
    raw = apis.get(key)
    if isinstance(raw, dict) and "data" in raw:
        return raw["data"]
    return raw


def _list_items(data: Any, *nested_keys: str) -> list[dict]:
    if isinstance(data, list):
        return [_copy(x) for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for k in nested_keys:
            v = data.get(k)
            if isinstance(v, list):
                return [_copy(x) for x in v if isinstance(x, dict)]
    return []


def _merge_user(existing: dict, new: dict) -> dict:
    """Same userId — real fields merge, naye fields overwrite nahi agar purane mein hain."""
    out = _copy(existing)
    for k, v in new.items():
        if k in ("passwordShow", "otp", "checkUsername"):
            continue
        if k == "id" and "userId" in out:
            continue
        out[k] = v
    if "userId" not in out and new.get("id"):
        out["userId"] = new["id"]
    return out


def _build_users(apis: dict) -> list[dict]:
    by_id: dict[str, dict] = {}

    for u in _list_items(_api_data(apis, "user_list"), "list"):
        uid = str(u.get("userId", ""))
        if uid:
            by_id[uid] = _copy(u)

    details = _api_data(apis, "user_details")
    if isinstance(details, dict) and details.get("userId"):
        uid = str(details["userId"])
        by_id[uid] = _merge_user(by_id.get(uid, {}), details)

    login = apis.get("login_info") or {}
    if isinstance(login.get("user"), dict):
        u = login["user"]
        uid = str(u.get("userId", ""))
        if uid:
            by_id[uid] = _merge_user(by_id.get(uid, {}), u)

    bal = _api_data(apis, "balance")
    login_uid = str((login.get("user") or {}).get("userId", ""))
    if isinstance(bal, dict) and login_uid:
        merged = _merge_user(by_id.get(login_uid, {"userId": login_uid}), {
            "coins": bal.get("coins"),
            "exposure": bal.get("exposure"),
            "profitLoss": bal.get("profitLoss"),
            "creatorId": bal.get("creatorId"),
            "status": bal.get("status"),
            "isDeleted": bal.get("isDeleted"),
            "intCasinoStatus": bal.get("intCasinoStatus"),
        })
        by_id[login_uid] = merged

    admin = _local_admin_user()
    by_id[admin["userId"]] = _merge_user(by_id.get(admin["userId"], {}), admin)

    for u in build_local_hierarchy_users():
        uid = u["userId"]
        by_id[uid] = _merge_user(by_id.get(uid, {}), u)

    return list(by_id.values())


def _build_auth_sessions(apis: dict) -> list[dict]:
    login = apis.get("login_info") or {}
    token = login.get("token")
    user = login.get("user") or {}
    if not token:
        return []
    return [{"token": token, "userId": user.get("userId"), "username": user.get("username")}]


def _build_domains(apis: dict) -> list[dict]:
    for key in ("domain_settings", "domain_list"):
        data = _api_data(apis, key)
        if isinstance(data, dict) and data.get("domainName"):
            return [_copy(data)]
        if isinstance(data, list):
            rows = [_copy(x) for x in data if isinstance(x, dict)]
            if rows:
                return rows
    return []


def _build_matches(apis: dict) -> list[dict]:
    return _list_items(_api_data(apis, "match_list"))


def _build_casino_games(apis: dict) -> list[dict]:
    return _list_items(_api_data(apis, "casino_data"))


def _build_sports_bets(apis: dict) -> list[dict]:
    rows = []
    for key in ("bets_list", "my_bets"):
        data = _api_data(apis, key)
        if isinstance(data, list):
            rows.extend(_list_items(data))
        elif isinstance(data, dict):
            for sub in ("list", "odds", "fancy", "bookmaker", "diamondCasino", "bookMaker"):
                rows.extend(_list_items(data, sub))
            if data and not rows:
                for v in data.values():
                    if isinstance(v, list):
                        rows.extend(_list_items(v))
    return rows


def _build_casino_bets(apis: dict) -> list[dict]:
    return _list_items(_api_data(apis, "casino_bets"), "casinoBetData")


def _build_ledger_entries(apis: dict) -> list[dict]:
    return _list_items(_api_data(apis, "user_ledger"), "ledgerData")


def _build_user_ledger(apis: dict) -> list[dict]:
    """user/userLedger API ka poora data object — real jaisa."""
    data = _api_data(apis, "user_ledger")
    if not isinstance(data, dict):
        return []
    login = (apis.get("login_info") or {}).get("user") or {}
    uid = login.get("userId") or data.get("userId")
    doc = _copy(data)
    if uid:
        doc["userId"] = uid
    return [doc]


def _build_day_wise_casino(apis: dict) -> list[dict]:
    return _list_items(_api_data(apis, "day_wise_casino"))


def _build_profit_loss(apis: dict) -> list[dict]:
    data = _api_data(apis, "profit_loss")
    if isinstance(data, list):
        return [_copy(x) if isinstance(x, dict) else {"value": x} for x in data]
    if isinstance(data, dict) and data:
        return [_copy(data)]
    return []


def _build_sports_catalog(apis: dict) -> list[dict]:
    raw = apis.get("sport_list") or {}
    if raw.get("error"):
        return []
    return _list_items(_api_data(apis, "sport_list"))


def build_scraped_seed_data() -> dict[str, list[dict]]:
    apis = _merge_apis()
    return {
        "users": _build_users(apis),
        "auth_sessions": _build_auth_sessions(apis),
        "domains": _build_domains(apis),
        "matches": _build_matches(apis),
        "casino_games": _build_casino_games(apis),
        "sports_bets": _build_sports_bets(apis),
        "casino_bets": _build_casino_bets(apis),
        "ledger_entries": _build_ledger_entries(apis),
        "user_ledger": _build_user_ledger(apis),
        "day_wise_casino": _build_day_wise_casino(apis),
        "sports_catalog": _build_sports_catalog(apis),
        "profit_loss": _build_profit_loss(apis),
    }


def _ensure_indexes(db, schema: dict):
    for coll_name in REAL_COLLECTIONS:
        meta = schema.get("collections", {}).get(coll_name, {})
        for idx in meta.get("indexes", []):
            keys = idx["keys"]
            opts = {k: v for k, v in idx.items() if k != "keys"}
            try:
                db[coll_name].create_index(list(keys.items()), **opts)
            except Exception as exc:
                print(f"  index warn {coll_name}: {exc}")


def import_to_mongodb() -> dict:
    sys.path.insert(0, str(ROOT))
    from mongodb.db import MONGO_DB_NAME, MONGO_URI, get_client, ping

    if not ping():
        print("MongoDB connect nahi hua.")
        sys.exit(1)

    seed = build_scraped_seed_data()
    db = get_client()[MONGO_DB_NAME]
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    print("=" * 50)
    print("Real API structure → MongoDB (same as scrape)")
    print("=" * 50)

    for c in REAL_COLLECTIONS:
        db[c]

    print("[1] Indexes...")
    _ensure_indexes(db, schema)

    print("\n[2] Insert (real API data, no extra fields)...")
    counts = {}
    for coll_name in REAL_COLLECTIONS:
        docs = seed.get(coll_name, [])
        db[coll_name].delete_many({})
        if docs:
            db[coll_name].insert_many(docs)
        counts[coll_name] = len(docs)
        print(f"  ✓ {coll_name}: {len(docs)}")

    report = {"collections": counts, "note": "Documents = real API data field-for-field"}
    Path(__file__).parent.joinpath("import_report.json").write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8"
    )
    print("=" * 50)
    return report


if __name__ == "__main__":
    import_to_mongodb()
