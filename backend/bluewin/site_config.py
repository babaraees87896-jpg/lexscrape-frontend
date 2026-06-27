"""Site configuration — staffpanel.1ex99.live Operating Panel."""

from __future__ import annotations

from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_FRONTEND_STAFF = _PROJECT_ROOT / "frontend" / "staff"

BASE_URL = "https://staffpanel.1ex99.live"
API_BASE_URL = "https://api.bluewin.live/v1/"
STAFF_HOST = "staffpanel.1ex99.live"

DEFAULT_USERNAME = "OW1000"
DEFAULT_PASSWORD = "Bluewin@4923"

OUTPUT_ROOT = str(_FRONTEND_STAFF)
BROWSER_DATA_DIR = str(Path(__file__).resolve().parent / "browser_data")

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = { runtime: {} };
"""
