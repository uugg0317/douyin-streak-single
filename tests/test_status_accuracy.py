"""Credential status regressions in isolated processes with temporary fake data."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
PREAMBLE = """
import json
import socket
from unittest import mock
from fastapi import HTTPException
import app_common
import session_service
from core import runtime, automation
from routers import pages, credentials, qr_credentials

def forbidden(*args, **kwargs):
    raise AssertionError('network/browser/background execution forbidden')

socket.create_connection = forbidden
socket.socket.connect = forbidden
automation._pw_executor.submit = forbidden

def upload(raw=None):
    if raw is None:
        raw = json.dumps({'cookies': [{'name': 'synthetic-cookie', 'value': 'fake-test-value',
                                      'domain': '.example.invalid', 'path': '/'}], 'origins': []}).encode()
    class FakeUpload:
        async def read(self, size):
            return raw[:size]
    # The endpoint only awaits the upload read. An immediately available fake
    # avoids creating an event loop or even Windows asyncio's loopback self-pipe.
    pending = credentials.api_upload_state(
        file=FakeUpload())
    try:
        pending.send(None)
    except StopIteration as completed:
        return completed.value
    finally:
        pending.close()
    raise AssertionError('unexpected asynchronous upload dependency')

def rejected(code, action):
    try:
        action()
    except HTTPException as exc:
        assert exc.status_code == code, (exc.status_code, code)
        return
    raise AssertionError('expected rejection')

def run_check(health):
    with mock.patch.object(session_service.automation, 'check_session_health', return_value=health), \\
         mock.patch.object(session_service, '_send_warning', return_value=False):
        session_service.run_session_check()
