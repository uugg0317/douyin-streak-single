"""Local-only edition: no administrator password, token or session gate.
HTTP access is guarded by middleware and request_origin, not legacy secrets.
"""
from request_origin import _check_local_bind
LOGIN_GATE = False
AUTH_TOKEN = ""  # Compatibility only; never reads a runtime credential.

def _check_auth_config() -> None:
    _check_local_bind()

def _check_auth(_token: str = "") -> None:
    """Compatibility hook; the local Host/Origin middleware is the boundary."""

def _clean_expired_sessions() -> None:
    """No management sessions are created by this edition."""
