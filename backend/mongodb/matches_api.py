"""Shared sports/matchList — client + admin dono ke liye."""

from __future__ import annotations

import copy
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import cloudscraper

from config import API_BASE_URL, BROWSER_HEADERS, OUTPUT_DIR, ADMIN_OUTPUT_DIR
from mongodb.bet_logic import normalize_match_for_api
from mongodb.db import get_db, ping

ROOT = Path(__file__).resolve().parent.parent
CLIENT_API_DIR = ROOT / OUTPUT_DIR / "api_data"
ADMIN_API_DIR = ROOT / ADMIN_OUTPUT_DIR / "api_data"

LIVE_MATCHES = os.getenv("EX99_LIVE_MATCHES", "1") not in ("0", "false", "False")
SCRAPE_LIVE_ONLY = os.getenv("EX99_SCRAPE_LIVE_ONLY", "1") not in ("0", "false", "False")
IST = timezone(timedelta(hours=5, minutes=30))
_MATCH_LIST_CACHE: dict[str, tuple[float, list[dict]]] = {}
_MATCH_LIST_CACHE_TTL = float(os.getenv("EX99_MATCH_LIST_CACHE_SEC", "15"))
ADMIN_MONGO_ONLY = os.getenv("EX99_ADMIN_MONGO_ONLY", "1") not in ("0", "false", "False")
# Inplay / matchList — live refresh (bets/positions MongoDB par rehte hain)
ADMIN_LIVE_MATCH_LIST = os.getenv("EX99_ADMIN_LIVE_MATCH_LIST", "1") not in ("0", "false", "False")

CACHE_REMOTE_PREFIX = "https://1excache.tresting.com"
CACHE_LOCAL_PREFIX = "/excache"
CACHE_URL_KEYS = ("cacheUrl", "checkOddsUrl", "otherMarketCacheUrl")
SOCKET_REMOTE = "https://1excache.tresting.com/"


def _local_cache_url(url: str) -> str:
    if not url or not isinstance(url, str):
        return url or ""
    return url.replace(CACHE_REMOTE_PREFIX, CACHE_LOCAL_PREFIX)


def _normalize_socket_url(url: str) -> str:
    val = str(url or "").strip()
    if not val or val.rstrip("/") in ("/excache", CACHE_LOCAL_PREFIX):
        return SOCKET_REMOTE
    return val


