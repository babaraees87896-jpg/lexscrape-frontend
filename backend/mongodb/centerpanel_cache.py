"""Center panel session/bookmaker — live excache se sync, MongoDB se serve."""

from __future__ import annotations

import copy
import json
import os
import re
import time
import uuid
from typing import Any, Optional
from urllib.parse import parse_qs
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener

from mongodb.bet_logic import is_fancy_market
from mongodb.db import get_db, ping

CACHE_REMOTE = "https://1excache.tresting.com/v2/api/oddsDataNew?market_id="
CACHE_EVENT_REMOTE = "https://1excache.tresting.com/v2/api/dataByEventId?eventId="
LOCAL_EXCACHE_PORT = os.getenv("EX99_PORT", "8902")
_OVER_RUN_FANCY_RE = re.compile(r"(\d+)\s*OVER\s*RUN", re.I)
_WKT_RUNS_FANCY_RE = re.compile(r"(\d+)\s*(?:ST|ND|RD|TH)?\s*(\d+)\s*WKT\s*RUNS", re.I)
_FALL_WKT_FANCY_RE = re.compile(r"FALL\s+OF\s+(\d+)(?:ST|ND|RD|TH)?\s*WKT", re.I)


def _fetch_json(url: str, timeout: float = 8.0) -> Optional[dict]:
    try:
        opener = build_opener(ProxyHandler({}))
        req = Request(url, headers={"User-Agent": "ex99-centerpanel/1.0"})
        with opener.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (URLError, OSError, json.JSONDecodeError, ValueError):
        return None


def _odds_cache_from_mongo(market_id: str) -> dict:
    """MongoDB matches / center_manual se odds — excache proxy loop avoid."""
    if not ping():
        return {}
    market_id = str(market_id or "").strip()
    if not market_id:
        return {}

    db = get_db()
    match = db.matches.find_one(
        {"marketId": market_id},
        {"sessionList": 1, "fancyList": 1, "teamData": 1, "bookmakerList": 1, "_id": 0},
    )
    if match:
        sessions = match.get("sessionList") or match.get("fancyList") or []
        teams: Any = match.get("bookmakerList") or []
        team_raw = match.get("teamData")
        if isinstance(team_raw, str) and team_raw.strip():
            try:
                parsed = json.loads(team_raw)
                if isinstance(parsed, list) and parsed:
                    teams = parsed
            except (json.JSONDecodeError, ValueError):
                pass
        elif isinstance(team_raw, list) and team_raw:
            teams = team_raw
        cache: dict[str, Any] = {}
        if sessions:
            cache["session"] = sessions
        if teams:
            cache["team_data"] = teams
        if cache:
            return cache

    sessions = [
        _normalize_session_row(row)
        for row in db.center_manual_fancy.find({"marketId": market_id}, {"_id": 0})
    ]
    teams = list(db.center_manual_bookmaker.find({"marketId": market_id}, {"_id": 0}))
    cache = {}
    if sessions:
        cache["session"] = sessions
    if teams:
        cache["team_data"] = teams
    return cache


def fetch_odds_cache(market_id: str) -> dict:
    """Live fancy/session + bookmaker odds — remote excache + Mongo declare overlay."""
    market_id = str(market_id or "").strip()
    if not market_id:
        return {}

    mongo = _odds_cache_from_mongo(market_id)
    remote: dict = {}
    raw = _fetch_json(f"{CACHE_REMOTE}{market_id}")
    if isinstance(raw, dict):
        result = raw.get("result")
        if isinstance(result, dict) and result:
            remote = copy.deepcopy(result)

    if remote:
        cache = remote
        if not cache.get("team_data") and mongo.get("team_data"):
            cache["team_data"] = mongo["team_data"]
    elif mongo:
        cache = mongo
    else:
        return {}

    cache = _apply_declared_fancy_to_cache(cache, market_id)
    cache = _inject_declared_fancy_sessions(cache, market_id)
    return cache


def _normalize_session_row(row: dict) -> dict:
    """Center panel sessionDecision UI ke liye — selectionId string, fancyName, commPerm, _id."""
    doc = copy.deepcopy(row)
    sid = str(
        doc.get("Selection_id")
        or doc.get("session_id")
        or doc.get("fancyId")
        or doc.get("selectionId")
        or ""
    )
    if sid.isdigit() and doc.get("Selection_id"):
        sid = str(doc["Selection_id"])
    elif sid.isdigit() and doc.get("session_id"):
        sid = str(doc["session_id"])

    name = doc.get("fancyName") or doc.get("session_name") or doc.get("sessionName") or ""
    doc["session_name"] = name
    doc["sessionName"] = name
    doc["fancyName"] = name
    doc["Selection_id"] = sid
    doc["session_id"] = sid
    doc["fancyId"] = sid or doc.get("fancyId")
    doc["selectionId"] = sid
    if doc.get("diamondSelectionId") is None:
        raw_sel = row.get("selectionId")
        if isinstance(raw_sel, int) or (isinstance(raw_sel, str) and raw_sel.isdigit()):
            doc["diamondSelectionId"] = int(raw_sel)
    doc["commPerm"] = str(doc.get("commPerm") or doc.get("com_perm") or "YES")
    doc["com_perm"] = doc["commPerm"]
    doc.setdefault("insertByCenterPanel", bool(doc.get("insertByCenterPanel")))
    doc.setdefault("isRollback", bool(doc.get("isRollback")))
    doc.setdefault("decisionRun", doc.get("decisionRun"))
    doc.setdefault("isDeclare", bool(doc.get("isDeclare")))
    doc.setdefault("isCancel", bool(doc.get("isCancel")))
    mongo_id = doc.pop("_id", None)
    doc["_id"] = str(mongo_id) if mongo_id is not None else sid
    return doc


def _float_val(val: Any, default: float = 0.0) -> float:
    try:
        if val is None or val == "":
            return default
        return float(val)
    except (TypeError, ValueError):
        return default


def _bhav_from_odds(val: Any, default: float = 100.0) -> float:
    v = _float_val(val, default)
    if 0 < v <= 10:
        return round(v * 100, 2)
    return v


def _odds_from_bhav(val: Any, default: float = 100.0) -> float:
    v = _float_val(val, default)
    if v > 10:
        return round(v / 100, 4)
    return v


def _play_status_ui(ps: Any) -> str:
    if ps is None:
        return "active"
    s = str(ps).lower()
    if s in ("active", "running", "suspended"):
        return s
    if s in ("0", "false", "inactive", "stop", "stopped"):
        return "suspended"
    if s in ("2", "run"):
        return "running"
    return "active"


def _play_status_store(ps: Any) -> int:
    return {"active": 1, "running": 2, "suspended": 0}.get(_play_status_ui(ps), 1)


def _excache_session_is_live(row: Optional[dict]) -> bool:
    """Excache par session abhi trade ho rahi hai — local declare mat karo."""
    if not isinstance(row, dict):
        return False
    if row.get("isDeclare"):
        return False
    yes, no = _session_row_odds(row)
    if yes > 0 or no > 0:
        return True
    rs = str(row.get("running_status") or row.get("remark") or "").strip().upper()
    if rs in ("BALL RUNNING", "RUNNING"):
        return True
    if _play_status_ui(row.get("playStatus")) == "running":
        return True
    return False


def _session_row_odds(row: dict) -> tuple[float, float]:
    yes = _float_val(row.get("runsYes"))
    no = _float_val(row.get("runsNo"))
    odds = row.get("odds")
    if isinstance(odds, dict):
        yes = max(yes, _float_val(odds.get("yesRun")))
        no = max(no, _float_val(odds.get("noRun")))
    yes = max(yes, _float_val(row.get("yesRun")))
    no = max(no, _float_val(row.get("noRun")))
    return yes, no


def _iter_cache_sessions(cache: dict) -> list[dict]:
    rows: list[dict] = []
    for key in ("session", "sessionList", "meterKhadoSession"):
        block = cache.get(key) or []
        if isinstance(block, list):
            rows.extend(r for r in block if isinstance(r, dict))
    return rows


def _build_session_index(cache: dict) -> tuple[dict[str, dict], dict[str, dict]]:
    by_sid: dict[str, dict] = {}
    by_name: dict[str, dict] = {}
    for row in _iter_cache_sessions(cache):
        sid = _cache_row_sid(row)
        if sid:
            by_sid[sid] = row
        name = _norm_fancy_name(
            str(row.get("session_name") or row.get("sessionName") or row.get("fancyName") or "")
        )
        if name:
            by_name[name] = row
    return by_sid, by_name


def _lookup_upstream_session(
    sid: str,
    name: str,
    by_sid: dict[str, dict],
    by_name: dict[str, dict],
) -> Optional[dict]:
    sid = str(sid or "").strip()
    if sid and sid in by_sid:
        return by_sid[sid]
    norm = _norm_fancy_name(name)
    if norm and norm in by_name:
        return by_name[norm]
    if norm:
        for key, row in by_name.items():
            if key and (norm in key or key in norm):
                return row
    return None


