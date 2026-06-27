"""Scorecard — scrape2 local JSON + live refresh when stale."""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Optional

_ROOT = Path(__file__).resolve().parent
SCRAPE2_SPORT = Path(os.environ.get("SCRAPE2_SPORT_DIR", _ROOT / "scrape2" / "output" / "sport"))
SCRAPE2_SCORECARD = SCRAPE2_SPORT / "scorecard"
SCORECARD_LIVE_MAX_AGE = float(os.getenv("SCORECARD_LIVE_MAX_AGE", "1"))
SCORECARD_UI_VERSION = os.getenv("EX99_SCORECARD_UI_VER", "ex99sc11")
# Browser polls ~400ms — background live hub pushes updates; HTTP live fetch optional.
SCORECARD_HTTP_LIVE = os.getenv("SCORECARD_HTTP_LIVE", "0") not in ("0", "false", "False", "off")

_gmid_map: dict[str, tuple[float, Optional[str]]] = {}
_live_hl_cache: dict[str, tuple[float, list[dict]]] = {}
_mem_sc_cache: dict[str, tuple[float, dict]] = {}
_gmid_lock = __import__("threading").Lock()
_scrape2_api_lock = __import__("threading").Lock()
_fetch_locks: dict[str, __import__("threading").Lock] = {}
_fetch_locks_guard = __import__("threading").Lock()
_LIVE_HL_TTL = 60
_NAME_SUFFIX_RE = re.compile(
    r"\b(t20|t10|t\-20|t\-10|odi|test|hundred|match|game)\b",
    re.I,
)


def _fetch_lock_for(key: str):
    with _fetch_locks_guard:
        lock = _fetch_locks.get(key)
        if lock is None:
            lock = __import__("threading").Lock()
            _fetch_locks[key] = lock
        return lock


def ex99_scorecard_iframe_url(event_id: str) -> str:
    """1ex99.in jaisa live scorecard — score.tresting.com socket iframe."""
    return f"https://score.tresting.com/socket-iframe-21/crickexpo/{event_id}"


def local_scorecard_iframe_url(event_id: str, sport_id: Any = "4") -> str:
    """Optional local scrape scorecard.html (EX99_USE_LOCAL_SCORECARD=1)."""
    eid = str(sport_id or "4")
    if eid not in ("1", "2", "4"):
        eid = "4"
    return f"/scorecard.html?gmid={event_id}&eid={eid}&v={SCORECARD_UI_VERSION}"


def set_live_scorecard_cache(eid: str, gmid: str, sc: dict) -> None:
    _mem_sc_cache[f"{eid}:{gmid}"] = (time.time(), sc)


def get_live_scorecard_cache(eid: str, gmid: str, max_age: float = 5.0) -> Optional[dict]:
    hit = _mem_sc_cache.get(f"{eid}:{gmid}")
    if hit and time.time() - hit[0] <= max_age:
        return hit[1]
    return None


def rewrite_score_iframe(row: dict) -> dict:
    event_id = str(row.get("eventId") or "")
    if not event_id:
        return row
    if not (row.get("isScore") or row.get("scoreIframe")):
        return row
    use_local = os.getenv("EX99_USE_LOCAL_SCORECARD", "0") in ("1", "true", "True", "on")
    existing = str(row.get("scoreIframe") or "")
    if use_local:
        row["scoreIframe"] = local_scorecard_iframe_url(event_id, row.get("sportId"))
    elif "score.tresting.com" in existing or "akamaized" in existing:
        row["scoreIframe"] = existing
    else:
        row["scoreIframe"] = ex99_scorecard_iframe_url(event_id)
    row["isScore"] = True
    return row


def _normalize_name(name: str) -> str:
    s = (name or "").lower()
    s = _NAME_SUFFIX_RE.sub("", s)
    return re.sub(r"[^a-z0-9]", "", s)


def _parse_scrape_stime(stime: str) -> str:
    if not stime:
        return ""
    from datetime import datetime

    for fmt in ("%m/%d/%Y %I:%M:%S %p", "%d-%m-%Y %H:%M:%S %p", "%d-%m-%Y %I:%M:%S %p"):
        try:
            dt = datetime.strptime(str(stime).strip(), fmt)
            return dt.strftime("%d-%m-%Y %I:%M:%S %p")
        except ValueError:
            continue
    return ""