def _load_json_file(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    data = raw.get("data") if isinstance(raw, dict) else raw
    return data if isinstance(data, list) else []


def _load_matches_from_db_or_files() -> list[dict]:
    if ping():
        rows = list(get_db().matches.find({}, {"_id": 0}))
        if rows:
            return rows
    for path in (CLIENT_API_DIR / "match_list.json", ADMIN_API_DIR / "match_list.json"):
        rows = _load_json_file(path)
        if rows:
            return rows
    return []


def _normalize_match_date(value: str) -> str:
    """Frontend moment.js format: DD-MM-YYYY HH:mm:ss A"""
    if not value:
        return value
    value = str(value).strip()
    m = re.match(
        r"^(\d{2}-\d{2}-\d{4})\s+(\d{2}):(\d{2}):(\d{2})\s+(AM|PM)$",
        value,
        re.I,
    )
    if m:
        hour = int(m.group(2))
        suffix = m.group(5).upper()
        if hour > 12:
            hour = hour - 12
            return f"{m.group(1)} {hour:02d}:{m.group(3)}:{m.group(4)} {suffix}"
        if hour == 12 and suffix == "AM":
            return f"{m.group(1)} 00:{m.group(3)}:{m.group(4)} AM"
        return value
    if re.search(r"\s(AM|PM)$", value, re.I):
        return value
    m = re.match(r"^(\d{2}-\d{2}-\d{4}\s+\d{2}:\d{2}:\d{2})$", value)
    if m:
        hour = int(value.split()[1].split(":")[0])
        suffix = "AM" if hour < 12 else "PM"
        return f"{value} {suffix}"
    return value


_COMPLETED_STATUSES = frozenset({
    "COMPLETED", "CLOSED", "SETTLED", "FINISHED", "CANCELLED", "ABANDONED",
})


def _match_is_declared(m: dict) -> bool:
    if not m:
        return False
    if m.get("isDeclare") in (True, 1, "1", "true", "True"):
        return True
    return str(m.get("status") or "").upper() in _COMPLETED_STATUSES


def _match_is_live(m: dict) -> bool:
    """Match abhi live/inplay hai — completed ya declared nahi."""
    if not m:
        return False
    if _match_is_declared(m):
        return False
    st = str(m.get("status") or "").upper()
    if st == "INPLAY":
        return True
    if m.get("inPlayStatus") is True or m.get("inplayStatus") is True:
        return True
    return _compute_in_play(m.get("matchDate", ""), m.get("status"))


def _compute_in_play(match_date: str, status: Any = None) -> bool:
    if str(status or "").upper() in _COMPLETED_STATUSES:
        return False
    if str(status or "").upper() == "INPLAY":
        return True
    try:
        dt = datetime.strptime(match_date, "%d-%m-%Y %H:%M:%S %p")
        dt = dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return dt <= now + timedelta(hours=24)
    except ValueError:
        return False


def normalize_match_row(row: dict) -> dict:
    m = copy.deepcopy(row)
    m["matchDate"] = _normalize_match_date(m.get("matchDate", ""))
    if isinstance(m.get("sportId"), str) and str(m["sportId"]).isdigit():
        m["sportId"] = int(m["sportId"])
    mmc = m.get("maxMinCoins")
    if isinstance(mmc, str) and mmc.startswith("{"):
        try:
            m["maxMinCoins"] = json.loads(mmc)
        except json.JSONDecodeError:
            fixed = re.sub(r"(\w+):", r'"\1":', mmc)
            try:
                m["maxMinCoins"] = json.loads(fixed)
            except json.JSONDecodeError:
                m["maxMinCoins"] = {}
    in_play = False
    if _match_is_declared(m):
        in_play = False
    elif m.get("inPlayStatus") is not None or m.get("inplayStatus") is not None:
        in_play = bool(m.get("inPlayStatus") if m.get("inPlayStatus") is not None else m.get("inplayStatus"))
    else:
        in_play = _compute_in_play(m.get("matchDate", ""), m.get("status"))
    m["inPlayStatus"] = bool(in_play)
    m["inplayStatus"] = m["inPlayStatus"]
    for key in CACHE_URL_KEYS:
        if m.get(key) and isinstance(m[key], str):
            val = str(m[key])
            if val.startswith(CACHE_REMOTE_PREFIX):
                m[key] = _local_cache_url(val)
            elif val.startswith("https://1excache.tresting.com"):
                m[key] = val.replace("https://1excache.tresting.com", CACHE_LOCAL_PREFIX)
    for key in ("socketUrl", "betfairSocketUrl"):
        if m.get(key):
            m[key] = _normalize_socket_url(m[key])
    try:
        from scorecard_api import rewrite_score_iframe
        m = rewrite_score_iframe(m)
    except Exception:
        pass
    return m


def fetch_live_matches(payload: Optional[dict] = None, auth_token: str = "") -> list[dict]:
    payload = payload or {}
    headers = {**BROWSER_HEADERS, "Content-Type": "application/json"}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"
    try:
        scraper = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "darwin", "desktop": True}
        )
        scraper.trust_env = False
        scraper.proxies = {"http": None, "https": None}
        scraper.get("https://1ex99.in", headers=BROWSER_HEADERS, timeout=15)
        resp = scraper.post(
            f"{API_BASE_URL}sports/matchList",
            json=payload,
            headers=headers,
            timeout=20,
        )
        if resp.status_code != 200:
            return []
        data = resp.json()
        if data.get("dataEncrupt") and data.get("data"):
            from decrypt import decrypt_response
            data = decrypt_response(data)
        rows = data.get("data")
        return rows if isinstance(rows, list) else []
    except Exception as exc:
        print(f"[matches_api] live fetch failed: {exc}")
        return []


