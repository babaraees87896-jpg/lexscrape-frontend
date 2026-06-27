#!/usr/bin/env python3
"""
Local MongoDB setup for 1ex99.in API stack.

  docker compose up -d mongodb
  python mongodb/setup_local.py

Creates database `ex99_local`, collections, indexes, seed data,
and writes mongodb/api_table.json (API -> collection mapping).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mongodb.api_registry import write_api_table
from mongodb.admin_api_registry import write_admin_api_structure
from mongodb.db import MONGO_DB_NAME, MONGO_URI, get_client, ping
from mongodb.seed_loader import build_all_seed_data

SCHEMA_PATH = Path(__file__).parent / "collections_schema.json"
SEED_DIR = Path(__file__).parent / "seed"
TABLES_DIR = Path(__file__).parent / "tables"


def export_table_files(schema: dict, seed_data: dict, api_table: dict):
    """Har collection ke liye alag table definition file."""
    TABLES_DIR.mkdir(parents=True, exist_ok=True)
    apis_by_collection: dict[str, list] = {}
    for api in api_table.get("apis", []):
        apis_by_collection.setdefault(api["collection"], []).append(api["endpoint"])

    for coll_name, meta in schema.get("collections", {}).items():
        docs = seed_data.get(coll_name, [])
        table_def = {
            "collection": coll_name,
            "database": schema.get("database", MONGO_DB_NAME),
            "description": meta.get("description", ""),
            "fields": meta.get("fields", {}),
            "indexes": meta.get("indexes", []),
            "apis": sorted(apis_by_collection.get(coll_name, [])),
            "document_count": len(docs),
            "sample_document": docs[0] if docs else {},
        }
        out = TABLES_DIR / f"{coll_name}.json"
        out.write_text(json.dumps(table_def, indent=2, default=str), encoding="utf-8")


def ensure_indexes(db, schema: dict):
    for coll_name, meta in schema.get("collections", {}).items():
        coll = db[coll_name]
        for idx in meta.get("indexes", []):
            keys = idx["keys"]
            opts = {k: v for k, v in idx.items() if k != "keys"}
            try:
                coll.create_index(list(keys.items()), **opts)
            except Exception as exc:
                print(f"  index warn {coll_name}: {exc}")


def seed_collections(db, seed_data: dict):
    for coll_name, docs in seed_data.items():
        coll = db[coll_name]
        coll.delete_many({})
        if docs:
            coll.insert_many(docs)
        print(f"  ✓ {coll_name}: {len(docs)} documents")


def export_seed_json(seed_data: dict):
    SEED_DIR.mkdir(parents=True, exist_ok=True)
    for name, docs in seed_data.items():
        path = SEED_DIR / f"{name}.json"
        path.write_text(json.dumps(docs, indent=2, default=str), encoding="utf-8")


def main():
    print("=" * 50)
    print("1ex99 Local MongoDB Setup")
    print("=" * 50)
    print(f"URI      : {MONGO_URI}")
    print(f"Database : {MONGO_DB_NAME}")
    print()

    if not ping():
        print("MongoDB connect nahi hua. Pehle chalao:")
        print("  docker compose up -d mongodb")
        print("  ya local mongod start karo")
        sys.exit(1)

    print("[1] API table file bana rahe hain...")
    table = write_api_table()
    print(f"    mongodb/api_table.json — {table['total_apis']} APIs")
    print(f"    Collections: {', '.join(table['collections_used'])}")

    print("\n[1b] Admin panel API structure...")
    admin_struct = write_admin_api_structure()
    print(f"    mongodb/admin_api_structure.json — {admin_struct['total_endpoints']} admin APIs")
    print(f"    mongodb/tables/admin/ — {len(admin_struct['mongodb_collections'])} table files")

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    seed_data = build_all_seed_data()

    print("\n[2] Table definition files (mongodb/tables/*.json)...")
    export_table_files(schema, seed_data, table)

    print("\n[3] Seed JSON files export...")
    export_seed_json(seed_data)

    db = get_client()[MONGO_DB_NAME]
    print("\n[4] Collections + indexes...")
    for coll_name in schema.get("collections", {}):
        db[coll_name]
    # seed_data me extra collections ho to bhi banao
    for coll_name in seed_data:
        db[coll_name]
    ensure_indexes(db, schema)

    print("\n[5] Seed data insert...")
    seed_collections(db, seed_data)

    all_colls = sorted(set(list(schema.get("collections", {})) + list(seed_data.keys())))
    counts = {c: db[c].count_documents({}) for c in all_colls}
    summary = {
        "database": MONGO_DB_NAME,
        "mongo_uri": MONGO_URI,
        "collections": counts,
        "api_table": str(Path(__file__).parent / "api_table.json"),
        "demo_logins": {
            "client": "C358167 / 615849",
            "admin": "ADMIN001 / admin@123",
            "hierarchy": "OWNER001 .. CLIENT001 / admin@123",
        },
    }
    (Path(__file__).parent / "setup_report.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )

    print("\n" + "=" * 50)
    print("MongoDB setup complete!")
    print("=" * 50)
    for c, n in sorted(counts.items()):
        print(f"  {c}: {n}")
    print(f"\nAPI mapping : mongodb/api_table.json")
    print(f"Table files : mongodb/tables/*.json ({len(all_colls)} tables)")
    print(f"Schemas     : mongodb/collections_schema.json")
    print(f"Seed files  : mongodb/seed/*.json")
    print("=" * 50)


if __name__ == "__main__":
    main()
