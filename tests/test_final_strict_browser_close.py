"""Exercise the real cleanup wrapper with fake resources on their owner thread."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]
PREAMBLE = r'''
import json
import threading
from types import SimpleNamespace
from unittest import mock
from fastapi import HTTPException
import app_common
from core import automation as A, browser as B, config
from core.browser_errors import BrowserSessionUnavailable
from routers import credentials, qr_credentials
qr_credentials.qr_login_active = lambda: False

drivers = []
class Driver:
    def __init__(self):
        self.thread = threading.get_ident()
        self.stops = 0
        self.fail_stop = False
        self.browser = Browser(self)
        self.chromium = SimpleNamespace(launch=lambda **kwargs: self.browser)
    def stop(self):
        assert threading.get_ident() == self.thread
        self.stops += 1
        if self.fail_stop:
            raise RuntimeError('synthetic stop failure')
class Browser:
    def __init__(self, driver):
        self.driver = driver
        self.closes = 0
        self.fail_close = False
    def close(self):
        assert threading.get_ident() == self.driver.thread
        self.closes += 1
        if self.fail_close:
            raise RuntimeError('synthetic close failure')
    def new_context(self, **kwargs):
        return SimpleNamespace(new_page=lambda: object())
def start():
    driver = Driver()
    drivers.append(driver)
    return driver
B.sync_playwright = mock.Mock(return_value=SimpleNamespace(start=start))
A.sync_playwright = mock.Mock(side_effect=AssertionError('no new prewarm while cleanup pending'))
def prepare(owned=True):
    if owned:
        owner = B.open_browser(use_state=False)
        p, browser, context, page = owner.__enter__()
    else:
        owner = None
        p = start()
        browser, context, page = p.browser, object(), object()
    A._prewarmed_browser = {'p': p, 'browser': browser, 'context': context,
                            'page': page, 'ready': True, 'created_at': 1}
    if owner is not None:
        A._prewarmed_browser['context_manager'] = owner
    return A._prewarmed_browser

old_raw = b'synthetic previous credential bytes'
new_raw = json.dumps({'cookies': [{'name': 'synthetic', 'value': 'new', 'domain': '.example.invalid', 'path': '/'}], 'origins': []}).encode()
config.STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
config.STATE_PATH.write_bytes(old_raw)
def reject_upload():
    try:
        credentials._persist_uploaded_state(new_raw)
    except HTTPException as error:
        assert error.status_code == 500
    else:
        raise AssertionError('failed close permitted credential replacement')
    assert config.STATE_PATH.read_bytes() == old_raw
    assert not app_common.run_lock.locked()
'''


class StrictBrowserCloseTests(unittest.TestCase):
    def isolated(self, body):
        temporary_root = ROOT / 'dist' / 'test-tmp'
        temporary_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='strict-close-', dir=temporary_root) as directory:
            base = Path(directory)
            env = dict(os.environ)
            env.update(DATA_DIR=str(base / 'data'), ENV_FILE_PATH=str(base / 'missing.env'),
                       INSTANCE_LOCK_PATH=str(base / 'instance.pid'), STATE_FILE_PATH='',
                       SPARKKEEPER_BACKGROUND_TASKS='0', SESSION_WARN_EMAIL_ENABLED='off',
                       LOGIN_GATE='on', PASSWORD_PUBLIC_LOGIN='off', AUTH_TOKEN='synthetic-token',
                       PYTHONUTF8='1', PYTHONDONTWRITEBYTECODE='1', TEMP=str(base), TMP=str(base))
            result = subprocess.run([sys.executable, '-B', '-c', PREAMBLE + textwrap.dedent(body)],
                                    cwd=ROOT, env=env, capture_output=True, text=True,
                                    encoding='utf-8', errors='replace', timeout=15)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_owned_close_failure_preserves_credential_and_retries_same_resources(self):
        self.isolated('''
        owned = A._run_in_pw_thread(prepare)
        driver = drivers[0]
        driver.browser.fail_close = True
        reject_upload()
        assert A._prewarmed_browser is owned and not owned['ready']
        assert driver.stops == 0, 'keep driver alive so failed browser close can be retried'
        driver.browser.fail_close = False
        assert credentials._persist_uploaded_state(new_raw)['ok'] is True
        assert config.STATE_PATH.read_bytes() == new_raw
        assert driver.browser.closes == 2 and driver.stops == 1
        assert A._prewarmed_browser is None
        A._pw_executor.shutdown(wait=True)
        ''')

    def test_driver_failure_retries_only_unfinished_step(self):
        self.isolated('''
        owned = A._run_in_pw_thread(prepare)
        driver = drivers[0]
        driver.fail_stop = True
        reject_upload()
        assert A._prewarmed_browser is owned and not owned['ready']
        driver.fail_stop = False
        assert credentials._persist_uploaded_state(new_raw)['ok'] is True
        assert driver.browser.closes == 1 and driver.stops == 2
        assert A._prewarmed_browser is None
        A._pw_executor.shutdown(wait=True)
        ''')

    def test_direct_prewarm_close_failure_is_also_retained(self):
        self.isolated('''
        owned = A._run_in_pw_thread(prepare, False)
        driver = drivers[0]
        driver.browser.fail_close = True
        reject_upload()
        assert A._prewarmed_browser is owned
        driver.browser.fail_close = False
        assert credentials._persist_uploaded_state(new_raw)['ok'] is True
        assert driver.browser.closes == 2 and driver.stops == 1
        A._pw_executor.shutdown(wait=True)
        ''')

    def test_failed_cleanup_cannot_be_reused_or_overwritten_by_prewarm(self):
        self.isolated('''
        owned = A._run_in_pw_thread(prepare)
        driver = drivers[0]
        driver.browser.fail_close = True
        reject_upload()
        try:
            A._run_in_pw_thread(A._get_prewarmed_page)
        except RuntimeError as error:
            assert '清理未完成' in str(error)
        else:
            raise AssertionError('unclosed resource allowed a cold-start fallback')
        assert A._run_in_pw_thread(A._prewarm_browser_impl) is False
        assert A._prewarmed_browser is owned
        assert len(drivers) == 1
        A.sync_playwright.assert_not_called()
        driver.browser.fail_close = False
        assert A.restart_browser(timeout=None) is True
        A._pw_executor.shutdown(wait=True)
        ''')

    def test_successful_owned_cleanup_closes_each_resource_once(self):
        self.isolated('''
        A._run_in_pw_thread(prepare)
        assert credentials._persist_uploaded_state(new_raw)['ok'] is True
        assert A.restart_browser(timeout=None) is True
        assert drivers[0].browser.closes == drivers[0].stops == 1
        assert A._prewarmed_browser is None
        A._pw_executor.shutdown(wait=True)
        ''')


if __name__ == '__main__':
    unittest.main()
