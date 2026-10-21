"""Strict loopback and same-origin boundary for the password-free local edition."""
from __future__ import annotations
import ipaddress
import os
from urllib.parse import urlsplit
from fastapi import Request

def _is_loopback(host: str) -> bool:
    if not isinstance(host, str) or not host:
        return False
    if host.lower() == 'localhost':
        return True
    try:
        address = ipaddress.ip_address(host)
        return bool((getattr(address, 'ipv4_mapped', None) or address).is_loopback)
    except ValueError:
        return False

def _check_local_bind(host: str | None = None) -> None:
    configured = os.environ.get('HOST', '127.0.0.1') if host is None else host
    if not _is_loopback(configured):
        raise SystemExit('本机版只允许监听回环地址（127.0.0.1 / localhost / ::1）')

def _client_ip(request: Request) -> str:
    peer = request.scope.get('client')
    return peer[0] if peer else 'unknown'

def _canonical_origin(value: str) -> tuple[str, str, int] | None:
    try:
        if not isinstance(value, str) or any(ord(c) <= 32 or ord(c) == 127 for c in value):
            return None
        parsed = urlsplit(value)
        if (parsed.scheme not in {'http', 'https'} or not parsed.hostname
                or parsed.username or parsed.password or parsed.path not in {'', '/'}
                or parsed.query or parsed.fragment):
            return None
        host = parsed.hostname.lower().encode('idna').decode('ascii')
        port = parsed.port if parsed.port is not None else (443 if parsed.scheme == 'https' else 80)
        if not 1 <= port <= 65535:
            return None
        return parsed.scheme, host, port
    except (ValueError, UnicodeError):
        return None

def _host_origin(scheme: str, authority: str) -> tuple[str, str, int] | None:
    if not authority or any(c.isspace() or c in '/?#@,' for c in authority):
        return None
    return _canonical_origin(f'{scheme}://{authority}')

def _effective_origin(request: Request, *, allow_api_aliases: bool = False) -> tuple[str, str, int] | None:
    """Never grant proxy/alias access, including legacy environment settings."""
    if not _is_loopback(_client_ip(request)):
        return None
    names = ('host', 'origin', 'sec-fetch-site', 'x-auth-token', 'x-requested-with',
             'x-forwarded-proto', 'x-forwarded-host', 'x-forwarded-for', 'x-real-ip', 'forwarded')
    if any(len(request.headers.getlist(name)) > 1 for name in names):
        return None
    if any(name in request.headers for name in ('forwarded', 'x-forwarded-proto',
            'x-forwarded-host', 'x-forwarded-for', 'x-real-ip')):
        return None
    actual = _host_origin(request.url.scheme, request.headers.get('host', ''))
    if actual is None or not _is_loopback(actual[1]):
        return None
    server = request.scope.get('server')
    if not server or not _is_loopback(server[0]) or actual[2] != server[1]:
        return None
    return actual

def _local_request_allowed(request: Request, *, allow_api_aliases: bool = False) -> bool:
    return _effective_origin(request) is not None

def _same_origin_allowed(request: Request, *, allow_api_aliases: bool = False) -> bool:
    fetch_site = request.headers.get('sec-fetch-site', '')
    if fetch_site and fetch_site not in {'same-origin', 'none'}:
        return False
    origin = request.headers.get('origin')
    if origin is not None:
        actual = _effective_origin(request)
        return actual is not None and _canonical_origin(origin) == actual
    if request.method.upper() not in {'GET', 'HEAD', 'OPTIONS'}:
        return fetch_site == 'same-origin'
    return True
