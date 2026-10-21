"""Verify existing loopback exemption; no new public-login protection is enabled."""
from __future__ import annotations
import asyncio
import unittest
from unittest import mock
from fastapi import Request
from starlette.responses import Response
import ip_guard as guard
import middleware as m


def request(ip, real_ip=None):
    headers = [(b'host', b'127.0.0.1:8000')]
    if real_ip is not None:headers.append((b'x-real-ip', real_ip.encode()))
    return Request({'type':'http', 'method':'POST', 'scheme':'http',
                    'path':'/api/auth/login', 'query_string':b'',
                    'client':(ip,1234), 'server':('127.0.0.1',8000), 'headers':headers})


async def bad(_request):return Response(status_code=401)
async def good(_request):return Response(status_code=200)


class LoopbackGuardTests(unittest.TestCase):
    def test_rate_limit_is_429_without_any_authentication_lock(self):
        with mock.patch.dict(guard._hits,{},clear=True):
            req=request('127.0.0.1')
            for _ in range(guard.RATE_GENERAL['limit']):
                self.assertEqual(asyncio.run(m.rate_limit(req,good)).status_code,200)
            self.assertEqual(asyncio.run(m.rate_limit(req,good)).status_code,429)
    def test_forwarded_headers_do_not_grant_local_access(self):
        from request_origin import _local_request_allowed
        self.assertFalse(_local_request_allowed(request('127.0.0.1','198.51.100.7')))
        self.assertFalse(_local_request_allowed(request('192.0.2.26','127.0.0.1')))
