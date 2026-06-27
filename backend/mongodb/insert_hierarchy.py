#!/usr/bin/env python3
"""Poori user hierarchy insert — owner(9) se client(1) tak, same users table."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mongodb.db import get_db, ping
from mongodb.import_scraped_data import build_local_hierarchy_users


def insert_hierarchy():
    if not ping():
        print("MongoDB connect nahi hua.")
        sys.exit(1)

    db = get_db()
    users = build_local_hierarchy_users()
    for u in users:
        db.users.update_one({"userId": u["userId"]}, {"$set": u}, upsert=True)

    print("✓ Poori hierarchy insert ho gayi (users table):\n")
    print("  9 owner       → OWNER001")
    print("  8 subowner    → SUBOWNER001")
    print("  7 superadmin  → SUPERADMIN001")
    print("  6 admin       → ADMIN001      (login: admin@123)")
    print("  5 subadmin    → SUBADMIN001")
    print("  4 master      → MASTER001")
    print("  3 superagent  → SUPERAGENT001  (Super Master)")
    print("  2 agent       → AGENT001")
    print("  1 client      → CLIENT001")
    print(f"\n  Total hierarchy users: {len(users)}")
    print("\n  Chain verify:")
    for u in sorted(users, key=lambda x: -x["userPriority"]):
        print(f"    [{u['userPriority']}] {u['userType']:10} {u['username']:14} parent={u.get('parentId') or '-'}")


if __name__ == "__main__":
    insert_hierarchy()