def _fetch_raw_remote_odds_cache(market_id: str) -> dict:
    """Upstream excache only — Mongo declare overlay ke bina."""
    market_id = str(market_id or "").strip()
    if not market_id:
        return {}
    raw = _fetch_json(f"{CACHE_REMOTE}{market_id}")
    if isinstance(raw, dict):
        result = raw.get("result")
        if isinstance(result, dict) and result:
            return copy.deepcopy(result)
    return {}


def _fancy_tradeable_on_upstream(
    sid: str,
    name: str,
    raw_by_sid: dict[str, dict],
    raw_by_name: dict[str, dict],
) -> bool:
    row = _lookup_upstream_session(sid, name, raw_by_sid, raw_by_name)
    return bool(row and _excache_session_is_live(row))


def _upstream_session_declared(row: Optional[dict]) -> bool:
    if not isinstance(row, dict):
        return False
    if not row.get("isDeclare"):
        return False
    return _decision_run_from_excache_row(row) is not None


def _staff_manual_fancy_declare(doc: Optional[dict]) -> bool:
    """Center panel / staff ne khud declare kiya — auto paths is par trust nahi."""
    if not isinstance(doc, dict):
        return False
    if doc.get("declareUserDetails"):
        return True
    source = str(doc.get("declareSource") or "").strip().lower()
    return source in ("staff", "manual", "centerpanel")


def _revoke_stale_auto_declares(
    market_id: str,
    raw_by_sid: dict[str, dict],
    raw_by_name: dict[str, dict],
) -> None:
    """Scorecard/auto declare hatao jab upstream excache abhi declare nahi karta."""
    if not ping():
        return
    db = get_db()
    for doc in db.center_manual_fancy.find(
        {"marketId": str(market_id), "isDeclare": True},
        {"_id": 0, "fancyId": 1, "selectionId": 1, "fancyName": 1, "sessionName": 1, "declareUserDetails": 1, "declareSource": 1},
    ):
        if _staff_manual_fancy_declare(doc):
            continue
        sid = str(doc.get("fancyId") or doc.get("selectionId") or "")
        name = str(doc.get("fancyName") or doc.get("sessionName") or "")
        row = _lookup_upstream_session(sid, name, raw_by_sid, raw_by_name)
        if row is None:
            continue
        if _upstream_session_declared(row):
            continue
        db.center_manual_fancy.update_one(
            {"fancyId": doc.get("fancyId") or doc.get("selectionId")},
            {
                "$set": {"isDeclare": False, "updatedAt": time.time()},
                "$unset": {"decisionRun": "", "declareSource": ""},
            },
        )


def _cache_row_sid(row: dict) -> str:
    return str(
        row.get("session_id")
        or row.get("Selection_id")
        or row.get("selectionId")
        or row.get("fancyId")
        or ""
    )


def _to_session_ui_row(row: dict) -> dict:
    """Session page (/app/session) — odds.noRun/yesRun/bhav + playStatus string."""
    doc = _normalize_session_row(row)
    odds_obj = doc.get("odds") if isinstance(doc.get("odds"), dict) else {}
    no_run = _float_val(odds_obj.get("noRun", doc.get("noRun", doc.get("runsNo", 0))))
    yes_run = _float_val(odds_obj.get("yesRun", doc.get("yesRun", doc.get("runsYes", no_run))))
    no_bhav = _float_val(odds_obj.get("noBhav", doc.get("noBhav", _bhav_from_odds(doc.get("oddsNo"), 100))))
    yes_bhav = _float_val(odds_obj.get("yesBhav", doc.get("yesBhav", _bhav_from_odds(doc.get("oddsYes"), 100))))
    rng = _float_val(doc.get("range"), max(0.0, yes_run - no_run))
    if yes_run < no_run:
        yes_run = no_run + rng
    doc["range"] = rng
    doc["odds"] = {
        "noRun": no_run,
        "yesRun": yes_run,
        "noBhav": no_bhav,
        "yesBhav": yes_bhav,
    }
    doc["playStatus"] = _play_status_ui(doc.get("playStatus"))
    doc["remark"] = str(doc.get("remark") or "")
    doc["runsNo"] = no_run
    doc["runsYes"] = yes_run
    doc["oddsNo"] = _odds_from_bhav(no_bhav)
    doc["oddsYes"] = _odds_from_bhav(yes_bhav)
    return doc


def _to_bookmaker_ui_row(row: dict) -> dict:
    doc = copy.deepcopy(row)
    doc.pop("_id", None)
    name = str(doc.get("teamName") or doc.get("runnerName") or doc.get("team_name") or "")
    doc["teamName"] = name
    doc["runnerName"] = name
    doc.setdefault("backSize", doc.get("backSize") or doc.get("back") or doc.get("backPrize") or 0)
    doc.setdefault("laySize", doc.get("laySize") or doc.get("lay") or doc.get("layPrize") or 0)
    doc.setdefault("range", doc.get("range") or 1)
    return doc


def _sessions_for_ui(payload: dict, *, enrich: bool = False) -> list[dict]:
    payload = payload or {}
    rows = get_cp_sessions(payload)
    if enrich:
        rows = _enrich_sessions_with_bets(rows, payload)
    rows = _apply_session_filters(rows, payload)
    return [_to_session_ui_row(r) for r in rows]


def _fancy_doc_from_save_payload(payload: dict, *, create: bool = False) -> dict:
    payload = payload or {}
    sid = str(
        payload.get("selectionId")
        or payload.get("fancyId")
        or payload.get("Selection_id")
        or payload.get("session_id")
        or ""
    )
    if create and not sid:
        sid = f"manual-{uuid.uuid4().hex[:16]}"
    name = str(payload.get("fancyName") or payload.get("sessionName") or payload.get("session_name") or "")
    no_run = _float_val(payload.get("noRun", payload.get("runsNo", 0)))
    yes_run = _float_val(payload.get("yesRun", payload.get("runsYes", no_run)))
    no_bhav = _float_val(payload.get("noBhav", _bhav_from_odds(payload.get("oddsNo"), 100)))
    yes_bhav = _float_val(payload.get("yesBhav", _bhav_from_odds(payload.get("oddsYes"), 100)))
    rng = _float_val(payload.get("range"), max(0.0, yes_run - no_run))
    if yes_run < no_run:
        yes_run = no_run + rng
    return {
        "fancyId": sid,
        "selectionId": sid,
        "Selection_id": sid,
        "session_id": sid,
        "eventId": str(payload.get("eventId") or ""),
        "marketId": str(payload.get("marketId") or ""),
        "fancyName": name,
        "sessionName": name,
        "session_name": name,
        "fancyType": str(payload.get("fancyType") or "Normal"),
        "gtype": str(payload.get("gType") or payload.get("gtype") or "fancy"),
        "playStatus": _play_status_store(payload.get("playStatus")),
        "range": rng,
        "runsNo": no_run,
        "runsYes": yes_run,
        "oddsNo": _odds_from_bhav(no_bhav),
        "oddsYes": _odds_from_bhav(yes_bhav),
        "noRun": no_run,
        "yesRun": yes_run,
        "noBhav": no_bhav,
        "yesBhav": yes_bhav,
        "odds": {"noRun": no_run, "yesRun": yes_run, "noBhav": no_bhav, "yesBhav": yes_bhav},
        "remark": str(payload.get("remark") or ""),
        "isDeclare": bool(payload.get("isDeclare")),
        "isCancel": bool(payload.get("isCancel")),
        "insertByCenterPanel": True if create else bool(payload.get("insertByCenterPanel")),
        "source": "centerpanel",
        "status": "active",
        "commPerm": str(payload.get("commPerm") or payload.get("com_perm") or "YES"),
        "com_perm": str(payload.get("commPerm") or payload.get("com_perm") or "YES"),
        "isOddsPanelOpen": payload.get("isOddsPanelOpen"),
        "oddsMode": payload.get("oddsMode"),
    }


