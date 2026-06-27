#!/usr/bin/env python3
"""Sports poll — tablist + cricket/football highlights (casino se alag)."""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from cf_session import api_login, fetch_match_info, fetch_match_odds, fetch_sport_api
from decrypt_cryptojs import maybe_decrypt
from scrape_vcasino import DEFAULT_PASSPHRASE
from sports import SPORT_APIS, match_file_path, match_json_path, scorecard_file_path, scorecard_json_path, slugify, sport_file_path, sport_json_path

OUT_DIR = Path("output/sport")
TAB_OUT = OUT_DIR / "tablist.json"
ALL_OUT = OUT_DIR / "all_sports.json"
INTERVAL = float(os.environ.get("SPORT_POLL_INTERVAL", "30"))
SPORT_DELAY = float(os.environ.get("SPORT_DELAY", "0.15"))
MATCH_DETAIL = os.environ.get("SPORT_MATCH_DETAIL", "1") == "1"
MATCH_ALL = os.environ.get("SPORT_MATCH_ALL", "1") == "1"
SCORECARD = os.environ.get("SPORT_SCORECARD", "0") == "1"
MATCH_DETAIL_LIMIT = int(os.environ.get("SPORT_MATCH_LIMIT", "0"))  # 0 = sab matches
DEFAULT_USER = os.environ.get("LOGIN_USER", "Demo9304")
DEFAULT_PASS = os.environ.get("LOGIN_PASS", "Demo1234")
REFRESH_EVERY = int(os.environ.get("COOKIE_REFRESH_SEC", "900"))


def now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def fetch_tablist(session) -> dict:
    raw = fetch_sport_api(session, SPORT_APIS["tablist"], {})
    data = maybe_decrypt(raw, DEFAULT_PASSPHRASE)
    return data if isinstance(data, dict) else {"raw": data}


def fetch_highlights(session, eid: int) -> dict:
    raw = fetch_sport_api(session, SPORT_APIS["highlights"], {"etid": eid})
    data = maybe_decrypt(raw, DEFAULT_PASSPHRASE)
    return data if isinstance(data, dict) else {"raw": data}


def fetch_match(session, eid: int, gmid) -> dict:
    info_raw = fetch_match_info(session, eid, gmid)
    odds_raw = fetch_match_odds(session, eid, gmid)
    info = maybe_decrypt(info_raw, DEFAULT_PASSPHRASE)
    odds = maybe_decrypt(odds_raw, DEFAULT_PASSPHRASE)
    if not isinstance(info, dict):
        info = {"raw": info}
    if not isinstance(odds, dict):
        odds = {"raw": odds}
    info_row = (info.get("data") or [None])[0] if isinstance(info.get("data"), list) else info.get("data")
    return {
        "success": bool(info.get("success") and odds.get("success")),
        "msg": odds.get("msg") or info.get("msg"),
        "status": odds.get("status") or info.get("status"),
        "info": info_row,
        "gamedetail": info,
        "data": odds.get("data"),
    }


def match_detail_limit(total: int) -> int:
    """0 = unlimited — har sport ke saare matches ka gamedataPrivate fetch."""
    if MATCH_ALL or MATCH_DETAIL_LIMIT <= 0:
        return total
    return min(total, MATCH_DETAIL_LIMIT)


def sport_tabs(tablist: dict) -> list[dict]:
    rows = tablist.get("data") or []
    return [r for r in rows if r.get("tab") and r.get("active")]


