#!/usr/bin/env python3
"""
Scrape allpanelexch vcasino/casino data2 API and decrypt CryptoJS-encrypted responses.

Setup:
  pip install -r requirements.txt
  python scrape_vcasino.py --cookies cookies.txt   # logged-in session required for live API
  python scrape_vcasino.py --decrypt-only sample_response.json -o out.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup

from decrypt_cryptojs import decrypt_api_payload, encrypt_api_payload, api_request_body

BASE_URL = "https://allpanelexch9.co"
DEFAULT_PASSPHRASE = "cae7b808-8b1e-4f47-87a5-1a4b6a08030e"
API_PATHS = {
    "vcasino": "/api/front/vcasino/data2",
    "casino": "/api/front/casino/data2",
}

CHROME_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def load_cookies(path: str | None) -> dict[str, str]:
    if not path:
        return {}
    jar = MozillaCookieJar(path)
    jar.load(ignore_discard=True, ignore_expires=True)
    return {c.name: c.value for c in jar}


def _headers(referer: str | None = None) -> dict[str, str]:
    return {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Origin": BASE_URL,
        "Referer": referer or f"{BASE_URL}/casino/vtrio",
        "User-Agent": CHROME_UA,
    }


def fetch_with_curl_cffi(
    gtype: str = "vtrio",
    api: str = "vcasino",
    cookies: dict[str, str] | None = None,
    passphrase: str = DEFAULT_PASSPHRASE,
    encrypt_body: bool = True,
) -> dict[str, Any]:
    from curl_cffi import requests as cffi_requests

    path = API_PATHS.get(api, api)
    if not path.startswith("/"):
        path = "/" + path
    url = f"{BASE_URL}{path}"
    session = cffi_requests.Session(impersonate="chrome124")
    if cookies:
        session.cookies.update(cookies)

    headers = _headers()
    session.get(BASE_URL, headers=headers, timeout=30)

    body: dict[str, Any] = {}
    if encrypt_body:
        body = api_request_body(gtype, passphrase)

    resp = session.post(
        url, params={"gtype": gtype}, json=body, headers=headers, timeout=30
    )
    if resp.status_code >= 400 and not resp.text.strip().startswith("{"):
        resp.raise_for_status()
    return resp.json()


def fetch_with_cloudscraper(
    gtype: str = "vtrio",
    api: str = "vcasino",
    cookies: dict[str, str] | None = None,
    passphrase: str = DEFAULT_PASSPHRASE,
    encrypt_body: bool = True,
) -> dict[str, Any]:
    import cloudscraper

    path = API_PATHS.get(api, api)
    if not path.startswith("/"):
        path = "/" + path
    url = f"{BASE_URL}{path}"
    headers = _headers()
    scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "darwin", "desktop": True}
    )
    if cookies:
        scraper.cookies.update(cookies)

    home = scraper.get(BASE_URL, headers=headers, timeout=30)
    if "challenge" in home.text.lower() or "_cf_chl_opt" in home.text:
        print(
            "Warning: homepage may still show Cloudflare challenge. "
            "Try --client curl_cffi or export browser cookies.",
            file=sys.stderr,
        )

    body: dict[str, Any] = {}
    if encrypt_body:
        body = api_request_body(gtype, passphrase)

    resp = scraper.post(url, params={"gtype": gtype}, json=body, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()


def fetch_with_requests(
    gtype: str = "vtrio",
    api: str = "vcasino",
    cookies: dict[str, str] | None = None,
    passphrase: str = DEFAULT_PASSPHRASE,
    encrypt_body: bool = True,
) -> dict[str, Any]:
    path = API_PATHS.get(api, api)
    if not path.startswith("/"):
        path = "/" + path
    url = f"{BASE_URL}{path}"
    session = requests.Session()
    if cookies:
        session.cookies.update(cookies)
    headers = _headers()
    body: dict[str, Any] = {}
    if encrypt_body:
        body = api_request_body(gtype, passphrase)
    r = session.post(url, params={"gtype": gtype}, json=body, headers=headers, timeout=30)
    r.raise_for_status()
    return r.json()


def parse_homepage_scripts(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[str] = []
    for tag in soup.find_all("script", src=True):
        src = tag.get("src", "")
        if src and "challenge-platform" not in src and "cdn-cgi" not in src:
            out.append(src)
    return out


def maybe_decrypt(payload: dict[str, Any], passphrase: str) -> Any:
    data = payload.get("data")
    if isinstance(data, str) and data.startswith("U2FsdGVkX1"):
        inner = decrypt_api_payload(payload, passphrase)
        if isinstance(inner, dict) and "data" in inner and inner.get("success") is not False:
            return inner
        return inner
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape and decrypt vcasino data2 API")
    parser.add_argument("--gtype", default="vtrio", help="Query param gtype")
    parser.add_argument(
        "--api",
        choices=list(API_PATHS),
        default="vcasino",
        help="API route (vcasino or casino)",
    )
    parser.add_argument("--cookies", help="Netscape cookies.txt from browser")
    parser.add_argument(
        "--passphrase",
        default=os.environ.get("DECRYPT_PASSPHRASE", DEFAULT_PASSPHRASE),
        help="CryptoJS AES passphrase",
    )
    parser.add_argument(
        "--decrypt-only",
        metavar="JSON_FILE",
        help="Decrypt saved response JSON (no network)",
    )
    parser.add_argument(
        "--client",
        choices=["curl_cffi", "cloudscraper", "requests"],
        default="curl_cffi",
        help="HTTP client (curl_cffi bypasses Cloudflare best)",
    )
    parser.add_argument(
        "--plain-body",
        action="store_true",
        help="POST {} without encrypting (server expects encrypted body)",
    )
    parser.add_argument("-o", "--output", help="Write decrypted JSON to file")
    args = parser.parse_args()

    if args.decrypt_only:
        raw = json.loads(Path(args.decrypt_only).read_text(encoding="utf-8"))
        decrypted = maybe_decrypt(raw, args.passphrase)
        _write_output(decrypted, args.output)
        return

    cookies = load_cookies(args.cookies)
    encrypt_body = not args.plain_body
    fetchers = {
        "curl_cffi": fetch_with_curl_cffi,
        "cloudscraper": fetch_with_cloudscraper,
        "requests": fetch_with_requests,
    }
    fetch = fetchers[args.client]

    try:
        payload = fetch(
            args.gtype,
            args.api,
            cookies or None,
            args.passphrase,
            encrypt_body,
        )
    except Exception as e:
        print(f"Request failed: {e}", file=sys.stderr)
        sys.exit(1)

    print("Raw API response keys:", list(payload.keys()))

    decrypted = maybe_decrypt(payload, args.passphrase)
    if isinstance(decrypted, dict):
        status = decrypted.get("status")
        msg = decrypted.get("msg", "")
        if status == 401 or "login" in str(msg).lower():
            print(
                "API says login required. Export cookies after logging in on the site.",
                file=sys.stderr,
            )
        if "exceed" in str(msg).lower():
            print("Rate limited — wait or use fewer requests.", file=sys.stderr)

    if isinstance(decrypted, dict) and decrypted.get("data") is not None:
        _write_output(decrypted, args.output)
        return

    if "data" not in payload:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return

    Path("response_encrypted.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )
    print("Saved raw response to response_encrypted.json (decrypt failed?)")


def _write_output(data: Any, path: str | None) -> None:
    text = json.dumps(data, indent=2, ensure_ascii=False)
    if path:
        Path(path).write_text(text, encoding="utf-8")
        print(f"Wrote {path}")
    else:
        print(text)


if __name__ == "__main__":
    main()