def iter_scrape_live_highlights(sport_id: Any = None) -> list[dict]:
    """Scrape API — sirf iplay=true (live fetch, stale local file nahi)."""
    sport_to_etid = {"4": "4", "1": "1", "2": "2", 4: "4", 1: "1", 2: "2"}
    if sport_id is not None and str(sport_id) != "":
        etids = [str(sport_to_etid.get(sport_id, sport_to_etid.get(str(sport_id), str(sport_id))))]
    else:
        etids = ["4", "1", "2"]

    out: list[dict] = []
    seen: set[str] = set()
    _fetch_live_highlights_batch(etids)
    for etid in etids:
        for row in _fetch_live_highlights(etid):
            if not isinstance(row, dict) or not row.get("iplay"):
                continue
            gmid = str(row.get("gmid") or "")
            if not gmid or gmid in seen:
                continue
            seen.add(gmid)
            out.append(row)
    return out


def match_row_from_highlight(row: dict) -> dict:
    """Scrape highlight row → client matchList format."""
    gmid = str(row.get("gmid") or "")
    mid_raw = row.get("mid")
    market_id = str(mid_raw) if mid_raw not in (None, "") else gmid
    if market_id.isdigit() and not market_id.startswith("1."):
        market_id = f"1.{market_id}"
    ename = str(row.get("ename") or "").strip()
    etid = row.get("etid", 4)
    match_date = _parse_scrape_stime(str(row.get("stime") or ""))
    mtype = ""
    mt = re.search(r"\(([^)]+)\)\s*$", ename)
    if mt:
        mtype = str(mt.group(1)).replace("-", "").strip()
    team_data: list[dict] = []
    for sec in row.get("section") or []:
        if isinstance(sec, dict) and sec.get("nat"):
            team_data.append({
                "runnerName": str(sec.get("nat")),
                "selectionId": sec.get("sid"),
            })
    doc = {
        "marketId": market_id,
        "eventId": gmid,
        "matchName": ename,
        "eventName": ename,
        "sportId": int(etid) if str(etid).isdigit() else etid,
        "seriesName": str(row.get("cname") or ""),
        "matchDate": match_date,
        "status": "INPLAY",
        "inPlayStatus": True,
        "inplayStatus": True,
        "betPerm": True,
        "isBlocked": False,
        "isTv": bool(row.get("tv")),
        "isScore": True,
        "isMatchOdds": True,
        "isFancy": bool(row.get("f")),
        "isBookmaker": bool(row.get("bm")),
        "matchType": mtype,
        "teamData": team_data,
        "scrapeLive": True,
        "scrapeGmid": gmid,
    }
    return rewrite_score_iframe(doc)


def build_client_inplay_from_scrape(sport_id: Any = None) -> list[dict]:
    return [match_row_from_highlight(r) for r in iter_scrape_live_highlights(sport_id)]


def find_scrape_highlight_match(market_id: str = "", event_id: str = "") -> Optional[dict]:
    market_id = str(market_id or "")
    event_id = str(event_id or "")
    mid_tail = market_id[2:] if market_id.startswith("1.") else market_id
    for row in iter_scrape_live_highlights(None):
        gmid = str(row.get("gmid") or "")
        mid = str(row.get("mid") or "")
        if event_id and event_id == gmid:
            return match_row_from_highlight(row)
        if market_id and (market_id == mid or mid_tail == mid or market_id == gmid):
            return match_row_from_highlight(row)
    return None


def _gmid_from_highlight_rows(norm: str, rows: list[dict]) -> tuple[Optional[str], Optional[str]]:
    """Match name → scrape2 gmid (+ optional oldgmid)."""
    if not norm:
        return None, None
    fuzzy_gmid: Optional[str] = None
    for row in rows:
        gmid = row.get("gmid")
        if gmid is None:
            continue
        en = _normalize_name(str(row.get("ename") or ""))
        if en == norm:
            gmid_s = str(gmid)
            return gmid_s, str(row.get("oldgmid") or gmid_s)
        if norm and en and (norm in en or en in norm):
            fuzzy_gmid = str(gmid)
    if fuzzy_gmid:
        return fuzzy_gmid, fuzzy_gmid
    return None, None


