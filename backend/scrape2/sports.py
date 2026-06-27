"""Sports / cricket API helpers — casino se alag."""

from __future__ import annotations

import re

# Site defaults (tablist se bhi aata hai)
CRICKET_EID = 4
FOOTBALL_EID = 1
TENNIS_EID = 2

SPORT_APIS = {
    "tablist": "/api/front/tablist",
    "highlights": "/api/front/highlighthomePrivate",
    "match": "/api/front/gamedataPrivate",
    "detail": "/api/front/gamedetailPrivate",
    "latest": "/api/front/get_latest_events",
}


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")
    return s or "sport"


def sport_json_path(eid: int | str) -> str:
    """Local URL path: /sport/{eid}.json (Sport ID)"""
    return f"/sport/{eid}.json"


def sport_file_path(out_dir, eid: int | str):
    from pathlib import Path
    return Path(out_dir) / f"{eid}.json"


def match_json_path(eid: int | str, gmid: int | str) -> str:
    """Local URL path: /sport/matches/{etid}/{gmid}.json"""
    return f"/sport/matches/{eid}/{gmid}.json"


def match_file_path(out_dir, eid: int | str, gmid: int | str):
    from pathlib import Path
    return Path(out_dir) / "matches" / str(eid) / f"{gmid}.json"


def scorecard_json_path(eid: int | str, gmid: int | str) -> str:
    return f"/sport/scorecard/{eid}/{gmid}.json"


def scorecard_file_path(out_dir, eid: int | str, gmid: int | str):
    from pathlib import Path
    return Path(out_dir) / "scorecard" / str(eid) / f"{gmid}.json"
