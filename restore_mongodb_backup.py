#!/usr/bin/env python3
"""saved_state/mongo_backup.json se ex99_local restore (sirf khali DB par)."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from bson import json_util
from pymongo import MongoClient

ROOT = Path(__file__).resolve().parent
BACKUP_JSON = Path(os.getenv("EX99_MONGO_BACKUP", ROOT / "saved_state" / "mongo_backup.json"))
MONGO_URI = os.getenv("EX99_MONGO_URI", "mongodb://127.0.0.1:27017")
DB_NAME = os.getenv("EX99_MONGO_DB", "ex99_local")


def _connect_mongo(retries: int = 8, delay_sec: float = 3.0) -> MongoClient:
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=8000)
            client.admin.command("ping")
            return client
        except Exception as exc:
            last = exc
            if attempt < retries:
                print(f"  MongoDB not ready ({attempt}/{retries}): {exc}")
                time.sleep(delay_sec)
    raise last or RuntimeError("MongoDB connection failed")


def main() -> int:
    if not BACKUP_JSON.exists():
        print(f"No backup found: {BACKUP_JSON}")
        return 1

    client = _connect_mongo()
    db = client[DB_NAME]

    payload = json_util.loads(BACKUP_JSON.read_text(encoding="utf-8"))
    collections = payload.get("collections") or {}
    total = 0
    for name, docs in collections.items():
        if not isinstance(docs, list):
            continue
        db[name].delete_many({})
        if docs:
            db[name].insert_many(docs)
        total += len(docs)
        print(f"  {name}: {len(docs)} docs")

    print(f"Restore OK — {total} documents in {DB_NAME}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