def collect_scrape_live_match_keys(sport_id: Any = None) -> tuple[set[str], set[str]]:
    """
    Scrape website highlights — sirf iplay=true rows.
    Returns (normalized_names, event_or_gmid_ids).
    """
    sport_to_etid = {"4": "4", "1": "1", "2": "2", 4: "4", 1: "1", 2: "2"}
    if sport_id is not None and str(sport_id) != "":
        etids = [str(sport_to_etid.get(sport_id, sport_to_etid.get(str(sport_id), str(sport_id))))]
    else:
        etids = ["4", "1", "2"]

    names: set[str] = set()
    ids: set[str] = set()
    for etid in etids:
        seen: set[str] = set()
        for row in iter_scrape_live_highlights(etid):
            gmid = row.get("gmid")
            if gmid is None:
                continue
            dedupe = f"{etid}:{gmid}"
            if dedupe in seen:
                continue
            seen.add(dedupe)
            gmid_s = str(gmid)
            ids.add(gmid_s)
            mf = SCRAPE2_SPORT / "matches" / str(etid) / f"{gmid_s}.json"
            info = _match_info(_read_json(mf) or {}) if mf.is_file() else {}
            old = str(info.get("oldgmid") or gmid_s)
            ids.add(old)
            en = _normalize_name(str(row.get("ename") or info.get("ename") or ""))
            if en:
                names.add(en)
    return names, ids


def match_row_on_scrape_live(row: dict, names: set[str], ids: set[str]) -> bool:
    """1ex match row scrape live list mein hai?"""
    if not names and not ids:
        return False
    eid = str(row.get("eventId") or "")
    if eid and eid in ids:
        return True
    mid = str(row.get("marketId") or "")
    if mid and mid in ids:
        return True
    name = _normalize_name(str(row.get("matchName") or row.get("eventName") or ""))
    if not name:
        return False
    if name in names:
        return True
    return any(name in n or n in name for n in names)


def _with_scrape2_cwd(fn):
    scrape2_dir = _ROOT / "scrape2"
    old = os.getcwd()
    try:
        os.chdir(scrape2_dir)
        return fn()
    finally:
        os.chdir(old)


def _fetch_live_highlights_batch(etids: list[str]) -> None:
    """Ek login — saare sport highlights cache karo."""
    now = time.time()
    need: list[str] = []
    for eid in etids:
        hit = _live_hl_cache.get(str(eid))
        if not hit or now - hit[0] >= _LIVE_HL_TTL:
            need.append(str(eid))
    if not need:
        return

    def _pull_batch() -> dict[str, list[dict]]:
        scrape2_dir = _ROOT / "scrape2"
        if str(scrape2_dir) not in sys.path:
            sys.path.insert(0, str(scrape2_dir))
        from cf_session import api_login, fetch_sport_api
        from decrypt_cryptojs import maybe_decrypt
        from scrape_vcasino import DEFAULT_PASSPHRASE
        from sports import SPORT_APIS

        user = os.environ.get("LOGIN_USER", "Demo9304")
        pwd = os.environ.get("LOGIN_PASS", "Demo1234")
        out: dict[str, list[dict]] = {eid: [] for eid in need}
        try:
            session = api_login(user, pwd)
        except Exception as exc:
            print(f"[scorecard] api_login failed: {exc}")
            for eid in need:
                out[eid] = [
                    r for r in _scrape2_highlights(eid)
                    if isinstance(r, dict) and r.get("iplay")
                ]
            return out
        for eid in need:
            try:
                raw = fetch_sport_api(session, SPORT_APIS["highlights"], {"etid": int(eid)})
                data = maybe_decrypt(raw, DEFAULT_PASSPHRASE)
                out[eid] = list(((data or {}).get("data") or {}).get("t1") or [])
            except Exception as exc:
                print(f"[scorecard] live highlights eid={eid}: {exc}")
                out[eid] = [
                    r for r in _scrape2_highlights(eid)
                    if isinstance(r, dict) and r.get("iplay")
                ]
        return out

    try:
        with _scrape2_api_lock:
            batch = _with_scrape2_cwd(_pull_batch)
    except Exception as exc:
        print(f"[scorecard] live highlights batch: {exc}")
        return

    ts = time.time()
    with _gmid_lock:
        for eid, rows in batch.items():
            _live_hl_cache[eid] = (ts, rows)


