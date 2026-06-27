#!/usr/bin/env python3
"""
Local server for scraped 1ex99.in website.

Serves static files + proxies API calls to avoid CORS/Cloudflare issues.
"""

import json
import mimetypes
import os
import re
import sys
import threading
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

import cloudscraper

from client_local_api import build_local_api_response, should_serve_local
from scorecard_api import scorecard_response
from config import ADMIN_OUTPUT_DIR, BASE_URL, HOST, OUTPUT_DIR, BROWSER_HEADERS
from mongodb.centerpanel_cache import proxy_odds_json
from mongodb.matches_api import CACHE_LOCAL_PREFIX, CACHE_REMOTE_PREFIX
from mongodb.auth import (
    _extract_bearer,
    mongo_login,
    mongo_logout,
    mongo_user_balance,
    validate_session,
)


def _api_http_status(body: bytes) -> int:
    """Original site jaisa — invalid session par HTTP 401 (header balance poll logout)."""
    try:
        data = json.loads(body.decode("utf-8"))
        if data.get("error") and data.get("code") in (401, 400):
            msg = str(data.get("message", "")).lower()
            if data.get("code") == 401 or "session" in msg:
                return 401
    except Exception:
        pass
    return 200
from serve_admin import patch_admin_js

class ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True
    max_workers = int(os.getenv("EX99_MAX_HTTP_THREADS", "200"))
    _workers_sem: threading.Semaphore | None = None
    _busy_body = (
        b"HTTP/1.1 503 Service Unavailable\r\n"
        b"Connection: close\r\n"
        b"Content-Type: text/html; charset=utf-8\r\n"
        b"Retry-After: 2\r\n"
        b"Content-Length: 142\r\n\r\n"
        b"<html><head><meta http-equiv=\"refresh\" content=\"2\"></head>"
        b"<body><p>Loading... please wait.</p></body></html>"
    )

    def server_bind(self):
        self._workers_sem = threading.Semaphore(self.max_workers)
        super().server_bind()

    def process_request(self, request, client_address):
        sem = self._workers_sem
        if sem is not None and not sem.acquire(blocking=False):
            try:
                request.sendall(self._busy_body)
            except OSError:
                pass
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            if self._workers_sem is not None:
                self._workers_sem.release()


PORT = int(os.getenv("EX99_PORT", "8888"))
POLL_MS = int(os.getenv("EX99_POLL_MS", "2000"))
POLL_JS = f"{POLL_MS // 1000}e3" if POLL_MS >= 1000 else str(POLL_MS)
SITE_DIR = Path(os.getenv("EX99_OUTPUT_DIR", OUTPUT_DIR)).resolve()
_SCRIPT_DIR = Path(__file__).resolve().parent
_admin_rel = Path(os.getenv("EX99_ADMIN_OUTPUT_DIR", ADMIN_OUTPUT_DIR))
ADMIN_DIR = (_admin_rel if _admin_rel.is_absolute() else _SCRIPT_DIR / _admin_rel).resolve()
ADMIN_STATIC_ROOT_PREFIXES = ("/static/js/", "/static/css/", "/static/media/")
ADMIN_LEGACY_JS_STUB = b"/* ex99 admin legacy stub */\n(function(){})();\n"
API_REMOTE = "https://api.ons3.co/v1/"
API_LOCAL_PREFIX = "/v1/"
STREAM_LOCAL_PREFIX = "/casino-stream/"
STREAM_REMOTE = "https://casinostream.tresting.com/"
STREAM_REMOTE_ALT = "https://stream.1ex99.in/"
# Upstream casino iframe only whitelists the scraped domain (1ex99.in), not EX99_HOST tunnel aliases.
STREAM_UPSTREAM_ORIGIN = BASE_URL
STREAM_UPSTREAM_REFERER = f"{BASE_URL}/"
SCORECARD_PATH_RE = re.compile(r"^/sport/scorecard/(\d+)/(\d+)\.json$")
SITE_HOST = HOST  # 1ex99.in - used for login/domain API payloads
USE_MONGO_AUTH = os.getenv("EX99_MONGO_AUTH", "1") not in ("0", "false", "False")
# Betting + balance ke liye live api.ons3.co par kabhi mat jao (default: ON)
LOCAL_ONLY = os.getenv("EX99_LOCAL_ONLY", "1") not in ("0", "false", "False")
SITE_SELECTOR_PATH = "/site-selector"
ADMIN_MOUNT = "/admin"
ADMIN_UPSTREAM_HOST = os.getenv("EX99_ADMIN_UPSTREAM_HOST", "127.0.0.1")
ADMIN_UPSTREAM_PORT = int(os.getenv("EX99_ADMIN_UPSTREAM_PORT", "6565"))

# Admin panel routes — client SPA in /app/* inhe galat se load kar leta hai.
ADMIN_ONLY_APP_PREFIXES = (
    "/app/statement/",
    "/app/userlist/",
    "/app/cash-transction/",
    "/app/game/",
    "/app/limit/",
    "/app/create/",
    "/app/edit/",
    "/app/login-report/",
    "/app/dataReport/",
    "/app/agentComm",
    "/app/AgentCommissionList/",
    "/app/show-bets/",
    "/app/display-game/",
    "/app/matka/",
    "/app/ledger/all/",
    "/app/ledger/client",
    "/app/casino/casino-report/",
    "/app/casino/plus-minus-select/",
    "/app/casino/report/",
    "/app/casino/casino-reports/",
    "/app/collection",
    "/app/profit-loss",
    "/app/create-collection",
    "/app/diamond-casino",
    "/app/live-casino",
)
_ADMIN_LEDGER_USER_TYPES = frozenset(
    {"owner", "subowner", "superadmin", "admin", "subadmin", "master", "superagent", "agent", "client", "all"}
)


def is_admin_only_app_path(path: str) -> bool:
    if not path.startswith("/app/"):
        return False
    for prefix in ADMIN_ONLY_APP_PREFIXES:
        if path == prefix.rstrip("/") or path.startswith(prefix):
            return True
    if path.startswith("/app/ledger/"):
        rest = path[len("/app/ledger/") :].split("/")[0]
        if rest in _ADMIN_LEDGER_USER_TYPES:
            return True
    return False

JS_PATCH_FROM = "https://api.ons3.co/v1/"
JS_PATCH_TO = API_LOCAL_PREFIX

scraper = cloudscraper.create_scraper(
    browser={"browser": "chrome", "platform": "darwin", "desktop": True}
)
_stream_session = cloudscraper.create_scraper(
    browser={"browser": "chrome", "platform": "darwin", "desktop": True}
)
_stream_session.trust_env = False


def _fetch_upstream_stream(url: str, headers: dict, timeout: int = 30) -> tuple[int, bytes, str]:
    """Casino stream fetch — urllib first (DNS reliable), cloudscraper fallback."""
    import urllib.request

    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(), resp.headers.get("Content-Type", "application/octet-stream")
    except Exception:
        pass

    no_proxy = {"http": None, "https": None}
    try:
        resp = _stream_session.get(url, headers=headers, timeout=timeout, proxies=no_proxy)
        return resp.status_code, resp.content, resp.headers.get("Content-Type", "application/octet-stream")
    except Exception as primary:
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read(), resp.headers.get("Content-Type", "application/octet-stream")
        except Exception:
            raise primary


def patch_api_payload(path: str, payload: dict) -> dict:
    """Fix API payloads when running on localhost."""
    payload = dict(payload or {})

    if path.endswith("user/login"):
        payload.setdefault("host", SITE_HOST)
        payload.setdefault("isClient", True)

    if path.endswith("website/domainSettingByDomainName"):
        payload.setdefault("domainName", SITE_HOST)

    return payload


EX99_API_FIX = """
function _ex99FixData(o){if(!o||typeof o!=="object")return;if(Array.isArray(o)){for(var i=0;i<o.length;i++)_ex99FixData(o[i]);return}if(typeof o.maxMinCoins==="string"&&o.maxMinCoins.charAt(0)==="{"){try{JSON.parse(o.maxMinCoins)}catch(e){try{o.maxMinCoins=Function("return "+o.maxMinCoins)()}catch(t){o.maxMinCoins={}}}}for(var k of["videoUrl1","videoUrl2","videoUrl3"]){if(typeof o[k]==="string"){o[k]=o[k].replace(/https?:\\/\\/casinostream\\.tresting\\.com/,"/casino-stream").replace(/https?:\\/\\/stream\\.1ex99\\.in/,"/casino-stream/stream99")}}for(var k in o)Object.prototype.hasOwnProperty.call(o,k)&&_ex99FixData(o[k])}
function _ex99GetDS(){try{return JSON.parse(localStorage.getItem("domainSetting")||"{}")}catch(e){return{}}}
function _ex99SafeUrl(u){if(!u||typeof u!=="string")return"";u=String(u).trim();if(!u)return"";if(u.charAt(0)==="/"||u.indexOf("./")===0||u.indexOf("../")===0||/^data:/i.test(u)||/^blob:/i.test(u))return u;try{return new URL(u).href}catch(e){return""}}
function _ex99ToastErr(e){var m="";try{var d=e&&e.response&&e.response.data;m=(d&&d.message)||(e&&e.userinfo&&e.userinfo.message)||(e&&e.message)||""}catch(x){}if(typeof m!=="string")m=String(m||"");if(/unexpected token|not valid json|<!doctype|failed to fetch|networkerror|load failed/i.test(m))return;if(!/did not match the expected pattern/i.test(m))m&&M.error("Error: "+m)}
function _ex99ApplyDS(ds){ds=ds||_ex99GetDS();if(!ds||typeof ds!=="object")return;try{if(ds.title)document.title=String(ds.title);var fav=_ex99SafeUrl(ds.favicon||ds.logo);if(fav)document.querySelectorAll('link[rel*="icon"]').forEach(function(l){l.href=fav});var lg=_ex99SafeUrl(ds.logo);if(lg)document.querySelectorAll("img").forEach(function(i){var s=i.getAttribute("src")||"";if(/1ex99-logo1/i.test(s))return;if(/1exlogo|1ex99-logo|ons-logo|logosidebar|favicon/i.test(s))i.src=lg});if(ds.themeSetting&&ds.themeSetting.colors){var c=ds.themeSetting.colors;for(var k in c)if(Object.prototype.hasOwnProperty.call(c,k)&&c[k])document.documentElement.style.setProperty("--ex99-theme-"+k,c[k])}window.__ex99DomainSetting=ds;try{window.dispatchEvent(new CustomEvent("ex99-domain-setting",{detail:ds}))}catch(e){}}catch(e){}}
async function _ex99FetchDS(){try{var r=await fetch("/v1/website/domainSettingByDomainName",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({domainName:(window.location.hostname||"").replace(/^www\\./,"")})});var j=await r.json();if(j&&j.data&&!j.error){localStorage.setItem("domainSetting",JSON.stringify(j.data));localStorage.setItem("lastDomainFetch",String(Date.now()));_ex99ApplyDS(j.data);return j.data}}catch(e){}return _ex99GetDS()}
_ex99FetchDS();setInterval(_ex99FetchDS,1e3);document.addEventListener("visibilitychange",function(){document.visibilityState==="visible"&&_ex99FetchDS()});
"""

V_FN_OLD = (
    'async function V(e){if(e!=null&&e.dataEncrupt&&(e!=null&&e.data))try{const r=Os.AES.decrypt(e.data,Pi).toString(Os.enc.Utf8);'
    'r&&(e.data=JSON.parse(r))}catch(t){console.error("Decryption error:",t)}return e}'
)
V_FN_NEW = (
    EX99_API_FIX
    + 'async function V(e){if(e!=null&&e.dataEncrupt&&(e!=null&&e.data))try{const r=Os.AES.decrypt(e.data,Pi).toString(Os.enc.Utf8);'
    'r&&(e.data=JSON.parse(r))}catch(t){console.error("Decryption error:",t)}return _ex99FixData(e.data),e}'
)

# IST matchDate — future scheduled INPLAY rows hide (inplay page)
_EX99_STARTED_FN = (
    '(o=>{try{const d=String((o==null?void 0:o.matchDate)||""),'
    'p=d.match(/^(\\d{2})-(\\d{2})-(\\d{4})\\s+(\\d{1,2}):(\\d{2}):(\\d{2})\\s*(AM|PM)$/i);'
    'if(!p)return!1;let h=+p[4]%12;const ap=String(p[7]).toUpperCase();'
    'if(ap==="PM")h+=12;if(+p[4]===12&&ap==="AM")h=0;'
    'return Date.now()>=Date.UTC(+p[3],+p[2]-1,+p[1],h-5,+p[5]-30,+p[6])}catch(_e){return!1}})'
)


