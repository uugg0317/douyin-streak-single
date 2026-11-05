"""T05 管理Cookie端口隔离：仅合成会话与本机临时测试服务，不连接平台。"""
from __future__ import annotations
import hashlib
import http.cookiejar
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock
import _isolation  # noqa: F401  先隔离数据目录，再导入项目模块
from auth_cookies import cookie_names
import test_auth_origin_boundary as origin_tests
ROOT=Path(__file__).resolve().parents[1]

class CookieNameTests(unittest.TestCase):
    def test_default_names_and_current_environment_port(self):
        with mock.patch.dict(os.environ,{},clear=True):
            self.assertEqual(cookie_names(),('session_auth_token_8000','device_auth_token_8000'))
        with mock.patch.dict(os.environ,{'PORT':'8123'}):
            self.assertEqual(cookie_names(),('session_auth_token_8123','device_auth_token_8123'))
        self.assertNotEqual(cookie_names(8123),cookie_names(8124))
    def test_port_is_canonical_and_not_a_public_proxy_origin(self):
        self.assertEqual(cookie_names(' 08123 '),cookie_names(8123))
        with mock.patch.dict(os.environ,{'PORT':'8123','QR_PUBLIC_ORIGIN':'https://console.example.com'}):
            self.assertEqual(cookie_names(),('session_auth_token_8123','device_auth_token_8123'))
    def test_invalid_ports_are_rejected_with_no_silent_fallback(self):
        for value in ('',' ','garbage','8123,8124',0,-1,65536,True,'8.0'):
            with self.subTest(port=value),self.assertRaises(ValueError):cookie_names(value)

class CookieBoundaryTests(unittest.TestCase):
    def isolated(self,body):origin_tests.AuthOriginBoundaryTests.isolated(self,body)
    def test_foreign_port_and_old_names_do_not_authenticate(self):
        self.isolated("""
        valid=device()
        for name in ('device_auth_token','session_auth_token','device_auth_token_8001','session_auth_token_8001'):
            assert request('GET','/api/review-state',cookie=f'{name}={valid}')[0]==401
        assert request('GET','/api/review-state',cookie=f'{DEVICE_COOKIE}={valid}')[0]==200
        """)
    def test_port_scoped_cookie_cannot_use_cli_exception(self):
        self.isolated("""
        valid=device()
        for value in (valid,'invalid-synthetic-value'):
            assert request('POST','/api/review-mutate',cookie=f'{DEVICE_COOKIE}={value}',headers={'X-Auth-Token':TOKEN})[0]==403
        assert not effects
        assert request('POST','/api/review-mutate',headers={'X-Auth-Token':TOKEN})[0]==200
        assert request('POST','/api/review-mutate',cookie=f'{DEVICE_COOKIE}={valid}',headers=ORIGIN)[0]==200
        assert len(effects)==2
        """)
    def test_logout_only_clears_current_names_and_revokes_both_presented_tokens(self):
        self.isolated("""
        short=session();long=device();other=device()
        cookie=f'{SESSION_COOKIE}={short}; {DEVICE_COOKIE}={long}; device_auth_token_8001={other}'
        status,_,headers=request('POST','/api/auth/logout',cookie=cookie,headers=ORIGIN)
        assert status==200
        from http.cookies import SimpleCookie
        deleted=set()
        for key,value in headers:
            if key==b'set-cookie':
                parsed=SimpleCookie();parsed.load(value.decode())
                for name,item in parsed.items():
                    assert item['max-age']=='0'
                    deleted.add(name)
        assert deleted=={SESSION_COOKIE,DEVICE_COOKIE}
        assert not auth_service._session_valid(short)
        assert not auth_service._session_valid(long)
        assert auth_service._session_valid(other)
        """)
    def test_https_proxy_uses_backend_port_and_keeps_security_attributes(self):
        self.isolated("""
        os.environ['QR_PUBLIC_ORIGIN']='https://console.example.com'
        headers={'Host':'console.example.com','Origin':'https://console.example.com',
                 'X-Forwarded-Proto':'https','X-Real-IP':'198.51.100.7'}
        from http.cookies import SimpleCookie
        for endpoint,body,expected,ttl in (
            ('/api/auth/login',{'token':TOKEN},SESSION_COOKIE,auth_service.SESSION_TTL),
            ('/api/auth/url-token-login',{'access_token':'','password':PASSWORD},DEVICE_COOKIE,auth_service.DEVICE_AUTH_TTL)):
            status,_,response_headers=request('POST',endpoint,body=body,headers=headers)
            assert status==200
            parsed=SimpleCookie()
            for key,value in response_headers:
                if key==b'set-cookie':parsed.load(value.decode())
            assert set(parsed)=={expected} and expected.endswith('_8000')
            item=parsed[expected]
            assert item['secure'] and item['httponly'] and item['samesite']=='lax' and item['path']=='/'
            assert int(item['max-age'])==ttl
        """)

