#!/usr/bin/env python3
"""Poll vcasino API and refresh output/vtrio_data.json (live updates)."""

from __future__ import annotations

import argparse
import json
import sys
import time
from http.cookiejar import MozillaCookieJar
from pathlib import Path

from decrypt_cryptojs import encrypt_api_payload
from scrape_vcasino import BASE_URL, DEFAULT_PASSPHRASE, maybe_decrypt

OUT = Path("output/vtrio_data.json")
INTERVAL_SEC = 2.0  # site polls ~700ms; 2s is safe for local use


def load_cookies(path: str) -> dict[str, str]:
    jar = MozillaCookieJar(path)
    jar.load(ignore_discard=True, ignore_expires=True)
    return {c.name: c.value for c in jar}


def fetch_live(gtype: str, cookies: dict[str, str], api: str = "vcasino") -> dict:
    from curl_cffi import requests as cffi_requests

    paths = {
        "vcasino": "/api/front/vcasino/data2",
        "casino": "/api/front/casino/data2",
    }
    url = f"{BASE_URL}{paths.get(api, paths['vcasino'])}"
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Origin": BASE_URL,
        "Referer": f"{BASE_URL}/casino/{gtype}",
    }
    session = cffi_requests.Session(impersonate="chrome124")
    session.cookies.update(cookies)
    body = {"data": encrypt_api_payload({}, DEFAULT_PASSPHRASE)}
    resp = session.post(url, params={"gtype": gtype}, json=body, headers=headers, timeout=30)
    return resp.json()


def write_json(data: dict) -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Live poll vcasino API → output/vtrio_data.json")
    parser.add_argument("--cookies", default="cookies.txt", help="Netscape cookies.txt")
    parser.add_argument("--gtype", default="vtrio")
    parser.add_argument("--api", default="vcasino", choices=["vcasino", "casino"])
    parser.add_argument("--interval", type=float, default=INTERVAL_SEC)
    parser.add_argument("--once", action="store_true", help="Single fetch, no loop")
    args = parser.parse_args()

    if not Path(args.cookies).is_file():
        print(f"cookies missing: {args.cookies}", file=sys.stderr)
        print("Login on site → export cookies.txt → ./serve.sh live cookies.txt", file=sys.stderr)
        sys.exit(1)

    cookies = load_cookies(args.cookies)
    print(f"Live poll every {args.interval}s → {OUT}", flush=True)

    while True:
        try:
            raw = fetch_live(args.gtype, cookies, args.api)
            data = maybe_decrypt(raw, DEFAULT_PASSPHRASE)
            write_json(data if isinstance(data, dict) else {"raw": data})

            if isinstance(data, dict):
                if data.get("status") == 401 or "login" in str(data.get("msg", "")).lower():
                    print(f"[{time.strftime('%H:%M:%S')}] 401 Please Login — cookies expire ho gayi?", flush=True)
                elif data.get("success") and isinstance(data.get("data"), dict):
                    g = data["data"]
                    print(
                        f"[{time.strftime('%H:%M:%S')}] round={g.get('mid')} "
                        f"lt={g.get('lt')} markets={len(g.get('sub', []))}",
                        flush=True,
                    )
                else:
                    print(f"[{time.strftime('%H:%M:%S')}] {data.get('msg', data)}", flush=True)
        except Exception as e:
            print(f"[{time.strftime('%H:%M:%S')}] error: {e}", file=sys.stderr, flush=True)

        if args.once:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