def patch_index_js(content: str) -> str:
    """API URL + maxMinCoins/videoUrl fixes + MongoDB logout."""
    content = content.replace(JS_PATCH_FROM, JS_PATCH_TO)
    content = content.replace(CACHE_REMOTE_PREFIX, CACHE_LOCAL_PREFIX)
    content = content.replace("http://1excache.tresting.com", CACHE_LOCAL_PREFIX)
    content = content.replace(V_FN_OLD, V_FN_NEW)
    # Single session — toast + turant logout (dusri device par login)
    content = content.replace(
        'import{s as M,c as io}from"./ui-DLBEMYc6.js";',
        'import{s as M,c as io}from"./ui-DLBEMYc6.js";'
        'window._ex99KickSession=function(_m){if(window.__ex99Kicked)return;window.__ex99Kicked=1;'
        'var msg=_m||"You are logged in from another device. Please login again.";'
        'try{M.error({content:msg,duration:4})}catch(e){try{alert(msg)}catch(x){}}'
        'setTimeout(function(){try{localStorage.clear();sessionStorage.clear()}catch(e){}'
        'window.location.href="/"},1800)};'
        '(function(){var _of=window.fetch;window.fetch=function(){return _of.apply(this,arguments).then(function(res){'
        'try{if(res&&res.status===401&&localStorage.getItem("user"))window._ex99KickSession()}catch(e){}'
        'return res})}})();',
    )
    content = content.replace(
        'if(n.status===401){localStorage.clear(),window.location.href="/";return}const c=await n.json(),o=await V(c);',
        'if(n.status===401){window._ex99KickSession();return}const c=await n.json(),o=await V(c);'
        'if(o!=null&&o.error&&(o.code===401||o.code===400)){window._ex99KickSession(o.message);return}',
    )
    # Logout pe local MongoDB session clear
    content = content.replace(
        'c=o=>{r(o),t(!1),localStorage.clear(),r("/")}',
        'c=async o=>{try{const u=JSON.parse(localStorage.getItem("user")||"{}");'
        'u.token&&await fetch("/v1/user/logout",{method:"POST",headers:{'
        '"Content-Type":"application/json",Authorization:"Bearer "+u.token},body:"{}"})'
        '}catch(e){}r(o),t(!1),localStorage.clear(),r("/")}',
    )
    # Aviator bet ke baad header CHIPS/EXP refresh (scraped ha thunk)
    content = content.replace(
        't.aviatorBetPlaceData=r.payload,t.aviatorBetPlaceMessage=r.payload,t.aviatorBetFlag=!0,t.aviatorOneTimeBet=!0',
        't.aviatorBetPlaceData=r.payload,t.aviatorBetPlaceMessage=r.payload,t.aviatorBetFlag=!0,t.aviatorOneTimeBet=!0,'
        'r.payload!=null&&(r.payload.totalCoins!=null&&localStorage.setItem("balance",JSON.stringify(r.payload.totalCoins)),'
        'r.payload.exposure!=null&&localStorage.setItem("exposure",JSON.stringify(Number(r.payload.exposure)||r.payload.exposure)))',
    )
    content = content.replace(
        't.aviatorBetPlaceData2=r.payload,t.aviatorBetPlaceMessage2=r.payload,t.aviatorBetFlag2=!0,t.aviatorOneTimeBet2=!0',
        't.aviatorBetPlaceData2=r.payload,t.aviatorBetPlaceMessage2=r.payload,t.aviatorBetFlag2=!0,t.aviatorOneTimeBet2=!0,'
        'r.payload!=null&&(r.payload.totalCoins!=null&&localStorage.setItem("balance",JSON.stringify(r.payload.totalCoins)),'
        'r.payload.exposure!=null&&localStorage.setItem("exposure",JSON.stringify(Number(r.payload.exposure)||r.payload.exposure)))',
    )
    # Cashout payload path fix + balance refresh
    content = content.replace(
        't.aviatorCashOutData=(s=(a=r.payload)==null?void 0:a.userinfo)==null?void 0:s.data',
        't.aviatorCashOutData=((a=r.payload)==null?void 0:a.userinfo)!=null?a.userinfo.data:a',
    )
    content = content.replace(
        't.aviatorCashout=!0,t.aviatorOneTimeCashout=!0,t.aviatorLoadingCashout1=!1',
        't.aviatorCashout=!0,t.aviatorOneTimeCashout=!0,t.aviatorLoadingCashout1=!1,'
        '((n=r.payload)==null?void 0:n.userinfo)!=null?n=n.userinfo.data:n=r.payload,'
        'n!=null&&(n.totalCoins!=null&&localStorage.setItem("balance",JSON.stringify(n.totalCoins)),'
        'n.exposure!=null&&localStorage.setItem("exposure",JSON.stringify(Number(n.exposure)||n.exposure)))',
    )
    content = content.replace(
        'r=`/favicon${at}.ico`,a=ve().theme==="2xbat"?"2EX India\'s Best Betting Website":`Welcome to ${at}`',
        'r="/favicon.ico",a="1ex"',
    )
    content = content.replace(
        'children:[v.jsx(J,{path:"/",element:v.jsx(Yu,{})}),v.jsxs(J,{path:"/app/*"',
        'children:[v.jsx(J,{path:"/",element:v.jsx(Yu,{})}),v.jsx(J,{path:"/login",element:v.jsx(Yu,{})}),v.jsxs(J,{path:"/app/*"',
    )
    # Mobile sidebar: scrollable menu + visible LOGOUT
    content = content.replace(
        'fixed w-[250px] right-0 !top-0 h-full theme1 transform',
        'fixed w-[250px] right-0 !top-0 h-[100dvh] max-h-[100dvh] theme1 transform',
    )
    content = content.replace(
        'className:"w-full h-screen overflow-y-auto"',
        'className:"w-full h-[100dvh] max-h-[100dvh] overflow-y-auto overscroll-y-contain pb-36 box-border"',
    )
    content = content.replace(
        'v.jsxs("nav",{className:"flex flex-col",children:[',
        'v.jsxs("nav",{className:"flex flex-col pb-10",children:[',
    )
    content = content.replace(
        'if(o)return localStorage.setItem("balance",JSON.stringify((a=o==null?void 0:o.data)==null?void 0:a.coins)),localStorage.setItem("exposure",JSON.stringify((s=o==null?void 0:o.data)==null?void 0:s.exposure)),{user:o}',
        'if(o)return localStorage.setItem("balance",JSON.stringify((a=o==null?void 0:o.data)==null?void 0:a.coins)),'
        'localStorage.setItem("exposure",JSON.stringify((s=o==null?void 0:o.data)==null?void 0:s.exposure)),'
        'window.dispatchEvent(new Event("ex99-wallet")),{user:o}',
    )
    # Header: bet ke baad turant CHIPS/EXP refresh (localStorage + wallet event)
    content = content.replace(
        'localStorage.setItem("balance",JSON.stringify(E)),localStorage.setItem("exposure",JSON.stringify(w)),c(E),f(w)',
        'localStorage.setItem("balance",JSON.stringify(E)),localStorage.setItem("exposure",JSON.stringify(w)),c(E),f(w),window.dispatchEvent(new Event("ex99-wallet"))',
    )
    content = content.replace(
        "C();const u=setInterval(C,5e3);return()=>clearInterval(u)",
        'C();const _w=()=>{try{const _b=localStorage.getItem("balance"),_e=localStorage.getItem("exposure");'
        '_b!=null&&c(JSON.parse(_b)),_e!=null&&f(JSON.parse(_e))}catch(_x){}};'
        'window.addEventListener("ex99-wallet",_w);const u=setInterval(C,1e3);'
        'const _ex99Bf=()=>{document.visibilityState==="visible"&&C()};'
        'window.addEventListener("focus",C);document.addEventListener("visibilitychange",_ex99Bf);'
        'return()=>{clearInterval(u),window.removeEventListener("ex99-wallet",_w),'
        'window.removeEventListener("focus",C),document.removeEventListener("visibilitychange",_ex99Bf)}',
    )
    # Domain settings — scraped site 3min localStorage cache; staff edits turant dikhein
    content = content.replace(
        'async function Nf(e){const r={method:"POST",headers:{"Content-Type":"application/json",Authorization:q().Authorization},body:JSON.stringify(e)}',
        'async function Nf(e){const r={method:"POST",headers:{"Content-Type":"application/json",Authorization:q().Authorization},'
        'body:JSON.stringify({domainName:(window.location.hostname||"").replace(/^www\\./,""),...(e||{})})}',
    )
    content = content.replace(
        'if(n)return localStorage.setItem("domainSetting",JSON.stringify(n==null?void 0:n.data)),{user:n}',
        'if(n)return localStorage.setItem("domainSetting",JSON.stringify(n==null?void 0:n.data)),_ex99ApplyDS(n==null?void 0:n.data),{user:n}',
    )
    content = content.replace(
        'case"1ex99":return{isCasino:!0,headerLogo:"/images/1exlogo.png",sidebarlogo:"/images/1exlogo.png",loginLogo:"/images/1ex99-logo1.jpg",loader:"/images/1exlogo.png"',
        'case"1ex99":{const _ex99ds=_ex99GetDS();const _ex99lg=_ex99ds.logo||"/images/1exlogo.png";return{isCasino:!0,headerLogo:_ex99lg,sidebarlogo:_ex99lg,loginLogo:_ex99ds.loginLogo||"/images/1ex99-logo1.jpg",loader:_ex99lg',
    )
    content = content.replace(
        'isDividedSession:!0,loginByDemo:!1};case"bpl99"',
        'isDividedSession:!0,loginByDemo:!1}};case"bpl99"',
        1,
    )
    content = content.replace(
        '[a,s]=U.useState({})',
        '[a,s]=U.useState(()=>{try{const C=localStorage.getItem("domainSetting");return C?JSON.parse(C):{}}catch(C){return{}}})',
        1,
    )
    content = content.replace(
        'const u=localStorage.getItem("domainSetting"),x=localStorage.getItem("lastDomainFetch"),p=Date.now(),A=3*60*1e3;if(u&&x&&p-parseInt(x)<A){s(JSON.parse(u));return}const D=await e(Sr());D!=null&&D.payload&&(localStorage.setItem("domainSetting",JSON.stringify(D.payload)),localStorage.setItem("lastDomainFetch",p.toString()),s(D.payload))',
        'const D=await e(Sr());D!=null&&D.payload&&(localStorage.setItem("domainSetting",JSON.stringify(D.payload)),localStorage.setItem("lastDomainFetch",Date.now().toString()),_ex99ApplyDS(D.payload),s(D.payload))',
    )
    content = content.replace(
        '})()},[e]),v.jsxs(v.Fragment,{children:[v.jsx(Pu,{isSidebarOpen:t,setIsSidebarOpen:r})',
        '})();const _ds=async()=>{try{const D=await e(Sr());D!=null&&D.payload&&(localStorage.setItem("domainSetting",JSON.stringify(D.payload)),localStorage.setItem("lastDomainFetch",Date.now().toString()),_ex99ApplyDS(D.payload),s(D.payload))}catch(_e){}};_ds();const _dsIv=setInterval(_ds,3*60*1e3);const _dsVis=()=>{document.visibilityState==="visible"&&_ds()};const _dsEv=e=>{e.detail&&s(e.detail)};window.addEventListener("focus",_ds);window.addEventListener("ex99-domain-setting",_dsEv);document.addEventListener("visibilitychange",_dsVis);return()=>{clearInterval(_dsIv),window.removeEventListener("focus",_ds),window.removeEventListener("ex99-domain-setting",_dsEv),document.removeEventListener("visibilitychange",_dsVis)}},[e]),v.jsxs(v.Fragment,{children:[v.jsx(Pu,{isSidebarOpen:t,setIsSidebarOpen:r})',
    )
    for _ver in ("ex99orig23", "ex99orig22", "ex99orig21", "ex99orig16", "ex99orig15", "ex99orig14", "ex99orig13", "ex99orig12", "ex99orig11", "ex99orig10", "ex99orig9", "ex99orig8", "ex99orig7", "ex99orig6", "ex99orig5", "ex99orig4", "ex99orig3", "ex99orig2", "ex99orig1"):
        content = content.replace(
            f"assets/Inplay-wyzuWlIy.js?v={_ver}",
            "assets/Inplay-wyzuWlIy.js?v=ex99orig19",
        )
    content = content.replace(
        "assets/Inplay-wyzuWlIy.js",
        "assets/Inplay-wyzuWlIy.js?v=ex99orig19",
    )
    content = content.replace(
        '"assets/MatchDetail-DcLvOyoM.js"',
        '"assets/MatchDetail-DcLvOyoM.js?v=ex99md34"',
    )
    for _mdver in ("ex99md33", "ex99md32", "ex99md31", "ex99md30", "ex99md29", "ex99md28", "ex99md27", "ex99md26", "ex99md22", "ex99md21", "ex99md20", "ex99md19", "ex99md18", "ex99md17", "ex99md16"):
        content = content.replace(
            f"assets/MatchDetail-DcLvOyoM.js?v={_mdver}",
            "assets/MatchDetail-DcLvOyoM.js?v=ex99md34",
        )
    # Client match list — blocked matches API + blockMarket map se hatao
    content = content.replace(
        'let c=n.filter(o=>o&&o.isBlocked!==!0&&o.betPerm!==!1&&((o==null?void 0:o.inPlayStatus)===!0||String((o==null?void 0:o.status)||"").toUpperCase()==="INPLAY")).map(o=>({marketId:o==null?void 0:o.marketId',
        f'const _ex99St={_EX99_STARTED_FN};let c=n.filter(o=>o&&o.isBlocked!==!0&&o.betPerm!==!1&&(o.scrapeLive===!0||_ex99St(o))&&((o==null?void 0:o.inPlayStatus)===!0||String((o==null?void 0:o.status)||"").toUpperCase()==="INPLAY")).map(o=>({{marketId:o==null?void 0:o.marketId',
        1,
    )
    content = content.replace(
        'localStorage.setItem("matchList",JSON.stringify(c))',
        'localStorage.setItem("matchList",JSON.stringify((()=>{try{let _b={};const _t=localStorage.getItem("blockMarket");'
        'if(_t){_b=JSON.parse(_t);if(Array.isArray(_b))_b=Object.fromEntries(_b.filter(x=>x&&x.marketId).map(x=>[String(x.marketId),!0]))}'
        'return c.filter(o=>o&&_b[String(o.marketId)]!==!0)}catch(e){return c}})()))',
        1,
    )
    # Safari "string did not match expected pattern" — toast mat dikhao; auth helper safe
    content = content.replace(
        'q=()=>{const e=JSON.parse(localStorage.getItem("user"));return e&&(e!=null&&e.token)?{Authorization:"Bearer "+(e==null?void 0:e.token)}:{}}',
        'q=()=>{try{const e=JSON.parse(localStorage.getItem("user")||"null");return e&&(e!=null&&e.token)?{Authorization:"Bearer "+(e==null?void 0:e.token)}:{}}catch(_e){return{}}}',
    )
    # Background sportByMarketId poll — toast spam band (local 1s poll)
    content = content.replace(
        'authentication/sportByMarketId",async(e,{rejectWithValue:t})=>{var r,a,s;try{const n=await z.sportByMarketId(e);return(r=n==null?void 0:n.userinfo)==null?void 0:r.data}catch(n){return M.error("Error: "+(((s=(a=n==null?void 0:n.response)==null?void 0:a.data)==null?void 0:s.message)||n.message)),t(n.message)}})',
        'authentication/sportByMarketId",async(e,{rejectWithValue:t})=>{var r,a,s;try{const n=await z.sportByMarketId(e);return(r=n==null?void 0:n.userinfo)==null?void 0:r.data}catch(n){return console.error("sportByMarketId:",((s=(a=n==null?void 0:n.response)==null?void 0:a.data)==null?void 0:s.message)||n.message),t(n.message)}})',
    )
    _toast_err_pat = re.compile(
        r'M\.error\("Error: "\+\(\(\('
        r'([a-z])=\(([a-z])=([a-z])==null\?void 0:\3\.response\)==null\?void 0:\2\.data\)==null\?void 0:\1\.message\)'
        r'\|\|([a-z])\.message\)\)'
    )
    content = _toast_err_pat.sub(r"_ex99ToastErr(\4)", content)
    _toast_userinfo_pat = re.compile(
        r'M\.error\("Error: "\+\(\(\('
        r'([a-z])=([a-z])==null\?void 0:\2\.userinfo\)'
        r'==null\?void 0:\1\.message\)'
        r'\|\|([a-z])\.message\)\)'
    )
    content = _toast_userinfo_pat.sub(r"_ex99ToastErr(\3)", content)
    # Client change password — PATCH response {status,msg} (ChangePassword.js expects this)
    content = content.replace(
        'async function au(e){const r={method:"PATCH",headers:{"Content-Type":"application/json",Authorization:q().Authorization},body:JSON.stringify(e)};try{const s=await(await fetch($.BACKEND_URL+"user/updateUserPassword",r)).json(),n=await V(s);if(n)return{userinfo:n}}catch(a){return console.error("userUpdate error:",a),Promise.reject(a)}}',
        'async function au(e){const r={method:"PATCH",headers:{"Content-Type":"application/json",Authorization:q().Authorization},body:JSON.stringify(e)};try{const s=await(await fetch($.BACKEND_URL+"user/updateUserPassword",r)).json(),n=await V(s);if(!n)return{status:"error",msg:"Something went wrong"};if(n.error||Number(n.code)===1)return{status:"error",msg:n.message||"Something went wrong"};return{status:"success",msg:n.message||"Password updated successfully"}}catch(a){return console.error("updateUserPassword error:",a),{status:"error",msg:"Failed to update password"}}}',
    )
    content = content.replace(
        '_a=W("authentication/updateUserPassword",async(e,{rejectWithValue:t})=>{var r,a;try{const s=await z.updateUserPassword(e);return M.error((s==null?void 0:s.message)||"Something went wrong."),s}catch(s){return _ex99ToastErr(s),t(s.message)}})',
        '_a=W("authentication/updateUserPassword",async(e,{rejectWithValue:t})=>{try{const s=await z.updateUserPassword(e);if((s==null?void 0:s.status)==="error")return M.error(s.msg||"Something went wrong"),t(s.msg||"Error");return M.success((s==null?void 0:s.msg)||"Password updated successfully"),s}catch(s){return _ex99ToastErr(s),t((s==null?void 0:s.message)||"Error")}})',
    )
    content = content.replace(
        '_a=W("authentication/updateUserPassword",async(e,{rejectWithValue:t})=>{var r,a;try{const s=await z.updateUserPassword(e);return M.error((s==null?void 0:s.message)||"Something went wrong."),s}catch(s){return M.error("Error: "+(((a=(r=s==null?void 0:s.response)==null?void 0:r.data)==null?void 0:a.message)||s.message)),t(s.message)}})',
        '_a=W("authentication/updateUserPassword",async(e,{rejectWithValue:t})=>{try{const s=await z.updateUserPassword(e);if((s==null?void 0:s.status)==="error")return M.error(s.msg||"Something went wrong"),t(s.msg||"Error");return M.success((s==null?void 0:s.msg)||"Password updated successfully"),s}catch(s){return _ex99ToastErr(s),t((s==null?void 0:s.message)||"Error")}})',
    )
    for _ver in ("ex99orig26", "ex99orig25", "ex99orig24", "ex99orig21", "ex99orig20", "ex99orig19", "ex99orig18", "ex99orig17", "ex99orig16", "ex99orig15", "ex99orig14"):
        content = content.replace(f"?v={_ver}", "?v=ex99orig27")
    content = content.replace(
        'const s=await(await fetch($.BACKEND_URL+"sports/matchList",r)).json(),n=await V(s);',
        'const _mr=await fetch($.BACKEND_URL+"sports/matchList",r);const _mt=await _mr.text();let s;try{s=JSON.parse(_mt)}catch(_ej){console.error("matchList non-JSON",_mr.status,_mt.slice(0,120));throw _ej}const n=await V(s);',
    )
    content = content.replace(
        'catch(s){return _ex99ToastErr(s),t(s.message)}}),ba=W("authentication/roundWiseResult"',
        'catch(s){return console.error("matchlist poll:",s),t(s.message)}}),ba=W("authentication/roundWiseResult"',
    )
    content = content.replace(
        '"assets/Login-CNc67dgC.js"',
        '"assets/Login-CNc67dgC.js?v=ex99login1"',
    )
    # Casino chunks — stale hook-injected JS cache bust
    for _ca in re.findall(r'"assets/([A-Za-z0-9_-]+\.js)(\?v=[^"]*)?"', content):
        name = _ca[0] if isinstance(_ca, tuple) else _ca
        if any(
            x in name
            for x in (
                "Teenpatti", "DragonTiger", "Casino", "Lucky7", "Card",
                "Amar", "Worli", "Virtual", "ResultModal", "PlaceBet", "Cards32",
            )
        ):
            content = re.sub(
                rf'"assets/{re.escape(name)}(\?v=[^"]*)?"',
                f'"assets/{name}?v=ex99casino9"',
                content,
            )
    return content