"""


class StatusAccuracyTests(unittest.TestCase):
    def isolated(self, body):
        with tempfile.TemporaryDirectory(prefix="status-accuracy-tests-") as folder:
            base = Path(folder)
            env = os.environ.copy()
            env.update({
                "DATA_DIR": str(base / "data"), "ENV_FILE_PATH": str(base / "missing.env"),
                "INSTANCE_LOCK_PATH": str(base / "server.pid"),
                "HOST": "127.0.0.1", "STATE_FILE_PATH": "", "QR_PUBLIC_ORIGIN": "",
                "SPARKKEEPER_BACKGROUND_TASKS": "0", "SPARKKEEPER_NO_ROOT_STATE_FALLBACK": "1",
                "SESSION_WARN_EMAIL_ENABLED": "off", "PYTHONIOENCODING": "utf-8",
                "PYTHONDONTWRITEBYTECODE": "1",
            })
            code = PREAMBLE + "\n" + textwrap.dedent(body) + "\nprint('isolated-status-pass')\n"
            result = subprocess.run([sys.executable, "-B", "-c", code], cwd=ROOT, env=env,
                                    capture_output=True, text=True, encoding="utf-8", timeout=25)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertIn("isolated-status-pass", result.stdout)

    def test_new_runtime_has_no_fabricated_check_time(self):
        self.isolated("""
        state = runtime.load_runtime()
        assert state['session_status'] == 'unknown'
        assert state['session_checked_at'] is None
        """)

    def test_legacy_runtime_without_check_time_stays_unchecked(self):
        self.isolated("""
        runtime.RUNTIME_PATH.parent.mkdir(parents=True, exist_ok=True)
        runtime.RUNTIME_PATH.write_text(json.dumps({'session_status': 'ok'}), encoding='utf-8')
        with mock.patch.object(pages, 'get_valid_state_path', return_value=None):
            result = pages.api_status()
        assert result['session_status'] == 'ok'
        assert result['session_checked_at'] is None
        """)

    def test_status_returns_check_time_and_keeps_background_switch(self):
        self.isolated("""
        runtime.update_runtime(session_status='ok', session_checked_at=12345.25)
        with mock.patch.object(pages, 'get_valid_state_path', return_value=None), \\
             mock.patch.object(pages.scheduler, 'background_tasks_enabled', return_value=False):
            result = pages.api_status()
        assert result['session_checked_at'] == 12345.25
        assert result['background_tasks_enabled'] is False
        """)

    def test_real_success_confirms_session_and_refreshes_check_time(self):
        self.isolated("""
        with mock.patch.object(runtime.time, 'time', return_value=12345.25):
            runtime.record_run({'ok': ['synthetic friend'], 'failed': [], 'dry_run': False})
        state = runtime.load_runtime()
        assert state['session_status'] == 'ok'
        assert state['session_checked_at'] == 12345.25
        """)

    def test_partial_real_success_still_has_confirmed_check_time(self):
        self.isolated("""
        with mock.patch.object(runtime.time, 'time', return_value=12345.25):
            runtime.record_run({'ok': ['synthetic friend'], 'failed': [{'name': 'another fake friend'}]})
        state = runtime.load_runtime()
        assert state['session_status'] == 'partial'
        assert state['session_checked_at'] == 12345.25
        """)

    def test_real_logged_out_confirms_expiry_and_refreshes_check_time(self):
        self.isolated("""
        with mock.patch.object(runtime.time, 'time', return_value=12345.25):
            runtime.record_run({'ok': [], 'logged_out': True, 'dry_run': False})
        state = runtime.load_runtime()
        assert state['session_status'] == 'expired'
        assert state['session_checked_at'] == 12345.25
        """)

    def test_dry_runs_do_not_change_credential_evidence(self):
        self.isolated("""
        for result in ({'ok': ['synthetic friend']}, {'logged_out': True}, {'failed': ['fake']}):
            runtime.update_runtime(session_status='unknown', session_checked_at=None)
            runtime.record_run(dict(result, dry_run=True))
            state = runtime.load_runtime()
            assert state['session_status'] == 'unknown'
            assert state['session_checked_at'] is None
        assert len(state['history']) == 3
        """)

    def test_empty_runs_do_not_claim_validity_or_refresh_previous_check(self):
        self.isolated("""
        runtime.record_run({'ok': [], 'failed': [], 'deferred': [], 'dry_run': False})
        assert runtime.load_runtime()['session_status'] == 'unknown'
        assert runtime.load_runtime()['session_checked_at'] is None
        runtime.update_runtime(session_status='expired', session_checked_at=111.0)
        runtime.record_run({'ok': [], 'failed': [], 'skipped': ['fake already sent friend']})
        state = runtime.load_runtime()
        assert state['session_status'] == 'expired'
        assert state['session_checked_at'] == 111.0
        """)

    def test_task_failure_without_login_evidence_does_not_refresh_time(self):
        self.isolated("""
        runtime.update_runtime(session_status='ok', session_checked_at=111.0)
        runtime.record_run({'ok': [], 'failed': [{'name': '_system', 'error': 'synthetic page failure'}]})
        state = runtime.load_runtime()
        assert state['session_status'] == 'failed'
        assert state['session_checked_at'] == 111.0
        """)

    def test_health_check_persists_confirmed_validity(self):
        self.isolated("""
        with mock.patch.object(session_service.time, 'time', return_value=23456.75):
            run_check({'logged_in': True, 'infra_error': False,
                       'cookie': {'has_state': True, 'has_session': True}})
        state = runtime.load_runtime()
        assert state['session_status'] == 'ok'
        assert state['session_checked_at'] == 23456.75
        assert not app_common.run_lock.locked()
        """)

    def test_health_check_persists_confirmed_expiry(self):
        self.isolated("""
        with mock.patch.object(session_service.time, 'time', return_value=23456.75):
            run_check({'logged_in': False, 'infra_error': False,
                       'cookie': {'has_state': True, 'has_session': True}})
        state = runtime.load_runtime()
        assert state['session_status'] == 'expired'
        assert state['session_checked_at'] == 23456.75
        assert not app_common.run_lock.locked()
        """)

    def test_infrastructure_error_does_not_fabricate_expiry_or_check_time(self):
        self.isolated("""
        runtime.update_runtime(session_status='ok', session_checked_at=111.0)
        run_check({'logged_in': False, 'infra_error': True, 'reason': 'synthetic browser unavailable'})
        state = runtime.load_runtime()
        assert state['session_status'] == 'ok'
        assert state['session_checked_at'] == 111.0
        assert not app_common.run_lock.locked()
        """)

    def test_incomplete_health_result_does_not_claim_confirmation(self):
        self.isolated("""
        run_check({'infra_error': False, 'cookie': {'has_state': True}})
        assert runtime.load_runtime()['session_status'] == 'unknown'
        assert runtime.load_runtime()['session_checked_at'] is None
        """)

    def test_busy_check_skips_without_releasing_someone_elses_lock(self):
        self.isolated("""
        assert app_common.run_lock.acquire(blocking=False)
        try:
            with mock.patch.object(session_service.automation, 'check_session_health', side_effect=forbidden):
                session_service.run_session_check()
            assert app_common.run_lock.locked()
            assert runtime.load_runtime()['session_checked_at'] is None
        finally:
            app_common.run_lock.release()
        """)

    def test_health_check_holds_credential_lock_until_result_is_saved(self):
        self.isolated("""
        def health():
            assert app_common.run_lock.locked()
            rejected(409, upload)
            return {'logged_in': True, 'infra_error': False,
                    'cookie': {'has_state': True, 'has_session': True}}
        def save(**fields):
            assert app_common.run_lock.locked()
            runtime.update_runtime(**fields)
        with mock.patch.object(session_service.automation, 'check_session_health', side_effect=health), \\
             mock.patch.object(session_service, 'update_runtime', side_effect=save):
            session_service.run_session_check()
        assert not app_common.run_lock.locked()
        assert runtime.load_runtime()['session_status'] == 'ok'
        """)

    def test_check_lock_is_released_on_exception_and_disabled_schedule(self):
        self.isolated("""
        with mock.patch.object(session_service.automation, 'check_session_health', side_effect=RuntimeError('fake')):
            session_service.run_session_check()
        assert not app_common.run_lock.locked()
        from core import config
        with mock.patch.object(config, 'load_config', return_value={'auto_run_enabled': False}), \\
             mock.patch.object(session_service.automation, 'check_session_health', side_effect=forbidden):
            session_service.run_session_check()
        assert not app_common.run_lock.locked()
        assert runtime.load_runtime()['session_checked_at'] is None
        """)

    def test_manual_upload_resets_previous_credential_confirmation(self):
        self.isolated("""
        runtime.update_runtime(session_status='ok', session_checked_at=111.0)
        with mock.patch.object(qr_credentials, 'qr_login_active', return_value=False), \\
             mock.patch.object(automation, 'restart_browser', return_value=True):
            assert upload()['ok']
        state = runtime.load_runtime()
        assert state['session_status'] == 'unknown'
        assert state['session_checked_at'] is None
        assert credentials._state_write_path().is_file()
        assert not app_common.run_lock.locked()
        """)

    def test_failed_upload_does_not_clear_confirmation_for_unchanged_credential(self):
        self.isolated("""
        runtime.update_runtime(session_status='ok', session_checked_at=111.0)
        rejected(400, lambda: upload(b'not-json'))
        with mock.patch.object(qr_credentials, 'qr_login_active', return_value=False), \\
             mock.patch.object(automation, 'restart_browser', return_value=False):
            rejected(500, upload)
        with mock.patch.object(qr_credentials, 'qr_login_active', return_value=False), \\
             mock.patch.object(automation, 'restart_browser', return_value=True), \\
             mock.patch.object(credentials, 'atomic_write_bytes', side_effect=OSError('synthetic write failure')):
            rejected(500, upload)
        state = runtime.load_runtime()
        assert state['session_status'] == 'ok'
        assert state['session_checked_at'] == 111.0
        assert not app_common.run_lock.locked()
        """)


if __name__ == "__main__":
    unittest.main()
