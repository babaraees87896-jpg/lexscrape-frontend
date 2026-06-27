"""Auto-decision sync — read declare state from api.wnp9.pro, settle local MongoDB bets."""

from __future__ import annotations

import os
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
import time

import requests

from mongodb.centerpanel_api import _err, _ok
from mongodb.db import get_db
from mongodb.matches_api import get_match_list


def _load_wnp9_credentials() -> tuple[str, str, str]:
    """Env → working/secrets/wnp9.json → scrape default."""
    user = os.getenv("WNP9_USERNAME", "").strip()
    pwd = os.getenv("WNP9_PASSWORD", "").strip()
    api_base = os.getenv("WNP9_API_BASE", "").strip()
    if user and pwd:
        return user, pwd, api_base or "https://api.wnp9.pro/v1/"

    paths: list[Path] = []
    explicit = os.getenv("WNP9_SECRETS_FILE", "").strip()
    if explicit:
        paths.append(Path(explicit))
    here = Path(__file__).resolve()
    paths.extend([
        here.parents[2] / "secrets" / "wnp9.json",
        here.parents[1] / "secrets" / "wnp9.json",
    ])
    for path in paths:
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        user = str(data.get("username") or user or "OW1000").strip()
        pwd = str(data.get("password") or pwd or "").strip()
        api_base = str(data.get("apiBase") or api_base or "https://api.wnp9.pro/v1/").strip()
        if user and pwd:
            return user, pwd, api_base

    return user or "OW1000", pwd or "Ravi@82518", api_base or "https://api.wnp9.pro/v1/"


_WNP9_USER, _WNP9_PASS, _WNP9_API = _load_wnp9_credentials()
WNP9_API_BASE = _WNP9_API.rstrip("/") + "/"
WNP9_USERNAME = _WNP9_USER
WNP9_PASSWORD = _WNP9_PASS

_TOSS_PLACEHOLDER_IDS = frozenset({"", None})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _session_label(sess: dict) -> str:
    names = sess.get("sessionNames") or sess.get("sessionName")
    if isinstance(names, list) and names:
        return str(names[0] or "")
    return str(names or "")


def _sync_wnp9_fancy_declare(db, market_id: str, event_id: str, sess: dict) -> None:
    """WNP9 declared fancy → center_manual_fancy + matches.sessionList (UI ke liye)."""
    sid = str(sess.get("selectionId") or "")
    if not sid or sess.get("decisionRun") is None:
        return

    fancy_name = _session_label(sess)
    upd: dict[str, Any] = {
        "isDeclare": True,
        "decisionRun": sess.get("decisionRun"),
        "fancyId": sid,
        "Selection_id": sid,
        "session_id": sid,
        "selectionId": sid,
        "marketId": market_id,
        "eventId": str(event_id or ""),
        "fancyName": fancy_name,
        "sessionName": fancy_name,
        "session_name": fancy_name,
        "gtype": sess.get("gtype") or sess.get("fancyType") or "",
        "fancyType": sess.get("fancyType") or sess.get("gtype") or "",
        "declareSource": str(sess.get("declareSource") or "wnp9"),
        "updatedAt": _now(),
    }
    db.center_manual_fancy.update_one({"fancyId": sid}, {"$set": upd}, upsert=True)

    match = db.matches.find_one({"marketId": market_id}, {"_id": 0, "sessionList": 1, "fancyList": 1})
    if not match:
        return
    match_upd: dict[str, Any] = {}
    for key in ("sessionList", "fancyList"):
        lst = match.get(key)
        if not isinstance(lst, list):
            continue
        changed = False
        for item in lst:
            if not isinstance(item, dict):
                continue
            item_sid = str(item.get("selectionId") or item.get("selectionid") or "")
            if item_sid == sid:
                item["isDeclare"] = True
                item["decisionRun"] = sess.get("decisionRun")
                changed = True
        if changed:
            match_upd[key] = lst
    if match_upd:
        db.matches.update_one({"marketId": market_id}, {"$set": match_upd})


