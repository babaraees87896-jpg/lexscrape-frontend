#!/usr/bin/env python3
"""
Local server for scraped admin.1ex99.in panel.

MongoDB mode: login + saari APIs apne database se (demo mode band).
"""

import json
import mimetypes
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

import cloudscraper

from config import (
    ADMIN_HOST,
    ADMIN_OUTPUT_DIR,
    BROWSER_HEADERS,
    MARCH2026_ADMIN_OUTPUT_DIR,
    MARCH2026_API_URL,
)
from mongodb.admin_api import handle_admin_api
from mongodb.db import ping
from mongodb.centerpanel_cache import proxy_odds_json
from mongodb.matches_api import CACHE_LOCAL_PREFIX, CACHE_REMOTE_PREFIX, fetch_live_matches, sync_live_matches_to_db

PORT = int(os.getenv("EX99_ADMIN_PORT", "8889"))
MARCH_ADMIN_PORT = os.getenv("EX99_MARCH_ADMIN_PORT", "8892")
POLL_MS = int(os.getenv("EX99_POLL_MS", "2000"))
POLL_JS = f"{POLL_MS // 1000}e3" if POLL_MS >= 1000 else str(POLL_MS)
SITE_DIR = Path(os.getenv("EX99_ADMIN_OUTPUT_DIR", ADMIN_OUTPUT_DIR)).resolve()
_march_rel = Path(os.getenv("EX99_MARCH_ADMIN_OUTPUT_DIR", MARCH2026_ADMIN_OUTPUT_DIR))
MARCH_SITE_DIR = (_march_rel if _march_rel.is_absolute() else Path(__file__).resolve().parent / _march_rel).resolve()
OLD_DATA_PREFIX = "/old-data"
API_LOCAL_PREFIX = "/v1/"

JS_PATCH_FROM = "https://api.ons3.co/v1/"
JS_PATCH_TO = API_LOCAL_PREFIX

scraper = cloudscraper.create_scraper(
    browser={"browser": "chrome", "platform": "darwin", "desktop": True}
)

ADMIN_LEGACY_JS_STUB = b"/* ex99 admin legacy stub */\n(function(){})();\n"
ADMIN_CHUNK_STUB_RE = re.compile(r"^(\d+)\.[a-f0-9]+\.chunk\.js$")


def admin_chunk_stub_js(chunk_id: str) -> bytes:
    return (
        f'"use strict";(globalThis.webpackChunk_1ex_admin_lte='
        f'globalThis.webpackChunk_1ex_admin_lte||[]).push([[{chunk_id}],{{}}]);\n'
    ).encode("utf-8")
ADMIN_LEGACY_JS_PATHS = {
    "/dist/js/demo.js",
    "/dist/js/dashboard.js",
    "/dist/js/pages/dashboard.js",
}

# Admin nested routes — /app/* ke bina URL galat match hota hai.
ADMIN_APP_ROUTE_PREFIXES = (
    "/statement/",
    "/userlist/",
    "/cash-transction/",
    "/game/",
    "/limit/",
    "/create/",
    "/edit/",
    "/login-report/",
    "/dataReport/",
    "/agentComm",
    "/AgentCommissionList/",
    "/show-bets/",
    "/display-game/",
    "/matka/",
    "/ledger/",
    "/casino/",
    "/collection",
    "/profit-loss",
    "/create-collection",
    "/diamond-casino",
    "/live-casino",
    "/dashboard",
    "/profile",
)


def admin_app_redirect(path: str) -> Optional[str]:
    if path.startswith("/app/"):
        return None
    for prefix in ADMIN_APP_ROUTE_PREFIXES:
        if path == prefix.rstrip("/") or path.startswith(prefix):
            return "/app" + path
    return None


def split_old_data_path(path: str) -> tuple[bool, str]:
    if path == OLD_DATA_PREFIX:
        return True, "/"
    if path.startswith(OLD_DATA_PREFIX + "/"):
        return True, path[len(OLD_DATA_PREFIX):] or "/"
    return False, path


def patch_old_data_mount_body(content: str) -> str:
    """Old Data panel — scraped march2026admin ko /old-data/ mount par chalao."""
    mount = OLD_DATA_PREFIX
    rewrites = (
        ('"/v1/', f'"{mount}/v1/'),
        ("'/v1/", f"'{mount}/v1/"),
        ('"/excache', f'"{mount}/excache'),
        ("'/excache", f"'{mount}/excache"),
        ('"/images/', f'"{mount}/images/'),
        ("'/images/", f"'{mount}/images/"),
        ('"/plugins/', f'"{mount}/plugins/'),
        ("'/plugins/", f"'{mount}/plugins/"),
        ('"/dist/', f'"{mount}/dist/'),
        ("'/dist/", f"'{mount}/dist/"),
        ('"/static/', f'"{mount}/static/'),
        ("'/static/", f"'{mount}/static/"),
        ('"/adminlite/', f'"{mount}/adminlite/'),
        ("'/adminlite/", f"'{mount}/adminlite/"),
        ("https://march2026admin.1ex99.in/", f"{mount}/"),
        (f"http://localhost:{MARCH_ADMIN_PORT}/", f"{mount}/"),
    )
    for old, new in rewrites:
        content = content.replace(old, new)
    content = re.sub(r'href="/(?!old-data/)', f'href="{mount}/', content)
    content = re.sub(r'href:"/(?!old-data/)', f'href:"{mount}/', content)
    content = re.sub(r"href:'/(?!old-data/)", f"href:'{mount}/", content)
    content = re.sub(r"src='/(?!old-data/)", f"src='{mount}/", content)
    content = re.sub(r'src="/(?!old-data/)', f'src="{mount}/', content)
    content = content.replace('.p="/"', f'.p="{mount}/"')
    content = re.sub(
        r"(\(0,\w+\.jsxs\)\(\w+\.Kd),\{children:",
        rf'\1,{{basename:"{mount}",children:',
        content,
        count=1,
    )
    double = f"{mount}{mount}/"
    if double in content:
        content = content.replace(double, f"{mount}/")
    return content