def _session_doc_from_cache(row: dict, market_id: str, event_id: str) -> dict:
    """Cache row ko center panel frontend format mein — Selection_id + com_perm preserve."""
    doc = copy.deepcopy(row)
    sid = str(
        row.get("session_id")
        or row.get("Selection_id")
        or row.get("selectionId")
        or row.get("diamondSelectionId")
        or ""
    )
    doc["fancyId"] = sid or f"fancy-{row.get('diamondSelectionId')}"
    doc["session_id"] = sid
    doc["Selection_id"] = sid
    if row.get("diamondSelectionId") is not None:
        doc["diamondSelectionId"] = row.get("diamondSelectionId")
    elif isinstance(row.get("selectionId"), int) or (
        isinstance(row.get("selectionId"), str) and str(row.get("selectionId")).isdigit()
    ):
        doc["diamondSelectionId"] = int(row.get("selectionId"))
    doc["selectionId"] = sid
    doc["marketId"] = str(row.get("marketId") or market_id)
    doc["eventId"] = str(row.get("eventId") or event_id)
    doc["session_name"] = row.get("session_name") or row.get("sessionName") or ""
    doc["sessionName"] = doc["session_name"]
    doc["fancyName"] = doc["session_name"]
    doc.setdefault("runsYes", row.get("runsYes", 0))
    doc.setdefault("runsNo", row.get("runsNo", 0))
    doc.setdefault("oddsYes", row.get("oddsYes", 0))
    doc.setdefault("oddsNo", row.get("oddsNo", 0))
    doc.setdefault("gtype", row.get("gtype") or "fancy")
    doc.setdefault("fancyType", row.get("fancyType") or "Normal")
    doc.setdefault("playStatus", row.get("playStatus", 1))
    doc.setdefault("priority", row.get("priority", 0))
    doc.setdefault("com_perm", row.get("com_perm") or "YES")
    doc.setdefault("commPerm", doc["com_perm"])
    doc.setdefault("running_status", row.get("running_status") or "")
    doc.setdefault("maximumAmount", row.get("max") or row.get("maximumAmount") or 0)
    doc.setdefault("max", row.get("max") or doc["maximumAmount"])
    doc.setdefault("isDeclare", bool(row.get("isDeclare")))
    doc.setdefault("isCancel", bool(row.get("isCancel")))
    doc["source"] = "excache"
    return doc


def _merge_fancy_declare_state(doc: dict, existing: Optional[dict], row: dict) -> dict:
    """Declare state — sirf upstream excache ya staff manual se."""
    if existing:
        for key in ("fancyName", "sessionName", "session_name"):
            if not str(doc.get(key) or "").strip() and str(existing.get(key) or "").strip():
                doc[key] = existing[key]
    if row.get("isDeclare") and row.get("decisionRun") is not None:
        doc["isDeclare"] = True
        doc["decisionRun"] = row.get("decisionRun")
        doc["declareSource"] = "excache"
    elif existing and _staff_manual_fancy_declare(existing):
        if existing.get("isDeclare") and existing.get("decisionRun") is not None:
            doc["isDeclare"] = True
            doc["decisionRun"] = existing.get("decisionRun")
            if existing.get("declareSource"):
                doc["declareSource"] = existing.get("declareSource")
            if existing.get("declareUserDetails"):
                doc["declareUserDetails"] = existing.get("declareUserDetails")
    else:
        doc["isDeclare"] = bool(row.get("isDeclare"))
        if not doc["isDeclare"]:
            doc.pop("decisionRun", None)
            doc.pop("declareSource", None)
    if existing and existing.get("lastSessionLine") is not None and doc.get("lastSessionLine") is None:
        doc["lastSessionLine"] = existing.get("lastSessionLine")
    yes = _float_val(doc.get("runsYes"))
    no = _float_val(doc.get("runsNo"))
    if yes > 0 and abs(yes - no) < 0.01:
        doc["lastSessionLine"] = int(round(yes))
    return doc


def _parse_innings_score(score_str: str) -> tuple[int, int, float]:
    """'199-7 (20.0)' → runs, wickets, overs completed."""
    text = str(score_str or "").strip()
    m = re.match(r"(\d+)\s*-\s*(\d+)\s*\(([\d.]+)\)", text)
    if not m:
        return 0, 0, 0.0
    try:
        return int(m.group(1)), int(m.group(2)), float(m.group(3))
    except (TypeError, ValueError):
        return 0, 0, 0.0


def _scorecard_for_event(event_id: str) -> dict:
    if not event_id:
        return {}
    try:
        from scorecard_api import (
            SCRAPE2_SCORECARD,
            load_scrape2_scorecard,
            refresh_scorecard_if_stale,
            resolve_scrape2_gmid,
        )

        event_id = str(event_id)
        match = get_db().matches.find_one({"eventId": event_id}, {"_id": 0, "matchName": 1})
        tokens: set[str] = set()
        if match:
            for part in re.split(r"\s+v\s+", str(match.get("matchName") or ""), flags=re.I):
                words = re.findall(r"[a-z]+", part.lower())
                if words:
                    tokens.add(words[0][:3])
                compact = re.sub(r"[^a-z0-9]", "", part.lower())
                if compact:
                    tokens.add(compact[:3])

        card_dir = SCRAPE2_SCORECARD / "4"
        if card_dir.is_dir() and tokens:
            for path in card_dir.glob("*.json"):
                try:
                    doc = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                sc = doc.get("scorecard") or {}
                team_keys = [
                    re.sub(r"[^a-z0-9]", "", str(sc.get("spnnation1") or "").lower()),
                    re.sub(r"[^a-z0-9]", "", str(sc.get("spnnation2") or "").lower()),
                ]
                if all(
                    any(tok in team or team.startswith(tok) for team in team_keys if team)
                    for tok in tokens
                ):
                    gmid = path.stem
                    try:
                        refresh_scorecard_if_stale("4", gmid, max_age=30)
                    except Exception:
                        pass
                    loaded = load_scrape2_scorecard("4", gmid)
                    return loaded or sc

        gmid, _old = resolve_scrape2_gmid("4", event_id)
        if gmid:
            try:
                refresh_scorecard_if_stale("4", gmid, max_age=30)
            except Exception:
                pass
            sc = load_scrape2_scorecard("4", gmid)
            if sc:
                return sc
            try:
                refresh_scorecard_if_stale("4", gmid, max_age=-1)
            except Exception:
                pass
            return load_scrape2_scorecard("4", gmid) or {}
    except Exception:
        pass
    return {}


def _innings_score_for_fancy(sc: dict, fancy_name: str) -> tuple[int, int, float]:
    """Pick completed-innings score row for e.g. '17 OVER RUN AUS W'."""
    fancy_up = str(fancy_name or "").upper()
    n1 = str(sc.get("spnnation1") or "").upper()
    n2 = str(sc.get("spnnation2") or "").upper()
    for nation, score_key in ((n1, "score1"), (n2, "score2")):
        if not nation:
            continue
        token = nation.split()[0][:3]
        if token and token in fancy_up:
            return _parse_innings_score(sc.get(score_key) or "")
    active = str(sc.get("activenation1") or "1") != "0"
    return _parse_innings_score(sc.get("score1" if active else "score2") or "")


def _fancy_team_tokens(fancy_name: str) -> set[str]:
    """Over-run session ke team side tokens — ENG/WI etc."""
    fancy_up = str(fancy_name or "").upper()
    tokens: set[str] = set()
    tail_match = re.search(r"OVER\s*RUN(?:S)?\s+(.+)$", fancy_up)
    if tail_match:
        for part in re.split(r"[\s()]+", tail_match.group(1).strip()):
            part = re.sub(r"[^A-Z]", "", part)
            if len(part) >= 2 and part not in ("ADV", "RUN", "RUNS", "OVER"):
                tokens.add(part[:3])
    return tokens


def _fancy_same_team(session_name: str, fancy_name: str) -> bool:
    """Sirf same batting side ke declared overs interpolate karo."""
    want = _fancy_team_tokens(fancy_name)
    if not want:
        return True
    have = _fancy_team_tokens(session_name)
    if not have:
        return True
    return bool(want & have)


def _collect_declared_over_run_points(
    db,
    market_id: str,
    fancy_name: str = "",
) -> list[tuple[int, int]]:
    points: list[tuple[int, int]] = []
    for doc in db.center_manual_fancy.find({"marketId": str(market_id), "isDeclare": True}, {"_id": 0}):
        name = str(doc.get("fancyName") or doc.get("sessionName") or "")
        if not name.strip():
            sid = str(doc.get("fancyId") or doc.get("selectionId") or "")
            bet = db.sports_bets.find_one(
                {"marketId": str(market_id), "selectionId": sid},
                {"_id": 0, "fancyName": 1, "runnerName": 1, "sessionName": 1},
            )
            if bet:
                name = str(bet.get("fancyName") or bet.get("sessionName") or bet.get("runnerName") or "")
        if not name.strip():
            sid = str(doc.get("fancyId") or doc.get("selectionId") or "")
            log = db.decision_logs.find_one(
                {"marketId": str(market_id), "selectionId": sid},
                {"_id": 0, "fancyName": 1},
            )
            if log:
                name = str(log.get("fancyName") or "")
        hit = _OVER_RUN_FANCY_RE.search(name)
        if not hit or doc.get("decisionRun") is None:
            continue
        if fancy_name and not _fancy_same_team(name, fancy_name):
            continue
        try:
            points.append((int(hit.group(1)), int(doc.get("decisionRun"))))
        except (TypeError, ValueError):
            continue
    return sorted(set(points))