def _parse_match_date_ist(match_date: str) -> Optional[datetime]:
    if not match_date:
        return None
    try:
        dt = datetime.strptime(str(match_date).strip(), "%d-%m-%Y %H:%M:%S %p")
        return dt.replace(tzinfo=IST)
    except ValueError:
        return None


def _match_has_started(match_date: str) -> bool:
    """Match ka scheduled time guzar chuka ho (IST)."""
    dt = _parse_match_date_ist(match_date)
    if not dt:
        return False
    return dt <= datetime.now(IST)


def _strict_inplay_row(m: dict) -> bool:
    """Sirf started + upstream INPLAY — future 'INPLAY' schedule mat dikhao."""
    if not m or _match_is_declared(m):
        return False
    st = str(m.get("status") or "").upper()
    if st in _COMPLETED_STATUSES:
        return False
    if m.get("scrapeLive") is True:
        return st in ("", "INPLAY", "OPEN") or m.get("inPlayStatus") is True
    if not _match_has_started(m.get("matchDate", "")):
        return False
    if m.get("inPlayStatus") is True or m.get("inplayStatus") is True:
        return True
    return st == "INPLAY"


def _inplay_matches_from_rows(rows: list[dict]) -> list[dict]:
    """MongoDB / file rows se sirf live/inplay matches."""
    out: list[dict] = []
    for m in rows:
        norm = normalize_match_row(m)
        if _strict_inplay_row(norm):
            out.append(m)
    return out


def _blocked_market_ids() -> set[str]:
    """Admin blockMarket — sirf matches collection (scraped reports/blockMarket nahi)."""
    if not ping():
        return set()
    db = get_db()
    blocked: set[str] = set()
    for doc in db.matches.find(
        {"$or": [{"isBlocked": True}, {"betPerm": False}]},
        {"_id": 0, "marketId": 1},
    ):
        mid = str(doc.get("marketId") or "")
        if mid:
            blocked.add(mid)
    return blocked


def _merge_live_with_local_flags(live_rows: list[dict]) -> list[dict]:
    """Live API rows par admin block flags (MongoDB matches se) merge karo."""
    if not live_rows:
        return []
    if not ping():
        return live_rows
    db = get_db()
    merged: list[dict] = []
    for row in live_rows:
        doc = copy.deepcopy(row)
        mid = str(row.get("marketId") or "")
        if not mid:
            merged.append(doc)
            continue
        local = db.matches.find_one({"marketId": mid}, {"_id": 0, "isBlocked": 1, "betPerm": 1})
        if local:
            if local.get("isBlocked") is True:
                doc["isBlocked"] = True
                doc["betPerm"] = False
            else:
                if local.get("betPerm") is not None:
                    doc["betPerm"] = local.get("betPerm")
                if local.get("isBlocked") is not None:
                    doc["isBlocked"] = local.get("isBlocked")
        merged.append(doc)
    return merged


def is_match_blocked(match: dict, blocked_ids: set[str] | None = None) -> bool:
    """Match user side par show / bet allow nahi hona chahiye."""
    if not match:
        return True
    mid = str(match.get("marketId") or "")
    if blocked_ids and mid and mid in blocked_ids:
        return True
    if match.get("isBlocked") is True:
        return True
    if match.get("betPerm") is False:
        return True
    return False