def poll_once(session, user: str) -> dict:
    tablist = fetch_tablist(session)
    tabs = sport_tabs(tablist) if tablist.get("success") else []
    highlights: dict[str, dict] = {}
    matches: dict[str, dict] = {}

    print(f"  tablist: {len(tabs)} active sports", flush=True)

    for tab in tabs:
        eid = tab.get("eid")
        ename = tab.get("ename", str(eid))
        slug = slugify(ename)
        if eid is None:
            continue
        try:
            hl = fetch_highlights(session, int(eid))
            highlights[str(eid)] = {
                "eid": eid,
                "ename": ename,
                "slug": slug,
                **hl,
            }
            t1 = (hl.get("data") or {}).get("t1") or []
            print(f"  {ename} (eid={eid}): {len(t1)} matches", flush=True)

            sport_payload = {
                "_meta": {
                    "eid": eid,
                    "ename": ename,
                    "mode": "sport",
                    "updated_at": now_str(),
                    "note": "highlights",
                    "url": sport_json_path(eid),
                    "slug": slug,
                },
                **hl,
            }
            write_json(OUT_DIR / f"{slug}.json", sport_payload)
            write_json(sport_file_path(OUT_DIR, eid), sport_payload)

            if MATCH_DETAIL and hl.get("success") and t1:
                limit = match_detail_limit(len(t1))
                print(f"    fetching {limit}/{len(t1)} match details (etid={eid})...", flush=True)
                for m in t1[:limit]:
                    gmid = m.get("gmid")
                    if gmid is None:
                        continue
                    key = f"{eid}/{gmid}"
                    try:
                        md = fetch_match(session, int(eid), gmid)
                        markets = md.get("data") if isinstance(md.get("data"), list) else []
                        info = md.get("info") or {}
                        match_name = info.get("ename") or m.get("ename")
                        scorecard = None
                        if SCORECARD and info.get("scard"):
                            event_id = info.get("oldgmid") or gmid
                            try:
                                from scorecard import fetch_scorecard
                                scorecard = fetch_scorecard(event_id)
                                if scorecard:
                                    write_json(
                                        scorecard_file_path(OUT_DIR, eid, gmid),
                                        {
                                            "_meta": {
                                                "eid": eid,
                                                "gmid": gmid,
                                                "event_id": str(event_id),
                                                "url": scorecard_json_path(eid, gmid),
                                            },
                                            "scorecard": scorecard,
                                        },
                                    )
                            except Exception as sc_err:
                                scorecard = {"error": str(sc_err)}
                        matches[key] = {
                            "eid": eid,
                            "ename": ename,
                            "gmid": gmid,
                            "match": match_name,
                            "markets": len(markets),
                            **md,
                        }
                        write_json(
                            match_file_path(OUT_DIR, eid, gmid),
                            {
                                "_meta": {
                                    "eid": eid,
                                    "gmid": gmid,
                                    "match": match_name,
                                    "competition": info.get("cname") or m.get("cname"),
                                    "stime": info.get("stime") or m.get("stime"),
                                    "iplay": info.get("iplay", m.get("iplay")),
                                    "fancy": info.get("f"),
                                    "bookmaker": info.get("bm"),
                                    "tv": info.get("tv"),
                                    "updated_at": now_str(),
                                    "url": match_json_path(eid, gmid),
                                    "site_url": f"/game-details/{eid}/{gmid}",
                                    "scorecard_url": scorecard_json_path(eid, gmid) if info.get("scard") else None,
                                },
                                **md,
                                **({"scorecard": scorecard} if scorecard else {}),
                            },
                        )
                        print(f"    gmid={gmid} {m.get('ename')}: {len(markets)} markets", flush=True)
                    except Exception as e:
                        matches[key] = {"success": False, "msg": str(e)}
                    time.sleep(SPORT_DELAY)
        except Exception as e:
            highlights[str(eid)] = {
                "eid": eid,
                "ename": ename,
                "success": False,
                "msg": str(e),
                "data": None,
            }
            print(f"  {ename}: ERROR {e}", flush=True)
        time.sleep(SPORT_DELAY)

    combined = {
        "_meta": {
            "mode": "sport",
            "updated_at": now_str(),
            "sports": [t.get("ename") for t in tabs],
            "note": f"sport live — {len(tabs)} sports",
        },
        "tablist": tablist,
        "highlights": highlights,
        "matches": matches,
    }
    write_json(TAB_OUT, {"_meta": combined["_meta"], **tablist})
    write_json(ALL_OUT, combined)
    return combined


def main() -> None:
    once = "--once" in sys.argv or os.environ.get("SPORT_POLL_ONCE") == "1"
    user, pwd = DEFAULT_USER, DEFAULT_PASS
    print(f"Sport poll every {INTERVAL}s" + (" (once)" if once else ""), flush=True)

    session = api_login(user, pwd)
    last_login = time.time()

    while True:
        try:
            if time.time() - last_login > REFRESH_EVERY:
                session = api_login(user, pwd)
                last_login = time.time()
            print(f"[{time.strftime('%H:%M:%S')}] sport fetch...", flush=True)
            poll_once(session, user)
            if once:
                break
        except Exception as e:
            print(f"sport loop error: {e}", flush=True)
            if once:
                break
            time.sleep(10)
            try:
                session = api_login(user, pwd)
                last_login = time.time()
            except Exception as e2:
                print(f"sport re-login fail: {e2}", flush=True)
        if once:
            break
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