class LocalDeclareClient:
    """WNP9 unavailable — declare state from local MongoDB + staff APIs."""

    def __init__(self) -> None:
        self.db = get_db()

    def post(self, endpoint: str, body: Optional[dict] = None) -> dict:
        body = body or {}
        ep = endpoint.lstrip("/").split("?")[0]
        if ep.endswith("sportByMarketId"):
            mid = str(body.get("marketId") or "")
            from mongodb.matches_api import fetch_match_by_market_id

            doc = fetch_match_by_market_id(mid, prefer_live=True) or {}
            if not doc:
                doc = self.db.matches.find_one({"marketId": mid}) or {}
            return {"error": False, "data": doc}
        if ep.endswith("getSessionList"):
            from mongodb.centerpanel_cache import get_cp_sessions_for_database

            rows = get_cp_sessions_for_database(body)
            return {"error": False, "data": rows}
        if ep.endswith("matchList"):
            status = str(body.get("status") or "INPLAY")
            rows = get_match_list(
                {"status": status, **{k: v for k, v in body.items() if k != "status"}},
                for_admin=True,
                prefer_live=True,
            )
            return {"error": False, "data": rows}
        return {"error": True, "message": f"Unsupported local endpoint: {ep}"}


class Wnp9Client:
    """Minimal client for wnp9 staff read/declare APIs."""

    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.proxies = {"http": None, "https": None}
        self.session.headers.update({
            "Content-Type": "application/json",
            "User-Agent": "ex99-auto-decision/1.0",
        })

    def login(self) -> tuple[bool, str]:
        if not WNP9_USERNAME or not WNP9_PASSWORD:
            return False, "WNP9 credentials not configured"
        last_err = ""
        for attempt in range(3):
            try:
                resp = self.session.post(
                    f"{WNP9_API_BASE}user/login",
                    json={"username": WNP9_USERNAME, "password": WNP9_PASSWORD},
                    timeout=45,
                )
                data = resp.json()
                token = data.get("token")
                if token and not data.get("error"):
                    self.session.headers["Authorization"] = f"Bearer {token}"
                    return True, "OK"
                last_err = str(data.get("message") or "WNP9 login failed")
            except Exception as exc:
                last_err = f"WNP9 login failed: {exc}"
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
        return False, last_err

    def post(self, endpoint: str, body: Optional[dict] = None) -> dict:
        try:
            resp = self.session.post(
                f"{WNP9_API_BASE}{endpoint.lstrip('/')}",
                json=body or {},
                timeout=45,
            )
            data = resp.json()
            return data if isinstance(data, dict) else {"error": True, "message": "Invalid response"}
        except Exception as exc:
            return {"error": True, "message": str(exc)}


def _valid_winner_id(val: Any) -> bool:
    if val in _TOSS_PLACEHOLDER_IDS:
        return False
    return str(val).strip() != ""


def _valid_toss_winner_id(val: Any) -> bool:
    """Toss winner — client UI ids 10000/20000 ya bookmaker 1/2."""
    if val in _TOSS_PLACEHOLDER_IDS:
        return False
    s = str(val).strip()
    return s in ("10000", "20000", "1", "2")


def _local_open_market_ids(db) -> set[str]:
    ids = db.sports_bets.distinct("marketId", {"status": "open"})
    return {str(mid) for mid in ids if mid}


def _market_ids_for_event(db, event_id: str) -> set[str]:
    if not event_id:
        return set()
    ids: set[str] = set()
    for doc in db.matches.find({"eventId": str(event_id)}, {"marketId": 1}):
        mid = doc.get("marketId")
        if mid:
            ids.add(str(mid))
    if not ids:
        for doc in db.sports_bets.find({"eventId": str(event_id), "status": "open"}, {"marketId": 1}):
            mid = doc.get("marketId")
            if mid:
                ids.add(str(mid))
    return ids


def _local_has_open_fancy(db, market_id: str, selection_id: str) -> bool:
    return db.sports_bets.count_documents({
        "marketId": market_id,
        "selectionId": selection_id,
        "status": "open",
        "isDeclare": {"$ne": True},
    }) > 0


def _local_has_open_odds(db, market_id: str, *, toss_only: bool = False) -> bool:
    q: dict[str, Any] = {
        "marketId": market_id,
        "status": "open",
        "isDeclare": {"$ne": True},
    }
    if toss_only:
        q["$or"] = [{"betFor": "toss"}, {"oddsType": "toss"}]
    else:
        q["betFor"] = {"$nin": ["fancy", "session", "toss"]}
        q["oddsType"] = {"$ne": "toss"}
    return db.sports_bets.count_documents(q) > 0


