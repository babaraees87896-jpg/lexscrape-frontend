"""Cloudflare-safe HTTP session for VPS + local."""

from __future__ import annotations

import json
import os
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from typing import Any

from decrypt_cryptojs import api_request_body, decrypt_api_payload, encrypt_api_payload
from scrape_vcasino import BASE_URL, DEFAULT_PASSPHRASE

COOKIES = Path("cookies.txt")
IMPERSONATE = os.environ.get("CURL_IMPERSONATE", "chrome124")
FLARE_URL = os.environ.get("FLARESOLVERR_URL", "http://127.0.0.1:8191/v1")


def _headers(referer: str) -> dict[str, str]:
    return {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Content-Type": "application/json",
        "Origin": BASE_URL,
        "Referer": referer,
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
    }


def load_cookies_dict() -> dict[str, str]:
    if not COOKIES.is_file():
        return {}
    jar = MozillaCookieJar(str(COOKIES))
    jar.load(ignore_discard=True, ignore_expires=True)
    return {c.name: c.value for c in jar}


def save_cookies_from_dict(cookies: dict[str, str], domain: str = ".allpanelexch9.co") -> None:
    lines = ["# Netscape HTTP Cookie File", ""]
    for name, value in cookies.items():
        lines.append(f"{domain}\tTRUE\t/\tTRUE\t0\t{name}\t{value}")
    COOKIES.write_text("\n".join(lines) + "\n", encoding="utf-8")


def save_cookies_from_session(session) -> None:
    lines = ["# Netscape HTTP Cookie File", ""]
    for c in session.cookies.jar:
        domain = c.domain or ".allpanelexch9.co"
        if not domain.startswith("."):
            domain = "." + domain
        secure = "TRUE" if c.secure else "FALSE"
        expires = str(int(c.expires)) if c.expires else "0"
        lines.append(
            f"{domain}\tTRUE\t{c.path or '/'}\t{secure}\t{expires}\t{c.name}\t{c.value}"
        )
    COOKIES.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_json_response(r, label: str = "API") -> dict:
    text = r.text or ""
    if not text.strip():
        raise RuntimeError(f"{label}: empty HTTP {getattr(r, 'status_code', '?')} — Cloudflare/IP block")
    if text.lstrip().startswith("<"):
        if "cloudflare" in text.lower() or "just a moment" in text.lower():
            raise RuntimeError(f"{label}: Cloudflare HTML block")
        raise RuntimeError(f"{label}: HTML not JSON — {text[:120]}")
    try:
        return r.json()
    except json.JSONDecodeError as e:
        raise RuntimeError(f"{label}: bad JSON — {text[:120]}") from e


def new_cffi_session():
    from curl_cffi import requests as cffi_requests

    s = cffi_requests.Session(impersonate=IMPERSONATE)
    existing = load_cookies_dict()
    if existing:
        s.cookies.update(existing)
    return s


def flaresolverr_available() -> bool:
    try:
        import requests

        r = requests.post(FLARE_URL, json={"cmd": "sessions.create"}, timeout=15)
        if r.ok:
            requests.post(
                FLARE_URL,
                json={"cmd": "sessions.destroy", "session": r.json().get("session")},
                timeout=5,
            )
            return True
    except Exception:
        pass
    return False


def flaresolverr_get_cookies(url: str) -> dict[str, str]:
    import requests

    create = requests.post(FLARE_URL, json={"cmd": "sessions.create"}, timeout=30).json()
    session_id = create.get("session")
    try:
        resp = requests.post(
            FLARE_URL,
            json={
                "cmd": "request.get",
                "url": url,
                "session": session_id,
                "maxTimeout": 120000,
            },
            timeout=130,
        ).json()
        if resp.get("status") != "ok":
            raise RuntimeError(resp.get("message", "FlareSolverr failed"))
        cookies = {}
        for c in resp.get("solution", {}).get("cookies", []):
            cookies[c["name"]] = c["value"]
        return cookies
    finally:
        try:
            requests.post(
                FLARE_URL,
                json={"cmd": "sessions.destroy", "session": session_id},
                timeout=10,
            )
        except Exception:
            pass


def browser_login(username: str, password: str) -> dict[str, str]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        ctx = browser.new_context(
            user_agent=_headers(f"{BASE_URL}/login")["User-Agent"],
            viewport={"width": 1280, "height": 800},
        )
        page = ctx.new_page()
        page.goto(f"{BASE_URL}/login", wait_until="domcontentloaded", timeout=120000)
        for _ in range(40):
            if "moment" not in (page.title() or "").lower():
                break
            page.wait_for_timeout(2000)
        page.wait_for_selector('input[name="username"]', timeout=90000)
        page.fill('input[name="username"]', username)
        page.fill('input[name="password"]', password)
        btn = page.locator('button:has-text("Login"), button[type="submit"]').first
        btn.click()
        page.wait_for_timeout(8000)
        page.goto(f"{BASE_URL}/casino/vtrio", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)
        cookies = {c["name"]: c["value"] for c in ctx.cookies()}
        browser.close()
    if not cookies.get("g_token") and not cookies.get("cf_clearance"):
        raise RuntimeError("Browser login — cookies nahi mili")
    return cookies


