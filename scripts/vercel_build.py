#!/usr/bin/env python3
"""Patch scraped frontends and copy to vercel-out/ for Vercel static deploy."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
OUT_ROOT = ROOT / "vercel-out"

# Hosts for runtime patches (center/staff login payloads)
os.environ.setdefault("EX99_HOST", "1ex.in")
os.environ.setdefault("EX99_ADMIN_HOST", "admin.1ex.in")
os.environ.setdefault("EX99_CENTERPANEL_HOST", "center.1ex.in")
os.environ.setdefault("STAFF_HOST", "staff.1ex.in")
os.environ.setdefault("BLUEWIN_PUBLIC_HOST", "staff.1ex.in")

sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(BACKEND / "bluewin"))


def _copy_tree(src: Path, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _patch_file(path: Path, patch_fn) -> None:
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8")
        patched = patch_fn(text)
        path.write_text(patched, encoding="utf-8")
    except Exception as exc:
        print(f"  WARN patch {path.name}: {exc}")


def _walk_patch(out_dir: Path, matcher) -> int:
    count = 0
    for path in out_dir.rglob("*"):
        if not path.is_file():
            continue
        fn = matcher(path)
        if fn is None:
            continue
        _patch_file(path, fn)
        count += 1
    return count


def build_main(out_dir: Path) -> None:
    from serve_local import (
        patch_aviator_js,
        patch_casino_js,
        patch_change_password_js,
        patch_client_statement_js,
        patch_html_content,
        patch_index_js,
        patch_inplay_js,
        patch_js_content,
        patch_login_js,
        patch_match_detail_js,
        patch_virtual_casino_js,
    )

    src = ROOT / "frontend" / "main"
    _copy_tree(src, out_dir)

    def matcher(path: Path):
        if path.suffix == ".html":
            return patch_html_content
        if path.suffix != ".js":
            return None
        name = path.name
        if name.startswith("index-"):
            return patch_index_js
        if name == "Login-CNc67dgC.js":
            return patch_login_js
        if name.startswith("ChangePassword-"):
            return patch_change_password_js
        if name == "MatchDetail-DcLvOyoM.js":
            return patch_match_detail_js
        if name == "Inplay-wyzuWlIy.js":
            return patch_inplay_js
        if name == "ClientStatement-DCCbn1pb.js":
            return patch_client_statement_js
        if name == "AviatorGames-CHyQ2Vox.js":
            return patch_aviator_js
        if name == "VirtualCasino-DN1vVk0T.js":
            return patch_virtual_casino_js
        if path.parent.name == "assets":
            return lambda c: patch_casino_js(patch_js_content(c))
        return patch_js_content

    n = _walk_patch(out_dir, matcher)
    print(f"  patched {n} files")


def build_admin(out_dir: Path) -> None:
    from serve_admin import (
        admin_chunk_stub_js,
        patch_admin_js,
        patch_admin_login_js,
        patch_html_content,
    )

    src = ROOT / "frontend" / "admin"
    _copy_tree(src, out_dir)

    chunk_re = re.compile(r"^(\d+)\.[a-f0-9]+\.chunk\.js$")

    def matcher(path: Path):
        if path.suffix == ".html":
            return patch_html_content
        if path.suffix != ".js":
            return None

        def patch_js(content: str) -> str:
            if content.lstrip()[:120].lower().startswith("<!doctype") or content.lstrip()[:6].lower().startswith("<html"):
                m = chunk_re.match(path.name)
                return admin_chunk_stub_js(m.group(1) if m else "0").decode("utf-8")
            js = patch_admin_js(content)
            if "hold-transition login-page" in js or "loginCardWrapper" in js:
                js = patch_admin_login_js(js)
            return js

        return patch_js

    n = _walk_patch(out_dir, matcher)
    print(f"  patched {n} files")


def build_center(out_dir: Path) -> None:
    from serve_centerpanel import patch_centerpanel_js, patch_html_content

    src = ROOT / "frontend" / "centerpanel"
    _copy_tree(src, out_dir)

    def matcher(path: Path):
        if path.suffix == ".html":
            return patch_html_content
        if path.suffix == ".js":
            return patch_centerpanel_js
        return None

    n = _walk_patch(out_dir, matcher)
    print(f"  patched {n} files")


def build_staff(out_dir: Path) -> None:
    from serve_bluewin import CLEAN_INDEX, patch_bluewin_js

    site_src = ROOT / "frontend" / "staff" / "site"
    assets_src = ROOT / "frontend" / "staff" / "assets"
    auto_page = BACKEND / "bluewin" / "auto_decision_settings.html"

    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    # Merge assets into site (same as serve_bluewin.prepare_site)
    if site_src.is_dir():
        shutil.copytree(site_src, out_dir, dirs_exist_ok=True)
    _write_text(out_dir / "index.html", CLEAN_INDEX)
    if auto_page.is_file():
        shutil.copy2(auto_page, out_dir / "auto-decision-settings.html")
    if assets_src.is_dir():
        for item in assets_src.iterdir():
            dest = out_dir / item.name
            if item.is_dir():
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(item, dest)
            else:
                shutil.copy2(item, dest)

    def matcher(path: Path):
        if path.suffix != ".js":
            return None
        if path.name.startswith("main.") and "static" in path.parts:
            return patch_bluewin_js
        return None

    n = _walk_patch(out_dir, matcher)
    print(f"  patched {n} files")


BUILDERS = {
    "main": build_main,
    "admin": build_admin,
    "center": build_center,
    "staff": build_staff,
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Build patched frontend for Vercel")
    parser.add_argument("target", choices=["main", "admin", "center", "centerpanel", "staff"])
    args = parser.parse_args()

    target = "center" if args.target == "centerpanel" else args.target
    out_name = "centerpanel" if target == "center" else target
    out_dir = OUT_ROOT / out_name
    print(f"Building {target} -> {out_dir}")

    BUILDERS[target](out_dir)

    print(f"Done: {out_dir}")
    print("  Rewrites: frontend/*/vercel.json (api.*.1ex.in -> backend DNS set karo)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
