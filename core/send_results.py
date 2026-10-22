"""Extract contact identity keys from current and historical send results.

Successful contacts currently use strings; other outcomes use ``{"name": ...}``.
Names stay byte-for-byte equivalent as Python strings so these sets can safely
exclude already resolved contacts from a later retry or deferred queue.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping


def contact_names(items: object) -> set[str]:
    """Return original, nonblank contact names; ignore malformed and system items.

    A single string or mapping is not an outcome collection. Only the ``name``
    field of mapping entries identifies a contact; a reason or display label
    must never be interpreted as another contact identity.
    """
    if isinstance(items, (str, bytes, bytearray, Mapping)) or not isinstance(items, Iterable):
        return set()
    names: set[str] = set()
    for item in items:
        name = item if isinstance(item, str) else item.get("name") if isinstance(item, Mapping) else None
        if isinstance(name, str) and name.strip() and name != "_system":
            names.add(name)
    return names


def _names_for(result: object, fields: tuple[str, ...]) -> set[str]:
    if not isinstance(result, Mapping):
        return set()
    names: set[str] = set()
    for field in fields:
        names.update(contact_names(result.get(field)))
    return names


def completed_count(result: object) -> int:
    """Count unique contacts with completed ok/failed/skipped outcomes.

    Deferred contacts have yet to complete; system-level entries are excluded.
    A contact appearing in multiple outcome fields is counted only once.
    """
    if not isinstance(result, Mapping):
        return 0
    keys = set()
    identified_success_names = set()
    for item in result.get("ok_entries", []) or []:
        if isinstance(item, Mapping) and item.get("entry_id"):
            keys.add(("id", item["entry_id"]))
            identified_success_names.add(item.get("name"))
    for field in ("ok", "failed", "skipped"):
        collection = result.get(field, [])
        if isinstance(collection, (str, bytes, Mapping)) or not isinstance(collection, Iterable):
            continue
        for item in collection:
            name = item if isinstance(item, str) else item.get("name") if isinstance(item, Mapping) else None
            if not isinstance(name, str) or not name.strip() or name == "_system":
                continue
            key = item.get("entry_id") if isinstance(item, Mapping) else None
            if key:
                keys.add(("id", key))
            elif name not in identified_success_names:
                keys.add(("name", name))
    return len(keys)


def resolved_contact_names(result: object) -> set[str]:
    """Return ok/failed/skipped/deferred contact keys already accounted for on abort."""
    return _names_for(result, ("ok", "failed", "skipped", "deferred"))


def contact_entry_ids(items: object) -> set[str]:
    if isinstance(items, (str, bytes, bytearray, Mapping)) or not isinstance(items, Iterable):
        return set()
    return {item["entry_id"] for item in items if isinstance(item, Mapping)
            and isinstance(item.get("entry_id"), str) and item["entry_id"]}
