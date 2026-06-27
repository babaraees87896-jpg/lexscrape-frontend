#!/usr/bin/env python3
"""
Parse a browser HAR export to find vcasino API + decrypt hints.

Usage (after exporting HAR from DevTools while on /casino/vtrio):
  python extract_from_har.py ~/Downloads/allpanelexch9.co.har
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

PATTERNS = [
    (r"AES\.decrypt\s*\(", "AES.decrypt call"),
    (r"CryptoJS\.AES\.decrypt\s*\(", "CryptoJS decrypt"),
    (r"vcasino/data2", "vcasino data2 API"),
    (r"U2FsdGVk", "encrypted payload"),
    (r'data2\?gtype=', "data2 query"),
]


def load_har(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8", errors="replace"))


def iter_entries(har: dict):
    for entry in har.get("log", {}).get("entries", []):
        yield entry


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python extract_from_har.py <file.har>")
        sys.exit(1)

    path = Path(sys.argv[1])
    har = load_har(path)
    js_urls: list[str] = []
    api_responses: list[str] = []

    print(f"HAR: {path}\n")

    for entry in iter_entries(har):
        req = entry.get("request", {})
        resp = entry.get("response", {})
        url = req.get("url", "")
        if url.endswith(".js") or ".js?" in url:
            js_urls.append(url)
        if "vcasino/data2" in url:
            content = resp.get("content", {}).get("text", "")
            api_responses.append((url, content[:500]))

    print("=== JS files in HAR ===")
    for u in sorted(set(js_urls))[:40]:
        print(u)
    if len(set(js_urls)) > 40:
        print(f"... and {len(set(js_urls)) - 40} more")

    print("\n=== vcasino/data2 responses ===")
    for url, snippet in api_responses:
        print(url)
        print(snippet[:300], "\n")

    print("=== Search downloaded JS in HAR (response bodies) ===")
    found = 0
    for entry in iter_entries(har):
        url = entry.get("request", {}).get("url", "")
        if not (url.endswith(".js") or ".js?" in url):
            continue
        text = entry.get("response", {}).get("content", {}).get("text", "")
        if not text or len(text) < 100:
            continue
        for pat, label in PATTERNS:
            if re.search(pat, text):
                print(f"\n[{label}] in {url}")
                for m in re.finditer(pat, text):
                    start = max(0, m.start() - 80)
                    end = min(len(text), m.end() + 120)
                    print(" ", text[start:end].replace("\n", " ")[:200])
                found += 1
                break

    if not found:
        print("No AES.decrypt / vcasino hints in JS bodies.")
        print("Save HAR with 'Save all as HAR with content' checked in DevTools.")

    # Save encrypted API responses
    out = Path("har_api_responses")
    out.mkdir(exist_ok=True)
    for i, (url, content) in enumerate(api_responses):
        if "U2FsdGVk" in content or '"data"' in content:
            fp = out / f"response_{i}.json"
            fp.write_text(content, encoding="utf-8")
            print(f"\nSaved {fp}")


if __name__ == "__main__":
    main()