def patch_change_password_js(content: str) -> str:
    """Change password — Redux unwrap se {status,msg} payload mile."""
    content = content.replace(
        "const o=await m(n(e));o.status===\"error\"",
        'const o=await m(n(e)).unwrap();o.status==="error"',
    )
    content = content.replace(
        "}catch(c){console.error(c)}}",
        '}catch(c){console.error(c),a(!0),d(String(c||"Failed to change password"),.8),r("danger")}}',
    )
    content = content.replace(
        'localStorage.setItem("isPasswordChanged",!0),window.location.href="/"',
        'localStorage.setItem("isPasswordChanged",!0),localStorage.removeItem("user"),window.location.href="/"',
    )
    return content


_TRESTING_SCORE_URL_JS = (
    'window._ex99ScoreUrl=function(u){if(!u)return"";u=String(u);'
    'if(/score\\.tresting\\.com|akamaized|socket-iframe|crickexpo/i.test(u))return u;'
    'var m=u.match(/gmid=(\\d+)/)||u.match(/crickexpo\\/(\\d+)/)||u.match(/(\\d+)\\/?$/);'
    'return m?"https://score.tresting.com/socket-iframe-21/crickexpo/"+m[1]:u};'
)


def patch_match_detail_js(content: str) -> str:
    """maxMinCoins invalid JSON (unquoted keys) ko safely parse karo."""
    if "window._ex99ScoreUrl=" not in content:
        content = _TRESTING_SCORE_URL_JS + content
    content = content.replace(CACHE_REMOTE_PREFIX, CACHE_LOCAL_PREFIX)
    content = content.replace("http://1excache.tresting.com", CACHE_LOCAL_PREFIX)
    content = content.replace(
        'JSON.parse(h.maxMinCoins||"{}")',
        '((x=>{try{return JSON.parse(x||"{}")}catch(e){try{return Function("return "+(x||"{}"))()}catch(t){return{}}}})(h.maxMinCoins))',
    )
    # maxMinCoins fallback — number 100 mat set karo (original sirf min check karta hai, max backend par)
    content = content.replace(
        "Te(v??100)",
        'Te(v&&typeof v==="object"?v:{minimum_session_bet:100,minimum_match_bet:100})',
    )
    # Dusre event ka position na dikhe — route/bet list change par reset
    content = content.replace(
        'if((a=Ce==null?void 0:Ce.oddsBetData)!=null&&a.length)try{',
        'if(!(a=Ce==null?void 0:Ce.oddsBetData)||!a.length){pe({}),Ee([]);return}try{',
    )
    content = content.replace(
        'const v=((a=be==null?void 0:be.find(k=>(k==null?void 0:k.marketId)===i))==null?void 0:a.scoreIframe)??"";',
        'const _exMk=(()=>{try{const _ml=JSON.parse(localStorage.getItem("matchList")||"[]");'
        'return(Array.isArray(_ml)?_ml:[]).find(k=>(k==null?void 0:k.marketId)===i)}catch(e){'
        'return be==null?void 0:be.find(k=>(k==null?void 0:k.marketId)===i)}})();'
        'const v=window._ex99ScoreUrl((_exMk==null?void 0:_exMk.scoreIframe)??"");',
    )
    content = content.replace(
        'if(g(v),!(Object.keys(h??{}).length>0)){const k=be.find(G=>(G==null?void 0:G.marketId)===i);k&&(Xe(k.socketUrl??""),Lt(k.isToss),Pt(k.isBookmaker),It(k.isFancy),Q(k.isScore),q(k.isTv),g(k.scoreIframe),T(k.socketUrl),R(k.tvUrl))}',
        'if(g(v),v&&Q(!0),(()=>{_exMk&&(Q(!!(_exMk.isScore||_exMk.scoreIframe)),q(!!_exMk.isTv),'
        '_exMk.scoreIframe&&g(window._ex99ScoreUrl(_exMk.scoreIframe)),_exMk.tvUrl&&R(_exMk.tvUrl))})(),'
        '!(Object.keys(h??{}).length>0)){const k=_exMk||be.find(G=>(G==null?void 0:G.marketId)===i);'
        'k&&(Xe(k.socketUrl??""),Lt(k.isToss),Pt(k.isBookmaker),It(k.isFancy),Q(!!(k.isScore||k.scoreIframe)),'
        'q(k.isTv),k.scoreIframe&&g(window._ex99ScoreUrl(k.scoreIframe)),T(k.socketUrl),R(k.tvUrl))}',
    )
    content = content.replace(
        'const kr=async()=>{try{await Tr(),await Kt()}catch(a){console.error("Error in setupAsyncActions:",a)}},Tr=async()=>{try{const m=await o(Br({marketId:i}));if(m!=null&&m.payload){const h=m.payload;if(p(h),b((h==null?void 0:h.socketPerm)??!1),T((h==null?void 0:h.socketUrl)??""),g((h==null?void 0:h.scoreIframe)??""),R((h==null?void 0:h.tvUrl)??""),F((h==null?void 0:h.cacheUrl)??""),_((h==null?void 0:h.notification)??""),q((h==null?void 0:h.isTv)??!1),Q((h==null?void 0:h.isScore)??!1),Pt((h==null?void 0:h.isBookmaker)??!1),It((h==null?void 0:h.isFancy)??!1),Zs((h==null?void 0:h.isMatchOdds)??!1),er((h==null?void 0:h.isTieOdds)??!1),Lt((h==null?void 0:h.isToss)??!1),rr((h==null?void 0:h.isCompletedOd',
        'const kr=async()=>{try{Tr().catch(()=>{}),await Kt()}catch(a){console.error("Error in setupAsyncActions:",a)}},Tr=async()=>{try{const m=await o(Br({marketId:i}));if(m!=null&&m.payload){const h=m.payload;if(p(h),b((h==null?void 0:h.socketPerm)??!1),T((h==null?void 0:h.socketUrl)??""),h!=null&&h.scoreIframe&&g(window._ex99ScoreUrl(h.scoreIframe)),R((h==null?void 0:h.tvUrl)??""),F((h==null?void 0:h.cacheUrl)??""),_((h==null?void 0:h.notification)??""),q((h==null?void 0:h.isTv)??!1),Q(!!((h==null?void 0:h.isScore)||(h==null?void 0:h.scoreIframe))),Pt((h==null?void 0:h.isBookmaker)??!1),It((h==null?void 0:h.isFancy)??!1),Zs((h==null?void 0:h.isMatchOdds)??!1),er((h==null?void 0:h.isTieOdds)??!1),Lt((h==null?void 0:h.isToss)??!1),rr((h==null?void 0:h.isCompletedOd',
    )
    content = content.replace(
        'Cr=x.useCallback(()=>{r(a=>!a)},[])',
        'Cr=x.useCallback(()=>{r(a=>{var nx=!a;try{document.querySelectorAll(\'iframe[src*="crickexpo"],iframe[src*="score.tresting"]\').forEach(function(f){'
        'f.className=nx?"h-[280px] opacity-100":"h-[135px] opacity-100";f.style.height=(nx?"280px":"135px");f.style.width="100%"})}catch(e){}return nx})},[])',
    )
    content = content.replace(
        'overflow-hidden bg-[#000000c6] transition-all duration-500 ease-in-out tabing ${e?"max-h-[500px] opacity-100":"max-h-0 opacity-0"}',
        'overflow-hidden bg-[#000000c6] transition-all duration-75 ease-in-out tabing ${e?"max-h-[500px] opacity-100":"max-h-0 opacity-0"}',
    )
    content = content.replace(
        'kr()}catch(h){console.error("Error in localStorage useEffect:",h)}},[o,l,i]);',
        'pe({}),Ee([]),Tr().catch(()=>{}),kr()}catch(h){console.error("Error in localStorage useEffect:",h)}},[o,l,i]);'
        'x.useEffect(()=>{try{const _u=window._ex99ScoreUrl(String(l||""));if(_u){g(_u);Q(!0)}}catch(_e){}},[l,i]);',
    )
    # Score iframe — API wait ke bina turant + animation band
    content = content.replace(
        's.jsx("div",{className:"overflow-hidden transition-all duration-500 ease-in-out -my-[3px]",children:J&&N&&s.jsx("iframe",{style:{width:"100%"},src:N,',
        's.jsx("div",{className:"overflow-hidden -my-[3px]",children:J&&N&&s.jsx("iframe",{loading:"eager",referrerPolicy:"no-referrer",style:{width:"100%",height:n?280:135},src:N,',
    )
    # Kt — direct /v1/ fetch (redux cache miss par bhi sahi list); declare ke baad turant clear
    content = content.replace(
        'const Kt=async()=>{var a,m;try{const v=await o(Lr({marketId:i,oddsBet:!0,fancyBet:!0,isDeleted:0})),w=(m=(a=v==null?void 0:v.payload)==null?void 0:a.userinfo)==null?void 0:m.data;if(!w){console.warn("No bet list data received.");return}',
        'const Kt=async()=>{var a,m;try{let w=null;try{const _u=JSON.parse(localStorage.getItem("user")||"{}");'
        'const _r=await fetch("/v1/user/clientBetListByMarketId",{method:"POST",headers:{"Content-Type":"application/json",'
        'Authorization:"Bearer "+(_u.token||"")},body:JSON.stringify({marketId:i,oddsBet:!0,fancyBet:!0,isDeleted:0})});'
        'const _j=await _r.json();if(_j&&!_j.error)w=_j.data}catch(_e){}'
        'if(!w)return;',
    )
    # Bet list — cache poll par refresh mat karo (blink / flicker band)
    content = content.replace(
        'await Xe(h.socketUrl??""),typeof Kt=="function"&&Kt().catch(()=>{})}}catch(a){console.error("Error in getMatchDataByMarketIdWise:",a)}},',
        'await Xe(h.socketUrl??"")}}catch(a){console.error("Error in getMatchDataByMarketIdWise:",a)}},',
    )
    content = content.replace(
        '};window.__ex99Kt=Kt;x.useEffect(()=>{Kt().catch(()=>{});'
        'const _wf=()=>Kt().catch(()=>{});window.addEventListener("ex99-wallet",_wf);'
        'const _iv=setInterval(_wf,1500);return()=>{clearInterval(_iv),window.removeEventListener("ex99-wallet",_wf)}},[i]);'
        'const Rr=x.useCallback(()=>{t(a=>!a)},[])',
        '};window.__ex99Kt=Kt;const Rr=x.useCallback(()=>{t(a=>!a)},[])',
    )
    # Odds/runs blink — har price tick par animate-blink mat chalao
    content = content.replace(
        'if(t!==b){l(!0);const j=setTimeout(()=>l(!1),500);return d.current=t,()=>clearTimeout(j)}',
        'd.current=t',
    )
    content = content.replace(
        'xt.current=setInterval(async()=>{Er(a)},1e3)',
        f'xt.current=setInterval(async()=>{{Er(a)}},{POLL_JS})',
    )
    # Bookmaker/match table P/L — cache team_data uses selectionid (1/2)
    content = content.replace("n[d.selectionId]", "n[d.selectionid??d.selectionId??d.bookmakerSelectionId]")
    content = content.replace("o[d.selectionId]", "o[d.selectionid??d.selectionId??d.bookmakerSelectionId]")
    # Bet success ke baad header turant refresh (original site 5s poll + localStorage)
    content = content.replace(
        'localStorage.setItem("exposure",JSON.stringify(((O=(Ne=(he=K==null?void 0:K.payload)==null?void 0:he.userinfo)==null?void 0:Ne.data)==null?void 0:O.exposure)??0)),await Kt(),await o(Ur())',
        'localStorage.setItem("exposure",JSON.stringify(((O=(Ne=(he=K==null?void 0:K.payload)==null?void 0:he.userinfo)==null?void 0:Ne.data)==null?void 0:O.exposure)??0)),'
        'window.dispatchEvent(new Event("ex99-wallet")),await Kt(),await o(Ur())',
    )
    # iOS Safari: type=number invalid value → "string did not match expected pattern"
    content = content.replace(
        'type:"number",placeholder:"AMOUNT",value:o,onChange:g=>c(g.target.value)',
        'type:"text",inputMode:"numeric",pattern:"[0-9]*",placeholder:"AMOUNT",value:o==null?"":String(o),onChange:g=>c((g.target.value||"").replace(/\\D/g,""))',
    )
    # Legacy broken scorecard patch (sf undeclared in strict mode)
    content = content.replace(
        "(sf=h==null?void 0:h.scoreIframe,sf&&g(window._ex99ScoreUrl(sf)))",
        "h!=null&&h.scoreIframe&&g(window._ex99ScoreUrl(h.scoreIframe))",
    )
    # Session bet — row max (green text) validate + API ko max bhejo
    content = content.replace(
        'run:l.runsNo,selectionId:l.session_id,fancyType',
        'run:l.runsNo,selectionId:l.session_id,max:l.max,fancyType',
    )
    content = content.replace(
        'run:l.runsYes,selectionId:l.session_id,fancyType',
        'run:l.runsYes,selectionId:l.session_id,max:l.max,fancyType',
    )
    content = content.replace(
        'run:j.runsYes,selectionId:j.session_id,fancyType',
        'run:j.runsYes,selectionId:j.session_id,max:j.max,fancyType',
    )
    content = content.replace(
        'if(L<(de||0)){je.error(`Please enter minimum amount: ${de}`,.8);return}const oe={odds:Ht,amount:L,selectionId:',
        'if(L<(de||0)){je.error(`Please enter minimum amount: ${de}`,.8);return}'
        'if(A){const _mx=Number((C==null?void 0:C.max)||0);if(_mx>0&&L>_mx){je.error(`Please enter maximum amount: ${_mx}`,.8);return}}'
        'const oe={odds:Ht,amount:L,selectionId:',
    )
    content = content.replace(
        'type:(C==null?void 0:C.betType)??""};ce&&(oe.betfairMarketId=',
        'type:(C==null?void 0:C.betType)??""};C!=null&&C.max&&(oe.max=C.max);ce&&(oe.betfairMarketId=',
    )
    # Original scraped scorecard (tresting socket iframe) — local scorecard override hatao
    content = content.replace(
        'return m?"/scorecard.html?gmid="+m[1]+"&eid=4&v=ex99sc11":u',
        'return m?"https://score.tresting.com/socket-iframe-21/crickexpo/"+m[1]:u',
    )
    content = content.replace(
        'return m?"/scorecard.html?gmid="+m[1]+"&eid=4&v=ex99sc10":u',
        'return m?"https://score.tresting.com/socket-iframe-21/crickexpo/"+m[1]:u',
    )
    content = content.replace(
        'if(/scorecard\\.html/i.test(u))return u;',
        'if(/score\\.tresting\\.com|akamaized|socket-iframe|crickexpo/i.test(u))return u;',
    )
    if "window._ex99ScoreUrl=" in content:
        idx = content.find("window._ex99ScoreUrl=")
        end = content.find("};", idx)
        if end > idx:
            content = content[:idx] + _TRESTING_SCORE_URL_JS + content[end + 2 :]
    return content


