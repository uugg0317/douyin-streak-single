"""T06：真实 ASGI 守卫、API 别名与 QR 严格边界；只有合成台账，不执行账号任务。"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / ".review-work" / "t06-tests"

PREAMBLE = '''
import asyncio, json, os
from unittest import mock
from fastapi import FastAPI, Header
from fastapi.responses import Response
from starlette.staticfiles import StaticFiles
import auth_service as auth
from auth_cookies import cookie_names
import middleware
from core import ledger
from routers import auth as auth_routes, contacts_ledger, qr_credentials
TOKEN='synthetic-t06-isolated-management-token-not-real'
MARK={'X-Requested-With':'sparkkeeper'}
VALID={**MARK,'Origin':'http://127.0.0.1:8000'}
ledger._save([{'entry_id':'synthetic-entry-x','display_name':'x','selected':False}])
app=FastAPI()
middleware.register_middleware(app)
app.include_router(auth_routes.router)
app.include_router(contacts_ledger.router)
app.include_router(qr_credentials.router)
effects=[]
@app.api_route('/api/test-mutate',methods=['POST','PUT','PATCH','DELETE','TRACE'])
def mutate(token: str=Header(default='',alias='X-Auth-Token')):
    auth._check_auth(token);effects.append(1);return {'ok':True}
@app.api_route('/api/test-read',methods=['GET','HEAD','OPTIONS'])
def read(token: str=Header(default='',alias='X-Auth-Token')):
    auth._check_auth(token);return {'ok':True}
@app.get('/')
def home():return {'ok':True}
app.mount('/static',StaticFiles(directory=str(__import__('app_common').STATIC_DIR)),name='static')
async def invoke(method,path,*,headers=None,extra=None,token=TOKEN,cookie=None,body=None,
                 peer='127.0.0.1',scheme='http'):
    fields={'host':'127.0.0.1:8000'}
    if token is not None:fields['x-auth-token']=token
    if cookie is not None:fields['cookie']=cookie
    for name,value in (headers or {}).items():
        if value is None:fields.pop(name.lower(),None)
        else:fields[name.lower()]=value
    raw=json.dumps(body or {}).encode()
    if body is not None:fields['content-type']='application/json'
    raw_headers=[(name.encode(),value.encode('utf-8')) for name,value in fields.items()]
    raw_headers.extend((name.lower().encode(),value.encode('utf-8')) for name,value in (extra or []))
    scope={'type':'http','http_version':'1.1','asgi':{'version':'3.0','spec_version':'2.4'},
       'scheme':scheme,'method':method,'path':path,'raw_path':path.encode(),
       'query_string':b'','root_path':'','headers':raw_headers,
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
    data=b''.join(item.get('body',b'') for item in sent if item['type']=='http.response.body')
    try:payload=json.loads(data)
    except (ValueError,UnicodeError):payload=None
    return start['status'],payload,start['headers']
def request(*args,**kwargs):return asyncio.run(invoke(*args,**kwargs))
def cookie():return cookie_names()[1]+'='+auth._issue_session(device=True)
QR_HEADERS={**VALID,'X-Spark-QR-Request':'1'}
QR_BODY={'request_id':'00000000-0000-4000-8000-000000000006'}
qr_credentials._get_qr_manager=mock.Mock(side_effect=AssertionError('禁止真实浏览器'))
'''


class RequestGuardTests(unittest.TestCase):
    def isolated(self, code):
        WORK.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="case-", dir=WORK) as temporary:
            folder = Path(temporary)
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith("SMTP_") and key not in {
                       "URL_ACCESS_TOKEN", "URL_PASSWORD_SALT", "URL_PASSWORD_HASH", "STATE_FILE_PATH",
                   }}
            env.update({
                "DATA_DIR": str(folder / "data"), "ENV_FILE_PATH": str(folder / "missing.env"),
                "INSTANCE_LOCK_PATH": str(folder / "pid"), "HOST": "127.0.0.1", "PORT": "8000",
                "LOGIN_GATE": "on", "AUTH_TOKEN": "synthetic-t06-isolated-management-token-not-real",
                "PASSWORD_PUBLIC_LOGIN": "off", "SPARKKEEPER_BACKGROUND_TASKS": "0",
                "SESSION_WARN_EMAIL_ENABLED": "off", "QR_PUBLIC_ORIGIN": "", "ALLOWED_HOSTS": "",
                "WHITELIST_IPS": "", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1",
            })
            source = PREAMBLE + "\n" + textwrap.dedent(code) + "\nprint('T06隔离验证通过')\n"
            result = subprocess.run([sys.executable, "-B", "-c", source], cwd=ROOT, env=env,
                                    text=True, encoding="utf-8", capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("T06隔离验证通过", result.stdout)

    def test_all_mutations_require_exact_marker_even_with_valid_cli_token(self):
        self.isolated('''
        for method in ('POST','PUT','PATCH','DELETE','TRACE'):
            for value in (None,'','wrong','Sparkkeeper',' sparkkeeper','sparkkeeper '):
                status,_,_=request(method,'/api/test-mutate',headers={'Origin':VALID['Origin'],
                    'X-Requested-With':value})
                assert status==403,(method,value,status)
            assert request(method,'/api/test-mutate',headers=VALID)[0]==200
        assert len(effects)==5
        assert request('POST','/api/test-mutate')[0]==403
        assert request('POST','/api/test-mutate',headers=MARK)[0]==200
        ''')

    def test_safe_methods_and_static_assets_do_not_require_marker(self):
        self.isolated('''
        for method in ('GET','HEAD','OPTIONS'):
            assert request(method,'/api/test-read')[0]==200
        assert request('GET','/')[0]==200
        assert request('GET','/static/favicon.ico',token=None)[0]==200
        ''')

    def test_marker_does_not_replace_cookie_origin_or_browser_metadata(self):
        self.isolated('''
        current=cookie()
        for headers in (MARK,{**MARK,'Origin':'https://evil.example.invalid'},
                        {**MARK,'Origin':'null'},
                        {**VALID,'Sec-Fetch-Site':'cross-site'},
                        {**VALID,'Sec-Fetch-Site':'same-site'}):
            assert request('POST','/api/test-mutate',cookie=current,token=None,headers=headers)[0]==403
        assert not effects
        assert request('POST','/api/test-mutate',cookie=current,token=None,headers=VALID)[0]==200
        assert request('POST','/api/test-mutate',cookie=current,token=None,
                       headers={**MARK,'Sec-Fetch-Site':'same-origin'})[0]==200
        ''')

    def test_invalid_current_cookie_blocks_cli_metadata_exemption(self):
        self.isolated('''
        invalid=cookie_names()[1]+'=invalid'
        assert request('POST','/api/test-mutate',headers=MARK,cookie=invalid)[0]==403
        assert request('POST','/api/test-mutate',headers=MARK,token='wrong')[0]==403
        assert request('POST','/api/test-mutate',headers=VALID,token=None)[0]==401
        assert not effects
        ''')

    def test_gate_off_and_ip_whitelist_do_not_bypass_marker_or_origin(self):
        self.isolated('''
        auth.LOGIN_GATE=False
        middleware.WHITELIST_IPS={'127.0.0.1'}
        assert request('POST','/api/ledger/selection',token=None,
                       headers={'Origin':VALID['Origin']},body={'selected_names':['x']})[0]==403
        assert request('POST','/api/ledger/selection',token=None,
                       headers={**MARK,'Origin':'https://evil.example.invalid'},body={'selected_names':['x']})[0]==403
        assert request('POST','/api/ledger/selection',token=None,
                       headers=VALID,body={'selected_names':['x']})[0]==200
        assert ledger.load_ledger()[0]['selected'] is True
        ''')

    def test_duplicate_security_headers_are_rejected_even_when_values_match(self):
        self.isolated('''
        for name,value in (('X-Requested-With','sparkkeeper'),('Host','127.0.0.1:8000'),
                           ('Origin',VALID['Origin']),('X-Auth-Token',TOKEN),
                           ('Sec-Fetch-Site','same-origin'),('X-Forwarded-Proto','http')):
            headers=dict(VALID)
            if name not in ('Host','X-Auth-Token'):headers[name]=value
            assert request('POST','/api/test-mutate',headers=headers,extra=[(name,value)])[0]==403,name
        assert not effects
        ''')

    def test_explicit_api_alias_preserves_host_and_port_origin_checks(self):
        self.isolated('''
        os.environ['ALLOWED_HOSTS']=' console.example.invalid , api.example.invalid:8080 '
        alias={**MARK,'Host':'console.example.invalid:9000','Origin':'http://console.example.invalid:9000'}
        assert request('POST','/api/test-mutate',headers=alias)[0]==200
        assert request('POST','/api/test-mutate',headers={**alias,'Origin':'http://console.example.invalid:9001'})[0]==403
        explicit={**MARK,'Host':'api.example.invalid:8080','Origin':'http://api.example.invalid:8080'}
        assert request('POST','/api/test-mutate',headers=explicit)[0]==200
        assert request('POST','/api/test-mutate',headers={**explicit,'Host':'api.example.invalid:8081',
                       'Origin':'http://api.example.invalid:8081'})[0]==403
        assert request('POST','/api/test-mutate',headers={**VALID,'Host':'evil.example.invalid',
                       'Origin':'http://evil.example.invalid'})[0]==403
        assert len(effects)==2
        ''')

    def test_wildcards_paths_and_userinfo_never_grant_alias_access(self):
        self.isolated('''
        for allowed in ('*','*.example.invalid','http://console.example.invalid',
                        'console.example.invalid/path','user@console.example.invalid',
                        'console.example.invalid:999999'):
            os.environ['ALLOWED_HOSTS']=allowed
            headers={**MARK,'Host':'console.example.invalid','Origin':'http://console.example.invalid'}
            assert request('POST','/api/test-mutate',headers=headers)[0]==403,allowed
        assert not effects
        ''')

    def test_alias_and_forwarded_headers_never_grant_nonlocal_peer_access(self):
        self.isolated('''
        os.environ['ALLOWED_HOSTS']='console.example.invalid'
        headers={**MARK,'Host':'console.example.invalid','Origin':'http://console.example.invalid',
                 'X-Real-IP':'127.0.0.1','X-Forwarded-For':'127.0.0.1','X-Forwarded-Proto':'http'}
        assert request('POST','/api/test-mutate',headers=headers,peer='198.51.100.7')[0]==403
        bad={**VALID,'X-Forwarded-Host':'console.example.invalid'}
        assert request('POST','/api/test-mutate',headers=bad)[0]==403
        assert not effects
        ''')

    def test_alias_does_not_infer_https_or_weaken_secure_cookie_contract(self):
        self.isolated('''
        os.environ['ALLOWED_HOSTS']='console.example.invalid'
        proxy={**MARK,'Host':'console.example.invalid','Origin':'https://console.example.invalid',
               'X-Forwarded-Proto':'https','X-Real-IP':'198.51.100.7'}
        status,_,headers=request('POST','/api/auth/login',token=None,headers=proxy,body={'token':TOKEN})
        assert status==403 and not any(name==b'set-cookie' for name,value in headers)
        os.environ['QR_PUBLIC_ORIGIN']='https://console.example.invalid'
        status,_,headers=request('POST','/api/auth/login',token=None,headers=proxy,body={'token':TOKEN})
        assert status==200
        assert any(name==b'set-cookie' and b'; Secure' in value for name,value in headers)
        ''')

    def test_api_alias_never_relaxes_qr_public_origin_or_qr_marker(self):
        self.isolated('''
        os.environ['ALLOWED_HOSTS']='console.example.invalid'
        alias={**QR_HEADERS,'Host':'console.example.invalid','Origin':'http://console.example.invalid'}
        assert request('POST','/api/test-mutate',headers=alias)[0]==200
        assert request('POST','/api/credentials/qr/start',headers=alias,body=QR_BODY)[0]==403
        assert request('POST','/api/credentials/qr/start',headers=VALID,body=QR_BODY)[0]==403
        assert request('POST','/api/credentials/qr/start',headers={**MARK,'X-Spark-QR-Request':'1'},body=QR_BODY)[0]==403
        qr_credentials._get_qr_manager.assert_not_called()
        ''')

    def test_configured_https_proxy_allows_real_business_write_only_with_full_contract(self):
        self.isolated('''
        os.environ['QR_PUBLIC_ORIGIN']='https://console.example.invalid'
        headers={**MARK,'Host':'console.example.invalid','Origin':'https://console.example.invalid',
                 'X-Forwarded-Proto':'https','X-Real-IP':'198.51.100.7'}
        assert request('POST','/api/ledger/selection',headers=headers,body={'selected_names':['x']})[0]==200
        for bad in ({**headers,'X-Requested-With':None},
                    {**headers,'Origin':'https://evil.example.invalid'},
                    {**headers,'X-Forwarded-Proto':'http'}):
            assert request('POST','/api/ledger/selection',headers=bad,body={'selected_names':['x']})[0]==403
        ''')


if __name__ == '__main__':
    unittest.main()