def api_login(username: str, password: str):
    """Returns curl_cffi session with valid cookies."""
    session = new_cffi_session()

    # 1) Warm Cloudflare cookies via FlareSolverr (VPS)
    if os.environ.get("USE_FLARESOLVERR", "1") == "1" and flaresolverr_available():
        print("[cf] FlareSolverr CF bypass...", flush=True)
        cf_cookies = flaresolverr_get_cookies(f"{BASE_URL}/login")
        session.cookies.update(cf_cookies)
        save_cookies_from_dict({**load_cookies_dict(), **cf_cookies})

    # 2) GET warmup
    try:
        session.get(f"{BASE_URL}/login", timeout=60)
    except Exception:
        pass

    # 3) API login
    body = {"data": encrypt_api_payload({"username": username, "password": password}, DEFAULT_PASSPHRASE)}
    r = session.post(
        f"{BASE_URL}/api/front/login",
        json=body,
        headers=_headers(f"{BASE_URL}/login"),
        timeout=60,
    )
    try:
        data = decrypt_api_payload(parse_json_response(r, "login"), DEFAULT_PASSPHRASE)
    except RuntimeError:
        # 4) Browser fallback (VPS headless)
        print("[cf] API login fail — browser login try...", flush=True)
        cookies = browser_login(username, password)
        session = new_cffi_session()
        session.cookies.update(cookies)
        save_cookies_from_dict(cookies)
        session.get(f"{BASE_URL}/login", timeout=60)
        r = session.post(
            f"{BASE_URL}/api/front/login",
            json=body,
            headers=_headers(f"{BASE_URL}/login"),
            timeout=60,
        )
        data = decrypt_api_payload(parse_json_response(r, "login"), DEFAULT_PASSPHRASE)

    if data.get("status") != 200:
        raise RuntimeError(data.get("msg", "login failed"))

    save_cookies_from_session(session)
    print(f"[cf] Login OK: {data.get('data', {}).get('uname')}", flush=True)
    return session


def _api_path(gtype: str) -> str:
    """v-prefix games → vcasino API, baaki → casino API."""
    if gtype.startswith("v"):
        return "/api/front/vcasino/data2"
    return "/api/front/casino/data2"


def fetch_game(session, gtype: str) -> dict:
    path = _api_path(gtype)
    r = session.post(
        f"{BASE_URL}{path}",
        params={"gtype": gtype},
        json=api_request_body(gtype, DEFAULT_PASSPHRASE),
        headers=_headers(f"{BASE_URL}/casino/{gtype}"),
        timeout=45,
    )
    return parse_json_response(r, gtype)


def fetch_sport_api(
    session,
    path: str,
    payload: dict | None = None,
    *,
    params: dict | None = None,
    referer: str = "/home",
    label: str | None = None,
) -> dict:
    """POST encrypted body — sports APIs (tablist, highlighthomePrivate, etc.)."""
    body = {"data": encrypt_api_payload(payload or {}, DEFAULT_PASSPHRASE)}
    r = session.post(
        f"{BASE_URL}{path}",
        params=params,
        json=body,
        headers=_headers(f"{BASE_URL}{referer}"),
        timeout=45,
    )
    return parse_json_response(r, label or path.rsplit("/", 1)[-1])


def fetch_match_odds(session, etid: int, gmid) -> dict:
    """Match markets/odds — gamedataPrivate?etid=&gmid="""
    payload = {"etid": etid, "gmid": gmid}
    return fetch_sport_api(
        session,
        "/api/front/gamedataPrivate",
        payload,
        params={"etid": etid, "gmid": gmid},
        referer=f"/game-details/{etid}/{gmid}",
        label=f"odds-{gmid}",
    )


def fetch_match_info(session, etid: int, gmid) -> dict:
    """Match info — gamedetailPrivate (name, competition, fancy/bm/tv flags)."""
    payload = {"etid": etid, "gmid": gmid}
    return fetch_sport_api(
        session,
        "/api/front/gamedetailPrivate",
        payload,
        referer=f"/game-details/{etid}/{gmid}",
        label=f"detail-{gmid}",
    )


def fetch_match_detail(session, etid: int, gmid) -> dict:
    """Alias for gamedataPrivate (backward compat)."""
    return fetch_match_odds(session, etid, gmid)