def get_match_list(
    payload: Optional[dict] = None,
    *,
    prefer_live: bool = LIVE_MATCHES,
    auth_token: str = "",
    for_admin: bool = False,
) -> list[dict]:
    payload = payload or {}
    is_client_inplay_list = (
        not for_admin
        and SCRAPE_LIVE_ONLY
        and not payload.get("marketId")
        and not payload.get("eventId")
    )
    cache_key = ""
    if is_client_inplay_list:
        cache_key = json.dumps(
            {k: payload.get(k) for k in sorted(payload.keys())},
            sort_keys=True,
            default=str,
        )
        hit = _MATCH_LIST_CACHE.get(cache_key)
        if hit and (__import__("time").time() - hit[0]) < _MATCH_LIST_CACHE_TTL:
            return copy.deepcopy(hit[1])

    live_rows: list[dict] = []

    if prefer_live and not is_client_inplay_list:
        fetch_payload = dict(payload)
        if not fetch_payload.get("marketId") and not fetch_payload.get("eventId"):
            fetch_payload.setdefault("status", "INPLAY")
        live_rows = fetch_live_matches(fetch_payload, auth_token)
        if live_rows:
            sync_live_matches_to_db(live_rows)

    db_rows = _load_matches_from_db_or_files()

    # Admin — MongoDB-first: live se sync karo, display hamesha MongoDB se
    if for_admin and ADMIN_MONGO_ONLY:
        if live_rows:
            live_ids = {str(m.get("marketId")) for m in live_rows if m.get("marketId")}
            rows = [m for m in db_rows if str(m.get("marketId")) in live_ids]
            if not rows:
                rows = _merge_live_with_local_flags(live_rows)
        else:
            rows = _inplay_matches_from_rows(db_rows)
    elif prefer_live:
        if live_rows:
            if for_admin:
                live_ids = {str(m.get("marketId")) for m in live_rows if m.get("marketId")}
                rows = [m for m in db_rows if str(m.get("marketId")) in live_ids]
                if not rows:
                    rows = _merge_live_with_local_flags(live_rows)
            else:
                rows = _merge_live_with_local_flags(live_rows)
        else:
            rows = _inplay_matches_from_rows(db_rows)
    else:
        rows = _inplay_matches_from_rows(db_rows)

    matches = [normalize_match_row(m) for m in rows]
    if for_admin:
        matches = [normalize_match_for_api(m) for m in matches]
        blocked_ids = _blocked_market_ids()
        for m in matches:
            mid = str(m.get("marketId") or "")
            if mid in blocked_ids or is_match_blocked(m, blocked_ids):
                m["isBlocked"] = True
                m["betPerm"] = False
            elif m.get("isBlocked") is not True:
                m.setdefault("isBlocked", False)
                m.setdefault("betPerm", True)
        # InPlay Games / dashboard — list API par sirf live matches
        if not payload.get("marketId") and not payload.get("eventId"):
            matches = [m for m in matches if _strict_inplay_row(m)]

    sport_id = payload.get("sportId")
    if sport_id is not None and str(sport_id) != "":
        matches = [m for m in matches if str(m.get("sportId")) == str(sport_id)]

    # Client — admin locked matches user side par hide + sirf live INPLAY
    if not for_admin:
        blocked_ids = _blocked_market_ids()
        matches = [m for m in matches if not is_match_blocked(m, blocked_ids)]
        if not payload.get("marketId") and not payload.get("eventId"):
            if SCRAPE_LIVE_ONLY:
                try:
                    from scorecard_api import (
                        build_client_inplay_from_scrape,
                        match_row_on_scrape_live,
                        _normalize_name,
                    )

                    scrape_sid = sport_id if sport_id not in (None, "") else None
                    scrape_rows = build_client_inplay_from_scrape(scrape_sid)
                    scrape_names = {
                        _normalize_name(str(m.get("matchName") or ""))
                        for m in scrape_rows
                        if m.get("matchName")
                    }
                    scrape_ids = {
                        str(m.get("eventId") or m.get("scrapeGmid") or "")
                        for m in scrape_rows
                        if m.get("eventId") or m.get("scrapeGmid")
                    }
                    if scrape_rows:
                        by_event: dict[str, dict] = {}
                        for row in scrape_rows:
                            norm = normalize_match_row(row)
                            eid = str(norm.get("eventId") or norm.get("scrapeGmid") or "")
                            if eid:
                                by_event[eid] = norm
                        for m in matches:
                            if not _strict_inplay_row(m):
                                continue
                            if scrape_names or scrape_ids:
                                if not match_row_on_scrape_live(m, scrape_names, scrape_ids):
                                    continue
                            eid = str(m.get("eventId") or "")
                            if eid and eid in by_event:
                                by_event[eid] = normalize_match_row({**by_event[eid], **m})
                            elif eid:
                                by_event[eid] = normalize_match_row(m)
                        matches = list(by_event.values())
                        sync_live_matches_to_db(matches)
                    else:
                        matches = []
                except Exception as exc:
                    print(f"[matches_api] scrape live filter: {exc}")
                    matches = [m for m in matches if _strict_inplay_row(m)]
            else:
                matches = [m for m in matches if _strict_inplay_row(m)]

    def _sort_key(m: dict):
        try:
            return datetime.strptime(m.get("matchDate", ""), "%d-%m-%Y %H:%M:%S %p")
        except ValueError:
            return datetime.min.replace(tzinfo=timezone.utc)

    matches.sort(key=_sort_key)
    if is_client_inplay_list and cache_key:
        _MATCH_LIST_CACHE[cache_key] = (__import__("time").time(), copy.deepcopy(matches))
    return matches


