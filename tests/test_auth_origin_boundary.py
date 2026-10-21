"""Public-edition no-gate and startup contracts, using isolated fake business handlers."""
import unittest
from tests import test_t06_request_guard as boundary

class PublicEditionBoundaryTests(unittest.TestCase):
    isolated = boundary.RequestGuardTests.isolated

    def test_old_auth_environment_cannot_restore_password_gate(self):
        self.isolated("""
        import importlib
        os.environ.update(LOGIN_GATE='on',PASSWORD_PUBLIC_LOGIN='on',AUTH_TOKEN='old-secret',URL_PASSWORD_HASH='legacy-hash')
        importlib.reload(auth)
        auth._check_auth_config()
        assert auth.LOGIN_GATE is False and auth.AUTH_TOKEN == ''
        assert request('GET','/api/test-read',token=None)[0]==200
        assert request('POST','/api/test-mutate',token=None,headers=VALID)[0]==200
        """)

    def test_old_login_endpoints_never_issue_management_cookies(self):
        self.isolated("""
        for method,path in [('GET','/api/auth/challenge'),('POST','/api/auth/login'),('POST','/api/auth/url-token-login'),('POST','/api/auth/logout')]:
            status,payload,headers=request(method,path,token=None,headers=VALID,body={'token':TOKEN,'password':'legacy-secret'})
            assert status==410
            assert not any(name.lower()==b'set-cookie' for name,value in headers)
        """)

    def test_startup_rejects_nonloopback_even_when_legacy_gate_is_enabled(self):
        self.isolated("""
        for host in ('0.0.0.0','::','192.0.2.5','console.example.invalid'):
            os.environ['HOST']=host
            try:auth._check_auth_config()
            except SystemExit:pass
            else:raise AssertionError(host)
        """)

    def test_background_tasks_are_opt_in_without_runtime_files(self):
        self.isolated("""
        from core import scheduler
        os.environ.pop('SPARKKEEPER_BACKGROUND_TASKS',None)
        assert scheduler.background_tasks_enabled() is False
        os.environ['SPARKKEEPER_BACKGROUND_TASKS']='1'
        assert scheduler.background_tasks_enabled() is True
        """)
