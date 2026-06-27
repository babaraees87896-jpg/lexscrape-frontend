#!/usr/bin/env python3
"""Static file server with whitelist API and access-key enforcement."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


CATEGORY_PATHS: dict[str, tuple[str, ...]] = {
    "Casino": ("/all_games.json",),
    "Sports": ("/sport/all_sports.json",),
    "Matches": ("/sport/all_sports.json", "/sport/matches/"),
    "Score Card": ("/sport/all_sports.json", "/sport/scorecard/", "/sport/matches/"),
}

LOCAL_IPS = {"127.0.0.1", "::1", "0:0:0:0:0:0:0:1"}


def normalize_host(value: str) -> str:
    value = (value or "").strip().lower()
    if value.startswith("http://"):
        value = value[7:]
    elif value.startswith("https://"):
        value = value[8:]
    return value.split("/")[0].rstrip("/")


def client_ip_from_headers(headers, fallback: str) -> str:
    forwarded = headers.get("X-Forwarded-For") or headers.get("X-Real-IP") or ""
    if forwarded:
        return forwarded.split(",")[0].strip()
    return fallback


def ip_matches(entry_ip: str, request_ip: str) -> bool:
    entry_ip = (entry_ip or "").strip()
    request_ip = (request_ip or "").strip()
    if not entry_ip:
        return True
    if entry_ip == request_ip:
        return True
    if entry_ip in LOCAL_IPS and request_ip in LOCAL_IPS:
        return True
    return False


def domain_matches(entry_domain: str, referer: str, origin: str, param_domain: str) -> bool:
    entry = normalize_host(entry_domain)
    if not entry:
        return True
    candidates = [normalize_host(referer), normalize_host(origin), normalize_host(param_domain)]
    for candidate in candidates:
        if not candidate:
            continue
        if candidate == entry:
            return True
        if candidate.endswith("." + entry):
            return True
    return False


def category_for_path(path: str) -> str | None:
    if path == "/all_games.json" or (path.endswith("_data.json") and path.startswith("/")):
        return "Casino"
    if path.startswith("/sport/matches/"):
        return "Matches"
    if path.startswith("/sport/scorecard/"):
        return "Score Card"
    if path.startswith("/sport/") and path.endswith(".json"):
        return "Sports"
    return None


def path_allowed_for_category(path: str, category: str) -> bool:
    if category == "Casino" and path.endswith("_data.json"):
        return True
    if category == "Sports":
        if path.startswith("/sport/matches/") or path.startswith("/sport/scorecard/"):
            return False
        if path.startswith("/sport/") and path.endswith(".json"):
            return True
    if category == "Matches":
        if path == "/sport/all_sports.json":
            return True
        if path.startswith("/sport/matches/"):
            return True
        if path.startswith("/sport/") and path.endswith(".json") and not path.startswith("/sport/scorecard/"):
            return True
    if category == "Score Card":
        for prefix in CATEGORY_PATHS.get("Score Card", ()):
            if path == prefix or path.startswith(prefix):
                return True
        return False
    prefixes = CATEGORY_PATHS.get(category, ())
    for prefix in prefixes:
        if path == prefix or path.startswith(prefix):
            return True
    return False


class NotFoundHandler(SimpleHTTPRequestHandler):
    whitelist_path: Path
    enforce_ip: bool = True
    admin_user: str = "admin"
    admin_pass: str = "admin123"
    admin_sessions: dict[str, dict] = {}

    def send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-DG-Key, X-DG-Auth")
        self.end_headers()
        self.wfile.write(body)

    def read_body_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def load_whitelist(self) -> list[dict]:
        if not self.whitelist_path.is_file():
            return []
        try:
            data = json.loads(self.whitelist_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        entries = data.get("entries", data if isinstance(data, list) else [])
        return entries if isinstance(entries, list) else []

    def save_whitelist(self, entries: list[dict]) -> None:
        self.whitelist_path.parent.mkdir(parents=True, exist_ok=True)
        self.whitelist_path.write_text(
            json.dumps({"entries": entries}, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def find_entry_by_key(self, key: str) -> dict | None:
        key = (key or "").strip()
        if not key:
            return None
        for entry in self.load_whitelist():
            if entry.get("key") == key and entry.get("status", "Whitelisted") == "Whitelisted":
                return entry
        return None

    def extract_key(self, query: dict[str, list[str]]) -> str:
        header_key = (self.headers.get("X-DG-Key") or "").strip()
        if header_key:
            return header_key
        return (query.get("key", [""])[0] or "").strip()

    def verify_entry(
        self,
        *,
        key: str,
        domain: str = "",
        ip: str = "",
        category: str = "",
        check_ip: bool = False,
        check_domain: bool = False,
    ) -> tuple[bool, dict | None, str]:
        entry = self.find_entry_by_key(key)
        if not entry:
            return False, None, "Invalid or missing access key"

        entry_domain = entry.get("url") or entry.get("domain") or ""
        entry_ip = entry.get("ip") or ""
        entry_category = entry.get("category") or ""

        if domain and normalize_host(domain) != normalize_host(entry_domain):
            return False, entry, "Domain does not match this access key"

        if ip and entry_ip and not ip_matches(entry_ip, ip):
            return False, entry, "IP does not match this access key"

        if category and entry_category and category != entry_category:
            return False, entry, f"This key is for {entry_category}, not {category}"

        if check_ip and entry_ip:
            request_ip = client_ip_from_headers(self.headers, self.client_address[0])
            if self.enforce_ip and not ip_matches(entry_ip, request_ip):
                return False, entry, f"Request IP {request_ip} is not whitelisted ({entry_ip})"

        if check_domain and entry_domain:
            referer = self.headers.get("Referer") or ""
            origin = self.headers.get("Origin") or ""
            if not domain_matches(entry_domain, referer, origin, domain):
                return False, entry, f"Domain must match whitelisted host ({entry_domain})"

        return True, entry, "OK"

    def extract_auth_token(self) -> str:
        return (self.headers.get("X-DG-Auth") or "").strip()

    def create_admin_session(self, username: str) -> str:
        token = secrets.token_hex(32)
        self.admin_sessions[token] = {
            "user": username,
            "expires": time.time() + 86400 * 7,
        }
        return token

    def valid_admin_session(self, token: str) -> dict | None:
        if not token:
            return None
        session = self.admin_sessions.get(token)
        if not session:
            return None
        if time.time() > session["expires"]:
            self.admin_sessions.pop(token, None)
            return None
        return session

    def revoke_admin_session(self, token: str) -> None:
        self.admin_sessions.pop(token, None)

    def is_admin_request(self) -> bool:
        return self.valid_admin_session(self.extract_auth_token()) is not None

    def verify_json_access(self, path: str, query: dict[str, list[str]]) -> tuple[bool, dict | None, str]:
        if self.is_admin_request():
            return True, {"category": category_for_path(path)}, "OK"

        key = self.extract_key(query)
        category = category_for_path(path) or ""
        domain = (query.get("domain", [""])[0] or "").strip()
        ip_param = (query.get("ip", [""])[0] or "").strip()

        ok, entry, message = self.verify_entry(
            key=key,
            domain=domain,
            ip=ip_param,
            category=category,
            check_ip=False,
            check_domain=False,
        )
        if not ok or not entry:
            return False, entry, message

        if not path_allowed_for_category(path, entry.get("category") or category):
            return False, entry, "This key cannot access this resource"

        # Share link opened in browser: domain+ip already verified from URL params
        if domain and ip_param:
            return True, entry, "OK"

        # Direct API call (key only): must come from whitelisted server IP
        ok, entry, message = self.verify_entry(
            key=key,
            category=category,
            check_ip=True,
            check_domain=False,
        )
        return ok, entry, message

    def handle_api(self, path: str, query: dict[str, list[str]]) -> bool:
        if path == "/api/login" and self.command == "POST":
            try:
                body = self.read_body_json()
                username = (body.get("username") or "").strip()
                password = body.get("password") or ""
                if username == self.admin_user and password == self.admin_pass:
                    token = self.create_admin_session(username)
                    self.send_json({"ok": True, "token": token, "user": username})
                else:
                    self.send_json({"ok": False, "error": "Invalid username or password"}, status=401)
            except json.JSONDecodeError:
                self.send_json({"ok": False, "error": "Invalid JSON body"}, status=400)
            return True

        if path == "/api/session" and self.command == "GET":
            session = self.valid_admin_session(self.extract_auth_token())
            if session:
                self.send_json({"ok": True, "user": session["user"]})
            else:
                self.send_json({"ok": False, "error": "Not logged in"}, status=401)
            return True

        if path == "/api/logout" and self.command == "POST":
            self.revoke_admin_session(self.extract_auth_token())
            self.send_json({"ok": True})
            return True

        if path == "/api/whitelist" and self.command == "GET":
            if not self.is_admin_request():
                self.send_json({"ok": False, "error": "Admin login required"}, status=401)
                return True
            self.send_json({"ok": True, "entries": self.load_whitelist()})
            return True

        if path == "/api/whitelist" and self.command in {"POST", "PUT"}:
            if not self.is_admin_request():
                self.send_json({"ok": False, "error": "Admin login required"}, status=401)
                return True
            try:
                body = self.read_body_json()
                entries = body.get("entries", [])
                if not isinstance(entries, list):
                    raise ValueError("entries must be a list")
                self.save_whitelist(entries)
                self.send_json({"ok": True, "count": len(entries)})
            except (ValueError, json.JSONDecodeError) as exc:
                self.send_json({"ok": False, "error": str(exc)}, status=400)
            return True

        if path == "/api/verify" and self.command == "GET":
            key = self.extract_key(query)
            domain = (query.get("domain", [""])[0] or "").strip()
            ip = (query.get("ip", [""])[0] or "").strip()
            category = (query.get("category", [""])[0] or "").strip()
            ok, entry, message = self.verify_entry(
                key=key,
                domain=domain,
                ip=ip,
                category=category,
                check_ip=False,
                check_domain=False,
            )
            if ok and entry:
                self.send_json(
                    {
                        "ok": True,
                        "entry": {
                            "url": entry.get("url"),
                            "ip": entry.get("ip"),
                            "category": entry.get("category"),
                            "key": entry.get("key"),
                        },
                    }
                )
            else:
                self.send_json({"ok": False, "error": message}, status=403)
            return True

        if path == "/api/access-example" and self.command == "GET":
            key = self.extract_key(query)
            ok, entry, message = self.verify_entry(key=key, check_ip=True, check_domain=False)
            if not ok or not entry:
                self.send_json({"ok": False, "error": message}, status=403)
                return True
            category = entry.get("category") or "Casino"
            sample_path = CATEGORY_PATHS.get(category, ("/all_games.json",))[0]
            self.send_json(
                {
                    "ok": True,
                    "example": f"{sample_path}?key={entry.get('key')}",
                    "header": "X-DG-Key: " + str(entry.get("key")),
                    "ip": entry.get("ip"),
                    "domain": entry.get("url"),
                    "category": category,
                }
            )
            return True

        return False

    def is_protected_json(self, path: str) -> bool:
        if not path.endswith(".json"):
            return False
        if path == "/whitelist.json":
            return False
        return category_for_path(path) is not None

    def is_blocked_path(self, path: str) -> bool:
        return path in {"/whitelist.json"}

    def wants_json_response(self, query: dict[str, list[str]]) -> bool:
        if (query.get("format", [""])[0] or "").lower() == "json":
            return True
        if self.headers.get("X-DG-Key") or self.headers.get("X-DG-Auth"):
            return True
        sec_dest = (self.headers.get("Sec-Fetch-Dest") or "").lower()
        sec_mode = (self.headers.get("Sec-Fetch-Mode") or "").lower()
        if sec_dest == "document" or sec_mode == "navigate":
            return False
        if sec_dest == "empty" and sec_mode in {"cors", "same-origin", "no-cors"}:
            return True
        accept = (self.headers.get("Accept") or "").lower()
        if accept.startswith("application/json") and "text/html" not in accept:
            return True
        return False

    def send_access_denied_page(self, message: str, status: int = 403) -> None:
        page = Path(self.directory) / "403.html"
        if page.is_file():
            html = page.read_text(encoding="utf-8").replace(
                "{{ERROR_MESSAGE}}",
                message or "Invalid or missing access key",
            )
            body = html.encode("utf-8")
        else:
            body = f"<h1>403 Access Denied</h1><p>{message}</p>".encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def deny_access(self, message: str, status: int = 403, query: dict[str, list[str]] | None = None) -> None:
        if self.wants_json_response(query or {}):
            self.send_json({"ok": False, "error": message, "protected": True}, status=status)
        else:
            self.send_access_denied_page(message, status=status)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-DG-Key, X-DG-Auth")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if self.is_blocked_path(path):
            self.deny_access("Forbidden", status=403, query=query)
            return

        if path.startswith("/api/") and self.handle_api(path, query):
            return

        if self.is_protected_json(path):
            ok, entry, message = self.verify_json_access(path, query)
            if not ok or not entry:
                self.deny_access(message, status=403, query=query)
                return

        return super().do_GET()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/") and self.handle_api(parsed.path, parse_qs(parsed.query)):
            return
        self.send_error(405, "Method Not Allowed")

    def do_PUT(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/") and self.handle_api(parsed.path, parse_qs(parsed.query)):
            return
        self.send_error(405, "Method Not Allowed")

    def send_error(self, code, message=None, explain=None):
        if code == 404:
            self.send_response(404)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.end_headers()
            page = Path(self.directory) / "404.html"
            if page.is_file():
                self.wfile.write(page.read_bytes())
            else:
                self.wfile.write(b"<h1>404 Not Found</h1>")
            return
        super().send_error(code, message, explain)

    def log_message(self, format, *args):
        if args and "404" in str(args):
            return
        super().log_message(format, *args)


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve output/ with whitelist access control")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    parser.add_argument(
        "--directory",
        default=os.environ.get("SERVE_DIR", "output"),
        help="Folder to serve (default: output)",
    )
    args = parser.parse_args()

    root = Path(args.directory).resolve()
    if not root.is_dir():
        raise SystemExit(f"Directory not found: {root}")

    whitelist_path = root / "whitelist.json"
    enforce_ip = os.environ.get("DG_ENFORCE_IP", "1") != "0"

    NotFoundHandler.whitelist_path = whitelist_path
    NotFoundHandler.enforce_ip = enforce_ip
    NotFoundHandler.admin_user = os.environ.get("DG_ADMIN_USER", "admin")
    NotFoundHandler.admin_pass = os.environ.get("DG_ADMIN_PASS", "admin123")
    handler = partial(NotFoundHandler, directory=str(root))

    server = ThreadingHTTPServer(("", args.port), handler)
    print(f"Serving {root} on http://localhost:{args.port}/", flush=True)
    print(f"Admin login: {NotFoundHandler.admin_user} (set DG_ADMIN_USER / DG_ADMIN_PASS)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.", flush=True)


if __name__ == "__main__":
    main()
