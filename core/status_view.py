"""Pure presentation helpers; freshness never changes stored credential validity."""
from __future__ import annotations

import math

SESSION_CHECK_MAX_AGE_SECONDS = 24 * 60 * 60
SESSION_CHECK_FUTURE_TOLERANCE_SECONDS = 5 * 60
TASK_PHASES = frozenset({"starting", "opening_browser", "loading_contacts", "locating",
                         "confirming", "waiting_gap", "finishing"})


def session_check_view(checked_at: object, *, now: float) -> dict:
    view = {"session_checked_at": None, "session_check_age_seconds": None,
            "session_check_stale": False, "session_check_reason": "missing"}
    if checked_at is None or checked_at == "":
        return view
    try:
        if isinstance(checked_at, bool):
            raise ValueError
        timestamp = float(checked_at)
        if not math.isfinite(timestamp) or timestamp <= 0 or not math.isfinite(now):
            raise ValueError
    except (ValueError, TypeError, OverflowError):
        view["session_check_reason"] = "invalid"
        return view
    if timestamp > now + SESSION_CHECK_FUTURE_TOLERANCE_SECONDS:
        view["session_check_reason"] = "future"
        return view
    age = max(0.0, now - timestamp)
    stale = age >= SESSION_CHECK_MAX_AGE_SECONDS
    view.update(session_checked_at=timestamp, session_check_age_seconds=age,
                session_check_stale=stale, session_check_reason="stale" if stale else "fresh")
    return view


def task_phase_view(phase: object, *, active: bool) -> str | None:
    return phase if active and isinstance(phase, str) and phase in TASK_PHASES else None