def patch_admin_js(content: str) -> str:
    for remote_api in (
        JS_PATCH_FROM,
        "https://api.ons3.co/v1/",
        "http://api.ons3.co/v1/",
        "https://api.ons3.co/v1",
        MARCH2026_API_URL,
        "https://march2026api.1ex99.in/v1/",
        "https://march2026api.1ex99.in/v1",
    ):
        content = content.replace(remote_api, JS_PATCH_TO)
    content = content.replace(CACHE_REMOTE_PREFIX, CACHE_LOCAL_PREFIX)
    content = content.replace("http://1excache.tresting.com", CACHE_LOCAL_PREFIX)
    content = content.replace(
        "https://march2026admin.1ex99.in/",
        f"{OLD_DATA_PREFIX}/",
    )
    # Old Data sidebar — click par kuch na ho (scraped external link disable)
    _old_data_nav = (
        'target:"blank",className:"nav-link",children:[(0,t.jsx)("i",{className:"nav-icon fas fa-bookmark"}),(0,t.jsx)("p",{children:"Old Data"})]'
    )
    _old_data_nav_off = (
        'className:"nav-link",href:"#",onClick:e=>{e.preventDefault()},children:[(0,t.jsx)("i",{className:"nav-icon fas fa-bookmark"}),(0,t.jsx)("p",{children:"Old Data"})]'
    )
    content = content.replace(
        f'href:"{OLD_DATA_PREFIX}/",{_old_data_nav}',
        _old_data_nav_off,
    )
    content = content.replace(
        f'href:"https://march2026admin.1ex99.in/",{_old_data_nav}',
        _old_data_nav_off,
    )
    content = content.replace(
        "if(400===e)return sessionStorage.clear(),localStorage.removeItem(\"user\"",
        "if(false&&400===e)return sessionStorage.clear(),localStorage.removeItem(\"user\"",
    )
    content = content.replace(
        "if(n.status===401){localStorage.clear(),window.location.href=\"/\";return}",
        "if(n.status===401){sessionStorage.clear(),localStorage.clear(),window.location.href=\"/\";return}",
    )
    # Local API error body = {message, error, code} — r.response undefined hota hai
    content = re.sub(
        r"(\w+)\.response\.data\.message",
        r"(\1.response&&\1.response.data&&\1.response.data.message||\1.message||'Something went wrong')",
        content,
    )
    # Dashboard inplay — frontend jaisa sport filter + API inPlayStatus
    content = content.replace(
        'const e=j()().add(1,"hour"),s=(null===i||void 0===i?void 0:i.map(s=>{const l=j()(s.matchDate,"DD-MM-YYYY HH:mm:ss A"),a=e.isSameOrAfter(l);return{...s,inplayStatus:a}})).filter(e=>e.inplayStatus).filter(e=>(null===e||void 0===e?void 0:e.sportId)===b);N(s)',
        'const s=(null===i||void 0===i?void 0:i.filter(e=>e.inPlayStatus!==false&&(null===e||void 0===e?void 0:e.sportId)===b))||[];N(s)',
    )
    # Inplay + dashboard — live matchList har 2 sec (real API jaisa)
    content = content.replace(
        "(0,a.useEffect)(()=>{C(),$()},[]);",
        f"(0,a.useEffect)(()=>{{(async()=>{{await $(),await E()}})();const _ex99Ip=setInterval(()=>{{(async()=>{{await $(),await E()}})()}},{POLL_JS});return()=>clearInterval(_ex99Ip)}},[]);",
    )
    content = content.replace(
        "(0,a.useEffect)(()=>{C(),$();const _ex99Ip=setInterval(()=>{E(),$()},1e4);return()=>clearInterval(_ex99Ip)},[]);",
        f"(0,a.useEffect)(()=>{{(async()=>{{await $(),await E()}})();const _ex99Ip=setInterval(()=>{{(async()=>{{await $(),await E()}})()}},{POLL_JS});return()=>clearInterval(_ex99Ip)}},[]);",
    )
    content = content.replace(
        "(0,a.useEffect)(()=>{k()},[]);",
        f"(0,a.useEffect)(()=>{{k();const _ex99Dash=setInterval(A,{POLL_JS});return()=>clearInterval(_ex99Dash)}},[]);",
    )
    content = content.replace(
        "(0,a.useEffect)(()=>{k();const _ex99Dash=setInterval(A,1e4);return()=>clearInterval(_ex99Dash)},[]);",
        f"(0,a.useEffect)(()=>{{k();const _ex99Dash=setInterval(A,{POLL_JS});return()=>clearInterval(_ex99Dash)}},[]);",
    )
    content = re.sub(
        r'(\w+)=>\1\.auth\.user\.data',
        r'\1=>{const u=\1.auth.user;return u&&(u.data||u)||{}}',
        content,
    )
    content = content.replace(
        'return localStorage.getItem("token")?t:(0,a.jsx)(o.C5,{to:"/",',
        'return(localStorage.getItem("token")&&localStorage.getItem("user"))?t:(0,a.jsx)(o.C5,{to:"/",',
    )
    content = content.replace(
        "user:JSON.parse(localStorage.getItem(\"user\"))||null",
        'user:(()=>{try{var _p=JSON.parse(localStorage.getItem("user")||"null");return _p&&(_p.user||_p)||null}catch(_e){return null}})()',
    )
    # All pages — localStorage user (.data || .user) for non-owner logins
    _ls_user = '(()=>{try{const _ls=JSON.parse(localStorage.getItem("user")||"null");return _ls&&(_ls.data||_ls.user)||null}catch(_e){return null}})()'
    content = content.replace('JSON.parse(localStorage.getItem("user")).data', _ls_user)
    content = re.sub(
        r'if\(400===\w+\)return sessionStorage\.clear\(\),localStorage\.removeItem\("user"',
        'if(false&&400===e)return sessionStorage.clear(),localStorage.removeItem("user"',
        content,
    )
    # Nested routes under /app/* must be relative (not "/dashboard" -> matches /app/dashboard).
    content = re.sub(r'(\{path:)"/', r'\1"', content)
    # Complete Game page — dropdown hover (same as InPlay page)
    if "completeSportList" in content and "plus-minus-select/completed" in content and "dropdown-fix" not in content:
        content = content.replace(
            'className:"btn-group",children:[(0,o.jsx)("button",{type:"button",className:"btn btn-outline-primary dropdown-toggle dropdown-hover dropdown-icon px-2 text-white rounded-sm",style:{backgroundColor:"#007bff"},"data-toggle":"dropdown"',
            'className:"btn-group dropdown-fix",children:[(0,o.jsx)("button",{type:"button",className:"btn btn-outline-primary dropdown-toggle dropdown-hover dropdown-icon px-2 text-white rounded-sm",style:{backgroundColor:"#007bff"},"data-toggle":"dropdown"',
            1,
        )
        content = content.replace(
            '"dropdown-menu",role:"menu",children:[(0,o.jsx)("a",{className:"dropdown-item btn",href:`/app/game/plus-minus-select/completed/${e.marketId}`',
            '"dropdown-menu",style:{zIndex:"99999"},role:"menu",children:[(0,o.jsx)("a",{className:"dropdown-item btn",href:`/app/game/plus-minus-select/completed/${e.marketId}`',
            1,
        )
        content = content.replace(
            'card-body overflow-x-auto",children:(0,o.jsxs)("table",{id:"example1",className:"table table-bordered  ",children:',
            'card-body overflow-x-auto",style:{paddingBottom:"300px",overflow:"visible"},children:(0,o.jsxs)("table",{id:"example1",className:"table table-bordered  ",children:',
            1,
        )
        _cg_style = (
            ',(0,o.jsx)("style",{children:"\\n        .dropdown-fix { position: relative; }\\n'
            "        .dropdown-fix .dropdown-menu {\\n"
            "          position: absolute !important;\\n"
            "          top: 100% !important;\\n"
            "          left: 0 !important;\\n"
            "          z-index: 2000 !important;\\n"
            "          display: block;\\n"
            "          visibility: hidden;\\n"
            "          opacity: 0;\\n"
            "          transition: opacity 0.2s ease, visibility 0.2s ease;\\n"
            "        }\\n"
            "        .dropdown-fix:hover .dropdown-menu {\\n"
            "          visibility: visible;\\n"
            "          opacity: 1;\\n"
            "        }\\n"
            "        .sport-detail-data .card { overflow: visible !important; }\\n"
            '        .sport-detail-data .card-body { overflow: visible !important; }\\n'
            '      "})'
        )
        if content.endswith("]})}}}]);"):
            content = content[: -len("]})}}}]);")] + _cg_style + "]})}}}]);"
    # Match Session Plus Minus Display (chunk 950) — localStorage user shape + null-safe userPriority
    if "decision/getPlusMinusByMarketId" in content and 'localStorage.getItem("user")' in content:
        content = content.replace(
            'const t=JSON.parse(localStorage.getItem("user"));r((null===t||void 0===t?void 0:t.data)||null)',
            'const t=JSON.parse(localStorage.getItem("user"));r((null===t||void 0===t?void 0:t.data)||(null===t||void 0===t?void 0:t.user)||null)',
            1,
        )
        if ",(null===t||void 0===t?void 0:t.userPriority)" not in content:
            content = content.replace(",t.userPriority", ",(null===t||void 0===t?void 0:t.userPriority)")
            content = content.replace("[t.userPriority", "[(null===t||void 0===t?void 0:t.userPriority)")
            content = content.replace("(t.userPriority", "((null===t||void 0===t?void 0:t.userPriority)")
        content = content.replace("===t.userPriority", "===(null===t||void 0===t?void 0:t.userPriority)")
        content = content.replace("!==t.userPriority", "!==(null===t||void 0===t?void 0:t.userPriority)")
        content = content.replace(
            "2===t.userPriority",
            "2===(null===t||void 0===t?void 0:t.userPriority)",
        )
    # Create user form (chunk 6864) — domain field hide (subowner New form)
    if "Select Domain" in content and "6864" in content:
        content = content.replace(
            ',"subowner"===x?(0,u.jsx)("div",{className:"form-group row mb-0",children:(0,u.jsx)("div",{className:"form-group col-md-6",children:(0,u.jsxs)("div",{className:"subowner-share w-100",children:[(0,u.jsx)("label",{htmlFor:"mshare",children:"Select Domain"}),(0,u.jsx)(l.default,{options:b,selectedValues:y,onSelect:s=>{p((0,t.sW)([...s]))},onRemove:s=>{p((0,t.sW)([...s]))},displayValue:"domainName"}),S&&S.selectedDomain?(0,u.jsx)("div",{className:"text-xs text-red-600 capitalize",children:S&&S.selectedDomain?S.selectedDomain:null}):null]})})}):null',
            "",
        )
        content = content.replace(
            ',"subowner"===x&&0===y.length&&(e=!1,s.selectedDomain="Plz select domain.")',
            "",
        )
    # Create user (6864) — coins/limit cannot exceed My Limit (parentDetails.coins)
    if "Share and Commission" in content and "My Limit" in content and "parentDetails:f" in content:
        content = content.replace(
            "const F=s=>{const{name:e,value:o}=s.target;p((0,t.dw)({...j,[e]:o})),p((0,t.Yd)({...S,[e]:\"\"}))}",
            'const F=s=>{const{name:e,value:o}=s.target;if("coins"===e){const mx=Number((null===f||void 0===f?void 0:f.coins)||0);const n=Number(o);if(""!==o&&!Number.isNaN(n)&&n>mx){p((0,t.dw)({...j,[e]:mx}));p((0,t.Yd)({...S,balance:`Limit can not be more than ${mx}`}));return}}p((0,t.dw)({...j,[e]:o})),p((0,t.Yd)({...S,[e]:""}))}',
        )
        content = content.replace(
            'p((0,t.Yd)(s)),e};let T=[{id:"NoCommission",name:"No Commission"}',
            'null!==j&&void 0!==j&&j.coins&&Number(j.coins)>Number((null===f||void 0===f?void 0:f.coins)||0)&&(e=!1,s.balance=`Limit can not be more than ${null===f||void 0===f?void 0:f.coins}`),p((0,t.Yd)(s)),e};let T=[{id:"NoCommission",name:"No Commission"}',
        )
    # Owner details user list (chunk 546) — All Active/Deactive uses all visible rows
    if 'id:"allActive"' in content and "userId:Z,status:" in content:
        content = content.replace(
            "const s={userId:Z,status:1}",
            "const s={userId:(Z.length?Z:p.map(e=>e.userId)),status:1}",
        )
        content = content.replace(
            "const s={userId:Z,status:0}",
            "const s={userId:(Z.length?Z:p.map(e=>e.userId)),status:0}",
        )
    # User list — copy credentials link → user site (1ex99.live, no /admin)
    if "Copy User Credential" in content:
        content = content.replace(
            "Link: https://${s}${(0,f.wm)().domianUrl}",
            "Link: https://1ex99.live",
        )
        content = content.replace(
            "n=`https://${s}${(0,f.wm)().domianUrl}`",
            "n=`https://1ex99.live`",
        )
        content = content.replace(
            "${window.location.origin}/admin",
            "https://1ex99.live",
        )
    # User type code prefixes — subadmin: AD, admin: ADM
    content = content.replace(
        'admin:{userType:"admin",priority:6,shortname:"AD"},subadmin:{userType:"subadmin",priority:5,shortname:"SUA"}',
        'admin:{userType:"admin",priority:6,shortname:"ADM"},subadmin:{userType:"subadmin",priority:5,shortname:"AD"}',
    )
    # Inplay / dashboard — spurious "Failed to fetch matches" toast on back navigation
    content = content.replace(
        'console.error("Error fetching match list:",t),l.oR.error((null===t||void 0===t||null===(e=t.response)||void 0===e?void 0:e.message)||"Failed to fetch matches")',
        'console.error("Error fetching match list:",t)',
    )
    content = content.replace(
        'console.error("Error fetching match list:",s),h.oR.error((null===s||void 0===s||null===(e=s.response)||void 0===e?void 0:e.message)||"Failed to fetch matches")',
        'console.error("Error fetching match list:",s)',
    )
    content = content.replace(
        'console.error("Error fetching match list:",t),d.oR.error((null===t||void 0===t||null===(e=t.response)||void 0===e?void 0:e.message)||"Failed to fetch matches")',
        'console.error("Error fetching match list:",t)',
    )
    # Inplay lock — blockMarket map normalize + match flags + fix shadowed var in handler
    content = content.replace(
        't&&(e=JSON.parse(t))}catch(r){console.error("Error parsing blockMarket:",r)}const t=null===x||void 0===x?void 0:x.map(t=>({...t,matchBlock:!!e[t.marketId]}));',
        't&&(e=JSON.parse(t),Array.isArray(e)&&(e=Object.fromEntries(e.filter(t=>t&&t.marketId).map(t=>[String(t.marketId),!0]))),v&&"object"==typeof v&&!Array.isArray(v)&&(e={...e,...v}))}catch(r){console.error("Error parsing blockMarket:",r)}const t=null===x||void 0===x?void 0:x.map(t=>({...t,matchBlock:!!((v&&typeof v==="object"&&!Array.isArray(v)?v[String(t.marketId)]:null)||e[String(t.marketId)]||t.isBlocked===!0||t.betPerm===!1)}));',
    )
    content = content.replace(
        't&&(e=JSON.parse(t),Array.isArray(e)&&(e=Object.fromEntries(e.filter(t=>t&&t.marketId).map(t=>[String(t.marketId),!0]))))}catch(r){console.error("Error parsing blockMarket:",r)}const t=null===x||void 0===x?void 0:x.map(t=>({...t,matchBlock:!!e[String(t.marketId)]}));',
        't&&(e=JSON.parse(t),Array.isArray(e)&&(e=Object.fromEntries(e.filter(t=>t&&t.marketId).map(t=>[String(t.marketId),!0]))),v&&"object"==typeof v&&!Array.isArray(v)&&(e={...e,...v}))}catch(r){console.error("Error parsing blockMarket:",r)}const t=null===x||void 0===x?void 0:x.map(t=>({...t,matchBlock:!!((v&&typeof v==="object"&&!Array.isArray(v)?v[String(t.marketId)]:null)||e[String(t.marketId)]||t.isBlocked===!0||t.betPerm===!1)}));',
    )
    content = content.replace(
        '(async t=>{const r={marketId:t.marketId,blockStatus:!t.matchBlock};p(!0);try{var a;const t=await d.kC.post("reports/blockMarket",r);var n,o;0==(null===t||void 0===t||null===(a=t.data)||void 0===a?void 0:a.error)?(l.oR.success((null===t||void 0===t||null===(n=t.data)||void 0===n?void 0:n.message)||"Updated"),await $(),await E(),e((0,s.d1)())):l.oR.error((null===t||void 0===t||null===(o=t.data)||void 0===o?void 0:o.message)||"Failed to update status")}catch(m){var i,c;l.oR.error((null===m||void 0===m||null===(i=m.response)||void 0===i||null===(c=i.data)||void 0===c?void 0:c.message)||"Error updating block status")}finally{p(!1)}})(I),O(!1),P(null)',
        '(async _m=>{const r={marketId:_m.marketId,blockStatus:!_m.matchBlock};p(!0);try{var a;const i=await d.kC.post("reports/blockMarket",r);var n,o;0==(null===i||void 0===i||null===(a=i.data)||void 0===a?void 0:a.error)?(l.oR.success((null===i||void 0===i||null===(n=i.data)||void 0===n?void 0:n.message)||"Updated"),await $(),await E(),O(!1),P(null),e((0,s.d1)())):l.oR.error((null===i||void 0===i||null===(o=i.data)||void 0===o?void 0:o.message)||"Failed to update status")}catch(m){var c,u;l.oR.error((null===m||void 0===m||null===(c=m.response)||void 0===c||null===(u=c.data)||void 0===u?void 0:u.message)||"Error updating block status")}finally{p(!1)}})(I)',
    )
    # Cash Transaction (4531) — Bet Delete column + modal (owner enforced on backend)
    if "delete the ledger" in content and "4531" in content:
        content = content.replace(
            '[E,L]=(0,l.useState)(!1);',
            '[E,L]=(0,l.useState)(!1);const _ex99Ow=()=>{try{const _u=JSON.parse(localStorage.getItem("user")||"null");if(!_u)return!1;const _d=_u.user||_u.data||_u;return((_d.userType||"").toLowerCase()==="owner"||9===_d.userPriority)}catch(_e){return!1}};',
            1,
        )
        content = content.replace(
            'ledgerId:t._id,date:t.date}});',
            'ledgerId:t._id||t.ledgerId,betId:t.betId,date:t.date}});',
            1,
        )
        content = content.replace(
            'const e={downlineUserId:d};',
            'const e={downlineUserId:d,...(_ex99Ow()?{ledgerMode:"betbybet"}:{})};',
            1,
        )
        content = content.replace(
            'const t={downlineUserId:e};',
            'const t={downlineUserId:e,...(_ex99Ow()?{ledgerMode:"betbybet"}:{})};',
            1,
        )
        content = content.replace(
            'l={downlineUserId:j.userId,ledgerType:a};',
            'l={downlineUserId:j.userId,ledgerType:a,...(_ex99Ow()?{ledgerMode:"betbybet"}:{})};',
            1,
        )
        content = content.replace(
            '(0,p.jsx)("th",{children:"Remark"})]}),(0,p.jsxs)("tr",{children:[(0,p.jsx)("th",{}),(0,p.jsx)("th",{}),(0,p.jsx)("th",{className:"text-blue",children:"Total Amount"})',
            '(0,p.jsx)("th",{children:"Remark"}),(0,p.jsx)("th",{children:"Bet Delete"})]}),(0,p.jsxs)("tr",{children:[(0,p.jsx)("th",{}),(0,p.jsx)("th",{}),(0,p.jsx)("th",{className:"text-blue",children:"Total Amount"})',
            1,
        )
        content = content.replace(
            '(0,p.jsx)("th",{}),(0,p.jsx)("th",{})]})]}),(0,p.jsx)("tbody"',
            '(0,p.jsx)("th",{}),(0,p.jsx)("th",{}),(0,p.jsx)("th",{})]})]}),(0,p.jsx)("tbody"',
            1,
        )
        content = content.replace(
            'children:e&&e.remark?e.remark:"NA"})]},t)):null}),(0,p.jsx)("tfoot"',
            'children:e&&e.remark?e.remark:"NA"}),(0,p.jsx)("td",{style:{backgroundColor:"settle"===(null===e||void 0===e?void 0:e.ledgerType)?"#fbb6ce":"#ffffff"},className:"p-2.5 text-center",children:_ex99Ow()&&(e.ledgerId||e.betId)?(0,p.jsx)("button",{type:"button",className:"btn btn-danger btn-sm",onClick:()=>{T(t=>({...t,ledgerId:e.ledgerId,betId:e.betId})),C((0,r.qf)())},children:"Delete"}):null})]},t)):null}),(0,p.jsx)("tfoot"',
            1,
        )
        content = content.replace(
            'children:"Are you sure want to delete the ledger"',
            'children:"Are you sure you want to delete this bet/ledger entry?"',
            1,
        )
        content = content.replace(
            'const e={downlineUserId:j.userId,ledgerId:A.ledgerId};C((0,s.R5)(e))',
            'const e={downlineUserId:j.userId,ledgerId:A.ledgerId,betId:A.betId};C((0,s.R5)(e))',
            1,
        )
        # # column click — open delete modal (scraped site behaviour)
        content = content.replace(
            'className:"cursor-pointer ",children:t+1})',
            'className:"cursor-pointer ",onClick:()=>{_ex99Ow()&&(e.ledgerId||e.betId)&&(T(t=>({...t,ledgerId:e.ledgerId,betId:e.betId})),C((0,r.qf)()))},children:t+1})',
            1,
        )
    # Delete ke baad ledger betbybet mode se refresh (betId + Delete column)
    if "match/deleteLedger" in content and "fetchUserLedger" in content:
        content = content.replace(
            'const t=await c.fetchUserLedger({downlineUserId:e.downlineUserId});return n((0,d.Oo)()),t',
            'const t=await c.fetchUserLedger({downlineUserId:e.downlineUserId,ledgerMode:"betbybet"});return n((0,d.Oo)()),t',
            1,
        )
    # InPlay Games page (1983) — sirf live matches (completed hide)
    if "inplay-sec content-wrapper" in content and 'breadcrumb-item active",children:"Inplay"' in content:
        content = content.replace(
            '(0,a.useEffect)(()=>{if("all"===N)w(f);else{const e=f.filter(e=>e.sportId===Number(N));w(e)}},[f,N]);',
            '(0,a.useEffect)(()=>{const _live=f.filter(e=>!1!==e.inPlayStatus&&!e.isDeclare&&"COMPLETED"!==String(e.status||"").toUpperCase());if("all"===N)w(_live);else{const e=_live.filter(e=>e.sportId===Number(N));w(e)}},[f,N]);',
            1,
        )
    # Agent Plus Minus (6963) — localStorage user shape + client type check
    if "getPlusMinusByMarketIdByUserWiseData" in content and 'localStorage.getItem("user")' in content:
        content = content.replace(
            '(0,a.useEffect)(()=>{let e=JSON.parse(localStorage.getItem("user")).data;p(e.userType),j(e)},[]);',
            '(0,a.useEffect)(()=>{try{const _ls=JSON.parse(localStorage.getItem("user")||"null");const e=_ls&&(_ls.data||_ls.user)||null;e&&(p(e.userType),j(e))}catch(_e){}},[]);',
            1,
        )
        content = content.replace(
            'const N=JSON.parse(localStorage.getItem("user"));',
            'const N=(()=>{try{const _ls=JSON.parse(localStorage.getItem("user")||"null");return(_ls&&(_ls.data||_ls.user))||{}}catch(_e){return{}}})();',
            1,
        )
        content = content.replace(
            '"client"===N?',
            '"client"===(null===N||void 0===N?void 0:N.userType)?',
            1,
        )
    # Match Bet Details — teamDataList safe parse (sportByMarketId empty/partial)
    if "teamDataList=JSON.parse(t.payload.data.teamData)" in content:
        content = content.replace(
            "t.payload.data?e.teamDataList=JSON.parse(t.payload.data.teamData):e.teamDataList={}",
            't.payload.data&&t.payload.data.teamData?e.teamDataList=(()=>{try{return JSON.parse(t.payload.data.teamData)}catch(_e){return[]}})():e.teamDataList=[]',
            1,
        )
    # Session expired — turant logout, error toast spam mat dikhao (local API HTTP 200 + code 401)
    _sess_ok_old = (
        '3===n||401===e.status?(r.oR.error(i||"Session expired"),l(),Promise.reject(t))'
        ':(r.oR.error(i||"Something went wrong"),Promise.reject(t))'
    )
    _sess_ok_new = (
        '3===n||401===n||401===e.status?(window.__ex99Kicked||(window.__ex99Kicked=1,l()),Promise.reject(t))'
        ':(r.oR.error(i||"Something went wrong"),Promise.reject(t))'
    )
    if _sess_ok_old in content:
        content = content.replace(_sess_ok_old, _sess_ok_new)
    _sess_err_old = (
        '401===t||403===t||3===(null===n||void 0===n?void 0:n.code)?'
        '(r.oR.error((null===n||void 0===n?void 0:n.message)||"Your session has expired. Please login again."),l())'
        ':r.oR.error((null===n||void 0===n?void 0:n.message)||"Unexpected error occurred")'
    )
    _sess_err_new = (
        '401===t||403===t||3===(null===n||void 0===n?void 0:n.code)||401===(null===n||void 0===n?void 0:n.code)?'
        '(window.__ex99Kicked||(window.__ex99Kicked=1,l()))'
        ':r.oR.error((null===n||void 0===n?void 0:n.message)||"Unexpected error occurred")'
    )
    if _sess_err_old in content:
        content = content.replace(_sess_err_old, _sess_err_new)
    return content