def _sync_match_fields(db, market_id: str, remote: dict) -> None:
    if not remote:
        return
    has_open_bets = db.sports_bets.count_documents(
        {"marketId": market_id, "status": "open"},
        limit=1,
    ) > 0
    upd: dict[str, Any] = {}
    for key in (
        "wonTeamBookmakerSelectionId",
        "wonTeamBetfairSelectionId",
        "wonTeamName",
        "isDeclare",
        "status",
        "eventId",
        "matchName",
        "teamData",
    ):
        if key not in remote or remote.get(key) in (None, ""):
            continue
        if key == "status" and has_open_bets:
            remote_status = str(remote.get("status") or "").upper()
            if remote_status == "COMPLETED":
                continue
        upd[key] = remote.get(key)
    if upd:
        db.matches.update_one({"marketId": market_id}, {"$set": upd}, upsert=False)


def _collect_market_ids(client: Wnp9Client, db, event_id: str = "") -> set[str]:
    ids = _local_open_market_ids(db)
    if event_id:
        ids |= _market_ids_for_event(db, event_id)

    for status in ("inplay", "completed"):
        body: dict[str, Any] = {"status": status}
        if event_id:
            body["eventId"] = event_id
        live = client.post("sports/matchList", body)
        rows = live.get("data") or []
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                if event_id and str(row.get("eventId") or "") != str(event_id):
                    continue
                mid = row.get("marketId")
                if mid:
                    ids.add(str(mid))
    return {mid for mid in ids if mid}


_last_market_auto: dict[str, float] = {}
_MARKET_AUTO_THROTTLE_SEC = max(0.5, float(os.getenv("EX99_MARKET_AUTO_SEC", "1")))


def _session_row_from_wnp9(sess: dict, market_id: str, event_id: str) -> dict:
    sid = str(sess.get("selectionId") or "")
    name = _session_label(sess)
    return {
        "selectionId": sid,
        "fancyId": sid,
        "session_id": sid,
        "marketId": market_id,
        "eventId": event_id,
        "fancyName": name,
        "sessionName": name,
        "session_name": name,
        "sessionNames": sess.get("sessionNames") or [name],
        "isDeclare": True,
        "decisionRun": sess.get("decisionRun"),
        "declareSource": "wnp9",
        "gtype": sess.get("gtype") or sess.get("fancyType") or "fancy",
        "fancyType": sess.get("fancyType") or sess.get("gtype") or "Normal",
    }


def _declared_sessions_for_auto_settle(
    client: Any,
    market_id: str,
    event_id: str,
) -> list[dict]:
    """Scrape jaisa — excache upstream isDeclare + WNP9 getSessionList."""
    from mongodb.centerpanel_cache import collect_declared_sessions_for_market

    by_sid: dict[str, dict] = {}
    for sess in collect_declared_sessions_for_market(market_id, event_id):
        if not isinstance(sess, dict) or not sess.get("isDeclare"):
            continue
        sid = str(sess.get("selectionId") or sess.get("fancyId") or "")
        if sid and sess.get("decisionRun") is not None:
            by_sid[sid] = sess

    if isinstance(client, Wnp9Client):
        resp = client.post("sports/getSessionList", {"marketId": market_id})
        rows = resp.get("data") if isinstance(resp, dict) else None
        if isinstance(rows, list):
            for sess in rows:
                if not isinstance(sess, dict) or not sess.get("isDeclare"):
                    continue
                if sess.get("decisionRun") is None:
                    continue
                sid = str(sess.get("selectionId") or "")
                if sid:
                    by_sid[sid] = _session_row_from_wnp9(sess, market_id, event_id)

    from mongodb.centerpanel_cache import collect_inferred_completed_sessions_for_market

    for sess in collect_inferred_completed_sessions_for_market(market_id, event_id):
        sid = str(sess.get("selectionId") or sess.get("fancyId") or "")
        if sid and sid not in by_sid and sess.get("decisionRun") is not None:
            by_sid[sid] = sess

    return list(by_sid.values())


