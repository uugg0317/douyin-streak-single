"""Synthetic storage-state uploads: validation, atomicity and worker ownership."""
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
import hashlib
import io
import json
import socket
import threading
import time
from pathlib import Path
from unittest import mock
from fastapi import FastAPI, HTTPException, UploadFile
import app_common
from core import automation, config, runtime
from desktop.local_cookie_helper import validate_state
from routers import credentials, qr_credentials

def forbidden(*args, **kwargs):
    raise AssertionError('real browser/network forbidden')
original_connect = socket.socket.connect
def local_only(sock, address):
    if sock.family in (socket.AF_INET, socket.AF_INET6) and address[0] not in {'127.0.0.1', '::1', 'localhost'}:
        forbidden()
    return original_connect(sock, address)
socket.socket.connect = local_only
automation._pw_executor.submit = forbidden
qr_credentials.qr_login_active = lambda: False

def state(**changes):
    cookie = {'name': 'synthetic-cookie', 'value': 'offline-only-value',
              'domain': '.example.invalid', 'path': '/', 'expires': -1,
              'httpOnly': True, 'secure': True, 'sameSite': 'Lax'}
    cookie.update(changes)
    return {'cookies': [cookie], 'origins': []}

def encode(value):
    return json.dumps(value).encode('utf-8')

async def upload(raw=None):
    raw = encode(state()) if raw is None else raw
    return await credentials.api_upload_state(
        file=UploadFile(filename='synthetic-state.json', file=io.BytesIO(raw)),
        token='r12-synthetic-management-token')

def rejected(code, raw):
    try:
        asyncio.run(upload(raw))
    except HTTPException as exc:
        assert exc.status_code == code, (exc.status_code, code)
    else:
        raise AssertionError('expected rejection')

config.STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
old_raw = encode(state(value='previous-offline-value'))
config.STATE_PATH.write_bytes(old_raw)
old_hash = hashlib.sha256(old_raw).hexdigest()
def old_preserved():
    assert hashlib.sha256(config.STATE_PATH.read_bytes()).hexdigest() == old_hash
