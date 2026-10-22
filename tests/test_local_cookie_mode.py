"""Local-export-only cloud API regressions, each in a temporary child process.

No actual .env, state file, login browser or remote upload is used. Subprocess
isolation prevents module/env caching from changing other regression tests.
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
TOKEN = "local-export-test-management-token-not-real"
PREAMBLE = """
import json
from unittest import mock
from fastapi import HTTPException
import app_common
from routers import credentials as routes

def rejected(code, action):
    try:
        action()
    except HTTPException as error:
        assert error.status_code == code, (error.status_code, code)
        return error
    raise AssertionError('expected HTTP rejection')

def safe(data):
    text = json.dumps(data, ensure_ascii=False)
    for field in ('ticket', 'cookies', 'storage_state', 'path', 'screenshot', 'auth_token'):
        assert field not in data, field
    assert 'local-export-test-management-token-not-real' not in text
    return data
"""


class LocalCookieModeTests(unittest.TestCase):
    def isolated(self, body):
        with tempfile.TemporaryDirectory(prefix="local-only-mode-tests-") as folder:
            base = Path(folder)
            env = os.environ.copy()
            env.update({"DATA_DIR": str(base / "data"), "ENV_FILE_PATH": str(base / "missing.env"),
                        "INSTANCE_LOCK_PATH": str(base / "server.pid"), "AUTH_TOKEN": TOKEN,
                        "LOGIN_GATE": "on", "HOST": "127.0.0.1", "URL_ACCESS_TOKEN": "",
                        "URL_PASSWORD_SALT": "", "URL_PASSWORD_HASH": "", "STATE_FILE_PATH": "",
                        "PYTHONIOENCODING": "utf-8"})
            code = PREAMBLE + "\n" + textwrap.dedent(body) + "\nprint('isolated-local-mode-pass')\n"
            result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env,
                                    capture_output=True, text=True, encoding="utf-8", timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertIn("isolated-local-mode-pass", result.stdout)

    def test_old_extract_requires_authentication_and_returns_gone(self):
        self.isolated("""
        rejected(401, lambda: routes.api_credentials_extract(token=''))
        rejected(401, lambda: routes.api_credentials_extract(token='wrong-dummy-token'))
        rejected(410, lambda: routes.api_credentials_extract(token='local-export-test-management-token-not-real'))
        assert not app_common.run_lock.locked()
        """)

    def test_old_extract_cannot_start_a_cloud_browser_thread_or_process(self):
        self.isolated("""
        with mock.patch('threading.Thread.start', side_effect=AssertionError('cloud thread started')), \\
             mock.patch('subprocess.Popen', side_effect=AssertionError('cloud process started')), \\
             mock.patch('core.browser.open_browser', side_effect=AssertionError('cloud browser started')):
            rejected(410, lambda: routes.api_credentials_extract(token='local-export-test-management-token-not-real'))
        assert not hasattr(routes, '_extract_worker')
        assert not app_common.run_lock.locked()
        """)

    def test_status_requires_authentication_and_reports_no_upload(self):
        self.isolated("""
        rejected(401, lambda: routes.api_credentials_extract_status(token=''))
        result = safe(routes.api_credentials_extract_status(token='local-export-test-management-token-not-real'))
        assert result['mode'] == 'local-client'
        assert result['automatic_upload'] is False
        assert result['status'] == 'idle' and result['running'] is False and result['count'] == 0
        """)

    def test_old_screenshot_endpoint_is_gone_and_reads_no_disk_data(self):
        self.isolated("""
        rejected(401, lambda: routes.api_credentials_extract_screenshot(token=''))
        with mock.patch('pathlib.Path.exists', side_effect=AssertionError('screenshot checked')), \\
             mock.patch('pathlib.Path.read_bytes', side_effect=AssertionError('screenshot read')), \\
             mock.patch('builtins.open', side_effect=AssertionError('private file read')):
            rejected(410, lambda: routes.api_credentials_extract_screenshot(token='local-export-test-management-token-not-real'))
        """)

    def test_helper_info_has_only_local_mode_metadata(self):
        self.isolated("""
        rejected(401, lambda: routes.api_local_helper_info(token=''))
        result = safe(routes.api_local_helper_info(token='local-export-test-management-token-not-real'))
        assert result['mode'] == 'local-client'
        assert result['helper_url'] == 'http://127.0.0.1:17653'
        assert result['automatic_upload'] is False and result['automatic_close'] is True
        """)

    def test_local_mode_metadata_does_not_read_cookie_files_or_claim_server_login(self):
        self.isolated("""
        with mock.patch('pathlib.Path.read_bytes', side_effect=AssertionError('state file read')), \\
             mock.patch('pathlib.Path.read_text', side_effect=AssertionError('state file read')), \\
             mock.patch('builtins.open', side_effect=AssertionError('private file read')):
            status = safe(routes.api_credentials_extract_status(token='local-export-test-management-token-not-real'))
            info = safe(routes.api_local_helper_info(token='local-export-test-management-token-not-real'))
        assert status['count'] == 0
        assert status.get('has_state') is not True
        assert info['automatic_upload'] is False
        """)

    def test_disabled_extract_does_not_release_a_lock_owned_by_other_tasks(self):
        self.isolated("""
        app_common.run_lock.acquire()
        try:
            rejected(410, lambda: routes.api_credentials_extract(token='local-export-test-management-token-not-real'))
            assert app_common.run_lock.locked()
        finally:
            app_common.run_lock.release()
        """)


if __name__ == "__main__":
    unittest.main()
