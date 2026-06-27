#!/usr/bin/env python3
"""Auto login → cookies.txt save. Credentials env se ya args se."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

OUT = Path("cookies.txt")
SITE = "https://allpanelexch9.co"


def to_netscape(cookies: list[dict]) -> str:
    lines = ["# Netscape HTTP Cookie File", ""]
    for c in cookies:
        domain = c.get("domain", "")
        if domain and not domain.startswith("."):
            domain = "." + domain.lstrip(".")
        path = c.get("path", "/")
        secure = "TRUE" if c.get("secure") else "FALSE"
        exp = c.get("expires")
        expires = str(int(exp)) if exp and exp > 0 else "0"
        lines.append(
            f"{domain}\tTRUE\t{path}\t{secure}\t{expires}\t{c.get('name','')}\t{c.get('value','')}"
        )
    return "\n".join(lines) + "\n"


def auto_login(username: str, password: str) -> list[dict]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=False, channel="chrome")
        except Exception:
            browser = p.chromium.launch(headless=False)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page = context.new_page()
        page.goto(f"{SITE}/login", wait_until="domcontentloaded", timeout=120000)

        # Cloudflare wait
        for _ in range(45):
            title = page.title().lower()
            if "moment" not in title and "just" not in title:
                break
            page.wait_for_timeout(2000)

        page.wait_for_selector('input[name="username"], input[placeholder*="Username" i]', timeout=60000)
        page.fill('input[name="username"], input[placeholder*="Username" i]', username)
        page.fill('input[name="password"], input[type="password"]', password)

        # Login button
        for sel in [
            'button:has-text("Login")',
            'button[type="submit"]',
            '.login-btn',
            'button:has-text("Sign")',
        ]:
            btn = page.locator(sel).first
            if btn.count() and btn.is_visible():
                btn.click()
                break

        page.wait_for_timeout(5000)
        # Wait until off login page or home loads
        for _ in range(30):
            if "/login" not in page.url.lower():
                break
            page.wait_for_timeout(2000)

        page.goto(f"{SITE}/casino/vtrio", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)
        cookies = context.cookies()
        browser.close()
        return cookies


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--username", default=os.environ.get("LOGIN_USER", ""))
    parser.add_argument("--password", default=os.environ.get("LOGIN_PASS", ""))
    args = parser.parse_args()

    if not args.username or not args.password:
        print("Usage: python auto_login.py --username ID --password PASS")
        sys.exit(1)

    print(f"Logging in as {args.username}...")
    cookies = auto_login(args.username, args.password)
    if not cookies:
        print("Login fail — cookies nahi mili")
        sys.exit(1)

    OUT.write_text(to_netscape(cookies), encoding="utf-8")
    print(f"Saved {len(cookies)} cookies → {OUT.resolve()}")
    print("Ab chalao: ./serve.sh live cookies.txt")


if __name__ == "__main__":
    main()
