"""
Decrypt CryptoJS OpenSSL-format strings (prefix U2FsdGVkX1 = "Salted__").
Same algorithm as CryptoJS.AES.decrypt(ciphertext, passphrase).
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad


def _evp_bytes_to_key(
    password: bytes, salt: bytes, key_len: int = 32, iv_len: int = 16
) -> tuple[bytes, bytes]:
    """OpenSSL EVP_BytesToKey with MD5 (CryptoJS default KDF)."""
    d = b""
    prev = b""
    while len(d) < key_len + iv_len:
        prev = hashlib.md5(prev + password + salt).digest()
        d += prev
    return d[:key_len], d[key_len : key_len + iv_len]


def encrypt_cryptojs_aes(plaintext: str, passphrase: str) -> str:
    """Encrypt to CryptoJS OpenSSL base64 (Salted__ + AES-CBC)."""
    import os

    salt = os.urandom(8)
    key, iv = _evp_bytes_to_key(passphrase.encode("utf-8"), salt)
    from Crypto.Util.Padding import pad

    cipher = AES.new(key, AES.MODE_CBC, iv)
    encrypted = cipher.encrypt(pad(plaintext.encode("utf-8"), AES.block_size))
    raw = b"Salted__" + salt + encrypted
    return base64.b64encode(raw).decode("ascii")


def encrypt_api_payload(payload: Any, passphrase: str) -> str:
    return encrypt_cryptojs_aes(json.dumps(payload, separators=(",", ":")), passphrase)


def decrypt_cryptojs_aes(ciphertext_b64: str, passphrase: str) -> str:
    raw = base64.b64decode(ciphertext_b64)
    if not raw.startswith(b"Salted__"):
        raise ValueError("Not CryptoJS OpenSSL format (expected 'Salted__' header)")
    salt = raw[8:16]
    encrypted = raw[16:]
    key, iv = _evp_bytes_to_key(passphrase.encode("utf-8"), salt)
    cipher = AES.new(key, AES.MODE_CBC, iv)
    decrypted = unpad(cipher.decrypt(encrypted), AES.block_size)
    return decrypted.decode("utf-8")


def api_request_body(gtype: str, passphrase: str) -> dict[str, str]:
    """Encrypted POST body — server expects {"type": "<gtype>"}."""
    return {"data": encrypt_api_payload({"type": gtype}, passphrase)}


def decrypt_api_payload(response_json: dict[str, Any], passphrase: str) -> Any:
    """Parse {"data": "<base64 encrypted>"} from vcasino API."""
    if "data" not in response_json:
        raise KeyError("Response has no 'data' field")
    plain = decrypt_cryptojs_aes(response_json["data"], passphrase)
    try:
        return json.loads(plain)
    except json.JSONDecodeError:
        return plain


def maybe_decrypt(payload: dict[str, Any], passphrase: str) -> Any:
    data = payload.get("data")
    if isinstance(data, str) and data.startswith("U2FsdGVkX1"):
        inner = decrypt_api_payload(payload, passphrase)
        if isinstance(inner, dict) and "data" in inner and inner.get("success") is not False:
            return inner
        return inner
    return payload