def sync_live_matches_to_db(matches: list[dict]) -> None:
    """Live matchList ko MongoDB mein cache karo — baaki admin APIs ke liye."""
    if not matches or not ping():
        return
    db = get_db()
    for row in matches:
        mid = str(row.get("marketId") or "")
        if not mid:
            continue
        doc = copy.deepcopy(row)
        doc.pop("_id", None)
        existing = db.matches.find_one(
            {"marketId": mid},
            {"_id": 0, "betPerm": 1, "isBlocked": 1},
        )
        if existing:
            if existing.get("isBlocked"):
                doc["isBlocked"] = True
                doc["betPerm"] = False
            else:
                if "betPerm" in existing:
                    doc["betPerm"] = existing["betPerm"]
                if "isBlocked" in existing:
                    doc["isBlocked"] = existing["isBlocked"]
        try:
            from scorecard_api import rewrite_score_iframe
            doc = rewrite_score_iframe(doc)
        except Exception:
            pass
        db.matches.update_one({"marketId": mid}, {"$set": doc}, upsert=True)


def post_live_api(
    endpoint: str,
    payload: Optional[dict] = None,
    auth_token: str = "",
) -> Optional[dict]:
    """Live api.ons3.co call — decrypted response dict ya None."""
    payload = payload or {}
    headers = {**BROWSER_HEADERS, "Content-Type": "application/json"}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"
    try:
        scraper = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "darwin", "desktop": True}
        )
        scraper.trust_env = False
        scraper.proxies = {"http": None, "https": None}
        scraper.get("https://1ex99.in", headers=BROWSER_HEADERS, timeout=15)
        resp = scraper.post(
            f"{API_BASE_URL}{endpoint.lstrip('/')}",
            json=payload,
            headers=headers,
            timeout=20,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        if data.get("dataEncrupt") and data.get("data"):
            from decrypt import decrypt_response
            data = decrypt_response(data)
        return data if isinstance(data, dict) else None
    except Exception as exc:
        print(f"[matches_api] {endpoint} failed: {exc}")
        return None


def _iter_scraped_match_rows() -> list[dict]:
    """Admin/client scraped matchList — Mongo partial ho to bhi poori list."""
    rows: list[dict] = []
    seen: set[str] = set()
    for path in (
        ADMIN_API_DIR / "sports_matchList.json",
        ADMIN_API_DIR / "match_list.json",
        CLIENT_API_DIR / "match_list.json",
    ):
        for row in _load_json_file(path):
            mid = str(row.get("marketId") or "")
            if not mid or mid in seen:
                continue
            seen.add(mid)
            rows.append(row)
    return rows


def _match_type_from_name(name: str) -> str:
    text = str(name or "")
    m = re.search(r"\(([^)]+)\)\s*$", text)
    if m:
        return str(m.group(1)).replace("-", "").strip()
    return ""


def _match_date_from_row(row: dict) -> str:
    raw = row.get("matchDate") or row.get("openDate") or ""
    if raw:
        return _normalize_match_date(str(raw))
    epoch = row.get("date")
    if epoch in (None, ""):
        return ""
    try:
        ts = int(epoch)
        if ts > 1_000_000_000_000:
            ts //= 1000
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        return dt.strftime("%d-%m-%Y %I:%M:%S %p")
    except (TypeError, ValueError, OSError):
        return ""


def enrich_match_metadata(row: dict) -> dict:
    """Scrape-style labels — matchName, matchDate, matchType fill karo."""
    doc = copy.deepcopy(row or {})
    name = str(doc.get("matchName") or doc.get("eventName") or doc.get("sportName") or "").strip()
    if not name:
        for sub in doc.get("marketList") or []:
            if isinstance(sub, dict) and sub.get("matchName"):
                name = str(sub["matchName"]).strip()
                break
    if name:
        doc["matchName"] = name
    if not doc.get("matchDate"):
        doc["matchDate"] = _match_date_from_row(doc)
    if not doc.get("matchType"):
        doc["matchType"] = (
            doc.get("matchType")
            or doc.get("sportType")
            or _match_type_from_name(doc.get("matchName", ""))
        )
    if doc.get("eventId") is not None:
        doc["eventId"] = str(doc["eventId"])
    return doc


def _find_match_local(market_id: str, event_id: str = "") -> Optional[dict]:
    if not market_id:
        return None
    market_id = str(market_id)
    event_id = str(event_id or "")
    if ping():
        db = get_db()
        doc = db.matches.find_one({"marketId": market_id}, {"_id": 0})
        if not doc and event_id:
            doc = db.matches.find_one({"eventId": event_id}, {"_id": 0})
        if not doc:
            doc = db.matches.find_one({"marketList.marketId": market_id}, {"_id": 0})
        if doc:
            return enrich_match_metadata(doc)
    for row in _iter_scraped_match_rows():
        if str(row.get("marketId")) == market_id:
            return enrich_match_metadata(row)
        if event_id and str(row.get("eventId")) == event_id:
            return enrich_match_metadata(row)
    return None


def prepare_match_for_admin(match: dict) -> dict:
    """Admin sportByMarketId — teamData JSON string chahiye (array nahi)."""
    row = copy.deepcopy(match)
    row.pop("_id", None)
    for key in ("cacheUrl", "checkOddsUrl", "otherMarketCacheUrl"):
        if row.get(key):
            row[key] = _local_cache_url(str(row[key]))
    td = row.get("teamData")
    if isinstance(td, str):
        pass
    elif isinstance(td, list):
        row["teamData"] = json.dumps(td)
    elif isinstance(td, dict):
        row["teamData"] = json.dumps(list(td.values()))
    else:
        row["teamData"] = "[]"
    if isinstance(row.get("sportId"), str) and str(row["sportId"]).isdigit():
        row["sportId"] = int(row["sportId"])
    return row


def fetch_match_by_market_id(
    market_id: str,
    event_id: str = "",
    auth_token: str = "",
    *,
    prefer_live: Optional[bool] = None,
) -> Optional[dict]:
    payload: dict = {"marketId": str(market_id)}
    if event_id:
        payload["eventId"] = str(event_id)
    use_live = LIVE_MATCHES if prefer_live is None else prefer_live
    if use_live and not ADMIN_MONGO_ONLY:
        live = post_live_api("sports/sportByMarketId", payload, auth_token)
        if live and not live.get("error") and live.get("data"):
            data = live["data"]
            if isinstance(data, dict):
                return prepare_match_for_admin(data)
            if isinstance(data, list) and data:
                return prepare_match_for_admin(data[0])
    local = _find_match_local(market_id, event_id)
    if local:
        return prepare_match_for_admin(local)
    return None