def _interpolate_over_runs(points: list[tuple[int, int]], target_over: int) -> Optional[int]:
    if not points or target_over <= 0:
        return None
    ordered = sorted(set(points))
    for over_no, runs in ordered:
        if over_no == target_over:
            return runs
    lower = [p for p in ordered if p[0] < target_over]
    upper = [p for p in ordered if p[0] > target_over]
    if not lower or not upper:
        return None
    o1, r1 = lower[-1]
    o2, r2 = upper[0]
    if o2 == o1:
        return r1
    frac = (target_over - o1) / (o2 - o1)
    return int(round(r1 + (r2 - r1) * frac))


def _runs_at_target_over(runs: int, overs: float, target_over: int) -> int:
    """Scrape-style — target over complete hone par cumulative runs estimate."""
    if runs <= 0 or overs <= 0 or target_over <= 0:
        return 0
    if overs <= float(target_over) + 0.001:
        return int(runs)
    run_rate = runs / overs
    return max(0, int(round(runs - (overs - float(target_over)) * run_rate)))


def _decision_run_from_excache_row(row: dict) -> Optional[int]:
    for key in ("decisionRun", "decision_run", "result", "finalRun", "runsResult"):
        val = row.get(key)
        if val in (None, ""):
            continue
        try:
            return int(float(val))
        except (TypeError, ValueError):
            continue
    return None


def try_infer_over_decision_run(market_id: str, fancy_name: str, event_id: str = "") -> Optional[int]:
    """
    Scrape-style over-run declare — scorecard se jab target over complete ho.
    Pehle declared sessions se interpolate, warna live innings se estimate.
    """
    hit = _OVER_RUN_FANCY_RE.search(str(fancy_name or ""))
    if not hit or not ping():
        return None
    target_over = int(hit.group(1))
    db = get_db()
    points = _collect_declared_over_run_points(db, market_id, fancy_name)

    try:
        from scorecard_api import refresh_scorecard_if_stale, resolve_scrape2_gmid

        gmid, _ = resolve_scrape2_gmid("4", str(event_id or ""))
        if gmid:
            refresh_scorecard_if_stale("4", gmid, max_age=15)
    except Exception:
        pass

    sc = _scorecard_for_event(event_id)
    if sc:
        runs, _wkts, overs = _innings_score_for_fancy(sc, fancy_name)
        # N-over session tab declare jab (N+1)th over shuru ho chuka ho
        if runs > 0 and float(overs) > float(target_over):
            est = _runs_at_target_over(runs, overs, target_over)
            if est > 0:
                points.append((target_over, est))

    points = sorted(set(points))
    if not points:
        return None
    max_known_over = max(p[0] for p in points)
    if max_known_over < target_over:
        return None
    return _interpolate_over_runs(points, target_over)


def _team_key(nation: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(nation or "").lower())


def _wicket_target_from_fancy(fancy_name: str) -> Optional[int]:
    name = str(fancy_name or "")
    hit = _WKT_RUNS_FANCY_RE.search(name)
    if hit:
        return int(hit.group(2))
    hit = _FALL_WKT_FANCY_RE.search(name)
    if hit:
        return int(hit.group(1))
    return None


def _is_player_line_fancy(fancy_name: str) -> bool:
    up = str(fancy_name or "").upper().strip()
    if not up or "OVER RUN" in up or "WKT" in up or "FALL OF" in up:
        return False
    return up.endswith(" RUN") or up.endswith(" BOUNDARIES")


def _session_line_from_row(row: dict) -> Optional[int]:
    yes = _float_val(row.get("runsYes"))
    no = _float_val(row.get("runsNo"))
    if yes <= 0 or abs(yes - no) >= 0.01:
        return None
    return int(round(yes))


def _record_innings_snapshots(event_id: str, sc: dict) -> None:
    """Wicket fall par runs capture — 1ST N WKT RUNS declare ke liye."""
    if not event_id or not sc or not ping():
        return
    db = get_db()
    for nation, score_key in (
        (sc.get("spnnation1"), "score1"),
        (sc.get("spnnation2"), "score2"),
    ):
        if not nation:
            continue
        runs, wkts, overs = _parse_innings_score(sc.get(score_key) or "")
        if wkts <= 0:
            continue
        db.event_innings_snapshots.update_one(
            {
                "eventId": str(event_id),
                "teamKey": _team_key(nation),
                "wickets": wkts,
            },
            {
                "$set": {
                    "runs": runs,
                    "overs": overs,
                    "team": str(nation),
                    "updatedAt": time.time(),
                }
            },
            upsert=True,
        )


def _lookup_wicket_runs(db, event_id: str, team_key: str, target_wkt: int) -> Optional[int]:
    doc = db.event_innings_snapshots.find_one(
        {"eventId": str(event_id), "teamKey": team_key, "wickets": target_wkt},
        {"_id": 0, "runs": 1},
    )
    if not doc or doc.get("runs") is None:
        return None
    try:
        return int(doc["runs"])
    except (TypeError, ValueError):
        return None


def _refresh_event_scorecard(event_id: str) -> dict:
    try:
        from scorecard_api import refresh_scorecard_if_stale, resolve_scrape2_gmid

        gmid, _ = resolve_scrape2_gmid("4", str(event_id or ""))
        if gmid:
            refresh_scorecard_if_stale("4", gmid, max_age=15)
    except Exception:
        pass
    return _scorecard_for_event(event_id)


def try_infer_wicket_decision_run(market_id: str, fancy_name: str, event_id: str = "") -> Optional[int]:
    """Nth wicket par team runs — scorecard snapshots se."""
    target = _wicket_target_from_fancy(fancy_name)
    if target is None or not ping():
        return None
    sc = _refresh_event_scorecard(event_id)
    if not sc:
        return None
    _record_innings_snapshots(event_id, sc)
    runs, wkts, _overs = _innings_score_for_fancy(sc, fancy_name)
    if wkts < target:
        return None
    fancy_up = str(fancy_name or "").upper()
    team_key = ""
    for nation in (sc.get("spnnation1"), sc.get("spnnation2")):
        if not nation:
            continue
        token = str(nation).split()[0][:3].upper()
        if token and token in fancy_up:
            team_key = _team_key(nation)
            break
    if not team_key:
        active = str(sc.get("activenation1") or "1") != "0"
        nation = sc.get("spnnation1") if active else sc.get("spnnation2")
        team_key = _team_key(str(nation or ""))
    snap = _lookup_wicket_runs(get_db(), event_id, team_key, target)
    if snap is not None:
        return snap
    if wkts == target:
        return runs
    return None


def try_infer_line_session_decision_run(
    fancy_name: str,
    excache_row: Optional[dict] = None,
    mongo_doc: Optional[dict] = None,
) -> Optional[int]:
    """Player RUN / BOUNDARIES — session band hone par last line se."""
    if not _is_player_line_fancy(fancy_name):
        return None
    row = excache_row if isinstance(excache_row, dict) else {}
    stored = mongo_doc if isinstance(mongo_doc, dict) else {}
    rs = str(row.get("running_status") or stored.get("running_status") or "").strip().upper()
    ps = _play_status_store(row.get("playStatus", stored.get("playStatus")))
    yes = _float_val(row.get("runsYes", stored.get("runsYes")))
    no = _float_val(row.get("runsNo", stored.get("runsNo")))
    settled = ps == 0 or (rs == "SUSPENDED" and yes <= 0 and no <= 0)
    if not settled:
        return None
    for src in (stored, row):
        if not isinstance(src, dict):
            continue
        last = src.get("lastSessionLine")
        if last is not None:
            try:
                return int(float(last))
            except (TypeError, ValueError):
                continue
        line = _session_line_from_row(src)
        if line is not None:
            return line
    return None


def try_infer_session_decision_run(
    market_id: str,
    fancy_name: str,
    event_id: str = "",
    *,
    excache_row: Optional[dict] = None,
    mongo_doc: Optional[dict] = None,
) -> Optional[int]:
    """Over run, wicket run, player run/boundary — unified scorecard + excache inference."""
    inferred = try_infer_over_decision_run(market_id, fancy_name, event_id)
    if inferred is not None:
        return inferred
    inferred = try_infer_wicket_decision_run(market_id, fancy_name, event_id)
    if inferred is not None:
        return inferred
    return try_infer_line_session_decision_run(fancy_name, excache_row, mongo_doc)


def _message_matches_team(msg: str, team_name: str) -> bool:
    msg_norm = re.sub(r"[^a-z0-9]", "", str(msg or "").lower())
    name_norm = re.sub(r"[^a-z0-9]", "", str(team_name or "").lower())
    if not msg_norm or not name_norm:
        return False
    if name_norm in msg_norm or msg_norm in name_norm:
        return True
    parts = [p for p in re.split(r"\s+", str(team_name or "").strip()) if p]
    if parts:
        tok = re.sub(r"[^a-z0-9]", "", parts[0].lower())[:4]
        if len(tok) >= 3 and tok in msg_norm:
            return True
    return False