def _apply_market_auto_decisions(
    db,
    market_id: str,
    remote: dict,
    session_user: Optional[dict],
    client: Any = None,
) -> dict[str, Any]:
    """Settle open local bets when WNP9/excache declare state matches scrape."""
    from mongodb.auto_decision_settings import is_bookmaker_auto_enabled, is_fancy_auto_enabled
    from mongodb.bluewin_decision import mongo_bw_odds_decision, mongo_bw_session_decision
    from mongodb.centerpanel_cache import infer_bookmaker_winner

    match_decisions: list[dict] = []
    toss_decisions: list[dict] = []
    session_decisions: list[dict] = []
    errors: list[str] = []
    synced_fancy = 0

    if remote:
        _sync_match_fields(db, market_id, remote)

    event_id = str(remote.get("eventId") or "")
    won_team = remote.get("wonTeamBookmakerSelectionId") or remote.get("wonTeamBetfairSelectionId")
    if not _valid_winner_id(won_team):
        inferred_bm = infer_bookmaker_winner(market_id, event_id)
        if inferred_bm:
            won_team = inferred_bm
            db.matches.update_one(
                {"marketId": market_id},
                {"$set": {"wonTeamBookmakerSelectionId": inferred_bm, "updatedAt": _now()}},
            )
            remote = {**remote, "wonTeamBookmakerSelectionId": inferred_bm}
    if (
        is_bookmaker_auto_enabled(db, market_id)
        and _valid_winner_id(won_team)
        and _local_has_open_odds(db, market_id, toss_only=False)
    ):
        payload = {
            "marketId": market_id,
            "decisionSelectionId": won_team,
            "eventId": remote.get("eventId") or "",
        }
        result = mongo_bw_odds_decision(payload, session_user)
        if result.get("error"):
            errors.append(f"{market_id} match: {result.get('message')}")
        else:
            match_decisions.append({
                "marketId": market_id,
                "decisionSelectionId": won_team,
                "settledBets": (result.get("data") or {}).get("settledBets", 0),
            })

    sessions = _declared_sessions_for_auto_settle(client, market_id, event_id)
    for sess in sessions:
        if not isinstance(sess, dict) or not sess.get("isDeclare"):
            continue
        selection_id = str(sess.get("selectionId") or sess.get("fancyId") or "")
        if not selection_id or sess.get("decisionRun") is None:
            continue
        _sync_wnp9_fancy_declare(db, market_id, event_id, sess)
        synced_fancy += 1

    if is_fancy_auto_enabled(db, market_id):
        for sess in sessions:
            if not isinstance(sess, dict) or not sess.get("isDeclare"):
                continue
            selection_id = str(sess.get("selectionId") or sess.get("fancyId") or "")
            if not selection_id or sess.get("decisionRun") is None:
                continue
            if not _local_has_open_fancy(db, market_id, selection_id):
                continue

            payload = {
                "marketId": market_id,
                "selectionId": selection_id,
                "decisionRun": sess.get("decisionRun"),
                "eventId": event_id,
                "gtype": sess.get("gtype") or sess.get("fancyType") or "",
                "fancyType": sess.get("fancyType") or sess.get("gtype") or "",
            }
            names = sess.get("sessionNames") or sess.get("sessionName") or sess.get("fancyName")
            if isinstance(names, list) and names:
                payload["fancyName"] = names[0]
            elif isinstance(names, str):
                payload["fancyName"] = names

            result = mongo_bw_session_decision(payload, session_user)
            if result.get("error"):
                errors.append(f"{market_id} session {selection_id[:8]}: {result.get('message')}")
            else:
                session_decisions.append({
                    "marketId": market_id,
                    "selectionId": selection_id,
                    "decisionRun": sess.get("decisionRun"),
                    "settledBets": (result.get("data") or {}).get("settledBets", 0),
                })

    total_settled = (
        sum(x.get("settledBets", 0) for x in match_decisions)
        + sum(x.get("settledBets", 0) for x in toss_decisions)
        + sum(x.get("settledBets", 0) for x in session_decisions)
    )
    return {
        "matchDecisions": match_decisions,
        "tossDecisions": toss_decisions,
        "sessionDecisions": session_decisions,
        "totalSettledBets": total_settled,
        "syncedFancy": synced_fancy,
        "errors": errors,
    }


_SETTLE_POLL_THROTTLE_SEC = max(5, int(os.getenv("EX99_AUTO_SETTLE_POLL_SEC", "20")))
_last_market_settle_at: dict[str, float] = {}
_wnp9_client_until = 0.0
_wnp9_cached_client: Any = None
_wnp9_fail_until = 0.0


