"""QR route/commit integration using a fake engine and temporary child processes.

No Playwright startup, external requests, real account state, or application
lifespan is used. Direct route + middleware calls avoid an httpx dependency.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]

PREAMBLE = r'''
import asyncio
import json
import os
import uuid
from pathlib import Path
from unittest import mock
from fastapi import FastAPI
from fastapi import HTTPException, Request, Response, UploadFile
from fastapi.responses import JSONResponse
from urllib.parse import parse_qs
import io
import app_common
import middleware
from core import config as cfg
from core.qr_login import QrLoginForbidden, QrLoginInvalid, QrLoginNotFound
from routers import qr_credentials as routes
from routers import credentials

JOB = "a" * 32
JOB_TOKEN = "fake-capability-" + "b" * 48
BASE = "/api/credentials/qr"
PNG = b"\x89PNG\r\n\x1a\nFAKE-QR-ONLY"

class FakeManager:
    def __init__(self):
        self.active = False
        self.calls = []
        self.request_id = None
        self.start_callback = None
        self.cleanup_pending = False
    def result(self, secret=False):
        data = {"job_id":JOB, "state":"qr_ready" if self.active else "cancelled", "expires_at":4102444800,
                "qr_revision":1, "image_ready":self.active, "cleanup_complete":not self.active,
                "remaining_seconds":300, "code":"", "message":"fake QR", "saved":False}
        if secret: data["job_token"] = JOB_TOKEN
        return data
    def start(self, client_request_id):
        self.calls.append(("start",client_request_id))
        if self.start_callback: self.start_callback()
        self.request_id = client_request_id
        self.active = True
        return self.result(True)
    def check(self, job_id, job_token):
        if job_id != JOB: raise QrLoginNotFound()
        if job_token != JOB_TOKEN: raise QrLoginForbidden()
    def status(self, job_id, job_token):
        self.check(job_id, job_token)
        self.calls.append(("status",job_id))
        return self.result()
    def qr(self, job_id, job_token):
        self.check(job_id, job_token)
        self.calls.append(("image",job_id))
        return PNG, self.result()
    def verification(self, job_id, job_token):
        self.check(job_id, job_token)
        self.calls.append(("verification-image",job_id))
        return (PNG if self.active else None), self.result()
    def verification_action(self, job_id, job_token, action_id, revision, action, points):
        self.check(job_id, job_token)
        self.calls.append(("verification-action",job_id,action_id,revision,action,points))
        return {**self.result(),"state":"verification_required","image_ready":False,
                "verification_ready":True,"verification_busy":True,"verification_revision":1,
                "verification_width":320,"verification_height":240}
    def refresh(self, job_id, job_token):
        self.check(job_id,job_token)
        self.calls.append(("refresh",job_id))
        return self.result(True)
    def cancel(self, job_id, job_token):
        self.check(job_id,job_token)
        self.calls.append(("cancel",job_id))
        self.active = False
        return self.result()
    def retry_cleanup(self, job_id, job_token):
        self.check(job_id,job_token)
        self.calls.append(("retry-cleanup",job_id))
        return {**self.result(),"state":"cleanup_pending","code":"cleanup_pending",
                "saved":True,"cleanup_complete":False,"cleanup_attempts":3,"cleanup_retrying":True}
    def recover_cleanup(self):
        if not self.active or not self.cleanup_pending:
            raise QrLoginInvalid('cleanup_not_pending')
        self.calls.append(('recover-cleanup',JOB))
        return {**self.result(True),'state':'cleanup_pending','code':'cleanup_pending',
                'saved':True,'cleanup_complete':False,'cleanup_attempts':3,'cleanup_retrying':False}
    def is_active(self): return self.active
    def shutdown(self, timeout=6):
        self.calls.append(("shutdown",timeout)); self.active=False; return True

manager = FakeManager()
routes._qr_manager = manager
app = FastAPI()
app.include_router(routes.router)
app.include_router(credentials.router)
middleware.register_middleware(app)

async def _request(method, path, *, data=None, headers=None, raw=None, client_ip="127.0.0.1"):
    request_headers = {"host":"127.0.0.1:8000"}
    if method != "GET":
        request_headers.update({"origin":"http://127.0.0.1:8000", "x-spark-qr-request":"1", "content-type":"application/json"})
    for key,value in (headers or {}).items():
        if value is None: request_headers.pop(key.lower(),None)
        else: request_headers[key.lower()] = value
    body = raw if raw is not None else json.dumps(data or {}).encode()
    route_path, _, query = path.partition("?")
    scope={"type":"http","asgi":{"version":"3.0","spec_version":"2.4"},"http_version":"1.1","method":method,
           "scheme":"http","path":route_path,"raw_path":route_path.encode(),"query_string":query.encode(),
           "root_path":"","headers":[(k.encode(),v.encode()) for k,v in request_headers.items()],
           "client":(client_ip,43210),"server":("127.0.0.1",8000)}
    async def receive():
        return {"type":"http.request","body":body,"more_body":False}
    req=Request(scope,receive)
    async def endpoint(current):
        response=Response()
        capability=current.headers.get('x-qr-job-token','')
        try:
            if route_path == BASE+'/start':
                result=routes.start_qr(current,response,body=data or {})
            elif route_path == BASE+'/status':
                result=routes.qr_status(current,response,job_id=parse_qs(query).get('job_id',[''])[0],job_token=capability)
            elif route_path == BASE+'/image':
                result=routes.qr_image(current,job_id=parse_qs(query).get('job_id',[''])[0],job_token=capability)
            elif route_path == BASE+'/verification-image':
                result=routes.verification_image(current,job_id=parse_qs(query).get('job_id',[''])[0],job_token=capability)
            elif route_path == BASE+'/verification-action':
                result=routes.verification_action(current,response,body=data or {},job_token=capability)
            elif route_path == BASE+'/retry-cleanup':
                result=routes.retry_cleanup_qr(current,response,body=data or {},job_token=capability)
            elif route_path == BASE+'/recover-cleanup':
                result=routes.recover_cleanup_qr(current,response,body=data or {})
            elif route_path in {BASE+'/refresh',BASE+'/cancel'}:
                handler=routes.refresh_qr if route_path.endswith('/refresh') else routes.cancel_qr
                result=handler(current,response,body=data or {},job_token=capability)
            elif route_path in {'/api/upload-state','/api/credentials/upload'}:
                content=body.split(b'\r\n\r\n',1)[1].rsplit(b'\r\n--',1)[0]
                result=await credentials.api_upload_state(file=UploadFile(filename='state.json',file=io.BytesIO(content)))
            else:
                raise AssertionError('unknown test route')
            if isinstance(result,Response): return result
            response_headers={k:v for k,v in response.headers.items() if k.lower() not in {'content-length','content-type'}}
            return JSONResponse(result,status_code=response.status_code,headers=response_headers)
        except HTTPException as exc:
            return JSONResponse({'detail':exc.detail},status_code=exc.status_code)
    async def guarded(current):
        return await middleware.local_request_guard(current,endpoint)
    result=await middleware.cache_policy(req,guarded)
    payload=result.body
    response_headers=dict(result.headers)
    try: parsed=json.loads(payload)
    except Exception: parsed=None
    return result.status_code,parsed,response_headers,payload

def request(method,path,**kwargs): return asyncio.run(_request(method,path,**kwargs))
def start(**kwargs): return request("POST",BASE+"/start",data={"request_id":str(uuid.uuid4())},**kwargs)
def valid_state():
    return json.dumps({"cookies":[{"name":"sessionid","value":"PRIVATE-STATE-SENTINEL","domain":".douyin.com","path":"/","expires":4102444800}],"origins":[]}).encode()
def assert_safe(data):
    encoded=json.dumps(data)
    for value in ("PRIVATE-STATE-SENTINEL","storage_state","cookies",str(cfg.DATA_DIR)):
        assert value not in encoded,value
'''


class QrRouteIntegrationTests(unittest.TestCase):
    def isolated(self, body):
        with tempfile.TemporaryDirectory(prefix="qr-route-tests-") as temporary:
            folder = Path(temporary)
            env = os.environ.copy()
            env.update({
                "DATA_DIR": str(folder / "data"), "ENV_FILE_PATH": str(folder / "missing.env"),
                "INSTANCE_LOCK_PATH": str(folder / "server.pid"), "STATE_FILE_PATH": "",
                "HOST": "127.0.0.1", "PORT": "8000",
                "SPARKKEEPER_BACKGROUND_TASKS": "0", "SPARKKEEPER_NO_ROOT_STATE_FALLBACK": "1",
                "COOKIE_EXTRACT_MODE": "local-client", "QR_PUBLIC_ORIGIN": "",
                "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1",
            })
            code = PREAMBLE + "\n" + textwrap.dedent(body) + "\nprint('qr-route-isolated-pass')\n"
            result = subprocess.run([sys.executable, "-B", "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertIn("qr-route-isolated-pass", result.stdout)

    def test_local_start_needs_no_management_credentials(self):
        self.isolated("""
        status,data,headers,_=start()
        assert status==200
        assert data['job_token']==JOB_TOKEN
        assert 'set-cookie' not in headers
        assert len(manager.calls)==1 and manager.calls[0][0]=='start'
        assert_safe(data)
        """)

    def test_cleanup_retry_requires_same_origin_capability_and_exact_body(self):
        self.isolated("""
        endpoint=BASE+'/retry-cleanup';body={'job_id':JOB}
        for bad in ({'Origin':'https://evil.example.invalid'},{'Origin':None},{'X-Spark-QR-Request':None}):
            assert request('POST',endpoint,data=body,headers={**bad,'X-QR-Job-Token':JOB_TOKEN})[0]==403
        assert request('POST',endpoint,data=body)[0]==403
        assert request('POST',endpoint,data=body,headers={'X-QR-Job-Token':'wrong'})[0]==403
        assert request('POST',endpoint,data={**body,'force':True},headers={'X-QR-Job-Token':JOB_TOKEN})[0]==400
        assert not manager.calls
        status,data,headers,_=request('POST',endpoint,data=body,headers={'X-QR-Job-Token':JOB_TOKEN})
        assert status==200 and headers['cache-control']=='no-store'
        assert data['status']=='cleanup_pending' and data['saved'] is True
        assert data['cleanup_complete'] is False and data['cleanup_retrying'] is True
        assert data['cleanup_attempts']==3 and 'job_token' not in data
        assert manager.calls==[('retry-cleanup',JOB)]
        assert_safe(data)
        """)

    def test_successful_commit_records_when_session_was_checked(self):
        self.isolated("""
        with mock.patch('core.automation.restart_browser',return_value=True), \\
                mock.patch.object(routes,'update_runtime') as updated:
            assert routes._persist_qr_state(valid_state(),{'job_id':JOB}) is True
        fields=updated.call_args.kwargs
        assert fields['session_status']=='ok'
        assert isinstance(fields['session_checked_at'],float) and fields['session_checked_at']>0
        """)

    def test_recovery_after_reload_requires_local_origin_and_empty_body(self):
        self.isolated("""
        endpoint=BASE+'/recover-cleanup';manager.active=True;manager.cleanup_pending=True
        for bad in ({'Origin':'https://evil.example.invalid'},{'Origin':None},{'X-Spark-QR-Request':None}):
            assert request('POST',endpoint,data={},headers=bad)[0]==403
        assert request('POST',endpoint,data={'job_id':JOB})[0]==400
        assert request('POST',endpoint,data={'force':True})[0]==400
        assert not manager.calls
        status,data,headers,_=request('POST',endpoint,data={})
        assert status==200 and headers['cache-control']=='no-store'
        assert data['job_id']==JOB and data['job_token']==JOB_TOKEN
        assert data['status']=='cleanup_pending' and data['cleanup_complete'] is False
        assert data['saved'] is True and data['cleanup_retrying'] is False
        assert manager.calls==[('recover-cleanup',JOB)]
        assert_safe(data)
        recovered=data['job_token']
        assert request('POST',BASE+'/retry-cleanup',data={'job_id':JOB},headers={'X-QR-Job-Token':recovered})[0]==200
        assert manager.calls[-1]==('retry-cleanup',JOB)
        """)

    def test_recovery_does_not_expose_scanning_jobs_or_create_an_engine(self):
        self.isolated("""
        endpoint=BASE+'/recover-cleanup'
        assert request('POST',endpoint,data={})[0]==409
        manager.active=True
        assert request('POST',endpoint,data={})[0]==409
        assert not manager.calls
        routes._qr_manager=None
        assert request('POST',endpoint,data={})[0]==409
        assert routes._qr_manager is None
        """)

    def test_verification_image_requires_capability_and_is_never_cached(self):
        self.isolated("""
        manager.active=True
        endpoint=BASE+'/verification-image?job_id='+JOB
        assert request('GET',endpoint)[0]==403
        assert request('GET',endpoint,headers={'X-QR-Job-Token':'wrong'})[0]==403
        status,data,headers,png=request('GET',endpoint,headers={'X-QR-Job-Token':JOB_TOKEN})
        assert status==200 and png==PNG
        assert headers['cache-control']=='no-store'
        manager.active=False
        status,data,headers,_=request('GET',endpoint,headers={'X-QR-Job-Token':JOB_TOKEN})
        assert status==202 and headers['cache-control']=='no-store'
        assert_safe(data)
        """)

    def test_verification_actions_enforce_origin_marker_and_exact_body(self):
        self.isolated("""
        body={'job_id':JOB,'action_id':str(uuid.uuid4()),'verification_revision':1,
              'action':'click','points':[{'x':.5,'y':.5,'t':0}]}
        endpoint=BASE+'/verification-action'
        for bad in ({'Origin':'https://evil.example.invalid'},{'Origin':None},{'X-Spark-QR-Request':None}):
            assert request('POST',endpoint,data=body,headers={**bad,'X-QR-Job-Token':JOB_TOKEN})[0]==403
        assert request('POST',endpoint,data={**body,'selector':'body'},headers={'X-QR-Job-Token':JOB_TOKEN})[0]==400
        assert not manager.calls
        """)

    def test_verification_action_queues_with_202_and_safe_status(self):
        self.isolated("""
        manager.active=True
        body={'job_id':JOB,'action_id':str(uuid.uuid4()),'verification_revision':1,
              'action':'drag','points':[{'x':.2,'y':.5,'t':0},{'x':.8,'y':.5,'t':500}]}
        status,data,headers,_=request('POST',BASE+'/verification-action',data=body,headers={'X-QR-Job-Token':JOB_TOKEN})
        assert status==202 and headers['cache-control']=='no-store'
        assert data['status']=='verification_required' and data['verification_busy'] is True
        assert manager.calls[0][0]=='verification-action'
        assert_safe(data)
        """)

    def test_mutations_reject_foreign_origin_missing_marker_and_missing_origin(self):
        self.isolated("""
        for headers in ({'Origin':'https://evil.example.invalid'}, {'X-Spark-QR-Request':None}, {'Origin':None}, {'Origin':'null'}):
            assert start(headers=headers)[0] == 403, headers
        assert not manager.calls
        """)

    def test_non_loopback_peer_cannot_start_or_read_a_job(self):
        self.isolated("""
        assert start(client_ip='203.0.113.8')[0]==403
        assert request('GET',BASE+'/status?job_id='+JOB,client_ip='203.0.113.8',
                       headers={'X-QR-Job-Token':JOB_TOKEN})[0]==403
        assert not manager.calls
        assert start()[0]==200
        """)

    def test_forwarded_external_peer_and_cross_site_metadata_are_rejected(self):
        self.isolated("""
        for headers in ({'X-Real-IP':'203.0.113.8'}, {'X-Forwarded-For':'203.0.113.8'},
                        {'Forwarded':'for=203.0.113.8'}, {'X-Forwarded-Host':'evil.example.invalid'},
                        {'Sec-Fetch-Site':'cross-site'}, {'Sec-Fetch-Site':'same-site'}):
            assert start(headers=headers)[0]==403,headers
        assert not manager.calls
        assert start()[0]==200
        """)

    def test_explicit_public_origin_proxy_keeps_qr_capability_and_origin_guards(self):
        self.isolated("""
        os.environ['QR_PUBLIC_ORIGIN']='https://console.example.com'
        proxy={'Host':'console.example.com','X-Forwarded-Proto':'https',
               'X-Forwarded-For':'203.0.113.10','Origin':'https://console.example.com'}
        status,data,headers,_=start(headers=proxy)
        assert status==200 and data['job_token']==JOB_TOKEN
        assert 'set-cookie' not in headers
        assert request('GET',BASE+'/status?job_id='+JOB,headers=proxy)[0]==403
        protected={**proxy,'X-QR-Job-Token':JOB_TOKEN}
        assert request('GET',BASE+'/status?job_id='+JOB,headers=protected)[0]==200
        for change in ({'Origin':'https://other.example.invalid'},
                       {'Origin':None},{'X-Spark-QR-Request':None}):
            assert request('POST',BASE+'/refresh',data={'job_id':JOB},
                           headers={**protected,**change})[0]==403
        assert request('POST',BASE+'/refresh',data={'job_id':JOB},headers=protected)[0]==200
        assert start()[0]==200
        assert start(headers={**proxy,'Host':'console.example.com:443'})[0]==200
        """)

    def test_public_proxy_rejects_wrong_authority_scheme_and_invalid_forwarding(self):
        self.isolated("""
        os.environ['QR_PUBLIC_ORIGIN']='https://console.example.com'
        proxy={'Host':'console.example.com','X-Forwarded-Proto':'https',
               'X-Forwarded-For':'203.0.113.10','Origin':'https://console.example.com'}
        for change in ({'Host':'other.example.invalid'}, {'Host':'console.example.com:80'},
                       {'X-Forwarded-Proto':'http'}, {'X-Forwarded-Proto':'https,http'},
                       {'X-Forwarded-Proto':None}, {'X-Forwarded-Host':'other.example.invalid'},
                       {'X-Forwarded-Host':'console.example.com:444'}, {'Forwarded':'for=203.0.113.10'},
                       {'X-Forwarded-For':'not-an-ip'}, {'X-Real-IP':'203.0.113.10,127.0.0.1'}):
            assert start(headers={**proxy,**change})[0]==403,change
        assert not manager.calls
        assert start(headers={**proxy,'X-Forwarded-Host':'console.example.com',
                              'X-Real-IP':'203.0.113.10',
                              'X-Forwarded-For':'203.0.113.10, 198.51.100.2'})[0]==200
        """)

    def test_public_proxy_never_trusts_an_external_raw_peer(self):
        self.isolated("""
        os.environ['QR_PUBLIC_ORIGIN']='https://console.example.com'
        proxy={'Host':'console.example.com','X-Forwarded-Proto':'https',
               'X-Forwarded-For':'127.0.0.1','X-Real-IP':'127.0.0.1',
               'Origin':'https://console.example.com'}
        assert start(client_ip='203.0.113.10',headers=proxy)[0]==403
        assert request('GET',BASE+'/status?job_id='+JOB,client_ip='203.0.113.10',
                       headers={**proxy,'X-QR-Job-Token':JOB_TOKEN})[0]==403
        assert not manager.calls
        """)

    def test_public_origin_nondefault_port_and_invalid_config_fail_closed(self):
        self.isolated("""
        os.environ['QR_PUBLIC_ORIGIN']='https://console.example.com:8443'
        proxy={'Host':'console.example.com:8443','X-Forwarded-Proto':'https',
               'Origin':'https://console.example.com:8443'}
        for change in ({'Host':'console.example.com'},
                       {'Origin':'https://console.example.com'},
                       {'X-Forwarded-Host':'console.example.com:443'}):
            assert start(headers={**proxy,**change})[0]==403,change
        assert not manager.calls
        assert start(headers=proxy)[0]==200
        calls=len(manager.calls)
        for invalid in ('https://console.example.com:8443/path',
                        'https://user:pass@console.example.com:8443',
                        'https://console.example.com:0'):
            os.environ['QR_PUBLIC_ORIGIN']=invalid
            assert start(headers=proxy)[0]==403
        assert len(manager.calls)==calls
        assert start()[0]==200
        """)

    def test_dns_rebinding_host_is_rejected_even_when_origin_matches(self):
        self.isolated("""
        status=start(headers={'Host':'evil.example.invalid:8000','Origin':'http://evil.example.invalid:8000'})[0]
        assert status in (400,403),status
        assert not manager.calls
        """)

    def test_busy_run_prevents_start_and_never_releases_other_lock(self):
        self.isolated("""
        app_common.run_lock.acquire()
        try:
            assert start()[0] == 409
            assert app_common.run_lock.locked()
            assert not manager.calls
        finally: app_common.run_lock.release()
        """)

    def test_start_registry_is_inside_short_running_lock(self):
        self.isolated("""
        manager.start_callback=lambda: (_ for _ in ()).throw(AssertionError('start registration not locked')) if not app_common.run_lock.locked() else None
        response=start()
        assert response[0] in (200,202),response
        assert not app_common.run_lock.locked()
        assert str(uuid.UUID(manager.request_id))==manager.request_id
        assert_safe(response[1])
        assert response[1]['job_token']==JOB_TOKEN
        """)

    def test_repeated_request_id_keeps_same_canonical_idempotency_key(self):
        self.isolated("""
        request_id=str(uuid.uuid4())
        for value in (request_id,request_id.upper()):
            status,data,_,_=request('POST',BASE+'/start',data={'request_id':value})
            assert status==200 and data['job_token']==JOB_TOKEN
        assert manager.calls==[('start',request_id),('start',request_id)]
        assert not app_common.run_lock.locked()
        """)

    def test_request_id_must_be_uuid(self):
        self.isolated("""
        for value in ('', 'not-a-uuid', '../state.json'):
            status=request('POST',BASE+'/start',data={'request_id':value})[0]
            assert status in (400,422),status
        assert not manager.calls
        """)

    def test_status_and_image_require_job_capability_and_do_not_leak_state(self):
        self.isolated("""
        assert start()[0] in (200,202)
        for endpoint in ('status','image'):
            assert request('GET',BASE+'/'+endpoint+'?job_id='+JOB,headers={'X-QR-Job-Token':'wrong'})[0] in (401,403)
        status,data,headers,_=request('GET',BASE+'/status?job_id='+JOB,headers={'X-QR-Job-Token':JOB_TOKEN})
        assert status==200
        assert_safe(data)
        assert 'job_token' not in data
        assert 'no-store' in headers.get('cache-control','')
        status,_,headers,payload=request('GET',BASE+'/image?job_id='+JOB,headers={'X-QR-Job-Token':JOB_TOKEN})
        assert status==200 and payload==PNG
        assert headers['content-type'].startswith('image/png')
        assert 'no-store' in headers.get('cache-control','')
        """)

    def test_cancel_requires_origin_and_preserves_previous_state(self):
        self.isolated("""
        cfg.STATE_PATH.parent.mkdir(parents=True,exist_ok=True)
        cfg.STATE_PATH.write_bytes(b'old-state')
        start()
        assert request('POST',BASE+'/cancel',data={'job_id':JOB},headers={'X-QR-Job-Token':JOB_TOKEN,'Origin':'https://evil.example.invalid'})[0]==403
        assert manager.is_active()
        response=request('POST',BASE+'/cancel',data={'job_id':JOB},headers={'X-QR-Job-Token':JOB_TOKEN})
        assert response[0]==200,response
        assert not manager.is_active()
        assert cfg.STATE_PATH.read_bytes()==b'old-state'
        """)

    def test_active_check_is_lazy(self):
        self.isolated("""
        routes._qr_manager=None
        with mock.patch.object(routes,'_get_qr_manager',side_effect=AssertionError('unexpected engine creation')):
            assert routes.qr_login_active() is False
        """)

    def test_commit_waits_for_run_lock_without_overwriting_or_unlocking(self):
        self.isolated("""
        cfg.STATE_PATH.parent.mkdir(parents=True,exist_ok=True)
        cfg.STATE_PATH.write_bytes(b'old-state')
        app_common.run_lock.acquire()
        try:
            assert routes._persist_qr_state(valid_state(),{'job_id':JOB}) is False
            assert app_common.run_lock.locked()
            assert cfg.STATE_PATH.read_bytes()==b'old-state'
        finally: app_common.run_lock.release()
        """)

    def test_commit_validates_state_before_replacing_existing_credentials(self):
        self.isolated("""
        cfg.STATE_PATH.parent.mkdir(parents=True,exist_ok=True)
        cfg.STATE_PATH.write_bytes(b'old-state')
        for raw in (b'bad-json',json.dumps({'cookies':[{'name':'other','value':'x','domain':'example.invalid','path':'/'}]}).encode()):
            try: routes._persist_qr_state(raw,{'job_id':JOB})
            except Exception: pass
            else: raise AssertionError('invalid state accepted')
            assert cfg.STATE_PATH.read_bytes()==b'old-state'
            assert not app_common.run_lock.locked()
        """)

    def test_commit_external_path_and_context_invalidation_order(self):
        self.isolated("""
        external=cfg.DATA_DIR.parent/'external-state.json'
        os.environ['STATE_FILE_PATH']=str(external)
        external.write_bytes(b'old-external-state')
        cfg.STATE_PATH.parent.mkdir(parents=True,exist_ok=True)
        cfg.STATE_PATH.write_bytes(b'old-default-state')
        def close_browser(**kwargs):
            assert 0 < kwargs.get('timeout',0) <= 15
            assert app_common.run_lock.locked()
            assert external.read_bytes()==b'old-external-state'
            return True
        with mock.patch('core.automation.restart_browser',side_effect=close_browser) as closed:
            result=routes._persist_qr_state(valid_state(),{'job_id':JOB})
        assert result is not False
        closed.assert_called_once()
        assert json.loads(external.read_bytes())==json.loads(valid_state())
        assert cfg.STATE_PATH.read_bytes()==b'old-default-state'
        assert cfg.get_valid_state_path()==external
        assert not app_common.run_lock.locked()
        """)

    def test_failed_context_invalidation_preserves_state(self):
        self.isolated("""
        cfg.STATE_PATH.parent.mkdir(parents=True,exist_ok=True)
        cfg.STATE_PATH.write_bytes(b'old-state')
        with mock.patch('core.automation.restart_browser',return_value=False):
            try: result=routes._persist_qr_state(valid_state(),{'job_id':JOB})
            except Exception: pass
            else: assert result is False
        assert cfg.STATE_PATH.read_bytes()==b'old-state'
        assert not app_common.run_lock.locked()
        """)

    def test_external_invalid_state_does_not_fall_back_to_old_default(self):
        self.isolated("""
        cfg.STATE_PATH.parent.mkdir(parents=True,exist_ok=True)
        cfg.STATE_PATH.write_bytes(valid_state())
        external=cfg.DATA_DIR.parent/'external-state.json'
        external.write_bytes(b'invalid')
        os.environ['STATE_FILE_PATH']=str(external)
        assert cfg.get_valid_state_path() is None
        """)

    def test_existing_upload_is_blocked_during_qr_login(self):
        self.isolated("""
        cfg.STATE_PATH.parent.mkdir(parents=True,exist_ok=True)
        cfg.STATE_PATH.write_bytes(b'old-state')
        manager.active=True
        boundary='unit-qr-boundary'
        upload=(('--'+boundary+'\\r\\nContent-Disposition: form-data; name="file"; filename="state.json"\\r\\nContent-Type: application/json\\r\\n\\r\\n').encode()+valid_state()+('\\r\\n--'+boundary+'--\\r\\n').encode())
        for endpoint in ('/api/upload-state','/api/credentials/upload'):
            status,_,_,_=request('POST',endpoint,raw=upload,headers={'Content-Type':'multipart/form-data; boundary='+boundary})
            assert status==409,status
        assert cfg.STATE_PATH.read_bytes()==b'old-state'
        assert not app_common.run_lock.locked()
        """)

    def test_existing_upload_aliases_remain_usable_when_qr_idle(self):
        self.isolated("""
        boundary='unit-qr-boundary'
        upload=(('--'+boundary+'\\r\\nContent-Disposition: form-data; name="file"; filename="state.json"\\r\\nContent-Type: application/json\\r\\n\\r\\n').encode()+valid_state()+('\\r\\n--'+boundary+'--\\r\\n').encode())
        with mock.patch('core.automation.restart_browser',return_value=True):
            for endpoint in ('/api/upload-state','/api/credentials/upload'):
                status,data,_,_=request('POST',endpoint,raw=upload,headers={'Content-Type':'multipart/form-data; boundary='+boundary})
                assert status==200,(status,data)
                assert data['ok'] is True
        assert json.loads(cfg.STATE_PATH.read_bytes())==json.loads(valid_state())
        assert not app_common.run_lock.locked()
        """)


if __name__ == "__main__":
    unittest.main()