def infer_bookmaker_winner(market_id: str, event_id: str = "") -> Optional[str]:
    """Scrape-style bookmaker winner — scorecard result message ya excache settle pattern."""
    market_id = str(market_id or "").strip()
    if not market_id or not ping():
        return None

    db = get_db()
    match = db.matches.find_one({"marketId": market_id}, {"_id": 0}) or {}
    if not event_id:
        event_id = str(match.get("eventId") or "")

    try:
        from scorecard_api import refresh_scorecard_if_stale, resolve_scrape2_gmid

        gmid, _ = resolve_scrape2_gmid("4", event_id)
        if gmid:
            refresh_scorecard_if_stale("4", gmid, max_age=15)
    except Exception:
        pass

    sc = _scorecard_for_event(event_id)
    msg = str(sc.get("spnmessage") or sc.get("spnballrunningstatus") or "")
    msg_l = msg.lower()
    match_finished = str(sc.get("isfinished") or "") in ("1", "true", "True")
    won_hint = match_finished or any(
        phrase in msg_l for phrase in ("won the match", "won by", " beat ", "wins the match")
    )

    from mongodb.bet_logic import parse_team_selections

    cache = fetch_odds_cache(market_id)
    teams = list(cache.get("team_data") or cache.get("teamData") or [])
    if not teams:
        teams = parse_team_selections(match)

    if won_hint and msg.strip():
        for row in teams:
            name = str(row.get("team_name") or row.get("runnerName") or row.get("teamName") or "")
            if not name or not _message_matches_team(msg, name):
                continue
            sel = row.get("selectionId") or row.get("selectionid") or row.get("bookmakerSelectionId")
            if sel is not None:
                return str(sel)

    return None


def _session_row_from_fancy_doc(doc: dict) -> dict:
    row = copy.deepcopy(doc)
    name = str(doc.get("fancyName") or doc.get("sessionName") or doc.get("session_name") or "")
    sid = str(doc.get("fancyId") or doc.get("selectionId") or doc.get("session_id") or "")
    row.setdefault("session_name", name)
    row.setdefault("sessionName", name)
    row.setdefault("fancyName", name)
    row.setdefault("selectionId", sid)
    row.setdefault("session_id", sid)
    row.setdefault("Selection_id", sid)
    return row


def _declared_session_row(
    sid: str,
    market_id: str,
    event_id: str,
    name: str,
    decision_run: int,
    source: str,
    gtype: Any = None,
) -> dict:
    g = str(gtype or "fancy")
    return {
        "selectionId": sid,
        "fancyId": sid,
        "session_id": sid,
        "Selection_id": sid,
        "marketId": market_id,
        "eventId": event_id,
        "fancyName": name,
        "sessionName": name,
        "session_name": name,
        "isDeclare": True,
        "decisionRun": decision_run,
        "declareSource": source,
        "gtype": g,
        "fancyType": g if g not in ("", "fancy") else "Normal",
    }


def collect_declared_sessions_for_market(market_id: str, event_id: str = "") -> list[dict]:
    """Excache upstream declare + staff mongo — scorecard se pehle declare nahi."""
    market_id = str(market_id or "").strip()
    if not market_id or not ping():
        return []

    db = get_db()
    if not event_id:
        match = db.matches.find_one({"marketId": market_id}, {"eventId": 1, "_id": 0})
        event_id = str((match or {}).get("eventId") or "")

    sync_market_cache_to_mongo(market_id, event_id)
    raw_cache = _fetch_raw_remote_odds_cache(market_id)
    raw_by_sid, raw_by_name = _build_session_index(raw_cache)
    _revoke_stale_auto_declares(market_id, raw_by_sid, raw_by_name)

    sc = _refresh_event_scorecard(event_id)
    if sc:
        _record_innings_snapshots(event_id, sc)

    by_sid: dict[str, dict] = {}
    for row in _iter_cache_sessions(raw_cache):
        dr = _decision_run_from_excache_row(row)
        if not _upstream_session_declared(row) or dr is None:
            continue
        doc = _session_doc_from_cache(row, market_id, event_id)
        doc["isDeclare"] = True
        doc["decisionRun"] = dr
        doc["declareSource"] = "excache"
        sid = str(doc.get("fancyId") or doc.get("selectionId") or "")
        if sid:
            by_sid[sid] = doc

    return list(by_sid.values())


def _max_over_from_excache_sessions(raw_cache: dict) -> int:
    max_over = 0
    for row in _iter_cache_sessions(raw_cache):
        name = str(row.get("session_name") or row.get("sessionName") or "")
        hit = _OVER_RUN_FANCY_RE.search(name)
        if hit:
            max_over = max(max_over, int(hit.group(1)))
    return max_over


def _max_over_from_market(market_id: str, raw_cache: dict) -> int:
    """Excache fail hone par mongo session names se current over estimate."""
    max_over = _max_over_from_excache_sessions(raw_cache)
    if max_over > 0 or not ping():
        return max_over
    db = get_db()
    for doc in db.center_manual_fancy.find({"marketId": str(market_id)}, {"fancyName": 1, "sessionName": 1, "_id": 0}):
        name = str(doc.get("fancyName") or doc.get("sessionName") or "")
        hit = _OVER_RUN_FANCY_RE.search(name)
        if hit:
            max_over = max(max_over, int(hit.group(1)))
    return max_over


def _over_session_completed(
    market_id: str,
    event_id: str,
    fancy_name: str,
    raw_cache: dict,
    raw_by_sid: dict[str, dict],
    raw_by_name: dict[str, dict],
    selection_id: str = "",
) -> bool:
    """Target over tab complete — next over session live ya scorecard aage badh chuka ho."""
    hit = _OVER_RUN_FANCY_RE.search(str(fancy_name or ""))
    if not hit:
        return False
    target_over = int(hit.group(1))
    row = _lookup_upstream_session(selection_id, fancy_name, raw_by_sid, raw_by_name)
    if row and _excache_session_is_live(row):
        return False
    if _max_over_from_market(market_id, raw_cache) > target_over:
        return True
    sc = _scorecard_for_event(event_id)
    if sc:
        _runs, _wkts, overs = _innings_score_for_fancy(sc, fancy_name)
        if float(overs) > float(target_over):
            return True
    return False


def _session_settled_on_upstream(row: Optional[dict]) -> bool:
    """Suspended + zero odds — scrape pattern jab session band ho chuki ho."""
    if not isinstance(row, dict):
        return False
    if row.get("isDeclare"):
        return True
    rs = str(row.get("running_status") or row.get("remark") or "").strip().upper()
    yes, no = _session_row_odds(row)
    ps = _play_status_store(row.get("playStatus"))
    return ps == 0 or (rs == "SUSPENDED" and yes <= 0 and no <= 0)


def collect_inferred_completed_sessions_for_market(market_id: str, event_id: str = "") -> list[dict]:
    """
    Rolled-off / completed session declare — sirf jab match us over se aage ho.
    Live excache session par declare nahi (instant declare bug fix).
    """
    market_id = str(market_id or "").strip()
    if not market_id or not ping():
        return []

    db = get_db()
    if not event_id:
        match = db.matches.find_one({"marketId": market_id}, {"eventId": 1, "_id": 0})
        event_id = str((match or {}).get("eventId") or "")

    raw_cache = _fetch_raw_remote_odds_cache(market_id)
    raw_by_sid, raw_by_name = _build_session_index(raw_cache)

    out: list[dict] = []
    seen: set[str] = set()
    for bet in db.sports_bets.find(
        {
            "marketId": market_id,
            "status": "open",
            "isDeclare": {"$ne": True},
        },
        {"_id": 0, "selectionId": 1, "fancyName": 1, "sessionName": 1, "runnerName": 1, "betFor": 1, "oddsType": 1, "gtype": 1},
    ):
        if not is_fancy_market(
            str(bet.get("betFor") or ""),
            str(bet.get("oddsType") or ""),
            str(bet.get("gtype") or ""),
        ):
            continue
        sid = str(bet.get("selectionId") or "")
        if not sid or sid in seen:
            continue
        seen.add(sid)
        name = str(
            bet.get("fancyName")
            or bet.get("sessionName")
            or bet.get("runnerName")
            or ""
        ).strip()
        if not name:
            continue

        over_hit = _OVER_RUN_FANCY_RE.search(name)
        if over_hit:
            if not _over_session_completed(market_id, event_id, name, raw_cache, raw_by_sid, raw_by_name, sid):
                continue
        else:
            row = _lookup_upstream_session(sid, name, raw_by_sid, raw_by_name)
            if row and _excache_session_is_live(row):
                continue
            if row is not None and not _session_settled_on_upstream(row):
                continue

        mongo_doc = db.center_manual_fancy.find_one(
            {"$or": [{"fancyId": sid}, {"selectionId": sid}]},
            {"_id": 0},
        )
        excache_row = _lookup_upstream_session(sid, name, raw_by_sid, raw_by_name)
        decision_run = try_infer_session_decision_run(
            market_id,
            name,
            event_id,
            excache_row=excache_row,
            mongo_doc=mongo_doc if isinstance(mongo_doc, dict) else None,
        )
        if decision_run is None:
            continue
        out.append({
            "selectionId": sid,
            "fancyId": sid,
            "session_id": sid,
            "marketId": market_id,
            "eventId": event_id,
            "fancyName": name,
            "sessionName": name,
            "session_name": name,
            "sessionNames": [name],
            "isDeclare": True,
            "decisionRun": int(decision_run),
            "declareSource": "scorecard_complete",
            "gtype": (mongo_doc or {}).get("gtype") or bet.get("gtype") or "fancy",
            "fancyType": (mongo_doc or {}).get("fancyType") or bet.get("gtype") or "Normal",
        })
    return out


