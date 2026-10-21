"""Pure storage-state validation shared by manual import and QR boundaries.

This module does not load configuration, touch files or determine login status.
An expired or absent session cookie is still a structurally valid manual import.
"""
from __future__ import annotations

import json
import math
from urllib.parse import urlsplit

MAX_STATE = 5 * 1024 * 1024


def _invalid(*_args):
    # Never include uploaded values in exceptions or logs.
    raise ValueError("invalid_state")


def _valid_expiry(value) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value) and (value == -1 or value >= 0)
    except OverflowError:
        return False


def _valid_origin(value) -> bool:
    if not isinstance(value, str) or not value or any(char.isspace() or ord(char) < 32 for char in value):
        return False
    try:
        parsed = urlsplit(value)
        # Accessing .port also validates malformed or out-of-range ports.
        parsed.port
        return (parsed.scheme in {"http", "https"} and bool(parsed.hostname)
                and parsed.username is None and parsed.password is None
                and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment
                and "\\" not in parsed.netloc)
    except ValueError:
        return False


def validate_storage_state(raw: bytes) -> dict:
    """Return validated JSON or ValueError, without authenticating an account."""
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_STATE:
        _invalid()
    try:
        state = json.loads(raw.decode("utf-8"), parse_constant=_invalid)
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError("invalid_state") from None
    if not isinstance(state, dict):
        _invalid()
    cookies = state.get("cookies")
    if not isinstance(cookies, list) or not cookies or len(cookies) > 2000:
        _invalid()
    for cookie in cookies:
        if not isinstance(cookie, dict) or not all(
            isinstance(cookie.get(key), str) for key in ("name", "value", "domain", "path")
        ):
            _invalid()
        if (not cookie["name"] or not cookie["domain"] or not cookie["path"].startswith("/")
                or any(char.isspace() or ord(char) < 32 for char in cookie["domain"])
                or "/" in cookie["domain"] or "\\" in cookie["domain"]):
            _invalid()
        if not _valid_expiry(cookie.get("expires", -1)):
            _invalid()
        if any(key in cookie and not isinstance(cookie[key], bool) for key in ("httpOnly", "secure")):
            _invalid()
        if "sameSite" in cookie and cookie["sameSite"] not in ("Lax", "Strict", "None"):
            _invalid()
        if cookie.get("partitionKey") is not None and not isinstance(cookie["partitionKey"], str):
            _invalid()
    origins = state.get("origins", [])
    if not isinstance(origins, list) or len(origins) > 100:
        _invalid()
    for origin in origins:
        if (not isinstance(origin, dict) or not _valid_origin(origin.get("origin"))
                or not isinstance(origin.get("localStorage"), list)
                or not all(isinstance(item, dict) and isinstance(item.get("name"), str)
                           and isinstance(item.get("value"), str) for item in origin["localStorage"])):
            _invalid()
    return state
