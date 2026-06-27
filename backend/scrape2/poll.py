#!/usr/bin/env python3
"""Live poll — Cloudflare bypass via FlareSolverr + cf_session."""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone

from cf_session import api_login, fetch_game, parse_json_response
from decrypt_cryptojs import maybe_decrypt
from games import GAME_TYPES
from scrape_vcasino import DEFAULT_PASSPHRASE

OUT_DIR = __import__("pathlib").Path("output")
ALL_OUT = OUT_DIR / "all_games.json"
INTERVAL = float(os.environ.get("POLL_INTERVAL", "5"))
GAME_DELAY = float(os.environ.get("GAME_DELAY", "0.12"))
SAVE_INDIVIDUAL = os.environ.get("SAVE_INDIVIDUAL", "1") == "1"
DEFAULT_USER = os.environ.get("LOGIN_USER", "Demo9304")
DEFAULT_PASS = os.environ.get("LOGIN_PASS", "Demo1234")
REFRESH_EVERY = int(os.environ.get("COOKIE_REFRESH_SEC", "900"))  # 15 min


def write_game(gtype, data, note):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "_meta": {
            "gtype": gtype,
            "mode": "live",
            "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "note": note,
        },
        **data,
    }
    (OUT_DIR / f"{gtype}_data.json").write_text(
        __import__("json").dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def write_all(results, note):
    combined = {
        "_meta": {
            "mode": "live",
            "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "games": list(results.keys()),
            "note": note,
        },
        "games": results,
    }
    ALL_OUT.write_text(
        __import__("json").dumps(combined, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def fetch_all(session, games, user):
    results = {}
    for g in games:
        try:
            raw = fetch_game(session, g)
            data = maybe_decrypt(raw, DEFAULT_PASSPHRASE)
            if not isinstance(data, dict):
                data = {"raw": data}
            results[g] = data
            if data.get("success") and isinstance(data.get("data"), dict):
                gd = data["data"]
                if SAVE_INDIVIDUAL:
                    write_game(g, data, f"live — {user}")
                print(f"  {g}: mid={gd.get('mid')} lt={gd.get('lt')}", flush=True)
            else:
                if SAVE_INDIVIDUAL:
                    write_game(g, data, str(data.get("msg")))
                print(f"  {g}: {data.get('msg')}", flush=True)
        except Exception as e:
            results[g] = {"success": False, "msg": str(e), "status": 0, "data": None}
            print(f"  {g}: ERROR {e}", flush=True)
        time.sleep(GAME_DELAY)
    return results


def parse_game_args(argv: list[str]) -> list[str]:
    games = [a for a in argv if a != "--once" and not a.startswith("-")]
    return games if games else GAME_TYPES


def main():
    games = parse_game_args(sys.argv[1:])
    once = "--once" in sys.argv or os.environ.get("POLL_ONCE") == "1"
    user, pwd = DEFAULT_USER, DEFAULT_PASS
    print(f"Poll {len(games)} games every {INTERVAL}s" + (" (once)" if once else ""), flush=True)

    session = api_login(user, pwd)
    last_login = time.time()

    while True:
        try:
            if time.time() - last_login > REFRESH_EVERY:
                print("[cf] cookie refresh...", flush=True)
                session = api_login(user, pwd)
                last_login = time.time()

            print(f"[{time.strftime('%H:%M:%S')}] fetch...", flush=True)
            results = fetch_all(session, games, user)
            ok = sum(1 for r in results.values() if r.get("success"))
            note = f"live {ok}/{len(games)}" if ok else f"API unavailable {ok}/{len(games)}"
            write_all(results, note)

            if ok == 0:
                print("[cf] all failed — re-login", flush=True)
                session = api_login(user, pwd)
                last_login = time.time()
                if once:
                    break
                time.sleep(5)
            elif once:
                break
        except Exception as e:
            print(f"loop error: {e}", flush=True)
            if once:
                break
            time.sleep(10)
            try:
                session = api_login(user, pwd)
                last_login = time.time()
            except Exception as e2:
                print(f"re-login fail: {e2}", flush=True)

        if once:
            break
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
