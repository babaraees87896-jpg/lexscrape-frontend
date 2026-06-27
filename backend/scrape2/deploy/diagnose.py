#!/usr/bin/env python3
"""VPS diagnose — FlareSolverr + login + vtrio fetch."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cf_session import api_login, fetch_game, flaresolverr_available
from decrypt_cryptojs import maybe_decrypt
from scrape_vcasino import BASE_URL, DEFAULT_PASSPHRASE

USER = os.environ.get("LOGIN_USER", "Demo9304")
PWD = os.environ.get("LOGIN_PASS", "Demo1234")


def main() -> None:
    print("=== DiaAPI Diagnose (CF bypass) ===")
    print(f"Site: {BASE_URL}")
    print(f"FlareSolverr: {flaresolverr_available()}")
    print(f"User: {USER}")
    print()
    try:
        session = api_login(USER, PWD)
        raw = fetch_game(session, "vtrio")
        data = maybe_decrypt(raw, DEFAULT_PASSPHRASE)
        if data.get("success"):
            g = data["data"]
            print(f"SUCCESS mid={g.get('mid')} lt={g.get('lt')} markets={len(g.get('sub', []))}")
        else:
            print(f"API error: {data.get('msg')}")
    except Exception as e:
        print(f"FAIL: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
