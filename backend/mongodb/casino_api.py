"""Casino games — user client site aur staff panel shared helpers."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from mongodb.admin_compute import _event_id_lookup, normalize_casino_game
from mongodb.db import get_db

_ROOT = Path(__file__).resolve().parents[1]
_CLIENT_CASINO_JSON = _ROOT / "output" / "api_data" / "casino_data.json"

# User /app/casino page (Casino-BsEAZsb4.js) — hardcoded routes.
_USER_CASINO_PAGE_EVENT_IDS = frozenset({
    3030, 3033, 3031, 3032, 3034, 3035, 3043, 3048, 3054, 3055, 3056, 3059, 3060, 3065,
})

# User /app/virtual-casino page (VirtualCasino-DN1vVk0T.js) — hardcoded grid.
_USER_VIRTUAL_CASINO_PAGE_GAMES: tuple[dict[str, Any], ...] = (
    {
        "gameId": 201206,
        "eventId": 303031,
        "gameName": "Aviator",
        "gameCode": "aviator",
        "providerName": "Spribe",
        "subProviderName": "Aviator",
        "category": "Crash Games",
        "urlThumb": "/images/aviator.jpeg",
        "path": "/app/aviator",
        "isLobby": False,
        "isTrending": True,
        "isPopular": True,
        "isLive": False,
        "status": "ACTIVE",
        "betStatus": True,
    },
    {
        "gameId": 501001,
        "gameName": "DUS KA DAM",
        "gameCode": "duskadum",
        "providerName": "Virtual Casino",
        "subProviderName": "DUS KA DAM",
        "category": "Virtual Casino",
        "urlThumb": "/images/duskadum.jpg",
        "isUpcoming": True,
        "isLobby": False,
        "isTrending": False,
        "isPopular": False,
        "isLive": False,
        "status": "ACTIVE",
        "betStatus": False,
    },
    {
        "gameId": 501002,
        "gameName": "TEEN PATTI",
        "gameCode": "teenpatti",
        "providerName": "Virtual Casino",
        "subProviderName": "TEEN PATTI",
        "category": "Virtual Casino",
        "urlThumb": "/images/tp1.jpg",
        "path": "/app/ledger",
        "isLobby": False,
        "isTrending": False,
        "isPopular": False,
        "isLive": False,
        "status": "ACTIVE",
        "betStatus": False,
    },
    {
        "gameId": 501003,
        "gameName": "ANDAR BAHAR",
        "gameCode": "andarbahar",
        "providerName": "Virtual Casino",
        "subProviderName": "ANDAR BAHAR",
        "category": "Virtual Casino",
        "urlThumb": "/images/andar-bahar.webp",
        "path": "/app/client-Statement",
        "isLobby": False,
        "isTrending": False,
        "isPopular": False,
        "isLive": False,
        "status": "ACTIVE",
        "betStatus": False,
    },
)

_INT_CASINO_MUTABLE_KEYS = frozenset({
    "gameName", "name", "gameCode", "providerName", "subProviderName", "category",
    "urlThumb", "path", "isUpcoming", "isLobby", "isTrending", "isPopular", "isLive",
    "status", "betStatus", "cateogeoryId", "subCateogeoryId", "eventId",
})

_CASINO_MUTABLE_KEYS = frozenset({
    "name", "shortName", "socketURL", "cacheURL", "betStatus", "cashinoStatus",
    "isVirtual", "videoUrlType", "fetchData", "videoUrl1", "videoUrl2", "videoUrl3",
    "maxStake", "minStake", "setting", "image", "isDisable", "casinoStatus",
})


def _load_client_casino_json() -> list[dict]:
    if not _CLIENT_CASINO_JSON.is_file():
        return []
    try:
        raw = json.loads(_CLIENT_CASINO_JSON.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    data = raw.get("data") if isinstance(raw, dict) else raw
    return data if isinstance(data, list) else []


def all_casino_games() -> list[dict]:
    """All casino games — MongoDB first (staff edits), scraped JSON fallback."""
    db = get_db()
    rows = list(db.casino_games.find({}).sort("eventId", 1))
    if rows:
        return [normalize_casino_game(g) for g in rows]
    return [normalize_casino_game(g) for g in _load_client_casino_json()]


def find_casino_game_by_event_id(event_id) -> Optional[dict]:
    eid = str(event_id or "")
    for row in all_casino_games():
        if str(row.get("eventId") or "") == eid:
            return row
    return None


def _is_user_casino_enabled(row: dict) -> bool:
    """User client only shows games with cashinoStatus === true."""
    return row.get("cashinoStatus") is True


def client_visible_casino_games(*, diamond_page_only: bool = False) -> list[dict]:
    """
    Games visible on user client site.

    diamond_page_only=False → all cashinoStatus=true (casino + aviator + ball-by-ball).
    diamond_page_only=True  → /app/casino grid only (hardcoded routes ∩ cashinoStatus).
    """
    rows = [g for g in all_casino_games() if _is_user_casino_enabled(g)]

    if diamond_page_only:
        rows = [r for r in rows if r.get("eventId") in _USER_CASINO_PAGE_EVENT_IDS]

    rows.sort(key=lambda r: int(r.get("eventId") or 0))
    return rows


def staff_diamond_casino_games() -> list[dict]:
    """Staff Diamond Casino list — saari /app/casino games, status off ho tab bhi dikhe."""
    rows = [
        g for g in all_casino_games()
        if g.get("eventId") in _USER_CASINO_PAGE_EVENT_IDS
    ]
    rows.sort(key=lambda r: int(r.get("eventId") or 0))
    return rows


def _find_int_casino_base(game_id) -> Optional[dict]:
    try:
        gid = int(game_id)
    except (TypeError, ValueError):
        return None
    for row in _USER_VIRTUAL_CASINO_PAGE_GAMES:
        if int(row["gameId"]) == gid:
            return dict(row)
    return None


def _sync_aviator_to_casino_games(row: dict) -> None:
    """Aviator bets eventId 303031 — int casino status/icon staff edit user bets par bhi."""
    event_id = row.get("eventId") or 303031
    active = str(row.get("status") or "").upper() == "ACTIVE"
    db = get_db()
    doc = db.casino_games.find_one(_event_id_lookup(event_id))
    if not doc:
        return
    upd = {
        "betStatus": active,
        "cashinoStatus": active,
        "updatedAt": datetime.now(timezone.utc),
    }
    db.casino_games.update_one({"_id": doc["_id"]}, {"$set": upd})


def normalize_int_casino_game(doc: dict) -> dict:
    """Staff /app/intCasino — website/getAllCasinoInternationalList row shape."""
    row = dict(doc or {})
    game_id = row.get("gameId")
    if game_id is not None:
        try:
            row["gameId"] = int(game_id)
        except (TypeError, ValueError):
            pass
    name = str(row.get("gameName") or row.get("name") or "Casino")
    row["gameName"] = name
    row["name"] = name
    row.setdefault("gameCode", str(row.get("gameCode") or name).lower().replace(" ", ""))
    row.setdefault("providerName", "Virtual Casino")
    row.setdefault("subProviderName", name)
    row.setdefault("category", "Virtual Casino")
    row.setdefault("urlThumb", "")
    row.setdefault("isLobby", False)
    row.setdefault("isTrending", False)
    row.setdefault("isPopular", False)
    row.setdefault("isLive", False)
    row["betStatus"] = bool(row.get("betStatus", False))
    status = row.get("status")
    if status in ("ACTIVE", "INACTIVE"):
        row["status"] = status
    elif isinstance(status, bool):
        row["status"] = "ACTIVE" if status else "INACTIVE"
    else:
        row["status"] = "ACTIVE" if row["betStatus"] and not bool(row.get("isDisable")) else "INACTIVE"
    return row


def _merge_aviator_from_casino_games(row: dict) -> dict:
    """Aviator bets use eventId 303031 — sync betStatus for staff list."""
    aviator = find_casino_game_by_event_id(row.get("eventId") or 303031)
    if not aviator:
        return row
    merged = {**row}
    bet_on = bool(aviator.get("betStatus", aviator.get("cashinoStatus", True)))
    disabled = bool(aviator.get("isDisable"))
    merged["betStatus"] = bet_on
    merged["status"] = "ACTIVE" if bet_on and not disabled else "INACTIVE"
    if aviator.get("name"):
        merged["gameName"] = "Aviator"
        merged["name"] = "Aviator"
    return merged


def int_casino_game_ids() -> frozenset[int]:
    return frozenset(int(g["gameId"]) for g in _USER_VIRTUAL_CASINO_PAGE_GAMES)


def int_casino_event_ids() -> frozenset[int]:
    """Staff int bet list — virtual-casino games (Aviator eventId 303031 + gameId fallbacks)."""
    ids: set[int] = set()
    for g in _USER_VIRTUAL_CASINO_PAGE_GAMES:
        if g.get("eventId") is not None:
            ids.add(int(g["eventId"]))
        else:
            ids.add(int(g["gameId"]))
    return frozenset(ids)


def staff_int_casino_report_games() -> list[dict]:
    """user/casinoReportByUser?gameType=internationalCasino — bet list game dropdown."""
    rows: list[dict] = []
    for g in staff_int_casino_games():
        rows.append(
            normalize_casino_game(
                {
                    "eventId": g.get("eventId") or g.get("gameId"),
                    "name": g.get("gameName") or g.get("name") or "Casino",
                    "gameId": g.get("gameId"),
                    "gameName": g.get("gameName"),
                    "shortName": g.get("gameCode") or "",
                    "betStatus": g.get("betStatus"),
                    "cashinoStatus": g.get("betStatus"),
                }
            )
        )
    return rows


def is_international_casino_bet(bet: dict) -> bool:
    """True for Aviator / virtual-casino bets — not Diamond Casino table games."""
    if not bet:
        return False
    gt = str(bet.get("gameType") or "").lower()
    if gt == "diamond":
        return False
    eid = bet.get("eventId")
    if eid is not None:
        try:
            if int(eid) in _USER_CASINO_PAGE_EVENT_IDS:
                return False
        except (TypeError, ValueError):
            pass
    if gt in ("aviator", "internationalcasino", "international"):
        return True
    if eid is not None:
        try:
            if int(eid) in int_casino_event_ids():
                return True
        except (TypeError, ValueError):
            pass
    gid = bet.get("gameId")
    if gid is not None:
        try:
            if int(gid) in int_casino_game_ids():
                return True
        except (TypeError, ValueError):
            pass
    return False


def staff_int_casino_games() -> list[dict]:
    """
    Staff Int. Casino list — user /app/virtual-casino grid + Aviator (always).
    Status off ho tab bhi staff ko edit ke liye dikhe.
    """
    db = get_db()
    stored = {
        int(g["gameId"]): g
        for g in db.int_casino_games.find({})
        if g.get("gameId") is not None
    }
    rows: list[dict] = []
    for base in _USER_VIRTUAL_CASINO_PAGE_GAMES:
        game_id = int(base["gameId"])
        doc = stored.get(game_id, base)
        row = normalize_int_casino_game(doc)
        if game_id == 201206 and game_id not in stored:
            row = normalize_int_casino_game(_merge_aviator_from_casino_games(row))
        rows.append(row)
    return rows


def client_virtual_casino_games() -> list[dict]:
    """User /app/virtual-casino — sirf ACTIVE games."""
    return [r for r in staff_int_casino_games() if str(r.get("status") or "").upper() == "ACTIVE"]


def virtual_casino_user_tiles() -> list[dict]:
    """VirtualCasino-DN1vVk0T.js grid tiles — staff int_casino_games se."""
    tiles: list[dict] = []
    for row in client_virtual_casino_games():
        game_id = int(row.get("gameId") or 0)
        base = _find_int_casino_base(game_id) or {}
        title = str(row.get("gameName") or base.get("gameName") or "Casino").upper()
        tile: dict[str, Any] = {
            "title": title,
            "subtitle": base.get("subtitle") or ("AVIATOR" if game_id == 201206 else "CASINO"),
            "icon": row.get("urlThumb") or base.get("urlThumb") or "",
            "description": "",
        }
        if base.get("path"):
            tile["path"] = base["path"]
        if base.get("isUpcoming"):
            tile["isUpcoming"] = True
        tiles.append(tile)
    return tiles


def update_international_casino(payload: dict) -> dict:
    """website/updateInternationalCasinoByOperating — staff edit → MongoDB → user site."""
    payload = payload or {}
    game_id = payload.get("gameId")
    if game_id is None:
        return {"message": "gameId required", "code": 1, "error": True, "data": {}}

    base = _find_int_casino_base(game_id)
    if not base:
        return {"message": "Casino game not found", "code": 1, "error": True, "data": {}}

    gid = int(base["gameId"])
    db = get_db()
    existing = db.int_casino_games.find_one({"gameId": gid}) or {}

    upd: dict[str, Any] = {}
    for key, val in payload.items():
        if key in ("gameId", "_id", "id"):
            continue
        if key in _INT_CASINO_MUTABLE_KEYS:
            upd[key] = val

    for bool_key in ("isLobby", "isPopular", "isLive", "isTrending"):
        if bool_key in payload:
            upd[bool_key] = bool(payload[bool_key])

    if "status" in payload:
        status = payload["status"]
        if status in ("ACTIVE", "INACTIVE"):
            upd["status"] = status
        else:
            upd["status"] = "ACTIVE" if status else "INACTIVE"
        upd["betStatus"] = upd["status"] == "ACTIVE"

    if not upd:
        return {"message": "No fields to update", "code": 1, "error": True, "data": {}}

    merged = {**base, **existing, **upd, "gameId": gid}
    merged["updatedAt"] = datetime.now(timezone.utc)
    db.int_casino_games.update_one({"gameId": gid}, {"$set": merged}, upsert=True)

    if gid == 201206:
        _sync_aviator_to_casino_games(merged)

    refreshed = next((r for r in staff_int_casino_games() if int(r.get("gameId") or 0) == gid), merged)
    row = normalize_int_casino_game(refreshed)
    return {
        "message": "Casino updated successfully",
        "code": 0,
        "error": False,
        "data": row,
    }


def client_visible_casino_event_ids(*, diamond_page_only: bool = False) -> frozenset:
    return frozenset(int(r.get("eventId") or 0) for r in client_visible_casino_games(diamond_page_only=diamond_page_only))


def find_client_casino_game(event_id, *, diamond_page_only: bool = False) -> Optional[dict]:
    eid = str(event_id or "")
    for row in client_visible_casino_games(diamond_page_only=diamond_page_only):
        if str(row.get("eventId") or "") == eid:
            return row
    return None


def update_diamond_casino(payload: dict) -> dict:
    """casino/updateDiamondCasino — staff edit → MongoDB → user site."""
    payload = payload or {}
    event_id = payload.get("eventId")
    if event_id is None:
        return {"message": "eventId required", "code": 1, "error": True, "data": {}}

    db = get_db()
    doc = db.casino_games.find_one(_event_id_lookup(event_id))
    if not doc:
        return {"message": "Casino game not found", "code": 1, "error": True, "data": {}}

    upd: dict[str, Any] = {}
    sel = payload.get("selectionIdArray")
    if isinstance(sel, dict) and sel:
        setting = doc.get("setting") if isinstance(doc.get("setting"), dict) else {}
        setting = {**setting, "selectionIdArray": sel}
        upd["setting"] = setting
    else:
        for key, val in payload.items():
            if key in ("eventId", "_id", "id", "selectionIdArray"):
                continue
            if key in _CASINO_MUTABLE_KEYS:
                upd[key] = val

    if not upd:
        return {"message": "No fields to update", "code": 1, "error": True, "data": {}}

    upd["updatedAt"] = datetime.now(timezone.utc)
    db.casino_games.update_one({"_id": doc["_id"]}, {"$set": upd})
    updated = db.casino_games.find_one({"_id": doc["_id"]}) or doc
    row = normalize_casino_game(updated)
    return {"message": "Casino updated successfully", "code": 0, "error": False, "data": row}