def patch_aviator_js(content: str) -> str:
    """Cashout API ko current multiplier bhejo (local backend ke liye)."""
    content = content.replace(
        'rt={roundId:He,eventId:303031,betId:$==null?void 0:$.betInsertId};try{(B==null?void 0:B.gameStatus)==="open"',
        'rt={roundId:He,eventId:303031,betId:$==null?void 0:$.betInsertId,multiplier:C==null?void 0:C.curentMultiplier};try{(B==null?void 0:B.gameStatus)==="open"',
    )
    content = content.replace(
        'const He={roundId:C==null?void 0:C.roundId,eventId:303031,betId:$==null?void 0:$.betInsertId};pe(fr(He))',
        'const He={roundId:C==null?void 0:C.roundId,eventId:303031,betId:$==null?void 0:$.betInsertId,multiplier:oe};pe(fr(He))',
    )
    # Crash/result ke baad open bet loss settle + exposure clear
    content = content.replace(
        'W.gameStatus==="close"&&(pe(Kr()),pe(ei()),N([]))',
        'W.gameStatus==="close"&&($!=null&&$.betInsertId&&!i&&fetch("/v1/casino/avaitorRoundLost",{method:"POST",'
        'headers:{"Content-Type":"application/json",Authorization:"Bearer "+((()=>{try{return JSON.parse('
        'localStorage.getItem("user")||"{}").token||""}catch(e){return""}})())},body:JSON.stringify({roundId:W.roundId,'
        'betId:$.betInsertId,crashValue:W.crashValue})}).then(rt=>rt.json()).then(n=>{var x=(n==null?void 0:n.data)||n;'
        'x!=null&&(x.totalCoins!=null&&localStorage.setItem("balance",JSON.stringify(x.totalCoins)),'
        'x.exposure!=null&&localStorage.setItem("exposure",JSON.stringify(Number(x.exposure)||x.exposure)))}).catch(()=>{}),'
        'pe(Kr()),pe(ei()),N([]))',
    )
    return content


def patch_virtual_casino_js(content: str) -> str:
    """Virtual casino grid — staff int casino edits MongoDB se load karo."""
    old = (
        'const u=()=>{const a=c(),[i,s]=p.useState(!1),l=[...r().isAviator===!0?[{title:"AVIATOR",subtitle:"AVIATOR",'
        'icon:"/images/aviator.jpeg",description:"",path:"/app/aviator"}]:[],{title:"DUS KA DAM",subtitle:"CASINO",'
        'icon:"/images/duskadum.jpg",description:"",isUpcoming:!0},{title:"TEEN PATTI",subtitle:"TEEN PATTI",'
        'icon:"/images/tp1.jpg",description:"",path:"/app/ledger"},{title:"ANDAR BAHAR",subtitle:"ANDAR BAHAR",'
        'icon:"/images/andar-bahar.webp",description:"",path:"/app/client-Statement"}];return'
    )
    new = (
        'const u=()=>{const a=c(),[i,s]=p.useState(!1),[l,f]=p.useState([]);'
        'p.useEffect(()=>{fetch("/v1/casino/getVirtualCasinoData",{method:"POST",headers:{"Content-Type":"application/json"}})'
        '.then(x=>x.json()).then(x=>{let g=(x==null?void 0:x.data)||[];'
        'r().isAviator!==!0&&(g=g.filter(e=>e.title!=="AVIATOR"));f(g)}).catch(()=>{})},[]);return'
    )
    if old not in content:
        raise RuntimeError("VirtualCasino patch anchor missing")
    return content.replace(old, new, 1)