'''


class CredentialValidationTests(unittest.TestCase):
    def isolated(self, body):
        temporary_root = ROOT / 'dist' / 'test-tmp'
        temporary_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='r12-', dir=temporary_root) as folder:
            base = Path(folder)
            env = os.environ.copy()
            env.update(DATA_DIR=str(base / 'data'), ENV_FILE_PATH=str(base / 'missing.env'),
                       INSTANCE_LOCK_PATH=str(base / 'instance.pid'), STATE_FILE_PATH='',
                       LOGIN_GATE='on', PASSWORD_PUBLIC_LOGIN='off', HOST='127.0.0.1',
                       AUTH_TOKEN='r12-synthetic-management-token', URL_ACCESS_TOKEN='',
                       URL_PASSWORD_SALT='', URL_PASSWORD_HASH='', QR_PUBLIC_ORIGIN='',
                       SPARKKEEPER_BACKGROUND_TASKS='0', SESSION_WARN_EMAIL_ENABLED='off',
                       SPARKKEEPER_NO_ROOT_STATE_FALLBACK='1', TEMP=str(base), TMP=str(base),
                       TMPDIR=str(base), PYTHONUTF8='1', PYTHONDONTWRITEBYTECODE='1')
            result = subprocess.run([sys.executable, '-B', '-c', PREAMBLE + '\n' + textwrap.dedent(body)],
                                    cwd=ROOT, env=env, capture_output=True, text=True,
                                    encoding='utf-8', errors='replace', timeout=20)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_missing_cookie_fields_preserve_state_without_browser_close(self):
        self.isolated('''
        for field in ('name', 'value', 'domain', 'path'):
            candidate = state()
            candidate['cookies'][0].pop(field)
            with mock.patch.object(automation, 'restart_browser', return_value=True) as restart:
                rejected(400, encode(candidate))
                restart.assert_not_called()
            old_preserved()
        ''')

    def test_bad_cookie_field_types_and_values_rejected(self):
        self.isolated('''
        invalid = [{'name': False}, {'name': ''}, {'value': 1}, {'domain': []},
                   {'domain': ''}, {'domain': 'invalid host'}, {'path': None}, {'path': 'relative'},
                   {'expires': True}, {'expires': 'tomorrow'}, {'expires': -2}, {'expires': -0.5},
                   {'expires': float('nan')}, {'expires': float('inf')},
                   {'expires': 10**350}, {'secure': 1}, {'httpOnly': 'true'},
                   {'sameSite': []}, {'sameSite': 'lax'}, {'partitionKey': []}]
        for change in invalid:
            with mock.patch.object(automation, 'restart_browser', return_value=True) as restart:
                rejected(400, encode(state(**change)))
                restart.assert_not_called()
            old_preserved()
        ''')

    def test_origins_and_local_storage_validate_before_mutation(self):
        self.isolated('''
        origins = [{}, {'origin': 'https://', 'localStorage': []},
                   {'origin': 'file:///state', 'localStorage': []},
                   {'origin': 'https://user:secret@example.invalid', 'localStorage': []},
                   {'origin': 'https://example.invalid:99999', 'localStorage': []},
                   {'origin': 'https://example.invalid/path', 'localStorage': []},
                   {'origin': 'https://example.invalid', 'localStorage': {}},
                   {'origin': 'https://example.invalid', 'localStorage': [{'name': 'k'}]},
                   {'origin': 'https://example.invalid', 'localStorage': [{'name': 1, 'value': 'v'}]}]
        for origin in origins:
            candidate = {**state(), 'origins': [origin]}
            with mock.patch.object(automation, 'restart_browser', return_value=True) as restart:
                rejected(400, encode(candidate))
                restart.assert_not_called()
            old_preserved()
        ''')

    def test_invalid_document_and_size_rejected(self):
        self.isolated('''
        raws = [b'not-json', b'\\xff', b'x' * (5 * 1024 * 1024 + 1),
                encode([]), encode({'cookies': []}), encode({'cookies': [{}]}),
                encode({**state(), 'origins': 'bad'}), b'[' * 2000 + b']' * 2000]
        with mock.patch.object(automation, 'restart_browser', return_value=True) as restart:
            for raw in raws:
                rejected(400, raw)
                old_preserved()
            restart.assert_not_called()
        ''')

    def test_manual_import_does_not_assert_account_online(self):
        self.isolated('''
        candidate = state(expires=1, value='')
        candidate['origins'] = [{'origin': 'https://example.invalid',
                                 'localStorage': [{'name': '', 'value': 'offline'}]}]
        runtime.update_runtime(session_status='ok', session_checked_at=1234)
        with mock.patch.object(automation, 'restart_browser', return_value=True) as restart:
            result = asyncio.run(upload(encode(candidate)))
        assert result['ok'] is True
        assert config.STATE_PATH.read_bytes() == encode(candidate)
        status = runtime.load_runtime()
        assert status['session_status'] == 'unknown'
        assert status['session_checked_at'] is None
        restart.assert_called_once()
        assert not app_common.run_lock.locked()
        ''')

    def test_qr_keeps_session_requirement_but_reuses_structure_validation(self):
        self.isolated('''
        for candidate in (state(), state(name='sessionid', domain='.douyin.com', expires=1),
                          state(name='sessionid', domain='.douyin.com', path='relative'),
                          {**state(name='sessionid', domain='.douyin.com'),
                           'origins': [{'origin': 'https://', 'localStorage': []}]}):
            try:
                validate_state(encode(candidate), 1000)
            except ValueError as exc:
                assert str(exc) == 'invalid_state'
            else:
                raise AssertionError('QR accepted invalid or expired session')
        valid = state(name='sessionid', domain='.douyin.com')
        assert validate_state(encode(valid), 1000) == valid
        ''')

    def test_replace_failure_retains_old_bytes_and_status(self):
        self.isolated('''
        runtime.update_runtime(session_status='ok', session_checked_at=1234)
        with mock.patch.object(automation, 'restart_browser', return_value=True), \\
             mock.patch('core.config.os.replace', side_effect=OSError('synthetic disk failure')):
            rejected(500, encode(state()))
        old_preserved()
        status = runtime.load_runtime()
        assert status['session_status'] == 'ok' and status['session_checked_at'] == 1234
        assert not list(config.STATE_PATH.parent.glob('state.json.tmp.*'))
        assert not app_common.run_lock.locked()
        ''')

    def test_close_failure_retains_old_bytes(self):
        self.isolated('''
        with mock.patch.object(automation, 'restart_browser', return_value=False):
            rejected(500, encode(state()))
        old_preserved()
        assert not app_common.run_lock.locked()
        ''')

    def test_qr_and_run_lock_conflicts_preserve_old_bytes(self):
        self.isolated('''
        with mock.patch.object(automation, 'restart_browser', return_value=True) as restart:
            with mock.patch.object(qr_credentials, 'qr_login_active', return_value=True):
                rejected(409, encode(state()))
            app_common.run_lock.acquire()
            try:
                rejected(409, encode(state()))
                assert app_common.run_lock.locked()
            finally:
                app_common.run_lock.release()
            restart.assert_not_called()
        old_preserved()
        ''')

    def test_slow_close_keeps_unrelated_http_responsive(self):
        self.isolated('''
        entered = threading.Event()
        release = threading.Event()
        def close(timeout=None):
            entered.set()
            release.wait(0.7)
            return True
        app = FastAPI()
        app.include_router(credentials.router)
        @app.get('/synthetic-health')
        async def health():
            return {'ok': True}
        async def request(path, method='GET', body=b'', headers=()):
            scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
                     'scheme': 'http', 'method': method, 'path': path, 'raw_path': path.encode(),
                     'root_path': '', 'query_string': b'', 'headers': list(headers),
                     'client': ('127.0.0.1', 43210), 'server': ('127.0.0.1', 8000)}
            async def receive():
                return {'type': 'http.request', 'body': body, 'more_body': False}
            sent = []
            async def send(message):
                sent.append(message)
            await app(scope, receive, send)
            return next(message['status'] for message in sent if message['type'] == 'http.response.start')
        async def scenario():
            boundary = 'r12-synthetic-boundary'
            body = (('--' + boundary + '\\r\\nContent-Disposition: form-data; name="file"; filename="state.json"\\r\\n'
                     'Content-Type: application/json\\r\\n\\r\\n').encode() + encode(state()) + ('\\r\\n--' + boundary + '--\\r\\n').encode())
            headers = [(b'x-auth-token', b'r12-synthetic-management-token'),
                       (b'content-type', ('multipart/form-data; boundary=' + boundary).encode())]
            pending = asyncio.create_task(request('/api/credentials/upload', 'POST', body, headers))
            before = time.monotonic()
            while not entered.is_set():
                await asyncio.sleep(0.001)
            try:
                status = await request('/synthetic-health')
                assert status == 200
                assert time.monotonic() - before < 0.5, 'browser close blocked event loop'
                assert app_common.run_lock.locked()
            finally:
                release.set()
                result = await pending
            assert result == 200
        with mock.patch.object(automation, 'restart_browser', side_effect=close):
            asyncio.run(scenario())
        ''')

    def test_cancelling_request_does_not_release_active_worker_lock(self):
        self.isolated('''
        entered = threading.Event()
        release = threading.Event()
        def close(timeout=None):
            entered.set()
            release.wait(0.7)
            return True
        async def scenario():
            pending = asyncio.create_task(upload())
            while not entered.is_set():
                await asyncio.sleep(0.001)
            try:
                assert not pending.done(), 'upload blocked event loop until browser close completed'
                pending.cancel()
                try:
                    await pending
                except asyncio.CancelledError:
                    pass
                else:
                    raise AssertionError('request was not cancelled')
                assert app_common.run_lock.locked(), 'active worker lost run lock after cancellation'
                try:
                    await upload()
                except HTTPException as exc:
                    assert exc.status_code == 409
                else:
                    raise AssertionError('second upload entered while first worker active')
                old_preserved()
            finally:
                release.set()
            for _ in range(500):
                if not app_common.run_lock.locked():
                    break
                await asyncio.sleep(0.002)
            assert not app_common.run_lock.locked()
            assert config.STATE_PATH.read_bytes() == encode(state())
        with mock.patch.object(automation, 'restart_browser', side_effect=close):
            asyncio.run(scenario())
        ''')

    def test_upload_keeps_async_read_contract_and_waits_for_actual_close(self):
        self.isolated('''
        class AsyncUpload:
            read = mock.AsyncMock(return_value=encode(state()))
        file = AsyncUpload()
        with mock.patch.object(automation, 'restart_browser', return_value=True) as restart:
            asyncio.run(credentials.api_upload_state(file=file, token='r12-synthetic-management-token'))
        file.read.assert_awaited_once_with(5 * 1024 * 1024 + 1)
        restart.assert_called_once_with(timeout=None)
        ''')

    def test_validator_import_is_pure_and_direct_helper_cli_still_loads(self):
        self.isolated('''
        import os
        import subprocess
        import sys
        check = "import sys; import state_validation; assert 'app_common' not in sys.modules; assert 'core.config' not in sys.modules"
        result = subprocess.run([sys.executable, '-B', '-c', check], capture_output=True, timeout=5)
        assert result.returncode == 0, result.stderr
        env = os.environ.copy()
        env.pop('PYTHONPATH', None)
        helper = Path(credentials.__file__).resolve().parents[1] / 'desktop' / 'local_cookie_helper.py'
        result = subprocess.run([sys.executable, '-B', str(helper), '--help'], env=env,
                                cwd=config.STATE_PATH.parent, capture_output=True, timeout=5)
        assert result.returncode == 0, result.stderr
        old_preserved()
        ''')


if __name__ == '__main__':
    unittest.main()
