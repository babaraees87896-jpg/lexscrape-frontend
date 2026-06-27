#!/usr/bin/env python3
"""Local server for scraped staff.bluewin.live — centerpanel/admin jaisa MongoDB mode."""

from __future__ import annotations

import json
import io
import mimetypes
import os
import re
import sys
import cgi
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
BACKEND = ROOT.parent
PROJECT = BACKEND.parent
sys.path.insert(0, str(BACKEND))

import cloudscraper

from mongodb.bluewin_api import ensure_bluewin_owner, handle_bluewin_api
from mongodb.db import ping
from mongodb.centerpanel_cache import proxy_odds_json
from mongodb.matches_api import CACHE_LOCAL_PREFIX, CACHE_REMOTE_PREFIX
from site_config import API_BASE_URL, BASE_URL, OUTPUT_ROOT
from site_config import STAFF_HOST as _DEFAULT_STAFF_HOST

PORT = int(os.getenv("BLUEWIN_PORT", "8110"))
STAFF_HOST = os.getenv("BLUEWIN_PUBLIC_HOST", os.getenv("STAFF_HOST", _DEFAULT_STAFF_HOST))
SITE_DIR = Path(os.getenv("BLUEWIN_SITE_DIR", PROJECT / "frontend" / "staff" / "site")).resolve()
ASSETS_SRC = Path(os.getenv("BLUEWIN_ASSETS_DIR", PROJECT / "frontend" / "staff" / "assets")).resolve()
AUTO_DECISION_PAGE = ROOT / "auto_decision_settings.html"
API_LOCAL_PREFIX = "/v1/"
JS_PATCH_FROM = API_BASE_URL
JS_PATCH_TO = API_LOCAL_PREFIX
POLL_MS = int(os.getenv("EX99_POLL_MS", "2000"))
POLL_JS = f"{POLL_MS // 1000}e3" if POLL_MS >= 1000 else str(POLL_MS)

scraper = cloudscraper.create_scraper(
    browser={"browser": "chrome", "platform": "darwin", "desktop": True}
)

CLEAN_INDEX = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1,shrink-to-fit=no"/>
  <meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate"/>
  <title>BlueWin - Operating Panel</title>
  <link href="https://fonts.googleapis.com/css?family=Poppins:300,400,500,600,700&display=swap" rel="stylesheet"/>
  <link rel="stylesheet" href="/css/bootstrap.min.css"/>
  <link rel="stylesheet" href="/css/style.css"/>
  <link rel="stylesheet" href="/css/responsive.css"/>
  <link rel="stylesheet" href="/index.css"/>
  <script defer="defer" src="/static/js/main.5580128d.js?v=bluewin6"></script>
  <link href="/static/css/main.c66ffc80.css" rel="stylesheet"/>
  <link rel="icon" type="image/x-icon" href="/faviconbluewin.ico"/>
</head>
<body>
  <div id="root"></div>
  <script src="/js/jquery-3.4.1.js"></script>
  <script src="/js/bootstrap.min.js"></script>
