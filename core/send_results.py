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
    return len(_names_for(result, ("ok", "failed", "skipped")))


def resolved_contact_names(result: object) -> set[str]:
    """Return ok/failed/skipped/deferred contact keys already accounted for on abort."""
    return _names_for(result, ("ok", "failed", "skipped", "deferred"))