def _fetch_live_highlights(eid: str) -> list[dict]:
    """Scrape website highlights — stale local 4.json par fallback."""
    _fetch_live_highlights_batch([str(eid)])
    with _gmid_lock:
        hit = _live_hl_cache.get(str(eid))
        if hit:
            return hit[1]
    return []


def _match_name_from_db(event_id: str) -> str:
    try:
        from mongodb.db import get_db

        row = get_db().matches.find_one(
            {"eventId": str(event_id)},
            {"_id": 0, "matchName": 1, "eventName": 1},
        )
        if row:
            return str(row.get("matchName") or row.get("eventName") or "")
    except Exception:
        pass
    return ""


def _read_json(path: Path) -> Optional[dict]:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _match_info(doc: dict) -> dict:
    info = doc.get("info")
    if isinstance(info, dict):
        return info
    gd = doc.get("gamedetail") or {}
    rows = gd.get("data") if isinstance(gd, dict) else None
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        return rows[0]
    return {}


def _scrape2_highlights(eid: str) -> list[dict]:
    doc = _read_json(SCRAPE2_SPORT / f"{eid}.json")
    if not doc:
        return []
    return list(((doc.get("data") or {}).get("t1") or []))


def _ws_event_id(eid: str, scrape2_gmid: str, oldgmid: str | None = None) -> str:
    if oldgmid:
        return str(oldgmid)
    mf = SCRAPE2_SPORT / "matches" / str(eid) / f"{scrape2_gmid}.json"
    if mf.is_file():
        info = _match_info(_read_json(mf) or {})
        return str(info.get("oldgmid") or scrape2_gmid)
    return scrape2_gmid


def _scorecard_file_age(eid: str, scrape2_gmid: str) -> float:
    path = SCRAPE2_SCORECARD / str(eid) / f"{scrape2_gmid}.json"
    if not path.is_file():
        return float("inf")
    return time.time() - path.stat().st_mtime


def _fetch_live_scorecard(ws_id: str, scrape2_gmid: str = "") -> Optional[dict]:
    scrape2_dir = _ROOT / "scrape2"
    if str(scrape2_dir) not in sys.path:
        sys.path.insert(0, str(scrape2_dir))
    from scorecard import fetch_scorecard

    lock_key = str(scrape2_gmid or ws_id)
    with _fetch_lock_for(lock_key):
        return fetch_scorecard(ws_id)


def resolve_scrape2_gmid(eid: str, request_id: str) -> tuple[Optional[str], Optional[str]]:
    """1ex99 eventId (crickexpo) → scrape2 sport gmid."""
    request_id = str(request_id or "")
    eid = str(eid or "4")
    if not request_id:
        return None, None

    cache_key = f"{eid}:{request_id}"
    now = time.time()
    with _gmid_lock:
        hit = _gmid_map.get(cache_key)
        if hit and now - hit[0] < 60:
            return hit[1], None

    direct_sc = SCRAPE2_SCORECARD / eid / f"{request_id}.json"
    if direct_sc.is_file():
        with _gmid_lock:
            _gmid_map[cache_key] = (now, request_id)
        return request_id, None

    match_path = SCRAPE2_SPORT / "matches" / eid / f"{request_id}.json"
    if match_path.is_file():
        info = _match_info(_read_json(match_path) or {})
        oldgmid = str(info.get("oldgmid") or request_id)
        with _gmid_lock:
            _gmid_map[cache_key] = (now, request_id)
        return request_id, oldgmid

    name = _match_name_from_db(request_id)
    if not name:
        return None, None
    norm = _normalize_name(name)

    for rows in (_scrape2_highlights(eid), _fetch_live_highlights(eid)):
        gmid_s, oldgmid = _gmid_from_highlight_rows(norm, rows)
        if gmid_s:
            with _gmid_lock:
                _gmid_map[cache_key] = (now, gmid_s)
            return gmid_s, _ws_event_id(eid, gmid_s, oldgmid)

    return None, None


def load_scrape2_scorecard(eid: str, scrape2_gmid: str) -> Optional[dict]:
    path = SCRAPE2_SCORECARD / str(eid) / f"{scrape2_gmid}.json"
    doc = _read_json(path)
    if not doc:
        return load_scrape2_match_scorecard(eid, scrape2_gmid)
    sc = doc.get("scorecard")
    if isinstance(sc, dict) and sc and not sc.get("error"):
        return sc
    return None