</body>
</html>
"""


def prepare_site() -> None:
    """Assets + clean index.html -> output/site/."""
    if not ASSETS_SRC.is_dir():
        raise SystemExit(f"Assets missing: {ASSETS_SRC} — run scrape_bluewin.py first")

    SITE_DIR.mkdir(parents=True, exist_ok=True)
    (SITE_DIR / "index.html").write_text(CLEAN_INDEX, encoding="utf-8")

    import shutil

    if AUTO_DECISION_PAGE.is_file():
        shutil.copy2(AUTO_DECISION_PAGE, SITE_DIR / "auto-decision-settings.html")

    def _safe_rmtree(path: Path) -> None:
        if not path.exists():
            return
        try:
            shutil.rmtree(path)
        except FileNotFoundError:
            pass

    for item in ASSETS_SRC.iterdir():
        dest = SITE_DIR / item.name
        if item.is_dir():
            _safe_rmtree(dest)
            shutil.copytree(item, dest)
        elif item.is_file():
            shutil.copy2(item, dest)


def patch_bluewin_js(content: str) -> str:
    for remote_api in (
        JS_PATCH_FROM,
        "https://api.bluewin.live/v1/",
        "http://api.bluewin.live/v1/",
        "https://api.bluewin.live/v1",
    ):
        content = content.replace(remote_api, JS_PATCH_TO)

    content = content.replace(CACHE_REMOTE_PREFIX, CACHE_LOCAL_PREFIX)
    content = content.replace("http://1excache.tresting.com", CACHE_LOCAL_PREFIX)
    content = content.replace(
        "host:window.location.host",
        f'host:"{STAFF_HOST}"',
    )
    content = content.replace(
        "if(400===e)return sessionStorage.clear(),localStorage.removeItem(\"user\"",
        "if(false&&400===e)return sessionStorage.clear(),localStorage.removeItem(\"user\"",
    )
    content = re.sub(
        r"(\w+)\.response\.data\.message",
        r"(\1.response&&\1.response.data&&\1.response.data.message||\1.message||'Something went wrong')",
        content,
    )
    content = content.replace(
        "function Lc(){let e=JSON.parse(localStorage.getItem(\"user\"));return e&&e.token?{Authorization:\"Bearer \"+e.token}:{}}",
        "function Lc(){let e=null;try{e=JSON.parse(localStorage.getItem(\"user\"))}catch(t){}const n=e&&e.token||localStorage.getItem(\"token\");return n?{Authorization:\"Bearer \"+n}:{}}",
    )
    content = content.replace(
        'localStorage.setItem("user",JSON.stringify(null===e||void 0===e?void 0:e.userinfo)),localStorage.setItem("UserPriority"',
        'localStorage.setItem("user",JSON.stringify(null===e||void 0===e?void 0:e.userinfo)),null!==e&&void 0!==e&&e.userinfo&&e.userinfo.token&&localStorage.setItem("token",e.userinfo.token),localStorage.setItem("UserPriority"',
    )
    content = content.replace(
        "(0,a.useEffect)(()=>{C(),$()},[]);",
        f"(0,a.useEffect)(()=>{{C(),$();const _bw=setInterval(()=>{{E(),$()}},{POLL_JS});return()=>clearInterval(_bw)}},[]);",
    )
    # Undeclare odds → dashboard/{marketId}: keep original dual load (market filter + INPLAY list).
    content = content.replace(
        'bc.success(t.message),this.getInplayData(this.state.fieldsMatchType.matchType)),this.setState({isFetch:!1})},this.submitOddsLedgerDecision',
        'bc.success(t.message),this.getInplayData(this.props.match.params.marketId?{marketId:this.props.match.params.marketId}:{status:this.state.fieldsMatchType.matchType,sportId:this.state.currentSportId})),this.setState({isFetch:!1})},this.submitOddsLedgerDecision',
    )
    # Bookmaker undeclare → dashboard + auto Match Decision modal (original step 2+3).
    old_bm_declare = (
        'this.handleBetDeclare=e=>{this.props.history.push(`/app/dashboard/${null===e||void 0===e?void 0:e.marketId}`)},'
        'this.handleBetDeclareFancy=e=>{this.props.history.push(`/app/fancydecision/${null===e||void 0===e?void 0:e.marketId}`)}'
    )
    new_bm_declare = (
        'this.handleBetDeclare=e=>{const t=null===e||void 0===e?void 0:e.marketId,n=String((null===e||void 0===e?void 0:e.oddsType)||"").toLowerCase();'
        '"bookmaker"===n&&t&&sessionStorage.setItem("bwBmDeclare",String(t));'
        'this.props.history.push(`/app/dashboard/${t}`)},'
        'this.handleBetDeclareFancy=e=>{this.props.history.push(`/app/fancydecision/${null===e||void 0===e?void 0:e.marketId}`)}'
    )
    if old_bm_declare not in content:
        raise RuntimeError("Bookmaker declare redirect patch anchor missing")
    content = content.replace(old_bm_declare, new_bm_declare)
    old_inplay_end = (
        ');"all"===this.state.currentSportId?this.setState({inplayMatchList:r,filterList:r}):'
        'this.setState({inplayMatchList:r,filterList:r.filter(e=>e.sportId===this.state.currentSportId)})}'
        'this.setState({isFetch:!1})},this.handlePageClick=async e=>{const t=e.selected;'
        'this.setState({offset:t*this.state.size,pageNo:t},()=>{this.getInplayData({status:"COMPLETED"'
    )
    new_inplay_end = (
        ');"all"===this.state.currentSportId?this.setState({inplayMatchList:r,filterList:r}):'
        'this.setState({inplayMatchList:r,filterList:r.filter(e=>e.sportId===this.state.currentSportId)})}'
        'try{const _bm=sessionStorage.getItem("bwBmDeclare");'
        'if(_bm&&t&&t.data){const _arr=Array.isArray(t.data)?t.data:[t.data],'
        '_m=_arr.find(x=>x&&String(x.marketId)===String(_bm));'
        '_m&&(sessionStorage.removeItem("bwBmDeclare"),this.setState({matchDecisionModal:!0,decisionModal:!1,teamData:_m.teamData,matchData:_m}))}}catch(_x){}'
        'this.setState({isFetch:!1})},this.handlePageClick=async e=>{const t=e.selected;'
        'this.setState({offset:t*this.state.size,pageNo:t},()=>{this.getInplayData({status:"COMPLETED"'
    )
    if old_inplay_end not in content:
        raise RuntimeError("Bookmaker declare modal patch anchor missing")
    content = content.replace(old_inplay_end, new_inplay_end, 1)
    # Dashboard header Auto Decision → settings page.
    old_auto_btn = "saveAutomaticBetfairDecision=()=>{this.setState({isModalOpen:!0})}"
    new_auto_btn = 'saveAutomaticBetfairDecision=()=>{window.location.href="/auto-decision-settings.html"}'
    if old_auto_btn not in content:
        raise RuntimeError("Auto Decision button patch anchor missing")
    content = content.replace(old_auto_btn, new_auto_btn, 1)
    old_int_update = (
        'let n=await up("website/updateInternationalCasinoByOperating",t);'
        'n&&bc.success(null===n||void 0===n?void 0:n.message),this.setState({isFetch:!1,casinoUpdateModal:!1})'
    )
    new_int_update = (
        'let n=await up("website/updateInternationalCasinoByOperating",t);'
        'n&&(bc.success(null===n||void 0===n?void 0:n.message),this.intCasinoList()),'
        'this.setState({isFetch:!1,casinoUpdateModal:!1})'
    )
    if old_int_update not in content:
        raise RuntimeError("Int casino update refresh patch anchor missing")
    content = content.replace(old_int_update, new_int_update, 1)
    # Int. Casino bet list (lP/bP) — original sends gameId 201206 for internationalCasino scope.
    int_bet_patches = (
        (
            'gameType:this.state.fieldsUser.gameType?this.state.fieldsUser.gameType:"diamondCasino",toDate:this.state.toDate,fromDate:this.state.fromDate};this.setState({isFetch:!0});let t=await up("user/casinoReportByUser",e);t&&this.setState({casinoList:t.data}),this.setState({isFetch:!1})},this.getCompleteCasinoList=async e=>{let t={gameId:"internationalCasino"===this.state.fieldsUser.gameType?"201206":this.state.fieldsUser.gameId,toDate:this.state.toDate,fromDate:this.state.fromDate,casinoBet:!0,downlineUserId:e,downlineUserType:"client"',
            'gameType:this.state.fieldsUser.gameType?this.state.fieldsUser.gameType:"internationalCasino",toDate:this.state.toDate,fromDate:this.state.fromDate};this.setState({isFetch:!0});let t=await up("user/casinoReportByUser",e);t&&this.setState({casinoList:t.data}),this.setState({isFetch:!1})},this.getCompleteCasinoList=async e=>{let t={gameId:"201206",toDate:this.state.toDate,fromDate:this.state.fromDate,casinoBet:!0,downlineUserId:e,downlineUserType:"client"',
        ),
        (
            'casinoBet:!0,isDeleted:"deletedCasino"===this.state.fieldsUser.casinoType?1:"0",isDeclare:1,pageNo:this.state.pageNo,size:this.state.size,sortData:{createdAt:1}};this.setState({isFetch:!0});let t=await up("sports/betsList",e);if(t){let e=0,n=0;t.data.casinoBetData.forEach(t=>{t.creditAmount>0&&t.isDeclare&&(e+=t.creditAmount-t.debitAmount),0===t.creditAmount&&t.isDeclare&&(n+=t.debitAmount)}),this.setState({casinoBetList:t.data.casinoBetData,totalOddsCount:t.data.totalOddsCount',
            'casinoBet:!0,gameId:"201206",isDeleted:"deletedCasino"===this.state.fieldsUser.casinoType?1:"0",isDeclare:1,pageNo:this.state.pageNo,size:this.state.size,sortData:{createdAt:1}};this.setState({isFetch:!0});let t=await up("sports/betsList",e);if(t){let e=0,n=0;t.data.casinoBetData.forEach(t=>{t.creditAmount>0&&t.isDeclare&&(e+=t.creditAmount-t.debitAmount),0===t.creditAmount&&t.isDeclare&&(n+=t.debitAmount)}),this.setState({casinoBetList:t.data.casinoBetData,totalOddsCount:t.data.totalOddsCount',
        ),
        (
            'gameId:"internationalCasino"===this.state.fieldsUser.gameType?"201206":this.state.fieldsUser.gameId,toDate:this.state.toDate,fromDate:this.state.fromDate,casinoBet:!0,isDeleted:"deletedCasino"===this.state.fieldsUser.casinoType?1:"0",isDeclare:1,sortData:{createdAt:1},userName:this.state.fieldsUser.username,pageNo:e.selected+1,size:this.state.size};this.setState({isFetch:!0});let r=await up("sports/betsList",n);',
            'gameId:"201206",toDate:this.state.toDate,fromDate:this.state.fromDate,casinoBet:!0,isDeleted:"deletedCasino"===this.state.fieldsUser.casinoType?1:"0",isDeclare:1,sortData:{createdAt:1},userName:this.state.fieldsUser.username,pageNo:e.selected+1,size:this.state.size};this.setState({isFetch:!0});let r=await up("sports/betsList",n);',
        ),
        (
            'toDate:MN()().format("YYYY-MM-DD"),fromDate:MN()().format("YYYY-MM-DD"),casinoBet:!0,isDeleted:"deletedCasino"===this.state.fieldsUser.casinoType?1:"0",isDeclare:"0",sortData:{createdAt:1}};this.setState({isFetch:!0});let t=await up("sports/betsList",e);if(t){let e=0,n=0;t.data.casinoBetData.forEach(t=>{t.creditAmount>0&&t.isDeclare&&(e+=t.creditAmount-t.debitAmount),0===t.creditAmount&&t.isDeclare&&(n+=t.debitAmount)}),this.setState({casinoBetList:t.data.casinoBetData,filteredData:t.data.casinoBetData,totalProfit:e,totalLoss:n})}this.setState({isFetch:!1})},this.getCasino=async()=>{let e={gameType:this.state.fieldsUser.gameType?this.state.fieldsUser.gameType:"diamondCasino",toDate:this.state.toDate,fromDate:this.state.fromDate};this.setState({isFetch:!0});let t=await up("user/casinoReportByUser",e);t&&this.setState({casinoList:t.data}),this.setState({isFetch:!1})},this.getCompleteCasinoList=async()=>{let e={casinoBet:!0,isDeleted:',
            'toDate:MN()().format("YYYY-MM-DD"),fromDate:MN()().format("YYYY-MM-DD"),casinoBet:!0,gameId:"201206",isDeleted:"deletedCasino"===this.state.fieldsUser.casinoType?1:"0",isDeclare:"0",sortData:{createdAt:1}};this.setState({isFetch:!0});let t=await up("sports/betsList",e);if(t){let e=0,n=0;t.data.casinoBetData.forEach(t=>{t.creditAmount>0&&t.isDeclare&&(e+=t.creditAmount-t.debitAmount),0===t.creditAmount&&t.isDeclare&&(n+=t.debitAmount)}),this.setState({casinoBetList:t.data.casinoBetData,filteredData:t.data.casinoBetData,totalProfit:e,totalLoss:n})}this.setState({isFetch:!1})},this.getCasino=async()=>{let e={gameType:this.state.fieldsUser.gameType?this.state.fieldsUser.gameType:"internationalCasino",toDate:this.state.toDate,fromDate:this.state.fromDate};this.setState({isFetch:!0});let t=await up("user/casinoReportByUser",e);t&&this.setState({casinoList:t.data}),this.setState({isFetch:!1})},this.getCompleteCasinoList=async()=>{let e={casinoBet:!0,gameId:"201206",isDeleted:',
        ),
    )
    for old, new in int_bet_patches:
        if old not in content:
            raise RuntimeError("Int casino bet list patch anchor missing")
        content = content.replace(old, new, 1)
    # User Exposure (XI) — User ID + Username search.
    user_expo_patches = (
        (
            'this.getUserExposerList=async()=>{if(this.handleValidationExposer()){let e={username:this.state.fieldsUser.username};this.setState({isFetch:!0});let t=await up("website/checkExposureClient",e);t&&this.setState({userExposerModal:!0,userExpoList:t.data?t.data:{}}),this.setState({isFetch:!1})}},this.clearUserExposer=async()=>{let e={_id:this.state.exposerData._id,password:this.state.fieldsUser.password};this.setState({isFetch:!0});let t=await up("website/clearExposure",e);t&&(bc.success(t.message),this.setState({message:t.message,exposerConfirmModal:!1,fieldsUser:{}})),this.setState({isFetch:!1})},this.handleValidationExposer=()=>{let e={},t=!0;return this.state.fieldsUser.username||(t=!1,e.username="Username can not be empty."),this.setState({errorsUser:e}),t},this.inputChange=e=>{e.preventDefault();let{name:t,value:n}=e.target,r=this.state.fieldsUser,a=this.state.errorsUser;r[t]=n,a[t]="",this.setState({fieldsUser:r,errorsUser:a})},this.handleExposerModalOpen=e=>{this.setState({exposerConfirmModal:!0,exposerData:e})},this.handleExposerModalClose=()=>{this.setState({exposerConfirmModal:!1,fieldsUser:{}})},this.state={isFetch:!1,exposerConfirmModal:!1,errorsUser:{},fieldsUser:{username:this.props.match.params.username?this.props.match.params.username:""},userExpoList:{},exposerData:{}},this.modalRef=t.createRef()}componentDidMount(){this.props.match.params.username&&this.getUserExposerList()}render(){let{isFetch:e,errorsUser:t,fieldsUser:n,userExpoList:r}=this.state;return(0,xp.jsxs)(xp.Fragment,{children:[(0,xp.jsx)(_N,{active:e}),(0,xp.jsxs)("div",{className:"flex flex-col flex-1 overflow-hidden",style:{background:"#f1f5f9",minHeight:"100vh"},children:[(0,xp.jsx)("main",{className:"relative flex-1 p-3 md:p-4 lg:p-6",children:(0,xp.jsxs)("div",{className:"max-w-screen-2xl mx-auto",children:[(0,xp.jsxs)("div",{className:"bg-white rounded-xl shadow-sm border border-gray-100 overflow-hidden mb-4",style:{animation:"fadeInUp 0.4s ease-out"},children:[(0,xp.jsx)(LN,{title:"User Exposer",history:this.props.history}),(0,xp.jsx)("div",{className:"p-3 border-t border-gray-100",children:(0,xp.jsxs)("div",{className:"flex items-end gap-3 flex-wrap",children:[(0,xp.jsxs)("div",{children:[(0,xp.jsx)("label",{className:"text-xs font-semibold text-gray-500 uppercase tracking-wide mb-1 block",children:"Username"}),(0,xp.jsx)("input",{className:"w-full px-3 py-2 text-sm border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500",id:"userType",name:"username",value:n&&n.username?n.username:"",onChange:this.inputChange,placeholder:"Enter username"}),t&&t.username?(0,xp.jsx)("span",{className:"text-xs text-red-500 mt-1 block",children:t.username}):null]}),(0,xp.jsx)("div",{children:(0,xp.jsx)("button",{onClick:this.getUserExposerList,className:"bg-gradient-to-r from-blue-500 to-blue-600 text-white rounded-lg px-4 py-2 text-sm font-semibold shadow-sm transition-all duration-200 hover:scale-[1.02] active:scale-[0.98]",children:"Apply"})})]})})]}),',
            'this.getUserExposerList=async()=>{if(this.handleValidationExposer()){let e={userId:this.state.fieldsUser.userId||"",username:this.state.fieldsUser.username||""};this.setState({isFetch:!0});let t=await up("website/checkExposureClient",e);t&&this.setState({userExposerModal:!0,userExpoList:t.data?t.data:{}}),this.setState({isFetch:!1})}},this.clearUserExposer=async()=>{let e={_id:this.state.exposerData._id,password:this.state.fieldsUser.password};this.setState({isFetch:!0});let t=await up("website/clearExposure",e);t&&(bc.success(t.message),this.setState({message:t.message,exposerConfirmModal:!1,fieldsUser:{}})),this.setState({isFetch:!1})},this.handleValidationExposer=()=>{let e={},t=!0;return(this.state.fieldsUser.userId||this.state.fieldsUser.username)||(t=!1,e.username="User ID or Username is required."),this.setState({errorsUser:e}),t},this.inputChange=e=>{e.preventDefault();let{name:t,value:n}=e.target,r=this.state.fieldsUser,a=this.state.errorsUser;r[t]=n,a[t]="",this.setState({fieldsUser:r,errorsUser:a})},this.handleExposerModalOpen=e=>{this.setState({exposerConfirmModal:!0,exposerData:e})},this.handleExposerModalClose=()=>{this.setState({exposerConfirmModal:!1,fieldsUser:{}})},this.state={isFetch:!1,exposerConfirmModal:!1,errorsUser:{},fieldsUser:{userId:"",username:this.props.match.params.username?this.props.match.params.username:""},userExpoList:{},exposerData:{}},this.modalRef=t.createRef()}componentDidMount(){this.props.match.params.username&&this.getUserExposerList()}render(){let{isFetch:e,errorsUser:t,fieldsUser:n,userExpoList:r}=this.state;return(0,xp.jsxs)(xp.Fragment,{children:[(0,xp.jsx)(_N,{active:e}),(0,xp.jsxs)("div",{className:"flex flex-col flex-1 overflow-hidden",style:{background:"#f1f5f9",minHeight:"100vh"},children:[(0,xp.jsx)("main",{className:"relative flex-1 p-3 md:p-4 lg:p-6",children:(0,xp.jsxs)("div",{className:"max-w-screen-2xl mx-auto",children:[(0,xp.jsxs)("div",{className:"bg-white rounded-xl shadow-sm border border-gray-100 overflow-hidden mb-4",style:{animation:"fadeInUp 0.4s ease-out"},children:[(0,xp.jsx)(LN,{title:"User Exposer",history:this.props.history}),(0,xp.jsx)("div",{className:"p-3 border-t border-gray-100",children:(0,xp.jsxs)("div",{className:"flex items-end gap-3 flex-wrap",children:[(0,xp.jsxs)("div",{children:[(0,xp.jsx)("label",{className:"text-xs font-semibold text-gray-500 uppercase tracking-wide mb-1 block",children:"User ID"}),(0,xp.jsx)("input",{className:"w-full px-3 py-2 text-sm border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500",id:"userIdType",name:"userId",value:n&&n.userId?n.userId:"",onChange:this.inputChange,onKeyDown:e=>{"Enter"===e.key&&this.getUserExposerList()},placeholder:"Enter user ID"}),null]}),(0,xp.jsxs)("div",{children:[(0,xp.jsx)("label",{className:"text-xs font-semibold text-gray-500 uppercase tracking-wide mb-1 block",children:"Username"}),(0,xp.jsx)("input",{className:"w-full px-3 py-2 text-sm border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500",id:"userType",name:"username",value:n&&n.username?n.username:"",onChange:this.inputChange,onKeyDown:e=>{"Enter"===e.key&&this.getUserExposerList()},placeholder:"Enter username"}),t&&t.username?(0,xp.jsx)("span",{className:"text-xs text-red-500 mt-1 block",children:t.username}):null]}),(0,xp.jsx)("div",{children:(0,xp.jsx)("button",{onClick:this.getUserExposerList,className:"bg-gradient-to-r from-blue-500 to-blue-600 text-white rounded-lg px-4 py-2 text-sm font-semibold shadow-sm transition-all duration-200 hover:scale-[1.02] active:scale-[0.98]",children:"Apply"})})]})})]}),',
        ),
    )
    for old, new in user_expo_patches:
        if old not in content:
            raise RuntimeError("User exposure search patch anchor missing")
        content = content.replace(old, new, 1)
    # Hide Matka Settings from sidebar (expanded + collapsed flyout).
    matka_nav_removals = (
        ',(0,xp.jsxs)("div",{className:"mt-1",children:[(0,xp.jsxs)("span",{onClick:()=>d("matkaSetting"===c?"":"matkaSetting"),className:b("matkaSetting"),children:[(0,xp.jsx)("span",{className:"ml-5",children:(0,xp.jsx)(Zp,{size:20})}),(0,xp.jsx)("span",{className:"ml-4 flex-1",children:"Matka Settings"}),(0,xp.jsx)(Fp,{className:"chevron-rotate "+("matkaSetting"===c?"chevron-rotate-open":""),size:16})]}),(0,xp.jsxs)("div",{className:"submenu-enter "+("matkaSetting"===c?"submenu-open":""),children:[(0,xp.jsxs)("span",{onClick:()=>h("/app/matka"),className:g("/app/matka"),children:[(0,xp.jsx)(ff,{size:14,className:"flex-shrink-0 sub-icon"}),"Matka List"]}),(0,xp.jsxs)("span",{onClick:()=>h("/app/resultDeclare"),className:g("/app/resultDeclare"),children:[(0,xp.jsx)(Lf,{size:15,className:"flex-shrink-0 sub-icon"}),"Result Declare"]}),(0,xp.jsxs)("span",{onClick:()=>h("/app/matkaGameType"),className:g("/app/matkaGameType"),children:[(0,xp.jsx)(cp,{size:15,className:"flex-shrink-0 sub-icon"}),"Game Type"]}),(0,xp.jsxs)("span",{onClick:()=>h("/app/matkaBetList"),className:g("/app/matkaBetList"),children:[(0,xp.jsx)(sf,{size:15,className:"flex-shrink-0 sub-icon"}),"Bet List"]})]})]})',
        ',(0,xp.jsxs)("div",{className:"group w-full relative",children:[(0,xp.jsx)("div",{className:"flex justify-center items-center py-3 text-gray-400 cursor-pointer transition-all duration-200 border-l-[3px] "+(f(v.matkaSetting)?"text-amber-400 bg-white/10 border-amber-400":"border-transparent hover:text-white hover:bg-white/5"),children:(0,xp.jsx)(Zp,{size:22})}),(0,xp.jsxs)("div",{className:"flyout-menu absolute left-20 top-0 z-50 w-56 rounded-lg shadow-2xl py-1.5",style:{background:"linear-gradient(135deg, #1e2222 0%, #2d3131 100%)",border:"1px solid rgba(255,255,255,0.1)"},children:[(0,xp.jsxs)("div",{className:"px-3 py-2 text-[10px] font-bold text-gray-500 uppercase tracking-widest border-b border-white/10 flex items-center gap-2",children:[(0,xp.jsx)(Zp,{size:12}),"Matka Settings"]}),(0,xp.jsxs)("span",{onClick:()=>h("/app/matka"),className:"cursor-pointer flex items-center gap-2.5 px-3 py-2 text-xs uppercase tracking-wide transition-all duration-200 "+(p("/app/matka")?"text-amber-400 bg-white/10 font-bold":"text-gray-300 hover:text-white hover:bg-white/5 font-medium"),children:[(0,xp.jsx)(ff,{size:13}),"Matka List"]}),(0,xp.jsxs)("span",{onClick:()=>h("/app/resultDeclare"),className:"cursor-pointer flex items-center gap-2.5 px-3 py-2 text-xs uppercase tracking-wide transition-all duration-200 "+(p("/app/resultDeclare")?"text-amber-400 bg-white/10 font-bold":"text-gray-300 hover:text-white hover:bg-white/5 font-medium"),children:[(0,xp.jsx)(Lf,{size:14}),"Result Declare"]}),(0,xp.jsxs)("span",{onClick:()=>h("/app/matkaGameType"),className:"cursor-pointer flex items-center gap-2.5 px-3 py-2 text-xs uppercase tracking-wide transition-all duration-200 "+(p("/app/matkaGameType")?"text-amber-400 bg-white/10 font-bold":"text-gray-300 hover:text-white hover:bg-white/5 font-medium"),children:[(0,xp.jsx)(cp,{size:14}),"Game Type"]}),(0,xp.jsxs)("span",{onClick:()=>h("/app/matkaBetList"),className:"cursor-pointer flex items-center gap-2.5 px-3 py-2 text-xs uppercase tracking-wide transition-all duration-200 "+(p("/app/matkaBetList")?"text-amber-400 bg-white/10 font-bold":"text-gray-300 hover:text-white hover:bg-white/5 font-medium"),children:[(0,xp.jsx)(sf,{size:14}),"Bet List"]})]})]})',
    )
    for block in matka_nav_removals:
        if block not in content:
            raise RuntimeError("Matka Settings sidebar patch anchor missing")
        content = content.replace(block, "", 1)
    return content


def patch_html(content: str) -> str:
    content = re.sub(
        r"<script[^>]*cloudflare[^>]*>.*?</script>",
        "",
        content,
        flags=re.DOTALL | re.IGNORECASE,
    )
    content = re.sub(r"<iframe[^>]*></iframe>", "", content, flags=re.IGNORECASE)
    return content


class BluewinSiteHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[bluewin {self.log_date_time_string()}] {fmt % args}")

    def _send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def do_OPTIONS(self):
        self.send_response(204)
        self._send_cors_headers()
        self.end_headers()

    def do_POST(self):
        if self.path.startswith(API_LOCAL_PREFIX):
            self._handle_api()
        else:
            self.send_error(404, "Not Found")

    def do_PATCH(self):
        if self.path.startswith(API_LOCAL_PREFIX):
            self._handle_api()
        else:
            self.send_error(404, "Not Found")

    def do_GET(self):
        path = urlparse(self.path).path
        if path.startswith(CACHE_LOCAL_PREFIX):
            self._proxy_cache()
            return
        if path.startswith(API_LOCAL_PREFIX):
            self._handle_api(method="GET")
            return
        file_path = self._resolve_file(path)
        if file_path is None or not file_path.exists():
            file_path = SITE_DIR / "index.html"
            path = "/index.html"
        self._serve_file(file_path, path)

    def _proxy_cache(self):
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
            print(f"[bluewin excache local] {rel} — {exc}")

        remote_url = CACHE_REMOTE_PREFIX + rel
        if query:
            remote_url += "?" + query
        try:
            resp = scraper.get(
                remote_url,
                headers={"User-Agent": "Mozilla/5.0", "Origin": BASE_URL},
                timeout=20,
            )
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
            print(f"[bluewin excache fallback] {remote_url} — {exc}")
            fallback = json.dumps({"result": {}, "code": 0, "error": False, "data": []}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(fallback)))
            self.send_header("Cache-Control", "no-cache")
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(fallback)

    def _resolve_file(self, path: str) -> Optional[Path]:
        rel = path.lstrip("/")
        if not rel:
            return SITE_DIR / "index.html"
        return SITE_DIR / rel

    def _serve_file(self, file_path: Path, url_path: str):
        try:
            if file_path.name == "auto-decision-settings.html" and AUTO_DECISION_PAGE.is_file():
                content = AUTO_DECISION_PAGE.read_bytes()
                content_type = "text/html; charset=utf-8"
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Cache-Control", "no-cache")
                self._send_cors_headers()
                self.end_headers()
                self.wfile.write(content)
                return

            content = file_path.read_bytes()
            content_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"

            if file_path.name == "index.html" or url_path == "/index.html":
                content = patch_html(content.decode("utf-8")).encode("utf-8")
                content_type = "text/html; charset=utf-8"

            if file_path.suffix == ".js":
                content = patch_bluewin_js(content.decode("utf-8")).encode("utf-8")
                content_type = "application/javascript; charset=utf-8"

            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-cache")
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(content)
        except Exception as exc:
            self.send_error(500, str(exc))

    def _handle_api(self, method: str = "POST"):
        parsed = urlparse(self.path)
        endpoint = parsed.path[len(API_LOCAL_PREFIX):]
        content_type = self.headers.get("Content-Type", "")

        if "multipart/form-data" in content_type and endpoint == "website/fileUpload":
            length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(length) if length else b""
            environ = {
                "REQUEST_METHOD": method,
                "CONTENT_TYPE": content_type,
                "CONTENT_LENGTH": str(len(body_bytes)),
            }
            form = cgi.FieldStorage(
                fp=io.BytesIO(body_bytes),
                headers=self.headers,
                environ=environ,
            )
            file_item = form["image"] if "image" in form else None
            filename = getattr(file_item, "filename", None) if file_item else None
            content = file_item.file.read() if file_item and getattr(file_item, "file", None) else b""
            auth = self.headers.get("Authorization", "")
            print(f"[bluewin mongo] {endpoint} (multipart)")
            from mongodb.bluewin_handlers import bw_website_file_upload

            result = bw_website_file_upload(
                {"filename": filename, "content": content},
                None,
            )
            body = json.dumps(result, default=str).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(body)
            return

        length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(length) if length and method != "GET" else b"{}"
        try:
            payload = json.loads(raw_body.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            payload = {}
        for key, values in parse_qs(parsed.query).items():
            if key not in payload and values:
                payload[key] = values[0] if len(values) == 1 else values

        auth = self.headers.get("Authorization", "")
        print(f"[bluewin mongo] {endpoint}")
        body = handle_bluewin_api(endpoint, payload, auth)

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(body)


def main():
    prepare_site()

    if not ping():
        print("Error: MongoDB not running.")
        print("  brew services start mongodb-community")
        print("  cd backend && python main.py --setup-mongo")
        sys.exit(1)

    owner = ensure_bluewin_owner()
    if owner.get("ok"):
        print(f"[mongo] BlueWin owner ready: {owner.get('username')}")

    server = ThreadingHTTPServer(("0.0.0.0", PORT), BluewinSiteHandler)
    print("=" * 50)
    print(f"{STAFF_HOST} - MongoDB Local Server")
    print("=" * 50)
    print(f"Site folder  : {SITE_DIR}")
    print(f"Local URL    : http://localhost:{PORT}")
    print(f"Login page   : http://localhost:{PORT}/#/login")
    print(f"Dashboard    : http://localhost:{PORT}/#/app/dashboard")
    print(f"API patch    : {JS_PATCH_FROM} -> {JS_PATCH_TO}")
    print(f"Database     : ex99_local (MongoDB — centerpanel jaisa)")
    print(f"Owner login  : OW1000 / Bluewin@4923")
    print(f"Alt owner    : OWNER001 / admin@123")
    print("=" * 50)
    print("Ctrl+C se band karo")
    print("=" * 50)

    try:
        from mongodb.auto_decision_worker import start_auto_decision_worker
        start_auto_decision_worker()
    except Exception as exc:
        print(f"[auto-decision] worker not started: {exc}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nBlueWin server stopped.")
        server.shutdown()


if __name__ == "__main__":
    main()