def patch_casino_js(content: str) -> str:
    """Casino stream URLs local proxy par point karo + live result settlement."""
    content = content.replace("https://casinostream.tresting.com", STREAM_LOCAL_PREFIX.rstrip("/"))
    content = content.replace("http://casinostream.tresting.com", STREAM_LOCAL_PREFIX.rstrip("/"))
    content = content.replace("https://stream.1ex99.in", f"{STREAM_LOCAL_PREFIX.rstrip('/')}/stream99")

    auth_hdr = '(_u&&_u.token?"Bearer "+_u.token:(_u&&_u.Authorization?_u.Authorization:""))'
    wallet_refresh = (
        'var _ex99WalletRefresh=function(_u){'
        f'fetch("/v1/user/userBalance",{{method:"POST",headers:{{"Content-Type":"application/json",Authorization:{auth_hdr}}},body:"{{}}"}})'
        '.then(function(_br){return _br.json()}).then(function(_bj){'
        'try{var _bd=_bj&&_bj.data;if(_bd){if(_bd.coins!=null)localStorage.setItem("balance",JSON.stringify(_bd.coins));'
        'if(_bd.exposure!=null)localStorage.setItem("exposure",JSON.stringify(_bd.exposure));'
        'window.dispatchEvent(new Event("ex99-wallet"));}if(typeof window._ex99RefreshCasino==="function")window._ex99RefreshCasino();'
        'window.dispatchEvent(new CustomEvent("ex99CasinoSettled"))}catch(_e){}'
        '}).catch(function(){try{if(typeof window._ex99RefreshCasino==="function")window._ex99RefreshCasino();'
        'window.dispatchEvent(new CustomEvent("ex99CasinoSettled"))}catch(_e){}})};'
    )
    sync_fetch = (
        f'return fetch("/v1/casino/syncLiveRoundResult",{{method:"POST",headers:{{"Content-Type":"application/json",Authorization:{auth_hdr}}},'
        'body:JSON.stringify({roundId:String(_mid),result:String(_res),casinoType:_g||""})})'
        '.then(function(_resp){return _resp.json()}).then(function(_j){{if(_j&&!_j.error)window._ex99SR[_mk]=_res;return _j}}).catch(function(){{return null}})'
    )
    sync_body = (
        ',(function(_evt){try{'
        'var _r=_evt&&(_evt.result||(_evt.data&&_evt.data.result)||(_evt.data&&_evt.data.data&&_evt.data.data.result));'
        'var _g=_evt&&_evt.data&&_evt.data.t1&&_evt.data.t1[0]&&_evt.data.t1[0].gtype;'
        'if(!_r)return;var _a=Array.isArray(_r)?_r:[_r];'
        'window._ex99SR=window._ex99SR||{};'
        'if(window._ex99Base==null){window._ex99Base=1;'
        'for(var _bi=0;_bi<_a.length;_bi++){var _bl=_a[_bi];'
        'if(_bl&&_bl.mid&&_bl.result)window._ex99SR[String(_bl.mid)]=String(_bl.result);}'
        'return;}'
        'var _u=JSON.parse(localStorage.getItem("user")||"{}");'
        + wallet_refresh +
        'var _n=0,_ps=[];for(var _i=0;_i<_a.length;_i++){var _l=_a[_i];'
        'if(!_l||!_l.mid||!_l.result)continue;var _mk=String(_l.mid),_rv=String(_l.result);'
        'if(window._ex99SR[_mk]===_rv)continue;window._ex99SR[_mk]=_rv;_n++;'
        '_ps.push((function(_mid,_res,_mk){' + sync_fetch + '})(_mk,_rv,_mk));}'
        'if(_n>0){Promise.all(_ps).finally(function(){_ex99WalletRefresh(_u);'
        'setTimeout(function(){if(typeof window._ex99RefreshCasino==="function")window._ex99RefreshCasino();},400);});}'
        '}catch(_e){}})(__VAR__)'
    )

    content = _inject_casino_refresh_hook(content)

    # Per-game min/max — game config load par window par store karo
    content = content.replace(
        "n!=null&&n.eventId&&H(n.eventId)",
        "n!=null&&n.eventId&&(window.__ex99CasinoMin=Number(n.minStake||0)||0,window.__ex99CasinoMax=Number(n.maxStake||0)||0,H(n.eventId))",
    )
    content = content.replace(
        "l!=null&&l.eventId&&$(l.eventId)",
        "l!=null&&l.eventId&&(window.__ex99CasinoMin=Number(l.minStake||0)||0,window.__ex99CasinoMax=Number(l.maxStake||0)||0,$(l.eventId))",
    )
    content = content.replace(
        "x!=null&&x.eventId&&X(x.eventId)",
        "x!=null&&x.eventId&&(window.__ex99CasinoMin=Number(x.minStake||0)||0,window.__ex99CasinoMax=Number(x.maxStake||0)||0,X(x.eventId))",
    )
    content = content.replace(
        "n!=null&&n.eventId&&O(n.eventId)",
        "n!=null&&n.eventId&&(window.__ex99CasinoMin=Number(n.minStake||0)||0,window.__ex99CasinoMax=Number(n.maxStake||0)||0,O(n.eventId))",
    )
    content = content.replace(
        "d!=null&&d.eventId&&re(d.eventId)",
        "d!=null&&d.eventId&&(window.__ex99CasinoMin=Number(d.minStake||0)||0,window.__ex99CasinoMax=Number(d.maxStake||0)||0,re(d.eventId))",
    )
    # Diamond casino bet — submit se pehle min/max (har game ka alag amount var)
    import re as _re_casino

    def _inject_casino_stake_guard(m: _re_casino.Match) -> str:
        loading_fn = m.group(1)
        obj_var = m.group(2)
        amount_var = m.group(4)
        toast_err = (
            f'{{const _exA=Number({amount_var})||0,_exMn=Number(window.__ex99CasinoMin||0),'
            f'_exMx=Number(window.__ex99CasinoMax||0);'
            f'if(_exMn>0&&_exA<_exMn){{typeof ee!="undefined"&&ee.error?ee.error({{content:"Please enter minimum amount: "+_exMn,duration:3}}):'
            f'typeof _!="undefined"&&_.error&&_.error({{content:"Please enter minimum amount: "+_exMn,duration:3}});'
            f'{loading_fn}(!1);return}}'
            f'if(_exMx>0&&_exA>_exMx){{typeof ee!="undefined"&&ee.error?ee.error({{content:"Please enter maximum amount: "+_exMx,duration:3}}):'
            f'typeof _!="undefined"&&_.error&&_.error({{content:"Please enter maximum amount: "+_exMx,duration:3}});'
            f'{loading_fn}(!1);return}}}}'
        )
        return f"{loading_fn}(!0);{toast_err}let {obj_var}={{roundId:{m.group(3)}amount:Number({amount_var})"

    content, _ = _re_casino.subn(
        r"(\w+)\(!0\);let ([a-z])=\{roundId:([\s\S]{0,400}?)amount:Number\((\$|\w+)\)",
        _inject_casino_stake_guard,
        content,
    )

    if "syncLiveRoundResult" not in content:
        import re
        def _inject_sync(m: re.Match) -> str:
            return m.group(0) + sync_body.replace("__VAR__", m.group(2))
        content, n = re.subn(
            r"(\w+)\((\w+)==null\?void 0:\2\.result\)",
            _inject_sync,
            content,
            count=1,
        )

    return content


def _inject_casino_refresh_hook(content: str) -> str:
    """Bet table refresh — mount par pull + settle event listener, hooks ke bina."""
    import re

    if "ex99CasinoRefreshReg" in content or "casinoBetData" not in content:
        return content

    bet_m = re.search(r",(\w+)=async (\w+)=>\{.*?casinoBetData", content, re.DOTALL)
    if not bet_m:
        return content
    fn_name = bet_m.group(1)
    param = bet_m.group(2)
    bal_m = re.search(rf"await (\w+)\((\w+)\(\)\),await {re.escape(fn_name)}\(", content)
    dispatch_var = bal_m.group(1) if bal_m else ""
    bal_var = bal_m.group(2) if bal_m else ""
    route_m = re.search(r"\{eventId:(\w+)\}=\w+\(\)", content)
    route_evt = route_m.group(1) if route_m else ""
    evt_m = re.search(rf"(?:await )?{re.escape(fn_name)}\((\w+)\)", content)
    evt_var = evt_m.group(1) if evt_m else param
    fallback_m = re.search(rf"let {re.escape(evt_var)}={route_evt}\|\|\"(\d+)\"", content) if route_evt else None
    if route_evt and fallback_m:
        evt_expr = f"{route_evt}||{evt_var}"
    elif route_evt:
        evt_expr = route_evt
    else:
        evt_expr = evt_var

    bal_part = ""
    if dispatch_var and bal_var:
        bal_part = f"typeof {dispatch_var}==='function'&&typeof {bal_var}==='function'&&{dispatch_var}({bal_var}());"

    reg = (
        "try{window._ex99RefreshCasino=function(){"
        f"var _ev={evt_expr};"
        f"typeof {fn_name}==='function'&&_ev&&{fn_name}(_ev);"
        + bal_part +
        "};}catch(_e){}/*ex99CasinoRefreshReg*/"
    )

    opener = f",{fn_name}=async {param}=>{{"
    if opener not in content:
        return content
    content = content.replace(opener, opener + reg, 1)

    if route_evt:
        setup_m = re.search(r"\(async\(\)=>await (\w+)\(t\)\)\(\)", content)
        setup_fn = setup_m.group(1) if setup_m else ""
        mount_old = f"r.useEffect(()=>{{const t={{eventId:{route_evt}}};"
        if mount_old in content:
            mount_new = (
                f"r.useEffect(()=>{{const t={{eventId:{route_evt}}};"
                f"window._ex99Base=null;window._ex99SR={{}};"
                f"window._ex99RefreshCasino=function(){{try{{var _ev={evt_expr};"
                f"typeof {fn_name}==='function'&&_ev&&{fn_name}(_ev);{bal_part}}}catch(_e){{}}}};"
                f'window.addEventListener("ex99CasinoSettled",window._ex99RefreshCasino);'
                f"const _ex99Iv=setInterval(function(){{window._ex99RefreshCasino&&window._ex99RefreshCasino();}},4000);"
            )
            content = content.replace(mount_old, mount_new, 1)
            if setup_fn:
                content = content.replace(
                    f"(async()=>await {setup_fn}(t))()",
                    f"(async()=>{{var _ev={evt_expr};typeof {fn_name}==='function'&&_ev&&await {fn_name}(_ev);await {setup_fn}(t);}})()",
                    1,
                )
            content, _ = re.subn(
                rf"clearInterval\((\w+\.current)\)\}}\}},\[{re.escape(route_evt)},(\w+)\.pathname\]\)",
                rf'clearInterval(\1),clearInterval(_ex99Iv),window.removeEventListener("ex99CasinoSettled",window._ex99RefreshCasino)}}}},[{route_evt},\2.pathname])',
                content,
                count=1,
            )

    return content


def patch_login_js(content: str) -> str:
    """Login page — demo login band, sirf host fix."""
    content = content.replace(
        "host:window.location.host",
        f'host:"{SITE_HOST}"',
    )
    # ons3 theme: hardcoded demo button
    content = content.replace(
        ',e.jsx("button",{type:"submit",className:"w-full !rounded-[5px] linear-login-btn !black-color !font-bold py-3 px-4  transition-colors duration-200 !text-[15px]",children:"LOGIN WITH DEMO ID"})',
        "",
    )
    # 1ex99 theme: conditional demo button
    content = content.replace(
        ',a().loginByDemo===!0&&e.jsx("button",{onClick:I,type:"submit",className:"w-full login-btn white-color !font-bold py-3 px-4 -mt-2 !rounded-sm transition-colors duration-200 !text-[15px]",children:"LOGIN WITH DEMO ID"})',
        "",
    )
    # demo login handler disable
    content = content.replace("I=async n=>{", "I=async n=>{return;")
    # Login logo — original scraped site jaisa size (225px + object-contain)
    content = content.replace(
        'src:a().loginLogo,className:"w-[225px] h-[225px]"',
        'src:a().loginLogo,className:"w-[225px] h-[225px] object-contain"',
    )
    return content


def patch_client_statement_js(content: str) -> str:
    """Passbook balance — API se aaya balance use karo (sum(amounts) galat hai open bets par)."""
    old = (
        "let d=0;s==null||s.forEach(r=>{d+=r.amount});let b=0,x=0;"
        "const o=s==null?void 0:s.map(r=>(b=d-x,x+=r.amount,{amount:r.amount,balance:b,"
    )
    new = (
        "const o=s==null?void 0:s.map(r=>({amount:r.amount,balance:r.balance!=null?r.balance:0,"
    )
    if old not in content:
        raise RuntimeError("ClientStatement passbook balance patch anchor missing")
    return content.replace(old, new, 1)


