#!/usr/bin/env python3
"""
Local server for scraped centerpanel.1ex99.in panel.

MongoDB mode: login + saari APIs apne database se (demo mode band).
"""

import json
import mimetypes
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

from config import CENTERPANEL_HOST, CENTERPANEL_OUTPUT_DIR
from mongodb.centerpanel_api import handle_centerpanel_api
from mongodb.centerpanel_cache import proxy_odds_json
from mongodb.db import ping

PORT = int(os.getenv("EX99_CENTERPANEL_PORT", "8891"))
SITE_DIR = Path(os.getenv("EX99_CENTERPANEL_OUTPUT_DIR", CENTERPANEL_OUTPUT_DIR)).resolve()
JS_PATCH_FROM = "https://cache.1ex99.in/"
JS_PATCH_TO = "/"

STATIC_PREFIXES = ("/static/", "/favicon", "/logo", "/manifest", "/asset")


def patch_centerpanel_js(content: str) -> str:
    content = content.replace(JS_PATCH_FROM, JS_PATCH_TO)
    content = content.replace("https://cache.1ex99.in/", JS_PATCH_TO)
    content = content.replace(
        "3===(null===(t=e.data)||void 0===t?void 0:t.code)?(rs(),Promise.reject",
        "3===(null===(t=e.data)||void 0===t?void 0:t.code)||401===(null===(t=e.data)||void 0===t?void 0:t.code)?(window.__ex99Kicked||(window.__ex99Kicked=1,rs()),Promise.reject",
    )
    content = content.replace(
        "401===a||403===a||3===r)return rs(),Promise.reject(e)",
        "401===a||403===a||3===r||401===r)return window.__ex99Kicked||(window.__ex99Kicked=1,rs()),Promise.reject(e)",
    )
    content = content.replace(
        "if(400===e)return sessionStorage.clear(),localStorage.removeItem(\"user\"",
        "if(false&&400===e)return sessionStorage.clear(),localStorage.removeItem(\"user\"",
    )
    content = content.replace(
        "host:window.location.host",
        f'host:"{CENTERPANEL_HOST}"',
    )
    content = re.sub(
        r"(\w+)\.response\.data\.message",
        r"(\1.response&&\1.response.data&&\1.response.data.message||\1.message||'Something went wrong')",
        content,
    )
    # Galat login par reload mat karo — sirf valid token pe
    content = content.replace(
        "try{const t=await cs.login(e);return a(vs({})),window.location.reload(),t}catch(n)",
        "try{const t=await cs.login(e);if(!t||t.error||!t.token){const m=(null===t||void 0===t?void 0:t.message)||'Invalid username or password';return Ia.error(m),r(m)}return a(vs({})),window.location.reload(),t}catch(n)",
    )
    content = content.replace(
        'p&&t("/app/dashboard")',
        'localStorage.getItem("token")&&p&&!(null!==p&&void 0!==p&&p.error)&&t("/app/dashboard")',
    )
    return content


def patch_html_content(content: str) -> str:
    content = re.sub(
        r'<script[^>]*cloudflareinsights[^>]*>.*?</script>',
        "",
        content,
        flags=re.DOTALL,
    )
    return content


class CenterPanelSiteHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[centerpanel {self.log_date_time_string()}] {fmt % args}")

    def _send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def do_OPTIONS(self):
        self.send_response(204)
        self._send_cors_headers()
        self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path
        if self._is_static_path(path):
            self.send_error(404, "Not Found")
            return
        if path.startswith("/v1/") or path.startswith("centerPanel/") or path.startswith(("sports/", "decision/", "user/", "manualOdds/")):
            self._handle_api(path, "POST")
            return
        self._handle_api(path, "POST")

    def do_GET(self):
        path = urlparse(self.path).path
        if path.startswith("/v2/") or path.startswith("/excache/"):
            self._serve_odds_proxy()
            return
        if path.startswith("centerPanel/") or path in ("logout",) or path.startswith(("sports/", "decision/", "user/")):
            self._handle_api(path, "GET")
            return
        file_path = self._resolve_file(path)
        if file_path is None or not file_path.exists():
            file_path = SITE_DIR / "index.html"
        self._serve_file(file_path)

    def _serve_odds_proxy(self):
        query = urlparse(self.path).query
        path = urlparse(self.path).path
        body = proxy_odds_json(path, query)
        if body is None:
            self.send_error(400, "market_id or eventId required")
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _is_static_path(self, path: str) -> bool:
        if path in ("/", "/index.html"):
            return True
        return any(path.startswith(p) for p in STATIC_PREFIXES)

    def _resolve_file(self, path: str) -> Optional[Path]:
        rel = path.lstrip("/")
        if not rel:
            return SITE_DIR / "index.html"
        return SITE_DIR / rel

    def _serve_file(self, file_path: Path):
        try:
            content = file_path.read_bytes()
            content_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"

            if file_path.name == "index.html":
                content = patch_html_content(content.decode("utf-8")).encode("utf-8")
                content_type = "text/html; charset=utf-8"

            if file_path.suffix == ".js":
                content = patch_centerpanel_js(content.decode("utf-8")).encode("utf-8")
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

    def _handle_api(self, path: str, method: str):
        endpoint = path.lstrip("/")
        length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw_body.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            payload = {}

        if method == "GET":
            qs = parse_qs(urlparse(self.path).query)
            for k, v in qs.items():
                payload.setdefault(k, v[0] if len(v) == 1 else v)

        auth = self.headers.get("Authorization", "")
        print(f"[centerpanel mongo] {endpoint}")
        body = handle_centerpanel_api(endpoint, payload, auth)

        try:
            resp = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            resp = {}

        status = 200
        if resp.get("error"):
            ep = endpoint.rstrip("/")
            code = resp.get("code")
            msg = str(resp.get("message", "")).lower()
            if ep.endswith("userLogin") or code in (401, 403) or "session" in msg or "authoris" in msg:
                status = int(code) if isinstance(code, int) and 400 <= code < 600 else 401

        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(body)


def main():
    if not SITE_DIR.exists():
        print(f"Error: '{SITE_DIR}' not found. Pehle: python main.py --center-panel")
        sys.exit(1)

    if not ping():
        print("Error: MongoDB not running.")
        print("  brew services start mongodb-community")
        print("  python3 main.py --setup-mongo")
        sys.exit(1)

    server = ThreadingHTTPServer(("0.0.0.0", PORT), CenterPanelSiteHandler)
    print("=" * 50)
    print("centerpanel.1ex99.in - MongoDB Local Server")
    print("=" * 50)
    print(f"Folder      : {SITE_DIR}")
    print(f"Local URL   : http://localhost:{PORT}")
    print(f"API patch   : {JS_PATCH_FROM} -> {JS_PATCH_TO}")
    print(f"Mode        : MongoDB (demo mode OFF)")
    print(f"Database    : ex99_local")
    print(f"Login       : ADMIN001 / admin@123  (owner/admin)")
    print("=" * 50)
    print("Ctrl+C se band karo")
    print("=" * 50)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nCenter panel server stopped.")
        server.shutdown()


if __name__ == "__main__":
    main()