def patch_admin_login_js(content: str) -> str:
    content = content.replace("value:s.email", "value:s.username")
    content = content.replace(
        "host:window.location.host",
        f'host:"{ADMIN_HOST}"',
    )
    content = content.replace(
        'children:o.qD})," ",u]',
        'children:"1ex99 admin"})]',
    )
    content = content.replace(
        'children:i.qD})," ",u]',
        'children:"1ex99 admin"})]',
    )
    return content


ADMIN_ESSENTIAL_SCRIPTS = """
  <script src="/plugins/jquery/jquery.min.js"></script>
  <script src="/plugins/bootstrap/js/bootstrap.bundle.min.js"></script>
  <script src="/dist/js/adminlte.js"></script>
"""

OLD_DATA_MAIN_JS = "main.34e9d6c7.js"

OLD_DATA_BOOTSTRAP = """<script id="ex99-admin-bootstrap">
(function(){
  var p=location.pathname;
  var mount=(function(){
    var i=p.indexOf("/old-data");
    if(i<0)return"/old-data/";
    return p.slice(0,i+9)+"/";
  })();
  var API=mount+"v1/";
  var block=/(?:\\/|^)(demo|dashboard)\\.js(?:\\?|$)/i;
  var mapUrl=function(u){
    if(typeof u!=="string")return u;
    return u
      .replace(/https?:\\/\\/api\\.ons3\\.co\\/v1\\/?/gi,API)
      .replace(/https?:\\/\\/march2026api\\.1ex99\\.in\\/v1\\/?/gi,API);
  };
  var desc=Object.getOwnPropertyDescriptor(HTMLScriptElement.prototype,"src");
  if(desc&&desc.set){
    Object.defineProperty(HTMLScriptElement.prototype,"src",{
      set:function(v){
        if(typeof v==="string"&&block.test(v)) v="data:text/javascript,(function(){})();";
        desc.set.call(this,v);
      },
      get:desc.get,configurable:true
    });
  }
  var fo=window.fetch;
  if(fo){window.fetch=function(u,o){return fo.call(this,mapUrl(u),o);};}
  var xo=XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open=function(m,u){
    arguments[1]=mapUrl(u);
    return xo.apply(this,arguments);
  };
  var kill=function(){
    document.querySelectorAll('script[src]').forEach(function(el){
      var s=el.getAttribute("src")||"";
      if(block.test(s)) el.remove();
      else if(/\\/plugins\\//.test(s)&&!/jquery\\/jquery\\.min\\.js/.test(s)&&!/bootstrap\\/js\\/bootstrap\\.bundle\\.min\\.js/.test(s)) el.remove();
    });
  };
  if(document.readyState==="loading") document.addEventListener("DOMContentLoaded",kill);
  else kill();
})();
</script>"""