def _merge_sessions_for_match_list(live_sessions: list[dict], market_id: str) -> list[dict]:
    """Keep declared fancies that rolled off live excache."""
    if not ping():
        return live_sessions
    db = get_db()
    merged = list(live_sessions)
    known = {
        _norm_fancy_name(s.get("session_name") or s.get("sessionName") or s.get("fancyName") or "")
        for s in merged
    }
    known_sids = {
        str(s.get("session_id") or s.get("Selection_id") or s.get("selectionId") or s.get("fancyId") or "")
        for s in merged
    }
    for doc in db.center_manual_fancy.find(
        {"marketId": str(market_id), "isDeclare": True},
        {"_id": 0},
    ):
        sid = str(doc.get("fancyId") or doc.get("selectionId") or doc.get("session_id") or "")
        name = str(doc.get("fancyName") or doc.get("sessionName") or "")
        norm = _norm_fancy_name(name)
        if sid and sid in known_sids:
            continue
        if norm and norm in known:
            continue
        if any(norm in k or k in norm for k in known if k and norm):
            continue
        merged.append(_session_row_from_fancy_doc(doc))
        if sid:
            known_sids.add(sid)
        if norm:
            known.add(norm)
    return merged


def _bookmaker_doc_from_cache(row: dict, market_id: str, event_id: str) -> dict:
    sel = row.get("selectionId") or row.get("selectionid") or row.get("bookmakerSelectionId")
    return {
        "bookmakerId": f"bm-{market_id}-{sel}",
        "marketId": str(market_id),
        "eventId": str(event_id),
        "selectionId": sel,
        "bookmakerSelectionId": sel,
        "runnerName": row.get("team_name") or row.get("runnerName") or "",
        "back": row.get("lgaai") or row.get("back") or 0,
        "lay": row.get("khaai") or row.get("lay") or 0,
        "backPrize": row.get("lgaai") or row.get("backPrize") or 0,
        "backSize": row.get("backSize") or row.get("lgaai") or 0,
        "layPrize": row.get("khaai") or row.get("layPrize") or 0,
        "laySize": row.get("laySize") or row.get("khaai") or 0,
        "status": str(row.get("status") or "ACTIVE").lower(),
        "source": "excache",
    }


def _bookmaker_list_for_match(team_rows: list[dict]) -> list[dict]:
    out = []
    for row in team_rows:
        sel = row.get("selectionId") or row.get("selectionid")
        try:
            idx = int(sel) - 1
        except (TypeError, ValueError):
            idx = len(out)
        while len(out) <= idx:
            out.append({})
        out[idx] = {
            "backPrize": row.get("lgaai") or row.get("backPrize") or 0,
            "backSize": row.get("backSize") or row.get("lgaai") or 0,
            "layPrize": row.get("khaai") or row.get("layPrize") or 0,
            "laySize": row.get("laySize") or row.get("khaai") or 0,
            "runnerName": row.get("team_name") or row.get("runnerName") or "",
            "selectionId": sel,
        }
    return out


def sync_market_cache_to_mongo(market_id: str, event_id: str = "") -> dict:
    """Excache → center_manual_fancy + center_manual_bookmaker + matches.sessionList."""
    market_id = str(market_id or "").strip()
    if not market_id or not ping():
        return {}

    db = get_db()
    if not event_id:
        match = db.matches.find_one({"marketId": market_id}, {"eventId": 1, "_id": 0})
        event_id = str((match or {}).get("eventId") or "")

    cache = fetch_odds_cache(market_id)
    sessions = cache.get("session") or []
    teams = cache.get("team_data") or cache.get("teamData") or []

    if event_id:
        sessions = [s for s in sessions if str(s.get("eventId") or event_id) == str(event_id)] or sessions

    for row in sessions:
        doc = _session_doc_from_cache(row, market_id, event_id)
        key = doc.get("fancyId") or doc.get("session_id")
        if not key:
            continue
        existing = db.center_manual_fancy.find_one({"fancyId": key}, {"_id": 0})
        doc = _merge_fancy_declare_state(doc, existing, row)
        db.center_manual_fancy.update_one({"fancyId": key}, {"$set": doc}, upsert=True)

    for row in teams:
        doc = _bookmaker_doc_from_cache(row, market_id, event_id)
        db.center_manual_bookmaker.update_one(
            {"marketId": market_id, "selectionId": doc.get("selectionId")},
            {"$set": doc},
            upsert=True,
        )

    if sessions or teams:
        upd: dict[str, Any] = {}
        if sessions:
            merged_sessions = _merge_sessions_for_match_list(sessions, market_id)
            upd["sessionList"] = merged_sessions
            upd["fancyList"] = merged_sessions
        if teams:
            upd["bookmakerList"] = _bookmaker_list_for_match(teams)
            upd["teamData"] = json.dumps(teams)
        if upd:
            db.matches.update_one({"marketId": market_id}, {"$set": upd})

    return cache


def _query_market_docs(collection: str, payload: dict) -> list[dict]:
    payload = payload or {}
    q: dict = {}
    if payload.get("marketId"):
        q["marketId"] = str(payload["marketId"])
    if payload.get("eventId"):
        q["eventId"] = str(payload["eventId"])
    rows = []
    for doc in get_db()[collection].find(q):
        row = copy.deepcopy(doc)
        row.pop("password", None)
        if collection == "center_manual_fancy":
            rows.append(_normalize_session_row(row))
        else:
            row.pop("_id", None)
            row.setdefault("session_name", row.get("sessionName") or row.get("fancyName") or "")
            rows.append(row)
    return rows


def _market_ids_for_event(event_id: str) -> list[str]:
    if not event_id or not ping():
        return []
    db = get_db()
    ids: list[str] = []
    for doc in db.matches.find({"eventId": str(event_id)}, {"marketId": 1, "_id": 0}):
        mid = str(doc.get("marketId") or "")
        if mid:
            ids.append(mid)
    return ids


def _apply_session_filters(rows: list[dict], payload: dict) -> list[dict]:
    payload = payload or {}
    out = rows
    if "isDeclare" in payload:
        want = bool(payload.get("isDeclare"))
        out = [r for r in out if bool(r.get("isDeclare")) == want]
    if "isCancel" in payload:
        want = bool(payload.get("isCancel"))
        out = [r for r in out if bool(r.get("isCancel")) == want]
    return out


def _is_fancy_bet(bet: dict) -> bool:
    return is_fancy_market(
        str(bet.get("betFor") or ""),
        str(bet.get("oddsType") or ""),
        str(bet.get("gtype") or bet.get("marketKind") or ""),
    )


