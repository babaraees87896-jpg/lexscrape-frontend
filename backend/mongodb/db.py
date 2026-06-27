"""MongoDB connection helper for local ex99 stack."""

import os

from typing import Optional

from pymongo import MongoClient
from pymongo.database import Database

MONGO_URI = os.getenv("EX99_MONGO_URI", "mongodb+srv://user_naam:DR9aWgoqCoePn8QJ@cluster0.xxxx.mongodb.net/?retryWrites=true&w=majority")
MONGO_DB_NAME = os.getenv("EX99_MONGO_DB", "ex99_local")

_client: Optional[MongoClient] = None


def get_client() -> MongoClient:
    global _client
    if _client is None:
        _client = MongoClient(
            MONGO_URI,
            serverSelectionTimeoutMS=5000,
            maxPoolSize=int(os.getenv("EX99_MONGO_POOL", "10")),
            maxIdleTimeMS=30_000,
        )
    return _client


def get_db() -> Database:
    return get_client()[MONGO_DB_NAME]


def ping() -> bool:
    try:
        get_client().admin.command("ping")
        return True
    except Exception:
        return False
