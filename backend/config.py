"""1ex99.in scraper configuration."""

import os
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _BACKEND_DIR.parent
_FRONTEND_ROOT = _PROJECT_ROOT / "frontend"


def _frontend_dir(name: str) -> str:
    return str(_FRONTEND_ROOT / name)


BASE_URL = "https://1ex99.in"
ADMIN_BASE_URL = "https://admin.1ex99.in"
CENTERPANEL_BASE_URL = "https://centerpanel.1ex99.in"
SUPERADMIN_BASE_URL = "https://admin.ons3.co"
API_BASE_URL = "https://api.ons3.co/v1/"
CENTERPANEL_API_BASE_URL = "https://cache.1ex99.in/v1/"
AES_KEY = "?l5V-37g~<Li"

USERNAME = os.getenv("EX99_USERNAME", "C358167")
PASSWORD = os.getenv("EX99_PASSWORD", "615849")
HOST = os.getenv("EX99_HOST", "1ex99.in")
ADMIN_HOST = os.getenv("EX99_ADMIN_HOST", "admin.1ex99.in")
MARCH2026_ADMIN_BASE_URL = os.getenv("EX99_MARCH_ADMIN_BASE_URL", "https://march2026admin.1ex99.in")
MARCH2026_ADMIN_HOST = os.getenv("EX99_MARCH_ADMIN_HOST", "march2026admin.1ex99.in")
MARCH2026_API_URL = os.getenv("EX99_MARCH_API_URL", "https://march2026api.1ex99.in/v1/")
MARCH2026_ADMIN_OUTPUT_DIR = os.getenv("EX99_MARCH_ADMIN_OUTPUT_DIR", _frontend_dir("march2026admin"))
CENTERPANEL_HOST = os.getenv("EX99_CENTERPANEL_HOST", "centerpanel.1ex99.in")
SUPERADMIN_HOST = os.getenv("EX99_SUPERADMIN_HOST", "admin.ons3.co")
ADMIN_USERNAME = os.getenv("EX99_ADMIN_USER", "ADMIN001")
ADMIN_PASSWORD = os.getenv("EX99_ADMIN_PASS", "admin@123")
SUPERADMIN_USERNAME = os.getenv("EX99_SUPERADMIN_USER", ADMIN_USERNAME)
SUPERADMIN_PASSWORD = os.getenv("EX99_SUPERADMIN_PASS", ADMIN_PASSWORD)
CENTERPANEL_USERNAME = os.getenv("EX99_CENTERPANEL_USER", ADMIN_USERNAME)
CENTERPANEL_PASSWORD = os.getenv("EX99_CENTERPANEL_PASS", ADMIN_PASSWORD)

OUTPUT_DIR = os.getenv("EX99_OUTPUT_DIR", _frontend_dir("main"))
ADMIN_OUTPUT_DIR = os.getenv("EX99_ADMIN_OUTPUT_DIR", _frontend_dir("admin"))
CENTERPANEL_OUTPUT_DIR = os.getenv("EX99_CENTERPANEL_OUTPUT_DIR", _frontend_dir("centerpanel"))
SUPERADMIN_OUTPUT_DIR = os.getenv("EX99_SUPERADMIN_OUTPUT_DIR", _frontend_dir("superadmin"))

MONGO_URI = os.getenv("EX99_MONGO_URI", "mongodb://localhost:27017")
MONGO_DB_NAME = os.getenv("EX99_MONGO_DB", "ex99_local")

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Origin": BASE_URL,
    "Referer": f"{BASE_URL}/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

ADMIN_BROWSER_HEADERS = {
    **BROWSER_HEADERS,
    "Origin": ADMIN_BASE_URL,
    "Referer": f"{ADMIN_BASE_URL}/",
}

SUPERADMIN_BROWSER_HEADERS = {
    **BROWSER_HEADERS,
    "Origin": SUPERADMIN_BASE_URL,
    "Referer": f"{SUPERADMIN_BASE_URL}/",
}

CENTERPANEL_BROWSER_HEADERS = {
    **BROWSER_HEADERS,
    "Origin": CENTERPANEL_BASE_URL,
    "Referer": f"{CENTERPANEL_BASE_URL}/",
}

API_ENDPOINTS = [
    "user/login",
    "user/userBalance",
    "user/userDetails",
    "user/userList",
    "user/userStatement",
    "user/userLedger",
    "user/completeLedgerDetails",
    "user/clientBetListByMarketId",
    "user/casinoLoginUrl",
    "user/userAccountDetails",
    "user/userSearch",
    "website/domainSettingByDomainName",
    "sports/matchList",
    "sports/betsList",
    "sports/sportByMarketId",
    "sports/clientListByMarketId",
    "sports/userPositionByMarketId",
    "casino/getDiamondCasinoData",
    "casino/getDiamondCasinoByEventId",
    "casino/diamondBetsList",
    "casino/roundWiseResult",
    "casino/resultByRoundWise",
    "reports/casinoTransactionReport",
    "reports/getOddsPositionForAllexch",
    "bpexch/loadBalance",
    "bpexch/bpexchAccountStatement",
    "bluexchReports/clientPlusMinus",
    "matka/getMatkaList",
    "matka/getMatkaByMatkaEventId",
    "matka/matkaBetList",
    "matka/matkaReportByUser",
]

ADMIN_API_ENDPOINTS = [
    "user/login",
    "user/userList",
    "user/userDetails",
    "user/userBalance",
    "user/userLedger",
    "user/userActivity",
    "user/domainList",
    "user/getUserShareData",
    "user/create",
    "sports/matchList",
    "sports/betsList",
    "sports/clientListByMarketId",
    "sports/sportByMarketId",
    "sports/getOddsPosition",
    "casino/getDiamondCasinoData",
    "casino/diamondBetsList",
    "casino/roundWiseResult",
    "casino/dayWiseCasinoReport",
    "reports/userProfitLoss",
    "decision/completeSportList",
    "matka/getMatkaList",
    "bluexchReports/clientPlusMinus",
]
