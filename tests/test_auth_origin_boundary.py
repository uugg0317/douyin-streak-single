"""真实 ASGI 认证／来源守卫，全部使用临时数据，无浏览器、账号或外部请求。"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "review-management-token-synthetic-123456789"
PASSWORD = "review-password-synthetic-123"

PREAMBLE = r'''
import asyncio, hashlib, json, os
from fastapi import FastAPI, Header, Response
import auth_service
import middleware
from routers import auth as auth_routes
from auth_cookies import cookie_names
SESSION_COOKIE, DEVICE_COOKIE = cookie_names()
TOKEN = "review-management-token-synthetic-123456789"
PASSWORD = "review-password-synthetic-123"
app=FastAPI()
middleware.register_middleware(app)
app.include_router(auth_routes.router)
effects=[]
@app.post('/api/review-mutate')
def mutate(token: str=Header(default='',alias='X-Auth-Token')):
    auth_service._check_auth(token)
    effects.append('mutated')
    return {'ok':True}
@app.get('/api/review-state')
def state(token: str=Header(default='',alias='X-Auth-Token')):
    auth_service._check_auth(token)
    return {'ok':True}
async def invoke(method,path,*,body=None,cookie=None,headers=None,peer='127.0.0.1'):
    fields=[('host','127.0.0.1:8000')]
    if method.upper() not in {'GET','HEAD','OPTIONS'}: fields.append(('x-requested-with','sparkkeeper'))
    if cookie is not None: fields.append(('cookie',cookie))
    for key,value in (headers or {}).items():
        fields=[item for item in fields if item[0].lower()!=key.lower()]
        if value is not None:fields.append((key.lower(),value))
    raw=json.dumps(body or {}).encode()
    if body is not None:fields.append(('content-type','application/json'))
    scope={'type':'http','http_version':'1.1','asgi':{'version':'3.0','spec_version':'2.4'},
           'scheme':'http','method':method,'path':path,'raw_path':path.encode(),'query_string':b'',
           'root_path':'','headers':[(k.encode(),v.encode('utf-8')) for k,v in fields],
           'client':(peer,43210),'server':('127.0.0.1',8000)}
    sent=[];received=False
    async def receive():
        nonlocal received
        if not received:
            received=True
            return {'type':'http.request','body':raw,'more_body':False}
        await asyncio.Event().wait()
    async def send(message):sent.append(message)
    await app(scope,receive,send)
    start=next(item for item in sent if item['type']=='http.response.start')
    payload=b''.join(item.get('body',b'') for item in sent if item['type']=='http.response.body')
    return start['status'],json.loads(payload),start['headers']
def request(*args,**kwargs):return asyncio.run(invoke(*args,**kwargs))
def device():return auth_service._issue_session(device=True)
def session():return auth_service._issue_session()
ORIGIN={'Origin':'http://127.0.0.1:8000'}
'''


class AuthOriginBoundaryTests(unittest.TestCase):
    def isolated(self, body):
        with tempfile.TemporaryDirectory(prefix="auth-origin-isolated-") as temporary:
            folder = Path(temporary)
            env = os.environ.copy()
            import hashlib
            salt = "review-password-salt"
            env.update({
                "DATA_DIR": str(folder / "data"), "ENV_FILE_PATH": str(folder / "missing.env"),
                "INSTANCE_LOCK_PATH": str(folder / "server.pid"), "STATE_FILE_PATH": "",
                "AUTH_TOKEN": TOKEN, "LOGIN_GATE": "on", "PASSWORD_PUBLIC_LOGIN": "on",
                "URL_ACCESS_TOKEN": "synthetic-review-url-token-synthetic-123456789",
                "URL_PASSWORD_SALT": salt, "URL_PASSWORD_HASH": hashlib.sha256((PASSWORD + salt).encode()).hexdigest(),
                "HOST": "127.0.0.1", "PORT": "8000", "QR_PUBLIC_ORIGIN": "", "WHITELIST_IPS": "",
                "SPARKKEEPER_BACKGROUND_TASKS": "0", "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1",
            })
            code = PREAMBLE + "\n" + textwrap.dedent(body) + "\nprint('auth-origin-isolated-pass')\n"
            result = subprocess.run([sys.executable, "-B", "-c", code], cwd=ROOT, env=env,
                                    capture_output=True, text=True, encoding="utf-8", timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertIn("auth-origin-isolated-pass", result.stdout)

    def test_logout_revokes_both_cookies_and_original_header_session(self):
        self.isolated("""
        short=session(); long=device(); header=session()
        cookies=f'{SESSION_COOKIE}={short}; {DEVICE_COOKIE}={long}'
        status,_,headers=request('POST','/api/auth/logout',cookie=cookies,
                                 headers={**ORIGIN,'X-Auth-Token':header})
        assert status==200
        assert len([item for item in headers if item[0]==b'set-cookie'])==2
        for token in (short,long,header):assert not auth_service._session_valid(token)
        assert request('GET','/api/review-state',cookie=f'{DEVICE_COOKIE}={long}')[0]==401
        """)

    def test_unicode_and_surrogate_credentials_are_401(self):
        self.isolated("""
        for token in ('非法令牌','\\ud800'):
            assert request('POST','/api/auth/login',body={'token':token},headers=ORIGIN)[0]==401
            assert request('POST','/api/auth/url-token-login',
                           body={'access_token':token,'password':PASSWORD},headers=ORIGIN)[0]==401
        assert request('GET','/api/review-state',headers={'X-Auth-Token':'非法令牌'})[0]==401
        """)

    def test_cookie_mutations_require_same_origin_evidence(self):
        self.isolated("""
        cookie=f'{DEVICE_COOKIE}={device()}'
        for headers in ({},{'Origin':'https://evil.example.invalid'},
                        {'Origin':'null'},{'Sec-Fetch-Site':'cross-site'},
                        {**ORIGIN,'Sec-Fetch-Site':'same-site'}):
            assert request('POST','/api/review-mutate',cookie=cookie,headers=headers)[0]==403
        assert not effects
        assert request('POST','/api/review-mutate',cookie=cookie,headers=ORIGIN)[0]==200
        assert request('POST','/api/review-mutate',cookie=cookie,headers={'Sec-Fetch-Site':'same-origin'})[0]==200
        assert len(effects)==2
        """)

    def test_explicit_cli_management_token_cannot_relax_browser_or_cookie_checks(self):
        self.isolated("""
        assert request('POST','/api/review-mutate',headers={'X-Auth-Token':TOKEN})[0]==200
        for headers in ({'X-Auth-Token':'wrong'},{'X-Auth-Token':TOKEN,'Origin':'https://evil.example.invalid'},
                        {'X-Auth-Token':TOKEN,'Sec-Fetch-Site':'cross-site'}):
            assert request('POST','/api/review-mutate',headers=headers)[0]==403
        assert request('POST','/api/review-mutate',cookie=f'{DEVICE_COOKIE}=invalid',headers={'X-Auth-Token':TOKEN})[0]==403
        assert len(effects)==1
        """)

    def test_dns_rebinding_and_nonlocal_raw_peer_cannot_use_valid_credentials(self):
        self.isolated("""
        token=device();cookie=f'{DEVICE_COOKIE}={token}'
        evil={'Host':'evil.example.invalid:8000','Origin':'http://evil.example.invalid:8000'}
        assert request('POST','/api/review-mutate',cookie=cookie,headers=evil)[0]==403
        assert request('GET','/api/review-state',cookie=cookie,headers=evil)[0]==403
        assert request('POST','/api/review-mutate',headers={'X-Auth-Token':TOKEN},peer='198.51.100.8')[0]==403
        assert not effects
        """)

    def test_configured_https_proxy_accepts_only_correct_public_origin(self):
        self.isolated("""
        os.environ['QR_PUBLIC_ORIGIN']='https://console.example.com'
        proxy={'Host':'console.example.com','X-Forwarded-Proto':'https',
               'X-Real-IP':'198.51.100.7','Origin':'https://console.example.com'}
        cookie=f'{DEVICE_COOKIE}={device()}'
        assert request('POST','/api/review-mutate',cookie=cookie,headers=proxy)[0]==200
        for bad in ({**proxy,'Origin':'http://console.example.com'},
                    {**proxy,'X-Forwarded-Proto':'http'},
                    {**proxy,'Origin':None}):
            assert request('POST','/api/review-mutate',cookie=cookie,headers=bad)[0]==403
        assert len(effects)==1
        """)

    def test_origin_guard_is_not_bypassed_by_gate_off_or_ip_whitelist(self):
        self.isolated("""
        auth_service.LOGIN_GATE=False
        middleware.WHITELIST_IPS={'127.0.0.1'}
        assert request('POST','/api/review-mutate',headers={'Origin':'https://evil.example.invalid'})[0]==403
        assert not effects
        """)


if __name__ == "__main__":
    unittest.main()
