#!/usr/bin/env python3
"""
Browser kholo → tum login karo → cookies.txt save.

Usage:
  .venv/bin/python get_cookies.py
  ./serve.sh live cookies.txt
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

OUT = Path("cookies.txt")
SITE = "https://allpanelexch9.co"
LOGIN_HINT = f"{SITE}/login"  # common path; site redirect kare to bhi OK


def to_netscape(cookies: list[dict]) -> str:
    lines = ["# Netscape HTTP Cookie File", "# https://curl.se/docs/http-cookies.html", ""]
    for c in cookies:
        domain = c.get("domain", "")
        if not domain.startswith("."):
            domain = "." + domain.lstrip(".")
        path = c.get("path", "/")
        secure = "TRUE" if c.get("secure") else "FALSE"
        expires = str(int(c.get("expires", 0) or 0))
        name = c.get("name", "")
        value = c.get("value", "")
        http_only = domain.startswith(".")
        flag = "TRUE" if http_only else "FALSE"
        lines.append(f"{domain}\t{flag}\t{path}\t{secure}\t{expires}\t{name}\t{value}")
    return "\n".join(lines) + "\n"


def main() -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright install karo: .venv/bin/pip install playwright && .venv/bin/playwright install chromium")
        sys.exit(1)

    print(f"Browser khul raha hai: {SITE}")
    print("1. Site pe LOGIN karo (casino/vtrio tak jao)")
    print("2. Login ke baad is terminal mein ENTER dabao")
    print("3. cookies.txt save hogi → phir: ./serve.sh live cookies.txt")
    print()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto(SITE, wait_until="domcontentloaded", timeout=60000)
        input("\n>>> Login ho gaya? ENTER dabao... ")

        cookies = context.cookies()
        browser.close()

    if not cookies:
        print("Koi cookie nahi mili — dubara try karo")
        sys.exit(1)

    OUT.write_text(to_netscape(cookies), encoding="utf-8")
    names = [c["name"] for c in cookies]
    print(f"\nSaved {len(cookies)} cookies → {OUT.resolve()}")
    print("Important:", ", ".join(n for n in names if "session" in n.lower() or "token" in n.lower() or "auth" in n.lower()) or names[:8])
    print("\nAb chalao: ./serve.sh live cookies.txt")


if __name__ == "__main__":
    main()
