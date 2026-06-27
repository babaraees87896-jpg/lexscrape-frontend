#!/usr/bin/env python3
"""Try to reach allpanelexch9 and discover decrypt key + fetch vcasino data."""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

BASE = "https://allpanelexch9.co"
VTrio = f"{BASE}/casino/vtrio"
API = f"{BASE}/api/front/vcasino/data2"
OUT = Path(__file__).parent / "discovered"
OUT.mkdir(exist_ok=True)

SEARCH_PATTERNS = [
    r'AES\.decrypt\s*\(\s*[^,]+,\s*["\']([^"\']+)["\']',
    r'CryptoJS\.AES\.decrypt\s*\(\s*[^,]+,\s*["\']([^"\']+)["\']',
    r'decrypt\s*\(\s*[^,]+,\s*["\']([^"\']{4,64})["\']',
    r'passphrase["\']?\s*[:=]\s*["\']([^"\']+)["\']',
    r'secretKey["\']?\s*[:=]\s*["\']([^"\']+)["\']',
    r'encryptKey["\']?\s*[:=]\s*["\']([^"\']+)["\']',
]


def try_curl_cffi():
    try:
        from curl_cffi import requests as cr
    except ImportError:
        return None, "curl_cffi not installed"
    s = cr.Session(impersonate="chrome124")
    r = s.get(VTrio, timeout=60)
    return r, r.text


def try_cloudscraper():
    import cloudscraper
    s = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "darwin", "desktop": True}
    )
    r = s.get(VTrio, timeout=60)
    return r, r.text


def try_playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None, "playwright not installed"

    html = ""
    cookies = []
    api_body = None
    js_bodies: dict[str, str] = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )
        page = context.new_page()

        def on_response(resp):
            nonlocal api_body, js_bodies
            url = resp.url
            try:
                if "vcasino/data2" in url and resp.status == 200:
                    api_body = resp.text()
                if url.endswith(".js") or ".js?" in url:
                    if resp.status == 200:
                        t = resp.text()
                        if t and len(t) > 500:
                            js_bodies[url] = t
            except Exception:
                pass

        page.on("response", on_response)
        page.goto(VTrio, wait_until="networkidle", timeout=120000)
        time.sleep(5)
        html = page.content()
        cookies = context.cookies()
        browser.close()

    return {"html": html, "cookies": cookies, "api_body": api_body, "js_bodies": js_bodies}


def extract_keys_from_text(text: str) -> list[str]:
    keys: list[str] = []
    for pat in SEARCH_PATTERNS:
        keys.extend(re.findall(pat, text, re.I))
    # also search vcasino context
    for m in re.finditer(r'.{0,200}vcasino.{0,400}', text, re.I | re.S):
        chunk = m.group(0)
        for pat in SEARCH_PATTERNS:
            keys.extend(re.findall(pat, chunk, re.I))
    return list(dict.fromkeys(keys))


def find_script_urls(html: str) -> list[str]:
    return re.findall(r'<script[^>]+src=["\']([^"\']+\.js[^"\']*)["\']', html, re.I)


def main():
    print("=== Auto discover allpanelexch9 vcasino ===\n")
    html = ""
    js_text = ""
    api_body = None

    print("1) curl_cffi (chrome impersonate)...")
    try:
        r, text = try_curl_cffi()
        if r and "challenge" not in text.lower()[:2000] and "cf_chl" not in text[:5000]:
            print(f"   OK status={r.status_code} len={len(text)}")
            html = text
        else:
            print(f"   Blocked/challenge status={getattr(r,'status_code',None)}")
    except Exception as e:
        print(f"   Error: {e}")

    if not html or "cf_chl" in html:
        print("2) cloudscraper...")
        try:
            r, text = try_cloudscraper()
            if r and "cf_chl" not in text[:8000]:
                print(f"   OK status={r.status_code}")
                html = text
            else:
                print(f"   Still challenge len={len(text)}")
        except Exception as e:
            print(f"   Error: {e}")

    if not html or "cf_chl" in html:
        print("3) playwright (headless browser)...")
        try:
            data = try_playwright()
            if isinstance(data, tuple):
                print(f"   {data[1]}")
            elif data:
                html = data.get("html", "")
                api_body = data.get("api_body")
                for url, body in data.get("js_bodies", {}).items():
                    js_text += "\n" + body
                    name = re.sub(r"[^\w.-]", "_", url.split("/")[-1])[:80]
                    (OUT / f"js_{name}").write_text(body[:2_000_000], encoding="utf-8", errors="replace")
                print(f"   HTML len={len(html)} js_files={len(data.get('js_bodies',{}))} api={'yes' if api_body else 'no'}")
                (OUT / "cookies.json").write_text(json.dumps(data.get("cookies", []), indent=2))
        except Exception as e:
            print(f"   Error: {e}")

    (OUT / "page.html").write_text(html[:5_000_000], encoding="utf-8", errors="replace")

    if api_body:
        (OUT / "api_response.json").write_text(api_body, encoding="utf-8")
        print("\nAPI response captured!")

    # fetch linked JS from HTML
    urls = find_script_urls(html)
    print(f"\nScript URLs in HTML: {len(urls)}")
    for u in urls[:15]:
        print(" ", u)

    combined = html + js_text
    for fp in OUT.glob("js_*"):
        combined += fp.read_text(encoding="utf-8", errors="replace")

    keys = extract_keys_from_text(combined)
    print(f"\nCandidate passphrases: {len(keys)}")
    for k in keys[:20]:
        print(f"  - {k!r}")

    if api_body and "U2FsdGVk" in api_body:
        from decrypt_cryptojs import decrypt_api_payload
        payload = json.loads(api_body) if api_body.strip().startswith("{") else {"data": api_body}
        for k in keys:
            try:
                dec = decrypt_api_payload(payload, k)
                print(f"\n*** DECRYPTED with {k!r} ***")
                out = OUT / "decrypted.json"
                out.write_text(json.dumps(dec, indent=2, ensure_ascii=False), encoding="utf-8")
                print(f"Saved {out}")
                return
            except Exception:
                pass
        print("\nAPI encrypted but no passphrase worked yet.")

    if "cf_chl" in html or "being checked" in html:
        print("\nFAILED: Cloudflare still blocking automated access.")
        print("Install browser: .venv/bin/playwright install chromium")
        sys.exit(1)


if __name__ == "__main__":
    main()
