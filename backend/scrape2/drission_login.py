#!/usr/bin/env python3
"""
DrissionPage se login → cookies.txt save.
Usage:
  .venv/bin/python drission_login.py
  .venv/bin/python drission_login.py --username Demo9304 --password Demo1234
  ./serve.sh live cookies.txt
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from DrissionPage import ChromiumOptions, ChromiumPage

SITE = "https://allpanelexch9.co"
OUT = Path("cookies.txt")
DEFAULT_USER = "Demo9304"
DEFAULT_PASS = "Demo1234"


def to_netscape(cookies: list[dict]) -> str:
    lines = ["# Netscape HTTP Cookie File", "# DrissionPage export", ""]
    for c in cookies:
        domain = c.get("domain", "")
        if domain and not domain.startswith("."):
            domain = "." + domain.lstrip(".")
        path = c.get("path", "/")
        secure = "TRUE" if c.get("secure") else "FALSE"
        exp = c.get("expires") or c.get("expiry") or 0
        try:
            expires = str(int(float(exp))) if exp and float(exp) > 0 else "0"
        except (TypeError, ValueError):
            expires = "0"
        name = c.get("name", "")
        value = c.get("value", "")
        lines.append(f"{domain}\tTRUE\t{path}\t{secure}\t{expires}\t{name}\t{value}")
    return "\n".join(lines) + "\n"


def get_session_cookies_after_login(username: str, password: str) -> list[dict] | None:
    print("DrissionPage login shuru...")

    co = ChromiumOptions()
    co.set_argument("--no-sandbox")
    co.set_argument("--disable-blink-features=AutomationControlled")
    # Headless off — Cloudflare ke liye visible browser better

    page = ChromiumPage(co)
    try:
        print("Login page khol rahe hain...")
        page.get(f"{SITE}/login")
        time.sleep(5)

        title = page.title or ""
        if "moment" in title.lower():
            print("Cloudflare challenge — 15 sec wait...")
            time.sleep(15)

        user_el = page.ele("@placeholder=Username", timeout=30)
        pass_el = page.ele("@placeholder=Password", timeout=30)
        if not user_el or not pass_el:
            print("Login form nahi mila — page inspect karo")
            return None

        print("Credentials fill...")
        user_el.clear()
        user_el.input(username)
        pass_el.clear()
        pass_el.input(password)

        if page.ele("@type=submit", timeout=3):
            page.ele("@type=submit").click()
        elif page.ele("text=Login", timeout=3):
            page.ele("text=Login").click()
        else:
            print("Login button nahi mila")
            return None

        print("Login wait...")
        time.sleep(8)

        # Dashboard / casino pe jao taaki session set ho
        page.get(f"{SITE}/casino/vtrio")
        time.sleep(5)

        cookies = page.cookies()
        print(f"Cookies mili: {len(cookies)}")
        for c in cookies:
            print(f"  - {c.get('name')}")

        url = page.url or ""
        if "/login" in url.lower():
            print("Warning: abhi bhi login page par ho — credentials galat ya captcha block")

        return cookies
    except Exception as e:
        print(f"Error: {e}")
        return None
    finally:
        page.quit()


def save_cookies(cookies: list[dict]) -> Path:
    OUT.write_text(to_netscape(cookies), encoding="utf-8")
    return OUT


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--username", default=os.environ.get("LOGIN_USER", DEFAULT_USER))
    parser.add_argument("--password", default=os.environ.get("LOGIN_PASS", DEFAULT_PASS))
    args = parser.parse_args()

    cookies = get_session_cookies_after_login(args.username, args.password)
    if not cookies:
        sys.exit(1)

    path = save_cookies(cookies)
    cookie_string = "; ".join(f"{c['name']}={c['value']}" for c in cookies if c.get("name"))
    Path("output/cookie_header.txt").write_text(cookie_string, encoding="utf-8")

    print(f"\nSaved → {path.resolve()}")
    print(f"Cookie header → output/cookie_header.txt")

    # API login se session complete karo (data2 ke liye zaroori)
    print("\nAPI login refresh...")
    import subprocess
    subprocess.run([sys.executable, "api_login.py", "--username", args.username, "--password", args.password], check=False)

    print("\nAb live server:")
    print("  ./live.sh")


if __name__ == "__main__":
    main()