def load_scrape2_match_scorecard(eid: str, scrape2_gmid: str) -> Optional[dict]:
    mf = SCRAPE2_SPORT / "matches" / str(eid) / f"{scrape2_gmid}.json"
    doc = _read_json(mf)
    if not doc:
        return None
    sc = doc.get("scorecard")
    if isinstance(sc, dict) and sc and not sc.get("error"):
        return sc
    return None


def _persist_scrape2_scorecard(eid: str, scrape2_gmid: str, ws_id: str, sc: dict) -> None:
    try:
        out = SCRAPE2_SCORECARD / str(eid)
        out.mkdir(parents=True, exist_ok=True)
        payload = {
            "_meta": {
                "eid": eid,
                "gmid": scrape2_gmid,
                "event_id": ws_id,
                "source": "live-refresh",
                "updated_at": time.time(),
                "url": f"/sport/scorecard/{eid}/{scrape2_gmid}.json",
            },
            "scorecard": sc,
        }
        (out / f"{scrape2_gmid}.json").write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        pass


def refresh_scorecard_if_stale(eid: str, scrape2_gmid: str, max_age: float | None = None) -> bool:
    """Live fetch + file write when cache older than max_age seconds."""
    max_age = SCORECARD_LIVE_MAX_AGE if max_age is None else max_age
    if _scorecard_file_age(eid, scrape2_gmid) <= max_age:
        return False
    ws_id = _ws_event_id(eid, scrape2_gmid)
    try:
        sc = _fetch_live_scorecard(ws_id, scrape2_gmid)
        if sc:
            _persist_scrape2_scorecard(eid, scrape2_gmid, ws_id, sc)
            return True
    except Exception as exc:
        print(f"[scorecard-live] {scrape2_gmid}: {exc}")
    return False


def scorecard_response(event_id: str | int, *, eid: str | None = None, gmid: str | None = None) -> dict[str, Any]:
    """JSON for /sport/scorecard/{eid}/{gmid}.json — scrape2 file + live refresh."""
    eid_s = str(eid or 4)
    req_id = str(gmid or event_id)
    meta: dict[str, Any] = {"eid": eid_s, "gmid": req_id, "event_id": str(event_id)}

    scrape2_gmid, _old = resolve_scrape2_gmid(eid_s, req_id)
    if scrape2_gmid:
        meta["scrape2_gmid"] = scrape2_gmid
        live = get_live_scorecard_cache(eid_s, scrape2_gmid)
        if live:
            meta["source"] = "live-mem"
            meta["file_age_sec"] = 0
            meta["stale"] = False
            return {"error": False, "scorecard": live, "_meta": meta}
        age = _scorecard_file_age(eid_s, scrape2_gmid)
        meta["file_age_sec"] = round(age, 1)
        sc = load_scrape2_scorecard(eid_s, scrape2_gmid)
        refreshed = False
        if not sc:
            refreshed = refresh_scorecard_if_stale(eid_s, scrape2_gmid, max_age=-1)
            sc = load_scrape2_scorecard(eid_s, scrape2_gmid)
        elif SCORECARD_HTTP_LIVE:
            refreshed = refresh_scorecard_if_stale(
                eid_s, scrape2_gmid, max_age=min(1.5, SCORECARD_LIVE_MAX_AGE)
            )
            if refreshed:
                sc = load_scrape2_scorecard(eid_s, scrape2_gmid)
        if sc:
            meta["source"] = "scrape2-live" if refreshed else "scrape2"
            meta["file_age_sec"] = round(_scorecard_file_age(eid_s, scrape2_gmid), 1)
            meta["stale"] = meta["file_age_sec"] > SCORECARD_LIVE_MAX_AGE * 2
            return {"error": False, "scorecard": sc, "_meta": meta}
        meta["message"] = "Scorecard mapped — waiting for live ball-by-ball feed"
        return {"error": False, "message": meta["message"], "scorecard": {}, "_meta": meta}

    return {
        "error": False,
        "message": "Scorecard loading — waiting for live feed",
        "scorecard": {},
        "_meta": meta,
    }