def get_cp_sessions_from_bets(payload: dict) -> list[dict]:
    """SessionDecision — sirf woh fancies jin par sports_bets (open) hain."""
    payload = payload or {}
    event_id = str(payload.get("eventId") or "")
    market_id = str(payload.get("marketId") or "")
    if not ping():
        return []

    db = get_db()
    q: dict = {}
    if market_id:
        q["marketId"] = market_id
    if event_id:
        q["eventId"] = event_id

    groups: dict[str, dict] = {}
    for bet in db.sports_bets.find(q, {"_id": 0}):
        if not _is_fancy_bet(bet):
            continue
        sid = str(bet.get("selectionId") or "")
        if not sid:
            continue
        mid = str(bet.get("marketId") or market_id)
        eid = str(bet.get("eventId") or event_id)
        name = (
            bet.get("fancyName")
            or bet.get("sessionName")
            or bet.get("runnerName")
            or bet.get("marketName")
            or ""
        )

        if sid not in groups:
            groups[sid] = {
                "selectionId": sid,
                "Selection_id": sid,
                "session_id": sid,
                "fancyId": sid,
                "fancyName": name,
                "session_name": name,
                "sessionName": name,
                "marketId": mid,
                "eventId": eid,
                "commPerm": "YES",
                "com_perm": "YES",
                "gtype": bet.get("gtype") or "fancy",
                "fancyType": bet.get("fancyType") or "Normal",
                "isDeclare": bool(bet.get("isDeclare")),
                "isCancel": str(bet.get("status") or "").lower() == "cancelled",
                "decisionRun": bet.get("decisionRun"),
                "betCount": 0,
                "totalStake": 0.0,
                "openBetCount": 0,
                "bets": [],
                "source": "sports_bets",
            }

        row = groups[sid]
        row["betCount"] += 1
        row["totalStake"] = round(float(row["totalStake"]) + float(bet.get("stake") or 0), 2)
        if str(bet.get("status") or "open").lower() == "open" and not bet.get("isDeclare"):
            row["openBetCount"] += 1
            row["isDeclare"] = False
        if bet.get("decisionRun") is not None:
            row["decisionRun"] = bet.get("decisionRun")
        if bet.get("isDeclare"):
            row["isDeclare"] = True
        row["bets"].append({
            "betId": bet.get("betId"),
            "userId": bet.get("userId"),
            "stake": bet.get("stake"),
            "odds": bet.get("odds"),
            "betType": bet.get("betType"),
            "run": bet.get("run"),
            "status": bet.get("status"),
            "isDeclare": bet.get("isDeclare"),
            "createdAt": bet.get("createdAt"),
        })

    for sid, row in groups.items():
        fancy = db.center_manual_fancy.find_one(
            {"$or": [{"fancyId": sid}, {"Selection_id": sid}, {"session_id": sid}]},
            {"_id": 0},
        )
        if fancy:
            for key in (
                "runsYes", "runsNo", "oddsYes", "oddsNo", "playStatus", "priority",
                "maximumAmount", "max", "running_status", "remark", "gtype", "fancyType",
            ):
                if fancy.get(key) is not None:
                    row[key] = fancy[key]
            if fancy.get("decisionRun") is not None and row.get("decisionRun") is None:
                row["decisionRun"] = fancy.get("decisionRun")
            if fancy.get("isDeclare"):
                row["isDeclare"] = True
            if fancy.get("isCancel"):
                row["isCancel"] = True
            row["commPerm"] = str(fancy.get("commPerm") or fancy.get("com_perm") or row["commPerm"])
            row["com_perm"] = row["commPerm"]

    rows = [_normalize_session_row(r) for r in groups.values()]
    rows.sort(key=lambda r: str(r.get("fancyName") or ""))
    return _apply_session_filters(rows, payload)


def _bet_stats_for_selection(
    db, sid: str, event_id: str = "", market_id: str = ""
) -> dict:
    q: dict = {"selectionId": sid}
    if market_id:
        q["marketId"] = market_id
    if event_id:
        q["eventId"] = event_id
    fancy_bets = [b for b in db.sports_bets.find(q, {"_id": 0}) if _is_fancy_bet(b)]
    open_bets = [
        b for b in fancy_bets
        if str(b.get("status") or "open").lower() == "open" and not b.get("isDeclare")
    ]
    decision_run = None
    is_declare = False
    for bet in fancy_bets:
        if bet.get("decisionRun") is not None:
            decision_run = bet.get("decisionRun")
        if bet.get("isDeclare") or str(bet.get("status") or "").lower() == "settled":
            is_declare = True
    return {
        "betCount": len(fancy_bets),
        "openBetCount": len(open_bets),
        "totalStake": round(sum(float(b.get("stake") or 0) for b in fancy_bets), 2),
        "isDeclare": is_declare,
        "decisionRun": decision_run,
        "hasBets": len(fancy_bets) > 0,
    }


def _enrich_sessions_with_bets(rows: list[dict], payload: dict) -> list[dict]:
    """Live sessions par sports_bets + center_manual_fancy declare status merge karo."""
    if not ping():
        return rows
    db = get_db()
    payload = payload or {}
    event_id = str(payload.get("eventId") or "")
    market_id = str(payload.get("marketId") or "")
    out: list[dict] = []
    for row in rows:
        doc = _normalize_session_row(row)
        sid = str(doc.get("selectionId") or "")
        fancy = db.center_manual_fancy.find_one(
            {"$or": [{"fancyId": sid}, {"Selection_id": sid}, {"session_id": sid}]},
            {"_id": 0},
        ) if sid else None
        if fancy:
            if fancy.get("isDeclare"):
                doc["isDeclare"] = True
            if fancy.get("decisionRun") is not None:
                doc["decisionRun"] = fancy.get("decisionRun")
            if fancy.get("isCancel"):
                doc["isCancel"] = True
            doc["commPerm"] = str(fancy.get("commPerm") or fancy.get("com_perm") or doc.get("commPerm") or "YES")
            doc["com_perm"] = doc["commPerm"]
            if fancy.get("declareUserDetails"):
                doc["declareUserDetails"] = fancy.get("declareUserDetails")

        stats = _bet_stats_for_selection(
            db, sid, event_id or str(doc.get("eventId") or ""), market_id or str(doc.get("marketId") or "")
        )
        doc.update(stats)
        if stats["isDeclare"]:
            doc["isDeclare"] = True
        if stats["decisionRun"] is not None and doc.get("decisionRun") is None:
            doc["decisionRun"] = stats["decisionRun"]
        out.append(doc)
    return out


def get_cp_sessions_for_database(payload: dict) -> list[dict]:
    """getSessionByDatabase — saari live sessions + declare/bet status (scraped site jaisa)."""
    rows = get_cp_sessions(payload)
    rows = _enrich_sessions_with_bets(rows, payload)
    return _apply_session_filters(rows, payload)


def get_cp_sessions(payload: dict) -> list[dict]:
    payload = payload or {}
    market_id = str(payload.get("marketId") or "")
    event_id = str(payload.get("eventId") or "")

    market_ids = [market_id] if market_id else _market_ids_for_event(event_id)
    for mid in market_ids:
        sync_market_cache_to_mongo(mid, event_id)

    rows = _query_market_docs("center_manual_fancy", payload)
    if not rows and market_ids:
        all_rows: list[dict] = []
        for mid in market_ids:
            cache = fetch_odds_cache(mid)
            meter = cache.get("meterKhadoSession") or []
            for r in (cache.get("session") or []) + meter:
                all_rows.append(_session_doc_from_cache(r, mid, event_id))
        rows = all_rows
    return [_normalize_session_row(r) for r in rows]


def get_cp_decision_logs(payload: dict) -> list[dict]:
    """sessionDecision dashboard — decision_logs + fancy audit + declared bets."""
    payload = payload or {}
    if not ping():
        return []

    db = get_db()
    q: dict = {}
    if payload.get("eventId"):
        q["eventId"] = str(payload["eventId"])
    if payload.get("marketId"):
        q["marketId"] = str(payload["marketId"])

    rows: list[dict] = []
    for doc in db.decision_logs.find(q).sort("createdAt", -1):
        row = copy.deepcopy(doc)
        row.pop("_id", None)
        pl = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        rows.append({
            "decisionId": row.get("logId") or row.get("decisionId") or str(row.get("_id") or ""),
            "status": row.get("status") or "completed",
            "type": row.get("type") or row.get("action") or "fancy_declare",
            "fancyName": pl.get("fancyName") or pl.get("sessionName") or row.get("fancyName") or "",
            "userName": row.get("userName") or row.get("username") or "system",
            "selectionId": str(pl.get("selectionId") or row.get("selectionId") or ""),
            "marketId": row.get("marketId"),
            "eventId": row.get("eventId"),
            "decisionRun": pl.get("decisionRun") or row.get("decisionRun"),
            "createdAt": row.get("createdAt"),
        })

    audit_q = {}
    if payload.get("marketId"):
        audit_q["marketId"] = str(payload["marketId"])
    for doc in db.center_fancy_audit.find(audit_q).sort("createdAt", -1):
        row = copy.deepcopy(doc)
        row.pop("_id", None)
        pl = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        rows.append({
            "decisionId": row.get("fancyId") or row.get("logId") or "",
            "status": "completed",
            "type": row.get("action") or "fancy_declare",
            "fancyName": pl.get("sessionName") or pl.get("fancyName") or "",
            "userName": row.get("userId") or "system",
            "selectionId": str(pl.get("selectionId") or row.get("fancyId") or ""),
            "marketId": row.get("marketId"),
            "eventId": payload.get("eventId"),
            "decisionRun": pl.get("decisionRun"),
            "createdAt": row.get("createdAt"),
        })

    bet_q: dict = {"decisionRun": {"$exists": True, "$ne": None}}
    if payload.get("marketId"):
        bet_q["marketId"] = str(payload["marketId"])
    for bet in db.sports_bets.find(bet_q).sort("createdAt", -1).limit(200):
        if payload.get("eventId"):
            mid = str(bet.get("marketId") or "")
            match = db.matches.find_one({"marketId": mid}, {"eventId": 1, "_id": 0})
            if str((match or {}).get("eventId") or "") != str(payload["eventId"]):
                continue
        rows.append({
            "decisionId": bet.get("betId") or "",
            "status": "completed",
            "type": "fancy_declare",
            "fancyName": bet.get("runnerName") or bet.get("sessionName") or "",
            "userName": bet.get("username") or bet.get("userId") or "client",
            "selectionId": str(bet.get("selectionId") or ""),
            "marketId": bet.get("marketId"),
            "eventId": payload.get("eventId"),
            "decisionRun": bet.get("decisionRun"),
            "createdAt": bet.get("createdAt"),
        })

    if payload.get("status") and str(payload["status"]) != "all":
        st = str(payload["status"])
        rows = [r for r in rows if str(r.get("status") or "") == st]
    if payload.get("type") and str(payload["type"]) != "all":
        tp = str(payload["type"])
        rows = [r for r in rows if str(r.get("type") or "") == tp]

    limit = int(payload.get("limit") or 200)
    return rows[:limit]