class CookieJarTwoPortTests(unittest.TestCase):
    def test_shared_cookie_jar_keeps_both_logins_and_logout_is_instance_scoped(self):
        processes=[];password='synthetic-two-port-password-only';salt='synthetic-two-port-salt'
        with tempfile.TemporaryDirectory(prefix='cookie-two-port-') as temporary:
            folder=Path(temporary);probes=[]
            try:
                for _ in range(2):
                    probe=socket.socket();probe.bind(('127.0.0.1',0));probes.append(probe)
                ports=[probe.getsockname()[1] for probe in probes]
            finally:
                for probe in probes:probe.close()
            cookies=http.cookiejar.CookieJar();opener=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))
            def request(index,method,path,body=None):
                origin=f'http://127.0.0.1:{ports[index]}';raw=json.dumps(body).encode() if body is not None else None
                headers={'Origin':origin}
                if method.upper() not in {'GET','HEAD','OPTIONS'}:headers['X-Requested-With']='sparkkeeper'
                if raw is not None:headers['Content-Type']='application/json'
                req=urllib.request.Request(origin+path,data=raw,method=method,headers=headers)
                with opener.open(req,timeout=3) as response:return response.status,json.load(response)
            try:
                for index,port in enumerate(ports):
                    item=folder/str(index);item.mkdir();env=os.environ.copy()
                    env.update({'DATA_DIR':str(item/'data'),'ENV_FILE_PATH':str(item/'missing.env'),
                        'INSTANCE_LOCK_PATH':str(item/'server.pid'),'STATE_FILE_PATH':'','HOST':'127.0.0.1',
                        'PORT':str(port),'LOGIN_GATE':'on','PASSWORD_PUBLIC_LOGIN':'on',
                        'AUTH_TOKEN':'synthetic-cookie-port-token-'+str(index)+'-123456789','URL_ACCESS_TOKEN':'',
                        'URL_PASSWORD_SALT':salt,'URL_PASSWORD_HASH':hashlib.sha256((password+salt).encode()).hexdigest(),
                        'QR_PUBLIC_ORIGIN':'','WHITELIST_IPS':'','SPARKKEEPER_BACKGROUND_TASKS':'0',
                        'PYTHONDONTWRITEBYTECODE':'1','PYTHONUTF8':'1'})
                    process=subprocess.Popen([sys.executable,'-B','app.py'],cwd=ROOT,env=env,
                        stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                    processes.append(process);deadline=time.monotonic()+15
                    while time.monotonic()<deadline:
                        if process.poll() is not None:raise RuntimeError('临时Cookie隔离服务启动失败')
                        try:
                            if request(index,'GET','/api/health')[1].get('ok'):break
                        except OSError:time.sleep(.05)
                    else:raise RuntimeError('临时Cookie隔离服务启动超时')
                    self.assertEqual(request(index,'POST','/api/auth/login',{'token':env['AUTH_TOKEN']})[0],200)
                    self.assertEqual(request(index,'POST','/api/auth/url-token-login',{'access_token':'','password':password})[0],200)
                expected={name for port in ports for name in cookie_names(port)}
                self.assertEqual({cookie.name for cookie in cookies},expected)
                for index in (0,1):self.assertEqual(request(index,'GET','/api/status')[0],200)
                probe_request=urllib.request.Request(f'http://127.0.0.1:{ports[0]}/api/status');cookies.add_cookie_header(probe_request)
                presented=probe_request.get_header('Cookie')
                self.assertTrue(all(name+'=' in presented for name in expected))
                self.assertEqual(request(0,'POST','/api/auth/logout',{})[0],200)
                self.assertEqual({cookie.name for cookie in cookies},set(cookie_names(ports[1])))
                with self.assertRaises(urllib.error.HTTPError) as rejected:request(0,'GET','/api/status')
                self.assertEqual(rejected.exception.code,401);rejected.exception.close()
                self.assertEqual(request(1,'GET','/api/status')[0],200)
            finally:
                for process in processes:
                    if process.poll() is None:
                        if os.name=='nt':subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=10,check=False)
                        else:process.terminate()
                        try:process.wait(timeout=5)
                        except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)

if __name__=='__main__':unittest.main()