ADMIN_BOOTSTRAP = """<script id="ex99-admin-bootstrap">
(function(){
  var p=location.pathname;
  if(/^\\/admin\\/admin(\\/|$)/.test(p)){
    var fixed=p.replace(/^\\/admin\\/admin/,"/admin");
    location.replace(fixed+location.search+location.hash);
    return;
  }
  var API="/v1/";
  var block=/(?:\\/|^)(demo|dashboard)\\.js(?:\\?|$)/i;
  var mapUrl=function(u){
    if(typeof u!=="string")return u;
    return u
      .replace(/https?:\\/\\/api\\.ons3\\.co\\/v1\\/?/gi,API)
      .replace(/https?:\\/\\/march2026api\\.1ex99\\.in\\/v1\\/?/gi,API);
  };
  var desc=Object.getOwnPropertyDescriptor(HTMLScriptElement.prototype,"src");
  if(desc&&desc.set){
    Object.defineProperty(HTMLScriptElement.prototype,"src",{
      set:function(v){
        if(typeof v==="string"&&block.test(v)) v="data:text/javascript,(function(){})();";
        desc.set.call(this,v);
      },
      get:desc.get,configurable:true
    });
  }
  var fo=window.fetch;
  if(fo){window.fetch=function(u,o){return fo.call(this,mapUrl(u),o);};}
  var xo=XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open=function(m,u){
    arguments[1]=mapUrl(u);
    return xo.apply(this,arguments);
  };
  var kill=function(){
    document.querySelectorAll('script[src]').forEach(function(el){
      var s=el.getAttribute("src")||"";
      if(block.test(s)) el.remove();
      else if(/\\/plugins\\//.test(s)&&!/jquery\\/jquery\\.min\\.js/.test(s)&&!/bootstrap\\/js\\/bootstrap\\.bundle\\.min\\.js/.test(s)) el.remove();
    });
  };
  if(document.readyState==="loading") document.addEventListener("DOMContentLoaded",kill);
  else kill();
  var blockOldData=function(e){
    var a=e.target&&e.target.closest?e.target.closest("a.nav-link"):null;
    if(!a)return;
    var lbl=a.querySelector("p");
    if(lbl&&/^\\s*Old Data\\s*$/i.test(lbl.textContent||"")){e.preventDefault();e.stopPropagation();}
  };
  document.addEventListener("click",blockOldData,true);
  document.addEventListener("auxclick",blockOldData,true);
  var hideDomainField=function(){
    document.querySelectorAll("label").forEach(function(lbl){
      if(/^\\s*Select Domain\\s*$/i.test((lbl.textContent||"").trim())){
        var row=lbl.closest(".form-group")||lbl.closest(".subowner-share")||lbl.parentElement;
        if(row) row.style.display="none";
      }
    });
  };
  hideDomainField();
  if(document.readyState==="loading") document.addEventListener("DOMContentLoaded",hideDomainField);
  try{
    var obs=new MutationObserver(function(){hideDomainField();});
    obs.observe(document.documentElement,{childList:true,subtree:true});
  }catch(_e){}
})();
</script>"""