def _empty_runner_ex() -> dict:
    empty = {"price": None, "size": None}
    return {
        "availableToBack": [empty, empty, empty],
        "availableToLay": [empty, empty, empty],
    }


def _mongo_event_markets(event_id: str) -> dict:
    """Match Odds modal — excache empty ho to MongoDB marketList se fallback."""
    if not ping():
        return {}
    match = get_db().matches.find_one({"eventId": str(event_id)}, {"_id": 0, "marketList": 1})
    if not match:
        return {}
    markets: dict[str, dict] = {}
    for ml in match.get("marketList") or []:
        mtype = str(ml.get("marketType") or ml.get("marketId") or "Market")
        runners = []
        for sel in ml.get("selectionIdData") or []:
            runners.append({
                "selectionId": sel.get("selectionId"),
                "selectionName": sel.get("runnerName") or sel.get("selectionName") or "",
                "status": sel.get("status") or "ACTIVE",
                "ex": _empty_runner_ex(),
            })
        markets[mtype] = {
            "marketType": mtype,
            "marketId": ml.get("marketId"),
            "eventId": str(event_id),
            "runners": runners,
        }
    return markets


def fetch_event_markets(event_id: str) -> Any:
    """GET /v2/api/dataByEventId — other markets (Match Odds, Tied, Completed)."""
    event_id = str(event_id or "").strip()
    if not event_id:
        return {}

    raw = _fetch_json(f"{CACHE_EVENT_REMOTE}{event_id}")
    if isinstance(raw, dict):
        data = raw.get("data")
        if isinstance(data, dict) and data:
            return data
        if isinstance(data, list) and data:
            keyed: dict[str, dict] = {}
            for row in data:
                if not isinstance(row, dict):
                    continue
                key = str(row.get("marketType") or row.get("marketId") or len(keyed))
                keyed[key] = row
            if keyed:
                return keyed
    return _mongo_event_markets(event_id)


def _apply_declared_fancy_to_cache(cache: dict, market_id: str) -> dict:
    """WNP9 auto-declare → client excache session par isDeclare/decisionRun."""
    if not cache or not ping():
        return cache
    db = get_db()
    declared = list(db.center_manual_fancy.find(
        {"marketId": str(market_id), "isDeclare": True},
        {"_id": 0},
    ))
    if not declared:
        return cache

    by_sid: dict[str, dict] = {}
    by_name: dict[str, dict] = {}
    for doc in declared:
        for key in ("fancyId", "Selection_id", "session_id", "selectionId"):
            sid = str(doc.get(key) or "")
            if sid:
                by_sid[sid] = doc
        name = str(doc.get("fancyName") or doc.get("sessionName") or "").strip().lower()
        if name:
            by_name[name] = doc
            by_name[_norm_fancy_name(name)] = doc

    sessions = cache.get("session") or cache.get("sessionList") or []
    if not isinstance(sessions, list):
        return cache

    for row in sessions:
        if not isinstance(row, dict):
            continue
        sid = str(
            row.get("session_id")
            or row.get("Selection_id")
            or row.get("selectionId")
            or row.get("fancyId")
            or ""
        )
        name = str(row.get("session_name") or row.get("sessionName") or row.get("fancyName") or "").strip().lower()
        hit = by_sid.get(sid) or by_name.get(name) or by_name.get(_norm_fancy_name(name))
        if not hit:
            for dname, doc in by_name.items():
                if not dname:
                    continue
                n = _norm_fancy_name(name)
                if n and (n in dname or dname in n):
                    hit = doc
                    break
        if not hit:
            continue
        if _excache_session_is_live(row):
            continue
        row["isDeclare"] = True
        row["decisionRun"] = hit.get("decisionRun")
        row["playStatus"] = 0
        row["runsYes"] = 0
        row["runsNo"] = 0
        row["oddsYes"] = 0
        row["oddsNo"] = 0

    cache["session"] = sessions
    return cache


def _norm_fancy_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _inject_declared_fancy_sessions(cache: dict, market_id: str) -> dict:
    """Declared fancy jo live cache mein nahi — result dikhane ke liye inject."""
    if not ping():
        return cache
    db = get_db()
    sessions = list(cache.get("session") or [])
    known = {_norm_fancy_name(s.get("session_name") or s.get("sessionName") or "") for s in sessions}

    for doc in db.center_manual_fancy.find({"marketId": str(market_id), "isDeclare": True}, {"_id": 0}):
        name = str(doc.get("fancyName") or doc.get("sessionName") or "")
        norm = _norm_fancy_name(name)
        if not norm or norm in known:
            continue
        if any(norm in k or k in norm for k in known if k):
            continue
        sid = str(doc.get("fancyId") or doc.get("selectionId") or "")
        sessions.append({
            "session_name": name,
            "sessionName": name,
            "fancyName": name,
            "session_id": sid,
            "Selection_id": sid,
            "selectionId": sid,
            "isDeclare": True,
            "decisionRun": doc.get("decisionRun"),
            "playStatus": 0,
            "runsYes": 0,
            "runsNo": 0,
            "oddsYes": 0,
            "oddsNo": 0,
            "gtype": doc.get("gtype") or "fancy",
            "fancyType": doc.get("fancyType") or "Normal",
        })
        known.add(norm)

    cache["session"] = sessions
    return cache


def proxy_odds_json(path: str, query: str = "") -> Optional[bytes]:
    """GET /v2/api/* — oddsDataNew (market_id) ya dataByEventId (eventId)."""
    qs = parse_qs(query.lstrip("?"))
    path_lower = (path or "").lower()

    if "databyeventid" in path_lower:
        event_id = (qs.get("eventId") or qs.get("event_id") or [""])[0]
        if not event_id and "eventId=" in query:
            event_id = query.split("eventId=")[-1].split("&")[0]
        if not event_id:
            return None
        data = fetch_event_markets(str(event_id))
        return json.dumps({
            "message": "data fetched",
            "code": 0,
            "error": False,
            "data": data,
        }).encode()

    market_id = (qs.get("market_id") or qs.get("marketId") or [""])[0]
    if not market_id and "market_id=" in query:
        market_id = query.split("market_id=")[-1].split("&")[0]
    if not market_id:
        return None
    cache = fetch_odds_cache(str(market_id))
    if not cache:
        return json.dumps({"result": {}}).encode()
    return json.dumps({"result": cache}).encode()


def get_cp_bookmakers(payload: dict) -> list[dict]:
    payload = payload or {}
    market_id = str(payload.get("marketId") or "")
    event_id = str(payload.get("eventId") or "")
    if market_id:
        sync_market_cache_to_mongo(market_id, event_id)
    rows = _query_market_docs("center_manual_bookmaker", payload)
    if rows:
        return [_to_bookmaker_ui_row(r) for r in rows]
    cache = fetch_odds_cache(market_id) if market_id else {}
    teams = cache.get("team_data") or cache.get("teamData") or []
    return [_to_bookmaker_ui_row(_bookmaker_doc_from_cache(r, market_id, event_id)) for r in teams]


def enrich_cp_sport_data(match: dict, market_id: str = "", event_id: str = "") -> dict:
    """getSportDataByEventId — bookmakerList + live sessions attach karo."""
    row = copy.deepcopy(match)
    market_id = str(market_id or row.get("marketId") or "")
    event_id = str(event_id or row.get("eventId") or "")
    cache = sync_market_cache_to_mongo(market_id, event_id) if market_id else {}
    teams = cache.get("team_data") or cache.get("teamData") or []
    if teams:
        row["bookmakerList"] = _bookmaker_list_for_match(teams)
        row["teamData"] = teams
    elif row.get("team_data") and not row.get("teamData"):
        row["teamData"] = row["team_data"]
    sessions = cache.get("session") or []
    if sessions:
        row["sessionList"] = sessions
        row["fancyList"] = sessions
    if row.get("fancyStatus") is None:
        row["fancyStatus"] = True
    if row.get("oddsStatus") is None:
        row["oddsStatus"] = True
    if row.get("bookmakerRange") is None:
        row["bookmakerRange"] = 1
    return row
