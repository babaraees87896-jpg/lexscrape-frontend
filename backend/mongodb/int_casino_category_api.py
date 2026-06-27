"""Staff Int. Casino → Casino Category — website/getCateogeory & cateogeoryCrud."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from bson import ObjectId

from mongodb.centerpanel_api import _err, _ok
from mongodb.db import get_db

_COLLECTION = "int_casino_categories"

_DEFAULT_MAIN: tuple[dict[str, Any], ...] = (
    {
        "cateogeoryName": "Virtual Casino",
        "cateogeoryType": "main",
        "cateogeoryImg": "/images/duskadum.jpg",
        "priority": 1,
        "remark": "Virtual casino table games",
        "status": 1,
    },
    {
        "cateogeoryName": "Crash Games",
        "cateogeoryType": "main",
        "cateogeoryImg": "/images/aviator.jpeg",
        "priority": 2,
        "remark": "International crash games",
        "status": 1,
    },
)

_DEFAULT_SUB: tuple[dict[str, Any], ...] = (
    {
        "cateogeoryName": "DUS KA DAM",
        "cateogeoryType": "sub",
        "cateogeoryImg": "/images/duskadum.jpg",
        "priority": 1,
        "remark": "DUS KA DAM",
        "status": 1,
        "parentName": "Virtual Casino",
    },
    {
        "cateogeoryName": "Aviator",
        "cateogeoryType": "sub",
        "cateogeoryImg": "/images/aviator.jpeg",
        "priority": 1,
        "remark": "Spribe Aviator",
        "status": 1,
        "parentName": "Crash Games",
    },
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _status_value(val: Any) -> int:
    if val is True or val == 1 or str(val).lower() in ("1", "true", "yes"):
        return 1
    return 0


def _normalize_category(doc: dict) -> dict:
    row = dict(doc or {})
    oid = row.pop("_id", None)
    if oid is not None:
        row["_id"] = str(oid)
    row["cateogeoryName"] = str(row.get("cateogeoryName") or row.get("name") or "")
    row["cateogeoryType"] = str(row.get("cateogeoryType") or "main")
    row["cateogeoryImg"] = str(row.get("cateogeoryImg") or row.get("image") or "")
    row["priority"] = int(row.get("priority") or 0)
    row["remark"] = str(row.get("remark") or "")
    row["status"] = _status_value(row.get("status", 1))
    parent = row.get("cateogeoryId")
    if parent is not None:
        row["cateogeoryId"] = str(parent)
    return row


def ensure_int_casino_categories() -> None:
    db = get_db()
    if db[_COLLECTION].count_documents({}) > 0:
        return
    now = _now()
    main_ids: dict[str, str] = {}
    for base in _DEFAULT_MAIN:
        doc = {**base, "createdAt": now, "updatedAt": now}
        res = db[_COLLECTION].insert_one(doc)
        main_ids[base["cateogeoryName"]] = str(res.inserted_id)
    for base in _DEFAULT_SUB:
        parent_name = base.pop("parentName", "")
        parent_id = main_ids.get(parent_name)
        doc = {
            **base,
            "cateogeoryId": parent_id,
            "createdAt": now,
            "updatedAt": now,
        }
        db[_COLLECTION].insert_one(doc)


def list_int_casino_categories(payload: dict) -> dict:
    ensure_int_casino_categories()
    payload = payload or {}
    db = get_db()

    cat_id = payload.get("_id")
    if cat_id:
        try:
            doc = db[_COLLECTION].find_one({"_id": ObjectId(str(cat_id))})
        except Exception:
            doc = None
        rows = [_normalize_category(doc)] if doc else []
        return _ok(rows, "Category fetched")

    q: dict[str, Any] = {}
    cat_type = payload.get("cateogeoryType")
    if cat_type:
        q["cateogeoryType"] = str(cat_type)

    parent_id = payload.get("cateogeoryId")
    if parent_id not in (None, ""):
        q["cateogeoryId"] = str(parent_id)

    status_param = payload.get("status")
    if status_param is not None and status_param is not True:
        if str(status_param).lower() in ("1", "true"):
            q["status"] = 1
        elif str(status_param).lower() in ("0", "false"):
            q["status"] = 0

    rows = [
        _normalize_category(doc)
        for doc in db[_COLLECTION].find(q).sort([("priority", 1), ("cateogeoryName", 1)])
    ]
    return _ok(rows, "Category list fetched")


def save_int_casino_category(payload: dict) -> dict:
    ensure_int_casino_categories()
    payload = payload or {}
    name = str(payload.get("cateogeoryName") or "").strip()
    if not name:
        return _err("Cateogeory Name Cannot Be Blank.", code=1)

    cat_type = str(payload.get("cateogeoryType") or "main").strip()
    if cat_type not in ("main", "sub"):
        return _err("Invalid category type", code=1)

    db = get_db()
    now = _now()
    upd: dict[str, Any] = {
        "cateogeoryName": name,
        "cateogeoryType": cat_type,
        "cateogeoryImg": str(payload.get("cateogeoryImg") or payload.get("image") or ""),
        "priority": int(payload.get("priority") or 0),
        "remark": str(payload.get("remark") or ""),
        "status": _status_value(payload.get("status", 1)),
        "updatedAt": now,
    }
    if cat_type == "sub":
        parent = payload.get("cateogeoryId")
        if not parent:
            return _err("Main Cateogeory Cannot Be Blank.", code=1)
        upd["cateogeoryId"] = str(parent)
    else:
        upd["cateogeoryId"] = None

    existing = None
    cat_oid: Optional[ObjectId] = None
    if payload.get("_id"):
        try:
            cat_oid = ObjectId(str(payload["_id"]))
            existing = db[_COLLECTION].find_one({"_id": cat_oid})
        except Exception:
            existing = None
    if existing is None:
        existing = db[_COLLECTION].find_one({"cateogeoryName": name})

    if existing:
        db[_COLLECTION].update_one({"_id": existing["_id"]}, {"$set": upd})
        row = _normalize_category({**existing, **upd, "_id": existing["_id"]})
        return _ok(row, "Category updated successfully")

    upd["createdAt"] = now
    res = db[_COLLECTION].insert_one(upd)
    row = _normalize_category({**upd, "_id": res.inserted_id})
    return _ok(row, "Category created successfully")


def save_int_casino_category_image(filename: str, content: bytes) -> dict:
    import os
    import re
    import time
    from pathlib import Path

    site_dir = Path(
        os.getenv(
            "BLUEWIN_SITE_DIR",
            Path(__file__).resolve().parents[2] / "frontend" / "staff" / "site",
        )
    ).resolve()
    upload_dir = site_dir / "images" / "int-category-uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)

    safe = re.sub(r"[^\w.\-]", "_", Path(filename or "upload.jpg").name) or "upload.jpg"
    if "." not in safe:
        safe = f"{safe}.jpg"
    dest = upload_dir / f"{int(time.time())}_{safe}"
    dest.write_bytes(content or b"")
    url = f"/images/int-category-uploads/{dest.name}"
    return _ok({"imageName": url, "imageUrl": url}, "File uploaded successfully")