def _auto_decision_client() -> Any:
    """WNP9 login cache — har poll par retry se worker slow na ho."""
    global _wnp9_client_until, _wnp9_cached_client, _wnp9_fail_until
    now = time.time()
    if _wnp9_cached_client is not None and now < _wnp9_client_until:
        return _wnp9_cached_client
    if now < _wnp9_fail_until:
        return LocalDeclareClient()

    client: Any = Wnp9Client()
    ok, msg = client.login()
    if ok:
        _wnp9_cached_client = client
        _wnp9_client_until = now + max(60, int(os.getenv("EX99_WNP9_LOGIN_CACHE_SEC", "300")))
        return client

    _wnp9_cached_client = None
    _wnp9_client_until = 0.0
    _wnp9_fail_until = now + max(30, int(os.getenv("EX99_WNP9_LOGIN_RETRY_SEC", "120")))
    print(f"[auto-decision] {msg} — using local MongoDB declare state")
    return LocalDeclareClient()


def maybe_auto_settle_market(market_id: str, event_id: str = "") -> int:
    """Throttled single-market settle — worker ke saath; live session par infer nahi."""
    market_id = str(market_id or "").strip()
    if not market_id:
        return 0
    now = time.time()
    last = _last_market_settle_at.get(market_id, 0.0)
    if now - last < _SETTLE_POLL_THROTTLE_SEC:
        return 0
    _last_market_settle_at[market_id] = now

    db = get_db()
    client = _auto_decision_client()

    if not event_id:
        match = db.matches.find_one({"marketId": market_id}, {"eventId": 1, "_id": 0})
        event_id = str((match or {}).get("eventId") or "")

    remote_resp = client.post("sports/sportByMarketId", {"marketId": market_id})
    remote = remote_resp.get("data") if isinstance(remote_resp.get("data"), dict) else {}
    if not event_id:
        event_id = str(remote.get("eventId") or "")

    applied = _apply_market_auto_decisions(db, market_id, remote, None, client)
    return int(applied.get("totalSettledBets") or 0)


def run_auto_decision_sync(
    session_user: Optional[dict] = None,
    event_id: str = "",
) -> dict:
    """
    Fetch declare state from wnp9 and apply local settlement.

    - Match/bookmaker: decision/oddsDecision when sportByMarketId has winner ids.
    - Fancy/session: decision/sessionDecision when getSessionList marks isDeclare.
    - Toss: manual staff declare only (no auto-settle).
    """
    client = _auto_decision_client()

    db = get_db()
    market_ids = _collect_market_ids(client, db, event_id=str(event_id or ""))
    if not market_ids:
        return _ok({
            "processedMarkets": 0,
            "matchDecisions": [],
            "tossDecisions": [],
            "sessionDecisions": [],
        }, "No markets to process")

    match_decisions: list[dict] = []
    toss_decisions: list[dict] = []
    session_decisions: list[dict] = []
    errors: list[str] = []
    synced_fancy = 0

    for market_id in sorted(market_ids):
        remote_resp = client.post("sports/sportByMarketId", {"marketId": market_id})
        remote = remote_resp.get("data") if isinstance(remote_resp.get("data"), dict) else {}
        applied = _apply_market_auto_decisions(db, market_id, remote, session_user, client)
        match_decisions.extend(applied.get("matchDecisions") or [])
        toss_decisions.extend(applied.get("tossDecisions") or [])
        session_decisions.extend(applied.get("sessionDecisions") or [])
        synced_fancy += int(applied.get("syncedFancy") or 0)
        errors.extend(applied.get("errors") or [])

    total_settled = (
        sum(x.get("settledBets", 0) for x in match_decisions)
        + sum(x.get("settledBets", 0) for x in toss_decisions)
        + sum(x.get("settledBets", 0) for x in session_decisions)
    )
    data = {
        "processedMarkets": len(market_ids),
        "matchDecisions": match_decisions,
        "tossDecisions": toss_decisions,
        "sessionDecisions": session_decisions,
        "totalSettledBets": total_settled,
        "syncedFancy": synced_fancy,
        "errors": errors,
    }
    if total_settled == 0 and not errors and synced_fancy:
        message = f"Synced {synced_fancy} fancy result(s) from WNP9"
    elif total_settled == 0 and not errors:
        message = "No pending local bets matched wnp9 declare state"
    elif errors:
        message = f"Auto decision completed with {len(errors)} warning(s)"
    else:
        message = f"Auto decision successful — {total_settled} bet(s) settled"
    return _ok(data, message)
