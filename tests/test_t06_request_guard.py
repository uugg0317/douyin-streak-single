"""T06：真实 ASGI 本机来源、旧配置拒绝与 QR 严格边界；只有合成台账，不执行账号任务。"""
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
import middleware
from core import ledger
from routers import auth as auth_routes, contacts_ledger, qr_credentials
TOKEN='t06-isolated-management-token-not-real'
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
def cookie():return 'legacy-device-cookie=ignored'
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
                "LOGIN_GATE": "on", "AUTH_TOKEN": "t06-isolated-management-token-not-real",
                "PASSWORD_PUBLIC_LOGIN": "off", "SPARKKEEPER_BACKGROUND_TASKS": "0",
                "SESSION_WARN_EMAIL_ENABLED": "off", "QR_PUBLIC_ORIGIN": "", "ALLOWED_HOSTS": "",
                "WHITELIST_IPS": "", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1",
            })
            source = PREAMBLE + "\n" + textwrap.dedent(code) + "\nprint('T06隔离验证通过')\n"
            result = subprocess.run([sys.executable, "-B", "-c", source], cwd=ROOT, env=env,
                                    text=True, encoding="utf-8", capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("T06隔离验证通过", result.stdout)

    def test_all_mutations_require_marker_and_same_origin(self):
        self.isolated("""
        for method in ('POST','PUT','PATCH','DELETE','TRACE'):
            for value in (None,'','wrong','Sparkkeeper',' sparkkeeper','sparkkeeper '):
                assert request(method,'/api/test-mutate',headers={'Origin':VALID['Origin'],'X-Requested-With':value})[0]==403
            assert request(method,'/api/test-mutate',token=None,headers=VALID)[0]==200
        assert len(effects)==5
        assert request('POST','/api/test-mutate',headers=MARK)[0]==403
        """)

    def test_safe_methods_static_and_root_need_no_password(self):
        self.isolated("""
        for method in ('GET','HEAD','OPTIONS'):
            assert request(method,'/api/test-read',token=None)[0]==200
        assert request('GET','/',token=None)[0]==200
        assert request('GET','/static/favicon.ico',token=None)[0]==200
        assert request('GET','/api/auth/mode',token=None)[1]['login_gate'] is False
        """)

    def test_origin_and_browser_metadata_are_required_even_with_legacy_tokens(self):
        self.isolated("""
        for headers in (MARK,{**MARK,'Origin':'https://evil.example.invalid'},{**MARK,'Origin':'null'},
                        {**VALID,'Sec-Fetch-Site':'cross-site'},{**VALID,'Sec-Fetch-Site':'same-site'}):
            assert request('POST','/api/test-mutate',cookie=cookie(),headers=headers)[0]==403
        assert not effects
        assert request('POST','/api/test-mutate',token=None,cookie=cookie(),headers=VALID)[0]==200
        assert request('POST','/api/test-mutate',token=None,headers={**MARK,'Sec-Fetch-Site':'same-origin'})[0]==200
        """)

    def test_bad_host_or_nonlocal_peer_never_reaches_business_handlers(self):
        self.isolated("""
        for path,method in (('/','GET'),('/static/favicon.ico','GET'),('/api/test-read','GET'),('/api/test-mutate','POST')):
            assert request(method,path,headers=VALID,peer='198.51.100.7')[0]==403
            assert request(method,path,headers={**VALID,'Host':'evil.example.invalid','Origin':'http://evil.example.invalid'})[0]==403
            assert request(method,path,headers={**VALID,'Host':'127.0.0.1:8001','Origin':'http://127.0.0.1:8001'})[0]==403
        assert not effects
        """)

    def test_duplicate_security_headers_are_rejected(self):
        self.isolated("""
        for name,value in (('X-Requested-With','sparkkeeper'),('Host','127.0.0.1:8000'),('Origin',VALID['Origin']),('X-Auth-Token',TOKEN),('Sec-Fetch-Site','same-origin')):
            headers=dict(VALID)
            if name not in ('Host','X-Auth-Token'):headers[name]=value
            assert request('POST','/api/test-mutate',headers=headers,extra=[(name,value)])[0]==403
        assert not effects
        """)

    def test_legacy_alias_proxy_and_whitelist_configuration_never_opens_external_access(self):
        self.isolated("""
        os.environ.update(ALLOWED_HOSTS='console.example.invalid,localhost:8000',QR_PUBLIC_ORIGIN='https://console.example.invalid',WHITELIST_IPS='198.51.100.7')
        headers={**MARK,'Host':'console.example.invalid','Origin':'https://console.example.invalid','X-Forwarded-Proto':'https','X-Real-IP':'198.51.100.7'}
        assert request('POST','/api/test-mutate',headers=headers)[0]==403
        assert request('POST','/api/credentials/qr/start',headers={**headers,'X-Spark-QR-Request':'1'},body=QR_BODY)[0]==403
        for name,value in [('X-Real-IP','127.0.0.1'),('X-Forwarded-For','127.0.0.1'),('X-Forwarded-Proto','http'),('X-Forwarded-Host','127.0.0.1:8000'),('Forwarded','for=127.0.0.1')]:
            assert request('POST','/api/test-mutate',headers={**VALID,name:value})[0]==403
        assert not effects
        qr_credentials._get_qr_manager.assert_not_called()
        """)

    def test_no_gate_does_not_weaken_qr_origin_or_job_contract(self):
        self.isolated("""
        for headers in (VALID,{**MARK,'X-Spark-QR-Request':'1'},{**QR_HEADERS,'Origin':'https://evil.example.invalid'}):
            assert request('POST','/api/credentials/qr/start',token=None,headers=headers,body=QR_BODY)[0]==403
        qr_credentials._get_qr_manager.assert_not_called()
        """)

    def test_local_business_write_without_management_token_remains_functional(self):
        self.isolated("""
        assert request('POST','/api/ledger/selection',token=None,headers=VALID,body={'selected_names':['x']})[0]==200
        assert ledger.load_ledger()[0]['selected'] is True
        """)

if __name__=='__main__':unittest.main()
