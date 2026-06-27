#!/usr/bin/env python3
"""Login test — uses cf_session (FlareSolverr + browser fallback)."""

from __future__ import annotations

import argparse
import json
import os
import sys

from cf_session import api_login, fetch_game
from decrypt_cryptojs import maybe_decrypt
from scrape_vcasino import DEFAULT_PASSPHRASE

DEFAULT_USER = "Demo9304"
DEFAULT_PASS = "Demo1234"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--username", default=os.environ.get("LOGIN_USER", DEFAULT_USER))
    parser.add_argument("--password", default=os.environ.get("LOGIN_PASS", DEFAULT_PASS))
    args = parser.parse_args()

    print(f"Login: {args.username}")
    session = api_login(args.username, args.password)
    raw = fetch_game(session, "vtrio")
    data = maybe_decrypt(raw, DEFAULT_PASSPHRASE)
    print(json.dumps(data, ensure_ascii=False)[:500])
    if data.get("success"):
        g = data["data"]
        print(f"LIVE OK mid={g.get('mid')} lt={g.get('lt')}")
    else:
        print(f"FAIL: {data.get('msg')}")
        sys.exit(1)


if __name__ == "__main__":
    main()