def patch_html_content(content: str) -> str:
    content = re.sub(
        r'<script[^>]*cloudflareinsights[^>]*>.*?</script>',
        "",
        content,
        flags=re.DOTALL,
    )

    def _keep_react_script(match: re.Match) -> str:
        tag = match.group(0)
        if "main.6e0cda40" in tag or "ex99-admin-bootstrap" in tag:
            return tag
        return ""

    # React SPA only — remove AdminLTE/jQuery plugin + inline $.widget scripts.
    content = re.sub(
        r"<script\b[^>]*>[\s\S]*?</script>\s*",
        _keep_react_script,
        content,
        flags=re.IGNORECASE,
    )
    content = content.replace(
        'src="/static/js/main.6e0cda40.js?v=ex99admin10"',
        'src="/static/js/main.6e0cda40.js?v=ex99admin28"',
    )
    content = content.replace(
        'src="/static/js/main.6e0cda40.js?v=ex99admin8"',
        'src="/static/js/main.6e0cda40.js?v=ex99admin28"',
    )
    content = content.replace(
        'src="/static/js/main.6e0cda40.js?v=ex99admin11"',
        'src="/static/js/main.6e0cda40.js?v=ex99admin28"',
    )
    content = content.replace(
        'src="/static/js/main.6e0cda40.js?v=ex99admin12"',
        'src="/static/js/main.6e0cda40.js?v=ex99admin28"',
    )
    content = content.replace(
        'src="/static/js/main.6e0cda40.js?v=ex99admin13"',
        'src="/static/js/main.6e0cda40.js?v=ex99admin28"',
    )
    content = content.replace(
        'src="/static/js/main.6e0cda40.js"',
        'src="/static/js/main.6e0cda40.js?v=ex99admin28"',
    )
    if "/plugins/jquery/jquery.min.js" not in content:
        content = content.replace("</body>", ADMIN_ESSENTIAL_SCRIPTS + "\n </body>", 1)
    if 'id="ex99-admin-bootstrap"' not in content:
        content = content.replace("<head>", "<head>\n  " + ADMIN_BOOTSTRAP, 1)
    if 'http-equiv="Cache-Control"' not in content:
        content = content.replace(
            "<head>",
            '<head>\n  <meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate"/>',
            1,
        )
    return content


