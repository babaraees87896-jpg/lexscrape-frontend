"""Domain settings — client site + staff panel shared helpers."""

from __future__ import annotations

import copy
import re
from typing import Optional

from mongodb.db import get_db


def _normalize_host(domain: str) -> str:
    host = str(domain or "").strip().lower()
    host = re.sub(r"^https?://", "", host)
    host = host.split("/")[0].split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    return host


def find_domain_by_name(domain: str) -> Optional[dict]:
    """Lookup domain doc by hostname — client site domainSettingByDomainName."""
    host = _normalize_host(domain)
    db = get_db()
    if not host:
        return db.domains.find_one({})

    exact = [
        {"domainName": host},
        {"domainUrl": host},
        {"domainName": {"$regex": f"^{re.escape(host)}$", "$options": "i"}},
        {"domainUrl": {"$regex": f"^{re.escape(host)}$", "$options": "i"}},
    ]
    for q in exact:
        doc = db.domains.find_one(q)
        if doc:
            return doc

    partial = re.escape(host).replace(r"\.", r"\.")
    doc = db.domains.find_one({
        "$or": [
            {"domainName": {"$regex": partial, "$options": "i"}},
            {"domainUrl": {"$regex": partial, "$options": "i"}},
        ]
    })
    if doc:
        return doc
    return db.domains.find_one({})


def domain_setting_response(payload: dict) -> dict:
    domain = str(payload.get("domainName") or payload.get("domainUrl") or "")
    doc = find_domain_by_name(domain)
    if not doc:
        return {"message": "Domain not found", "code": 1, "error": True, "data": {}}
    row = copy.deepcopy(doc)
    row.pop("_id", None)
    # Client marquee reads clientNotification — mirror userNotification when client empty.
    if not str(row.get("clientNotification") or "").strip():
        user_msg = str(row.get("userNotification") or "").strip()
        if user_msg:
            row["clientNotification"] = user_msg
    if not str(row.get("userNotification") or "").strip():
        client_msg = str(row.get("clientNotification") or "").strip()
        if client_msg:
            row["userNotification"] = client_msg
    row.setdefault("domainName", row.get("domainUrl") or "")
    row.setdefault("domainUrl", row.get("domainName") or "")
    row.setdefault("title", row.get("domainName") or "1ex99")
    row.setdefault("status", True)
    row.setdefault("themeSetting", {"colors": {}})
    row.setdefault("sportsSetting", {})
    row.setdefault("socialMedia", {})
    row.setdefault("apiKey", {"talkTo": bool(row.get("talkTo"))})
    row.setdefault("banner", [{"name": "", "priority": "", "image": ""}])
    row.setdefault("account", {})
    row.setdefault("barcode", {})
    row.setdefault("upi", {"paytm": {}, "googlePay": {}, "phonePay": {}, "bhimUpi": {}})
    row.setdefault("signUpBonusSetting", {})
    row.setdefault("reffrelSetting", {})
    row.setdefault("bonusSetting", {})
    return {"message": "OK", "code": 0, "error": False, "data": row}