def patch_inplay_js(content: str) -> str:
    """Inplay — original live site jaisa 2s matchList polling + localStorage cache."""
    content = patch_js_content(content)

    _poll_mount = (
        f'c.useEffect(()=>{{const _ipLoad=async(_ld)=>{{try{{if(_ld)u(!0);await n(N());if(_ld)u(!1)}}catch(a){{console.error("Error fetching match list:",a);if(_ld)u(!1)}}}};'
        f'_ipLoad(!0);const _ipIv=setInterval(()=>_ipLoad(!1),{POLL_JS});'
        f'return()=>clearInterval(_ipIv)}},[n,l]);'
    )

    # Original mount + purane broken patches — sab ko live polling se replace karo
    for old in (
        'c.useEffect(()=>{(async()=>{try{let a=JSON.parse(localStorage.getItem("matchList"));'
        'if(a&&a.length>0){const b=a.filter(f=>(f==null?void 0:f.sportId)===l);j(a),h(b),await n(N())'
        '}else u(!0),await n(N()),u(!1)}catch(a){console.error("Error fetching match list:",a)}})()},[n]);',
        'useEffect(()=>{(async()=>{try{let a=JSON.parse(localStorage.getItem("matchList"));'
        'if(a&&a.length>0){const b=a.filter(f=>(f==null?void 0:f.sportId)===l);j(a),h(b),await n(N())'
        '}else u(!0),await n(N()),u(!1)}catch(a){console.error("Error fetching match list:",a)}})()},[n]);',
        'useEffect(()=>{(async()=>{if(window._ex99InplayReady)return;window._ex99InplayReady=1;'
        'try{let a=JSON.parse(localStorage.getItem("matchList"));'
        'if(a&&a.length>0){const b=a.filter(f=>(f==null?void 0:f.sportId)===l);j(a),h(b);'
        'n(N()).catch(()=>{})}else{u(!0),await n(N()),u(!1)}'
        '}catch(a){console.error("Error fetching match list:",a);window._ex99InplayReady=0}})()},[]);',
    ):
        content = content.replace(old, _poll_mount)

    _orig_redux = (
        f'c.useEffect(()=>{{const _ex99St={_EX99_STARTED_FN};if(d&&d.length>0){{const s=d.filter(a=>'
        f'(a==null?void 0:a.sportId)===l&&(a.scrapeLive===!0||_ex99St(a))&&((a==null?void 0:a.inPlayStatus)===!0||'
        f'String((a==null?void 0:a.status)||"").toUpperCase()==="INPLAY"));h(s),j(d)}}}},[d,l])'
    )
    content = content.replace(_orig_redux, _orig_redux)
    content = content.replace(
        'useEffect(()=>{if(d&&d.length>0){const s=d.filter(a=>(a==null?void 0:a.sportId)===l);'
        'const _k=JSON.stringify(s.map(x=>x&&x.marketId));if(window._ex99Mk===_k)return;'
        'window._ex99Mk=_k;h(s),j(d)}},[d,l])',
        'useEffect(()=>{if(d&&d.length>0){const s=d.filter(a=>(a==null?void 0:a.sportId)===l);h(s),j(d)}},[d,l])',
    )
    # Purana localStorage fallback hatao — admin matchList cache blocked match flash karta tha
    content = content.replace(
        'c.useEffect(()=>{if(d&&d.length>0){const s=d.filter(a=>(a==null?void 0:a.sportId)===l);h(s),j(d)}'
        'else{try{let a=JSON.parse(localStorage.getItem("matchList"));'
        'if(a&&a.length>0){const b=a.filter(f=>(f==null?void 0:f.sportId)===l);j(a),h(b)}}catch(_e){}}},[d,l])',
        _orig_redux,
    )

    content = content.replace(
        'v=s=>{x(`/app/match-detail/${s==null?void 0:s.marketId}/${s==null?void 0:s.eventId}`)}',
        'v=s=>{try{window.ex99WarmScore&&window.ex99WarmScore(s==null?void 0:s.eventId)}catch(_e){}'
        'x(`/app/match-detail/${s==null?void 0:s.marketId}/${s==null?void 0:s.eventId}`)}',
    )
    content = content.replace(
        'useEffect(()=>{if(d&&d.length>0){const s=d.filter(a=>(a==null?void 0:a.sportId)===l);h(s),j(d)}},[d,l])',
        'useEffect(()=>{if(d&&d.length>0){const s=d.filter(a=>(a==null?void 0:a.sportId)===l);h(s),j(d);'
        'try{window.ex99WarmMatchList&&window.ex99WarmMatchList(d)}catch(_e){}}},[d,l])',
    )
    content = content.replace('!1?e.jsx(k,{}):', 'w?e.jsx(k,{}):')
    content = content.replace(']},s.marketId):null)', ']},(s.marketId||s.eventId||a)):null)')
    content = content.replace(']},a):null)', ']},(s.marketId||s.eventId||a)):null)')
    content = content.replace(
        '],a)):e.jsx("p",{className:"text-center text-sm font-semibold text-gray-500"',
        '],(s.marketId||s.eventId||a)):e.jsx("p",{className:"text-center text-sm font-semibold text-gray-500"',
    )
    content = content.replace("const g=()=>Math.random()+1", "const g=()=>1.5")
    content = content.replace('class:"', 'className:"')
    content = content.replace(
        '(s==null?void 0:s.inPlayStatus)!==!1&&e.jsx(L,{})',
        '(s==null?void 0:s.inPlayStatus)!==!1&&e.jsx("span",{className:"h-2 w-2 rounded-full bg-[#bdff37] inline-block"})',
    )
    return content


def patch_js_content(content: str) -> str:
    """Point frontend API calls to local proxy."""
    content = content.replace(JS_PATCH_FROM, JS_PATCH_TO)
    content = content.replace(CACHE_REMOTE_PREFIX, CACHE_LOCAL_PREFIX)
    content = content.replace("http://1excache.tresting.com", CACHE_LOCAL_PREFIX)
    content = content.replace("https://1excache.tresting.com", CACHE_LOCAL_PREFIX)
    return content


def build_site_selector_html() -> str:
    """Post-login site selector with clickable theme rows."""
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Select Website</title>
  <style>
    * { box-sizing: border-box; }
    html, body {
      margin: 0;
      min-height: 100vh;
      font-family: Arial, sans-serif;
      color: #ffffff;
      background: #000000;
      padding: 0;
    }
    .stage {
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding:
        max(12px, env(safe-area-inset-top, 0px))
        max(12px, env(safe-area-inset-left, 0px))
        max(12px, env(safe-area-inset-bottom, 0px))
        max(12px, env(safe-area-inset-right, 0px));
    }
    .frame {
      position: relative;
      width: min(94vw, 441px);
      line-height: 0;
      cursor: pointer;
      -webkit-tap-highlight-color: transparent;
    }
    .frame img {
      display: block;
      width: 100%;
      height: auto;
      pointer-events: none;
      user-select: none;
      -webkit-user-drag: none;
    }
    .sr-only {
      position: absolute;
      width: 1px;
      height: 1px;
      padding: 0;
      margin: -1px;
      overflow: hidden;
      clip: rect(0, 0, 0, 0);
      white-space: nowrap;
      border: 0;
    }
    .toast {
      position: fixed;
      left: 50%;
      bottom: 28px;
      transform: translate(-50%, 24px);
      min-width: min(360px, calc(100vw - 32px));
      border: 1px solid rgba(255, 255, 255, 0.16);
      border-radius: 12px;
      background: rgba(15, 15, 18, 0.92);
      box-shadow: 0 14px 40px rgba(0, 0, 0, 0.42);
      color: #ffffff;
      font-size: 16px;
      font-weight: 700;
      line-height: 1.35;
      opacity: 0;
      padding: 14px 18px;
      pointer-events: none;
      text-align: center;
      transition: opacity 0.2s ease, transform 0.2s ease;
      z-index: 10;
    }
    .toast.show {
      opacity: 1;
      transform: translate(-50%, 0);
    }
  </style>
</head>
<body>
  <main class="stage" aria-label="Choose website">
    <div class="frame" id="selector" role="group" aria-label="Theme options">
      <img src="/IMG_9695.PNG" alt="Choose your theme" id="selector-img" />
      <span class="sr-only">Tap Betguru, Winpro, or 1ex99 theme row to continue</span>
    </div>
  </main>
  <div class="toast" id="toast" role="status" aria-live="polite">This feature is not available.</div>
  <script>
    const toast = document.getElementById("toast");
    let toastTimer;

    function showUnavailable() {
      clearTimeout(toastTimer);
      toast.classList.add("show");
      toastTimer = setTimeout(() => toast.classList.remove("show"), 2200);
    }

    /* IMG_9695.PNG (941x1672) — card rows top→bottom: Betguru, Winpro, 1ex99 */
    const ZONES = [
      { y0: 0.012, y1: 0.335, href: null, label: "Betguru" },
      { y0: 0.335, y1: 0.655, href: "https://winpro.1ex99.live/#/login?from=selector", label: "Winpro" },
      { y0: 0.655, y1: 0.985, href: "/login", label: "1ex99" },
    ];

    function zoneAt(img, clientY) {
      const rect = img.getBoundingClientRect();
      if (clientY < rect.top || clientY > rect.bottom) return -1;
      const frac = (clientY - rect.top) / rect.height;
      return ZONES.findIndex((z) => frac >= z.y0 && frac < z.y1);
    }

    function activate(idx) {
      if (idx < 0) return;
      const zone = ZONES[idx];
      if (!zone.href) {
        showUnavailable();
        return;
      }
      window.location.href = zone.href;
    }

    const frame = document.getElementById("selector");
    const img = document.getElementById("selector-img");

    frame.addEventListener("click", (e) => {
      activate(zoneAt(img, e.clientY));
    });

    frame.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        activate(2);
      }
    });
  </script>
</body>
</html>"""


def patch_html_content(content: str) -> str:
    """Remove Cloudflare beacon + inject local casino stream/card fixes."""
    content = re.sub(
        r'<script[^>]*cloudflareinsights[^>]*>.*?</script>',
        "",
        content,
        flags=re.DOTALL,
    )
    content = re.sub(
        r'<script[^>]*beacon\.min\.js[^>]*/>\s*</script>',
        "",
        content,
        flags=re.DOTALL,
    )
    content = re.sub(
        r'<!--\s*<meta charset="UTF-8"\s*/>\s*<link rel="icon" href="/favicon\.ico"\s*/>\s*-->',
        '<meta charset="UTF-8"/>',
        content,
    )
    if '<meta charset="UTF-8"' not in content and "<head>" in content:
        content = content.replace("<head>", '<head>\n  <meta charset="UTF-8"/>', 1)
    favicon_tags = (
        '<link rel="icon" href="/favicon.ico" sizes="any"/>'
        '<link rel="icon" type="image/png" href="/images/1exlogo.png"/>'
        '<link rel="apple-touch-icon" href="/apple-touch-icon.png"/>'
    )
    if 'rel="icon"' not in content and "<head>" in content:
        content = content.replace("<head>", f"<head>\n  {favicon_tags}", 1)
    content = re.sub(
        r"<title>[\s\S]*?</title>",
        "<title>1ex</title>",
        content,
        count=1,
    )
    inject = f"""<link rel="preconnect" href="https://score.tresting.com" crossorigin>