def patch_old_data_html_content(content: str) -> str:
    """march2026admin index — React main.34e9d6c7.js + bootstrap (admin jaisa)."""
    content = re.sub(
        r'<script[^>]*cloudflareinsights[^>]*>.*?</script>',
        "",
        content,
        flags=re.DOTALL,
    )

    def _keep_react_script(match: re.Match) -> str:
        tag = match.group(0)
        if OLD_DATA_MAIN_JS in tag or "ex99-admin-bootstrap" in tag:
            return tag
        return ""

    content = re.sub(
        r"<script\b[^>]*>[\s\S]*?</script>\s*",
        _keep_react_script,
        content,
        flags=re.IGNORECASE,
    )
    content = content.replace(
        f'src="/static/js/{OLD_DATA_MAIN_JS}"',
        f'src="/static/js/{OLD_DATA_MAIN_JS}?v=ex99old2"',
    )
    if "/plugins/jquery/jquery.min.js" not in content:
        content = content.replace("</body>", ADMIN_ESSENTIAL_SCRIPTS + "\n </body>", 1)
    if 'id="ex99-admin-bootstrap"' not in content:
        content = content.replace("<head>", "<head>\n  " + OLD_DATA_BOOTSTRAP, 1)
    if 'http-equiv="Cache-Control"' not in content:
        content = content.replace(
            "<head>",
            '<head>\n  <meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate"/>',
            1,
        )
    return content


class AdminSiteHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[admin {self.log_date_time_string()}] {fmt % args}")

    def _send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def do_OPTIONS(self):
        self.send_response(204)
        self._send_cors_headers()
        self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path.startswith(API_LOCAL_PREFIX) or path.startswith(f"{OLD_DATA_PREFIX}{API_LOCAL_PREFIX}"):
            self._handle_api()
        else:
            self.send_error(404, "Not Found")

    def do_PATCH(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path.startswith(API_LOCAL_PREFIX) or path.startswith(f"{OLD_DATA_PREFIX}{API_LOCAL_PREFIX}"):
            self._handle_api()
        else:
            self.send_error(404, "Not Found")

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == OLD_DATA_PREFIX:
            location = OLD_DATA_PREFIX + "/" + (("?" + parsed.query) if parsed.query else "")
            self.send_response(301)
            self.send_header("Location", location)
            self.send_header("Cache-Control", "no-cache")
            self._send_cors_headers()
            self.end_headers()
            return

        is_old_data, route_path = split_old_data_path(path)
        site_dir = MARCH_SITE_DIR if is_old_data else SITE_DIR
        redirect = admin_app_redirect(route_path)
        if redirect:
            location = ((OLD_DATA_PREFIX if is_old_data else "") + redirect) + (
                ("?" + parsed.query) if parsed.query else ""
            )
            self.send_response(301)
            self.send_header("Location", location)
            self.send_header("Cache-Control", "no-cache")
            self._send_cors_headers()
            self.end_headers()
            return
        if path.startswith(CACHE_LOCAL_PREFIX):
            self._proxy_cache()
            return
        if path.startswith(API_LOCAL_PREFIX):
            self.send_error(405, "Method Not Allowed")
            return
        file_path = self._resolve_file(route_path, site_dir)
        if file_path is None or not file_path.exists():
            if route_path in ADMIN_LEGACY_JS_PATHS:
                self._serve_js_stub()
                return
            if route_path.endswith(".js"):
                self.send_error(404, "Not Found")
                return
            file_path = site_dir / "index.html"
            route_path = "/index.html"
        self._serve_file(file_path, path, is_old_data=is_old_data)

    def _serve_js_stub(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/javascript; charset=utf-8")
        self.send_header("Content-Length", str(len(ADMIN_LEGACY_JS_STUB)))
        self.send_header("Cache-Control", "no-store")
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(ADMIN_LEGACY_JS_STUB)

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
            print(f"[admin excache local] {rel} — {exc}")

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
            print(f"[admin excache fallback] {remote_url} — {exc}")
            fallback = json.dumps({"result": {}, "code": 0, "error": False, "data": []}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(fallback)))
            self.send_header("Cache-Control", "no-cache")
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(fallback)

    def _resolve_file(self, path: str, site_dir: Path = SITE_DIR) -> Optional[Path]:
        rel = path.lstrip("/")
        if not rel:
            return site_dir / "index.html"
        return site_dir / rel

    def _serve_file(self, file_path: Path, url_path: str, is_old_data: bool = False):
        try:
            content = file_path.read_bytes()
            content_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"

            if file_path.name == "index.html" or url_path.endswith("/index.html"):
                if is_old_data:
                    text = patch_old_data_html_content(content.decode("utf-8"))
                    text = patch_old_data_mount_body(text)
                else:
                    text = patch_html_content(content.decode("utf-8"))
                content = text.encode("utf-8")
                content_type = "text/html; charset=utf-8"

            if file_path.suffix == ".js":
                if content.lstrip()[:120].lower().startswith(b"<!doctype") or content.lstrip()[:6].lower().startswith(b"<html"):
                    m = ADMIN_CHUNK_STUB_RE.match(file_path.name)
                    content = admin_chunk_stub_js(m.group(1) if m else "0")
                else:
                    js = patch_admin_js(content.decode("utf-8"))
                    if is_old_data:
                        js = patch_old_data_mount_body(js)
                    if "hold-transition login-page" in js or "loginCardWrapper" in js:
                        js = patch_admin_login_js(js)
                    content = js.encode("utf-8")
                content_type = "application/javascript; charset=utf-8"

            if is_old_data and file_path.suffix == ".css":
                content = patch_old_data_mount_body(content.decode("utf-8")).encode("utf-8")
                content_type = "text/css; charset=utf-8"

            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-cache")
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(content)
        except Exception as exc:
            self.send_error(500, str(exc))

    def _handle_api(self):
        parsed = urlparse(self.path)
        api_path = parsed.path
        if api_path.startswith(OLD_DATA_PREFIX):
            api_path = api_path[len(OLD_DATA_PREFIX):]
        endpoint = api_path[len(API_LOCAL_PREFIX):]
        length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw_body.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            payload = {}
        for key, values in parse_qs(parsed.query).items():
            if key not in payload and values:
                payload[key] = values[0] if len(values) == 1 else values

        auth = self.headers.get("Authorization", "")
        print(f"[admin mongo] {endpoint}")
        try:
            body = handle_admin_api(endpoint, payload, auth)
        except Exception as exc:
            print(f"[admin api error] {endpoint} — {exc}")
            body = json.dumps({
                "message": str(exc),
                "code": 1,
                "error": True,
                "data": {},
            }).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(body)


def _start_match_sync_loop() -> None:
    """Background — live matchList se MongoDB sync (admin panel hamesha DB se padhe)."""
    interval = max(POLL_MS / 1000 * 5, 10)

    def loop() -> None:
        while True:
            try:
                rows = fetch_live_matches({})
                if rows:
                    sync_live_matches_to_db(rows)
                    print(f"[admin match-sync] {len(rows)} live → MongoDB")
            except Exception as exc:
                print(f"[admin match-sync] {exc}")
            time.sleep(interval)

    threading.Thread(target=loop, daemon=True, name="admin-match-sync").start()


def main():
    if not SITE_DIR.exists():
        print(f"Error: '{SITE_DIR}' not found. Pehle: python main.py --admin")
        sys.exit(1)

    if not ping():
        print("Error: MongoDB not running.")
        print("  brew services start mongodb-community")
        print("  python3 mongodb/insert_hierarchy.py")
        sys.exit(1)

    server = ReusableThreadingHTTPServer(("0.0.0.0", PORT), AdminSiteHandler)
    _start_match_sync_loop()
    print("=" * 50)
    print(f"{ADMIN_HOST} - MongoDB Local Server")
    print("=" * 50)
    print(f"Admin folder : {SITE_DIR}")
    print(f"Local URL    : http://localhost:{PORT}")
    print(f"Mode         : MongoDB-first (EX99_ADMIN_MONGO_ONLY=1)")
    print(f"Match list   : Live sync → MongoDB read every {POLL_MS}ms")
    print(f"Cache proxy  : http://localhost:{PORT}{CACHE_LOCAL_PREFIX}/")
    print(f"Database     : ex99_local / users collection")
    print(f"Login        : ADMIN001, OWNER001, SUPERADMIN001... (admin@123)")
    print("=" * 50)
    print("Ctrl+C se band karo")
    print("=" * 50)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nAdmin server stopped.")
        server.shutdown()


if __name__ == "__main__":
    main()