<link rel="dns-prefetch" href="//score.tresting.com">
<script>
(function(){{
  var SP="{STREAM_LOCAL_PREFIX.rstrip('/')}";
  function rw(u){{
    if(!u||typeof u!=="string")return u;
    if(/score\\.tresting\\.com|akamaized|socket-iframe|crickexpo\\/\\d+/i.test(u))return u;
    if(u.indexOf("scorecard.html")>=0){{
      var sm=u.match(/gmid=(\\d+)/);
      if(sm) return "https://score.tresting.com/socket-iframe-21/crickexpo/"+sm[1];
    }}
    return u
      .replace("https://casinostream.tresting.com",SP)
      .replace("http://casinostream.tresting.com",SP)
      .replace("https://stream.1ex99.in",SP+"/stream99");
  }}
  var desc=Object.getOwnPropertyDescriptor(HTMLIFrameElement.prototype,"src");
  if(desc&&desc.set){{
    Object.defineProperty(HTMLIFrameElement.prototype,"src",{{
      set:function(v){{desc.set.call(this,rw(v));}},
      get:desc.get,configurable:true
    }});
  }}
  var origSetAttribute=Element.prototype.setAttribute;
  Element.prototype.setAttribute=function(name,value){{
    if(this.tagName==="IFRAME"&&String(name).toLowerCase()==="src"){{
      value=rw(value);
    }}
    return origSetAttribute.call(this,name,value);
  }};
  new MutationObserver(function(){{
    document.querySelectorAll("iframe[src]").forEach(function(el){{
      var f=el.getAttribute("src");
      var n=rw(f);
      if(n&&n!==f)el.setAttribute("src",n);
    }});
  }}).observe(document.documentElement,{{childList:true,subtree:true,attributes:true,attributeFilter:["src"]}});
  window.ex99WarmScore=function(eid){{
    if(!eid)return;
    eid=String(eid).replace(/\\D/g,"");
    if(!eid)return;
    window.__ex99ScEid=eid;
    if(window.__ex99ScPf)return;
    var u="https://score.tresting.com/socket-iframe-21/crickexpo/"+eid;
    var f=document.createElement("link");
    f.rel="prefetch";f.href=u;f.as="document";
    document.head.appendChild(f);
  }};
  window.ex99WarmMatchList=function(list){{
    try{{
      if(!list||!list.length)return;
      var m=list.find(function(x){{return x&&x.eventId&&(x.isScore||x.scoreIframe);}});
      if(m)window.ex99WarmScore(m.eventId);
    }}catch(e){{}}
  }};
  function ex99WarmFromStorage(){{
    try{{
      var ml=JSON.parse(localStorage.getItem("matchList")||"[]");
      window.ex99WarmMatchList(ml);
    }}catch(e){{}}
  }}
  var _ex99SSI=Storage.prototype.setItem;
  Storage.prototype.setItem=function(k,v){{
    _ex99SSI.apply(this,arguments);
    if(k==="matchList")setTimeout(ex99WarmFromStorage,0);
  }};
  ex99WarmFromStorage();
  function ex99PrefetchRoute(){{
    try{{
      var m=location.pathname.match(/\\/match-detail\\/[^/]+\\/(\\d+)/);
      if(m)window.ex99WarmScore(m[1]);
    }}catch(e){{}}
  }}
  ex99PrefetchRoute();
  window.addEventListener("popstate",ex99PrefetchRoute);
  var _ps=history.pushState;history.pushState=function(){{_ps.apply(this,arguments);ex99PrefetchRoute()}};
  var _rs=history.replaceState;history.replaceState=function(){{_rs.apply(this,arguments);ex99PrefetchRoute()}};
  window.addEventListener("message",function(ev){{
    if(!ev.data||ev.data.type!=="ex99-sc-height")return;
    var raw=Number(ev.data.height)||88;
    document.querySelectorAll('iframe[src*="scorecard.html"]').forEach(function(f){{
      if(!(ev.source&&f.contentWindow===ev.source))return;
      var full=(f.className||"").indexOf("280")>=0||f.getAttribute("data-ex99-full")==="1"||ev.data.expanded;
      var cap=full?420:180;
      var h=Math.max(44,Math.min(raw,cap));
      f.style.height=h+"px";
    }});
  }});
}})();
</script>
<style id="ex99-mobile-fix">
iframe[src*="scorecard.html"]{{transition:none!important;animation:none!important}}
.scorecard,.scorecard *{{transition:none!important;animation:none!important}}
.animate-Blinking,.animate-pulse,.animate-blink{{animation:none!important}}
@media (max-width: 768px) {{
  .theme1.fixed {{
    height: 100dvh !important;
    max-height: 100dvh !important;
    top: 0 !important;
  }}
  .theme1 .overflow-y-auto {{
    -webkit-overflow-scrolling: touch;
    max-height: 100dvh !important;
    padding-bottom: calc(9rem + env(safe-area-inset-bottom, 0px)) !important;
  }}
  .theme1 nav.flex.flex-col {{
    padding-bottom: calc(2.5rem + env(safe-area-inset-bottom, 0px)) !important;
  }}
}}
</style>"""
    content = re.sub(
        r'src="/assets/index-CKjJtyLu\.js(\?v=[^"]*)?"',
        'src="/assets/index-CKjJtyLu.js?v=ex99orig27"',
        content,
        count=1,
    )
    if "</head>" in content:
        content = content.replace("</head>", inject + "\n</head>", 1)
    return content


class LocalSiteHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def end_headers(self):
        if not getattr(self, "_sent_connection_close", False):
            self.send_header("Connection", "close")
            self._sent_connection_close = True
        super().end_headers()

    def _send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def do_OPTIONS(self):
        if urlparse(self.path).path.startswith(ADMIN_MOUNT):
            self._proxy_admin_mount("OPTIONS")
            return
        self.send_response(204)
        self._send_cors_headers()
        self.end_headers()

    def do_POST(self):
        if urlparse(self.path).path.startswith(ADMIN_MOUNT):
            self._proxy_admin_mount("POST")
        elif self.path.startswith(API_LOCAL_PREFIX):
            self._proxy_api()
        else:
            self.send_error(404, "Not Found")

    def do_PATCH(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith(ADMIN_MOUNT):
            self._proxy_admin_mount("PATCH")
        elif parsed.path.startswith(API_LOCAL_PREFIX):
            self._proxy_api()
        else:
            self.send_error(404, "Not Found")

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == ADMIN_MOUNT:
            self.send_response(302)
            self.send_header("Location", ADMIN_MOUNT + "/")
            self.end_headers()
            return

        # /admin/admin/app/... — double prefix breaks React Router basename.
        if path.startswith(f"{ADMIN_MOUNT}{ADMIN_MOUNT}/") or path == f"{ADMIN_MOUNT}{ADMIN_MOUNT}":
            location = path[len(ADMIN_MOUNT):]
            if parsed.query:
                location += "?" + parsed.query
            self.send_response(301)
            self.send_header("Location", location)
            self.send_header("Cache-Control", "no-cache")
            self._send_cors_headers()
            self.end_headers()
            return

        if path.startswith(ADMIN_MOUNT + "/"):
            self._proxy_admin_mount("GET")
            return

        if path.startswith(API_LOCAL_PREFIX):
            self.send_error(405, "Method Not Allowed")
            return

        if path in ("/", SITE_SELECTOR_PATH):
            self._serve_site_selector()
            return

        if path.startswith(STREAM_LOCAL_PREFIX):
            self._proxy_casino_stream()
            return

        if path.startswith(CACHE_LOCAL_PREFIX):
            self._proxy_cache()
            return

        if path.startswith("/sport/scorecard/"):
            self._proxy_scorecard()
            return

        if is_admin_only_app_path(path):
            location = ADMIN_MOUNT + path
            if parsed.query:
                location += "?" + parsed.query
            self.send_response(302)
            self.send_header("Location", location)
            self.send_header("Cache-Control", "no-cache")
            self._send_cors_headers()
            self.end_headers()
            return

        admin_legacy_js = {
            "/dist/js/demo.js": ADMIN_LEGACY_JS_STUB,
            "/dist/js/dashboard.js": ADMIN_LEGACY_JS_STUB,
            "/dist/js/pages/dashboard.js": ADMIN_LEGACY_JS_STUB,
            "/dist/js/adminlte.js": ADMIN_LEGACY_JS_STUB,
        }
        if path in admin_legacy_js:
            body = admin_legacy_js[path]
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(body)
            return

        if self._try_serve_admin_root_static(path):
            return

        file_path = self._resolve_file(path)

        if file_path is None:
            file_path = SITE_DIR / "index.html"
            path = "/index.html"
        elif not file_path.exists():
            file_path = self._try_image_fallback(file_path)

        if not file_path.exists():
            # SPA fallback for React routes like /app/dashboard
            file_path = SITE_DIR / "index.html"
            path = "/index.html"

        self._serve_file(file_path, path)

    def _try_image_fallback(self, file_path: Path) -> Path:
        """Casino/card images - alternate paths try karo."""
        if file_path.exists():
            return file_path

        name = file_path.name
        parent = file_path.parent.name  # 'images' or 'cards'

        # webp -> png (casino thumbnails)
        if name.endswith(".webp"):
            png_path = file_path.with_suffix(".png")
            if png_path.exists():
                return png_path

        # CardBox: /images/7.jpg missing -> /cards/1.png (face-down back)
        if parent == "images" and re.match(r"^\d+\.jpg$", name):
            back = SITE_DIR / "cards" / "1.png"
            if back.exists():
                return back

        # /cards/NUM.png missing -> /images/NUM.jpg
        if parent == "cards" and re.match(r"^\d+\.png$", name):
            jpg = SITE_DIR / "images" / name.replace(".png", ".jpg")
            if jpg.exists():
                return jpg

        return file_path

    def _try_serve_admin_root_static(self, path: str) -> bool:
        """Serve admin webpack assets requested without /admin prefix (cached bundles)."""
        if not any(path.startswith(prefix) for prefix in ADMIN_STATIC_ROOT_PREFIXES):
            return False
        file_path = ADMIN_DIR / path.lstrip("/")
        if not file_path.is_file():
            return False
        try:
            content = file_path.read_bytes()
            content_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
            if file_path.suffix == ".js":
                content_type = "application/javascript; charset=utf-8"
            elif file_path.suffix == ".css":
                content_type = "text/css; charset=utf-8"
            if file_path.suffix == ".js":
                content_type = "application/javascript; charset=utf-8"
                text = content.decode("utf-8")
                text = patch_admin_js(text)
                content = text.encode("utf-8")
            content = self._rewrite_admin_mount_body(content, content_type)
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-cache")
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(content)
            return True
        except Exception as exc:
            print(f"[admin-static] {path}: {exc}")
            return False

    def _resolve_file(self, path: str) -> Optional[Path]:
        rel = path.lstrip("/")
        if not rel:
            return SITE_DIR / "index.html"
        return SITE_DIR / rel

    def _serve_file(self, file_path: Path, url_path: str):
        try:
            content = file_path.read_bytes()
            content_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"

            if file_path.name == "index.html" or url_path == "/index.html":
                content = patch_html_content(content.decode("utf-8")).encode("utf-8")
                content_type = "text/html; charset=utf-8"

            if file_path.suffix == ".js":
                js = content.decode("utf-8")
                if file_path.name.startswith("index-"):
                    js = patch_index_js(js)
                elif file_path.name == "Login-CNc67dgC.js":
                    js = patch_login_js(js)
                elif file_path.name.startswith("ChangePassword-"):
                    js = patch_change_password_js(js)
                elif file_path.name == "MatchDetail-DcLvOyoM.js":
                    js = patch_match_detail_js(js)
                elif file_path.name == "Inplay-wyzuWlIy.js":
                    js = patch_inplay_js(js)
                elif file_path.name == "ClientStatement-DCCbn1pb.js":
                    js = patch_client_statement_js(js)
                elif file_path.name == "AviatorGames-CHyQ2Vox.js":
                    js = patch_aviator_js(js)
                elif file_path.name == "VirtualCasino-DN1vVk0T.js":
                    js = patch_virtual_casino_js(js)
                elif file_path.parent.name == "assets":
                    js = patch_casino_js(patch_js_content(js))
                content = js.encode("utf-8")
                content_type = "application/javascript; charset=utf-8"

            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("CDN-Cache-Control", "no-store")
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(content)
        except Exception as exc:
            self.send_error(500, str(exc))

    def _rewrite_admin_mount_body(self, body: bytes, content_type: str) -> bytes:
        if not body:
            return body
        if not (
            "text/html" in content_type
            or "javascript" in content_type
            or "text/css" in content_type
        ):
            return body
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            return body

        rewrites = (
            ("https://api.ons3.co/v1/", f"{ADMIN_MOUNT}/v1/"),
            ("http://api.ons3.co/v1/", f"{ADMIN_MOUNT}/v1/"),
            ("https://march2026api.1ex99.in/v1/", f"{ADMIN_MOUNT}/v1/"),
            ('"/v1/', f'"{ADMIN_MOUNT}/v1/'),
            ("'/v1/", f"'{ADMIN_MOUNT}/v1/"),
            ('"/excache', f'"{ADMIN_MOUNT}/excache'),
            ("'/excache", f"'{ADMIN_MOUNT}/excache"),
            ('"/images/', f'"{ADMIN_MOUNT}/images/'),
            ("'/images/", f"'{ADMIN_MOUNT}/images/"),
            ('"/plugins/', f'"{ADMIN_MOUNT}/plugins/'),
            ("'/plugins/", f"'{ADMIN_MOUNT}/plugins/"),
            ('"/dist/', f'"{ADMIN_MOUNT}/dist/'),
            ("'/dist/", f"'{ADMIN_MOUNT}/dist/"),
            ('"/static/', f'"{ADMIN_MOUNT}/static/'),
            ("'/static/", f"'{ADMIN_MOUNT}/static/"),
            ('"/adminlite/', f'"{ADMIN_MOUNT}/adminlite/'),
            ("'/adminlite/", f"'{ADMIN_MOUNT}/adminlite/"),
            ("http://localhost:6565/", f"{ADMIN_MOUNT}/"),
            ('"/old-data/v1/', f'"{ADMIN_MOUNT}/old-data/v1/'),
            ("'/old-data/v1/", f"'{ADMIN_MOUNT}/old-data/v1/"),
            ('.p="/old-data/"', f'.p="{ADMIN_MOUNT}/old-data/"'),
            ('basename:"/old-data"', f'basename:"{ADMIN_MOUNT}/old-data"'),
        )
        for old, new in rewrites:
            text = text.replace(old, new)
        if '"/old-data/v1/"' in text or '.p="/old-data/"' in text:
            text = re.sub(
                r"(\(0,\w+\.jsxs\)\(\w+\.Kd),\{children:",
                rf'\1,{{basename:"{ADMIN_MOUNT}/old-data",children:',
                text,
                count=1,
            )
        # href/src/action="/..." — skip paths already under /admin/ (avoid /admin/admin/...).
        text = re.sub(r'href="/(?!admin/)', f'href="{ADMIN_MOUNT}/', text)
        text = re.sub(r'href:"/(?!admin/)', f'href:"{ADMIN_MOUNT}/', text)
        text = re.sub(r"href:'/(?!admin/)", f"href:'{ADMIN_MOUNT}/", text)
        text = re.sub(r"src='/(?!admin/)", f"src='{ADMIN_MOUNT}/", text)
        text = re.sub(r'src="/(?!admin/)', f'src="{ADMIN_MOUNT}/', text)
        text = re.sub(r'action="/(?!admin/)', f'action="{ADMIN_MOUNT}/', text)
        text = re.sub(r"url\(/(?!admin/)", f"url({ADMIN_MOUNT}/", text)
        # Hard window redirects (React Router navigate/to use basename — don't prefix /app/).
        text = text.replace(
            'window.location.href="/app/',
            f'window.location.href="{ADMIN_MOUNT}/app/',
        )
        # Webpack lazy chunks use n.p + "static/..." at runtime; mount them under /admin.
        text = text.replace('.p="/"', f'.p="{ADMIN_MOUNT}/"')
        # React Router BrowserRouter — set basename so routes resolve under /admin.
        text = text.replace(
            '(0,a.jsxs)(r.Kd,{children:',
            f'(0,a.jsxs)(r.Kd,{{basename:"{ADMIN_MOUNT}",children:',
        )
        # Auth interceptor redirect — go to /admin/ not root on session expire.
        text = text.replace('window.location.href="/"', f'window.location.href="{ADMIN_MOUNT}/"')
        text = text.replace("window.location.href='/'", f"window.location.href='{ADMIN_MOUNT}/'")
        # auth.user.data crashes when user/session missing — accept both API shapes.
        text = re.sub(
            r'(\w+)=>\1\.auth\.user\.data',
            r'\1=>{const u=\1.auth.user;return u&&(u.data||u)||{}}',
            text,
        )
        # PrivateRoute: require token + user (token alone caused blank dashboard crash).
        text = text.replace(
            'return localStorage.getItem("token")?t:(0,a.jsx)(o.C5,{to:"/",',
            'return(localStorage.getItem("token")&&localStorage.getItem("user"))?t:(0,a.jsx)(o.C5,{to:"/",',
        )
        # Avoid hard crash on corrupted localStorage user JSON.
        text = text.replace(
            "user:JSON.parse(localStorage.getItem(\"user\"))||null",
            'user:(()=>{try{var _u=localStorage.getItem("user");return _u?JSON.parse(_u):null}catch(_e){return null}})()',
        )
        # Don't wipe session on API code 400 during local dev.
        text = re.sub(
            r'if\(400===\w+\)return sessionStorage\.clear\(\),localStorage\.removeItem\("user"',
            'if(false&&400===e)return sessionStorage.clear(),localStorage.removeItem("user"',
            text,
        )
        # Nested admin routes under /app/* — use relative paths.
        text = re.sub(r'(\{path:)"/', r'\1"', text)
        text = text.replace(
            "user:JSON.parse(localStorage.getItem(\"user\"))||null",
            'user:(()=>{try{var _p=JSON.parse(localStorage.getItem("user")||"null");return _p&&(_p.user||_p)||null}catch(_e){return null}})()',
        )
        # Match Session Plus Minus Display (chunk 950)
        if "decision/getPlusMinusByMarketId" in text and 'localStorage.getItem("user")' in text:
            text = text.replace(
                'const t=JSON.parse(localStorage.getItem("user"));r((null===t||void 0===t?void 0:t.data)||null)',
                'const t=JSON.parse(localStorage.getItem("user"));r((null===t||void 0===t?void 0:t.data)||(null===t||void 0===t?void 0:t.user)||null)',
                1,
            )
            if ",(null===t||void 0===t?void 0:t.userPriority)" not in text:
                text = text.replace(",t.userPriority", ",(null===t||void 0===t?void 0:t.userPriority)")
                text = text.replace("[t.userPriority", "[(null===t||void 0===t?void 0:t.userPriority)")
                text = text.replace("(t.userPriority", "((null===t||void 0===t?void 0:t.userPriority)")
            text = text.replace("===t.userPriority", "===(null===t||void 0===t?void 0:t.userPriority)")
            text = text.replace("!==t.userPriority", "!==(null===t||void 0===t?void 0:t.userPriority)")
        # Collapse accidental /admin/admin/ from basename + prefixed paths.
        double = f"{ADMIN_MOUNT}{ADMIN_MOUNT}/"
        if double in text:
            text = text.replace(double, f"{ADMIN_MOUNT}/")
        return text.encode("utf-8")

    def _proxy_admin_mount(self, method: str):
        parsed = urlparse(self.path)
        upstream_path = parsed.path[len(ADMIN_MOUNT):] or "/"
        if parsed.query:
            upstream_path += "?" + parsed.query

        body = b""
        if method in ("POST", "PATCH"):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b""

        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in ("host", "content-length", "connection", "accept-encoding")
        }
        headers["Host"] = f"{ADMIN_UPSTREAM_HOST}:{ADMIN_UPSTREAM_PORT}"
        if body:
            headers["Content-Length"] = str(len(body))

        conn = http.client.HTTPConnection(ADMIN_UPSTREAM_HOST, ADMIN_UPSTREAM_PORT, timeout=30)
        try:
            conn.request(method, upstream_path, body=body if body else None, headers=headers)
            resp = conn.getresponse()
            resp_body = resp.read()
            content_type = resp.getheader("Content-Type", "")
            if upstream_path.endswith(".js") and (
                "text/html" in content_type
                or resp_body.lstrip().startswith(b"<!DOCTYPE")
                or resp_body.lstrip().startswith(b"<html")
            ):
                resp_body = b"/* ex99 admin js stub */\n(function(){})();\n"
                content_type = "application/javascript; charset=utf-8"
            resp_body = self._rewrite_admin_mount_body(resp_body, content_type)

            self.send_response(resp.status)
            excluded = {
                "connection",
                "content-length",
                "transfer-encoding",
                "content-encoding",
            }
            for key, value in resp.getheaders():
                if key.lower() in excluded:
                    continue
                if key.lower() == "content-type":
                    continue
                self.send_header(key, value)
            self.send_header("Content-Type", content_type or "application/octet-stream")
            self.send_header("Content-Length", str(len(resp_body)))
            self.send_header("Cache-Control", "no-cache")
            self._send_cors_headers()
            self.end_headers()
            if method != "OPTIONS":
                self.wfile.write(resp_body)
        except Exception as exc:
            error = json.dumps({
                "error": True,
                "message": f"Admin upstream unavailable on {ADMIN_UPSTREAM_PORT}: {exc}",
            }).encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(error)))
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(error)
        finally:
            conn.close()

    def _serve_site_selector(self):
        content = build_site_selector_html().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-cache")
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(content)

    def _proxy_cache(self):
        """1excache odds — pehle local MongoDB, phir remote; 502 mat bhejo."""
        parsed = urlparse(self.path)
        rel = parsed.path[len(CACHE_LOCAL_PREFIX):]
        query = parsed.query

        try:
            body = proxy_odds_json(rel, query)
            if body is not None:
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-cache")
                self._send_cors_headers()
                self.end_headers()
                self.wfile.write(body)
                return
        except Exception as exc:
            print(f"[excache local] {rel} — {exc}")

        remote_url = CACHE_REMOTE_PREFIX + rel
        if query:
            remote_url += "?" + query
        try:
            resp = scraper.get(remote_url, headers=BROWSER_HEADERS, timeout=20)
            if resp.status_code >= 400:
                raise RuntimeError(f"remote status {resp.status_code}")
            body = resp.content
            self.send_response(resp.status_code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(body)
        except Exception as exc:
            print(f"[excache fallback] {remote_url} — {exc}")
            fallback = json.dumps({"result": {}, "code": 0, "error": False, "data": []}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(fallback)))
            self.send_header("Cache-Control", "no-cache")
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(fallback)

    def _proxy_scorecard(self):
        """Scorecard JSON — scrape2 local files + dimclscore fallback."""
        parsed = urlparse(self.path)
        m = SCORECARD_PATH_RE.match(parsed.path)
        if not m:
            error = json.dumps({"error": True, "message": "Invalid scorecard path"}).encode()
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(error)))
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(error)
            return

        eid, gmid = m.group(1), m.group(2)
        qs = {}
        if parsed.query:
            qs = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        event_id = qs.get("eventId") or gmid

        try:
            payload = scorecard_response(event_id, eid=eid, gmid=gmid)
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(body)
        except Exception as exc:
            error = json.dumps({"error": True, "message": str(exc), "scorecard": {}}).encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(error)))
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(error)

    def _proxy_casino_stream(self):
        """Proxy live casino video iframe (bypasses frame-ancestors CSP)."""
        parsed = urlparse(self.path)
        rel = parsed.path[len(STREAM_LOCAL_PREFIX):].lstrip("/")

        if rel.startswith("stream99/"):
            remote_base = STREAM_REMOTE_ALT
            rel = rel[len("stream99/"):].lstrip("/")
        else:
            remote_base = STREAM_REMOTE
            # Player_files live under /route/ on upstream (relative to route page)
            if rel.startswith("Player_files/"):
                rel = "route/" + rel

        remote_url = remote_base + rel
        if parsed.query:
            remote_url += "?" + parsed.query

        headers = {
            "Referer": STREAM_UPSTREAM_REFERER,
            "Origin": STREAM_UPSTREAM_ORIGIN,
            "User-Agent": self.headers.get("User-Agent", "Mozilla/5.0"),
        }

        try:
            status_code, body, content_type = _fetch_upstream_stream(remote_url, headers, timeout=30)

            if "text/html" in content_type:
                text = body.decode("utf-8", errors="replace")
                # Relative Player_files/* resolve against /casino-stream/route/ — do NOT rewrite src
                base_tag = f'<base href="{STREAM_LOCAL_PREFIX}route/">'
                if "<head>" in text and "<base " not in text.lower():
                    text = text.replace("<head>", f"<head>{base_tag}", 1)
                body = text.encode("utf-8")

            self.send_response(status_code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            # Allow embedding on localhost (strip upstream CSP)
            self.send_header("Content-Security-Policy", "frame-ancestors *")
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(body)
        except Exception as exc:
            print(f"[casino-stream] proxy error {remote_url}: {exc}")
            self.send_error(502, f"Stream proxy error: {exc}")

    def _try_mongo_api(self, endpoint: str, payload: dict, auth_header: str) -> Optional[bytes]:
        """Login / logout / balance — local MongoDB."""
        if not USE_MONGO_AUTH:
            return None

        try:
            if endpoint.endswith("user/login"):
                body = mongo_login(payload)
                print(f"[mongo auth] Login: {payload.get('username')} -> "
                      f"{'OK' if not body.get('error') else body.get('message')}")
            elif endpoint.endswith("user/logout"):
                body = mongo_logout(auth_header)
                print("[mongo auth] Logout")
            elif endpoint.endswith("user/userBalance"):
                token = _extract_bearer(auth_header) or ""
                if validate_session(token):
                    body = mongo_user_balance(payload, auth_header)
                else:
                    body = {"error": True, "code": 401, "message": "You are logged in from another device. Please login again.", "data": {}}
            else:
                return None
            return json.dumps(body, default=str).encode("utf-8")
        except Exception as exc:
            print(f"[mongo auth] Error: {exc}")
            return json.dumps({
                "error": True,
                "code": 500,
                "message": str(exc),
            }).encode("utf-8")

    def _proxy_api(self):
        endpoint = self.path[len(API_LOCAL_PREFIX):]

        length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(length) if length else b"{}"

        try:
            payload = json.loads(raw_body.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            payload = {}

        auth = self.headers.get("Authorization", "")
        mongo_body = self._try_mongo_api(endpoint, payload, auth)
        if mongo_body is not None:
            self.send_response(_api_http_status(mongo_body))
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(mongo_body)))
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(mongo_body)
            return

        if should_serve_local(endpoint, auth, USE_MONGO_AUTH):
            local_body = build_local_api_response(endpoint, payload, auth)
            if local_body is not None:
                tag = "bet/local" if endpoint.startswith(("sports/", "user/client")) or "Bet" in endpoint else "local"
                print(f"[{tag} api] {endpoint}")
                self.send_response(_api_http_status(local_body))
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(local_body)))
                self._send_cors_headers()
                self.end_headers()
                self.wfile.write(local_body)
                return

        if LOCAL_ONLY and USE_MONGO_AUTH:
            blocked = json.dumps({
                "message": f"Local-only mode: {endpoint} not available offline",
                "code": 503,
                "error": True,
                "data": {},
            }).encode("utf-8")
            print(f"[local-only BLOCK] {endpoint} — live API disabled")
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(blocked)))
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(blocked)
            return

        remote_url = f"{API_REMOTE}{endpoint}"
        payload = patch_api_payload(endpoint, payload)

        headers = {
            "Content-Type": "application/json",
            "Origin": f"https://{SITE_HOST}",
            "Referer": f"https://{SITE_HOST}/",
        }
        auth = self.headers.get("Authorization")
        if auth:
            headers["Authorization"] = auth

        try:
            resp = scraper.post(remote_url, json=payload, headers=headers, timeout=30)
            body = resp.content

            self.send_response(resp.status_code)
            self.send_header("Content-Type", resp.headers.get("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(body)))
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(body)
        except Exception as exc:
            error = json.dumps({"error": True, "message": str(exc)}).encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(error)))
            self.end_headers()
            self.wfile.write(error)


def main():
    for key in (
        "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
        "ALL_PROXY", "all_proxy", "SOCKS_PROXY", "socks_proxy",
    ):
        os.environ.pop(key, None)

    if not SITE_DIR.exists():
        print(f"Error: '{SITE_DIR}' not found. Pehle scrape karo:")
        print("  python main.py")
        sys.exit(1)

    server = ReusableThreadingHTTPServer(("0.0.0.0", PORT), LocalSiteHandler)
    print("=" * 50)
    print("1ex99.in - Local Server")
    print("=" * 50)
    print(f"Site folder : {SITE_DIR}")
    print(f"Local URL   : http://localhost:{PORT}")
    print(f"API proxy   : http://localhost:{PORT}/v1/ -> {API_REMOTE}")
    print(f"Cache proxy : http://localhost:{PORT}{CACHE_LOCAL_PREFIX} -> {CACHE_REMOTE_PREFIX}")
    print(f"Stream proxy: http://localhost:{PORT}{STREAM_LOCAL_PREFIX} -> {STREAM_REMOTE}")
    print(f"Match list  : Live refresh every {POLL_MS}ms (EX99_LIVE_MATCHES=1)")
    print(f"Login host  : {SITE_HOST}")
    if USE_MONGO_AUTH:
        print("Auth        : MongoDB (login/logout/balance)")
        print("Casino/Sports: Scraped local JS + MongoDB (no live API)")
        print("Betting     : Local only (MatchDetail JS logic in mongodb/bet_logic.py)")
        if LOCAL_ONLY:
            print("Live API    : BLOCKED (EX99_LOCAL_ONLY=1)")
        print("Demo client : C358167 / 615849  or  C324001 / 123456")
    print("=" * 50)
    print("Browser mein kholo: http://localhost:{0}".format(PORT))
    print("Ctrl+C se band karo")
    print("=" * 50)

    try:
        from mongodb.auto_decision_worker import start_auto_decision_worker
        start_auto_decision_worker()
    except Exception as exc:
        print(f"[auto-decision] worker not started: {exc}")

    try:
        from mongodb.scorecard_prewarm_worker import start_scorecard_prewarm_worker
        start_scorecard_prewarm_worker()
    except Exception as exc:
        print(f"[scorecard-prewarm] worker not started: {exc}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
        server.shutdown()


if __name__ == "__main__":
    main()
