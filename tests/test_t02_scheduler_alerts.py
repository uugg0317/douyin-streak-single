"""T02：历史失败不跳过发送日，升级告警独立去重；全部使用临时数据和假邮件。"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / ".review-work" / "t02-tests"

PREAMBLE = '''
from unittest import mock
from core.send_day import business_now
from core import scheduler, runtime
import session_service as session
session.socket.gethostname=lambda:'isolated-test-host'
session.socket.gethostbyname=lambda host:'127.0.0.1'
session._send_email=mock.Mock(side_effect=AssertionError('测试必须显式打桩，禁止真实邮件'))
FAIL={'dry_run':False,'ok':[],'failed':[{'name':'测试好友','reason':'假失败'}],
      'skipped':[],'deferred':[]}
TODAY=business_now().strftime('%Y-%m-%d')
'''


class SchedulerAlertTests(unittest.TestCase):
    def isolated(self, body):
        WORK.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="case-", dir=WORK) as temporary:
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith("SMTP_") and key not in {
                       "STATE_FILE_PATH", "URL_ACCESS_TOKEN", "URL_PASSWORD_SALT", "URL_PASSWORD_HASH",
                   }}
            folder = Path(temporary)
            env.update({
                "DATA_DIR": str(folder / "data"), "ENV_FILE_PATH": str(folder / "missing.env"),
                "INSTANCE_LOCK_PATH": str(folder / "pid"), "HOST": "127.0.0.1",
                "LOGIN_GATE": "on", "AUTH_TOKEN": "t02-isolated-management-token-not-real",
                "PASSWORD_PUBLIC_LOGIN": "off", "SPARKKEEPER_BACKGROUND_TASKS": "0",
                "SESSION_WARN_EMAIL_ENABLED": "on", "FAILURE_BREAKER_THRESHOLD": "3",
                "QR_PUBLIC_ORIGIN": "", "WHITELIST_IPS": "",
                "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1",
            })
            code = PREAMBLE + "\n" + textwrap.dedent(body) + "\nprint('T02隔离验证通过')\n"
            completed = subprocess.run([sys.executable, "-B", "-c", code], cwd=ROOT, env=env,
                                       text=True, encoding="utf-8", capture_output=True, timeout=20)
            self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
            self.assertIn("T02隔离验证通过", completed.stdout)

    def test_nine_failure_days_never_skip_due_to_historical_count_or_cooldown(self):
        self.isolated('''
        runtime.update_runtime(consecutive_failures=3,auto_paused_until=10**12)
        scheduler.load_config=lambda:{'auto_run_enabled':True,'jitter_minutes':0}
        calls=[]
        scheduler._run_func=lambda:calls.append(1)
        scheduler._scheduler=None
        timeline=''
        for day in range(9):
            before=len(calls)
            scheduler._daily_job()
            timeline+='S' if len(calls)>before else 'X'
            runtime.record_run(dict(FAIL))
        assert timeline=='SSSSSSSSS',timeline
        assert runtime.load_runtime()['consecutive_failures']==12
        assert runtime.load_runtime()['auto_paused_until']==0
        ''')

    def test_auto_run_disabled_still_prevents_sending(self):
        self.isolated('''
        scheduler.load_config=lambda:{'auto_run_enabled':False,'jitter_minutes':0}
        scheduler._run_func=mock.Mock()
        scheduler._daily_job()
        scheduler._run_func.assert_not_called()
        ''')

    def test_upgraded_warning_is_not_swallowed_by_regular_warning_and_deduplicates(self):
        self.isolated('''
        runtime.update_runtime(consecutive_failures=3)
        session._send_email=mock.Mock()
        session.notify_after_run(FAIL)
        assert session._send_email.call_count==2
        assert runtime.load_runtime()['session_warn_date']==TODAY
        assert runtime.load_runtime()['failure_warning_date']==TODAY
        subject=session._send_email.call_args_list[1].args[1]
        assert '升级告警' in subject and '连续 3 轮未成功' in subject
        assert '天未成功' not in subject
        session.notify_after_run(FAIL)
        assert session._send_email.call_count==2
        ''')

    def test_regular_warning_already_sent_does_not_prevent_upgrade(self):
        self.isolated('''
        runtime.update_runtime(consecutive_failures=4,session_warn_date=TODAY)
        session._send_email=mock.Mock()
        session.notify_after_run(FAIL)
        session._send_email.assert_called_once()
        assert '升级告警' in session._send_email.call_args.args[1]
        ''')

    def test_failed_upgrade_does_not_spend_day_and_can_retry(self):
        self.isolated('''
        runtime.update_runtime(consecutive_failures=3,session_warn_date=TODAY)
        session._send_email=mock.Mock(side_effect=RuntimeError('假投递失败'))
        session.notify_after_run(FAIL)
        assert runtime.load_runtime().get('failure_warning_date')!=TODAY
        session._send_email=mock.Mock()
        session.notify_after_run(FAIL)
        session._send_email.assert_called_once()
        assert runtime.load_runtime()['failure_warning_date']==TODAY
        ''')

    def test_regular_warning_failure_does_not_swallow_upgrade_attempt(self):
        self.isolated('''
        runtime.update_runtime(consecutive_failures=3)
        session._send_email=mock.Mock(side_effect=[RuntimeError('普通预警假失败'),None])
        session.notify_after_run(FAIL)
        assert session._send_email.call_count==2
        assert runtime.load_runtime().get('session_warn_date')!=TODAY
        assert runtime.load_runtime()['failure_warning_date']==TODAY
        ''')

    def test_email_switch_off_disables_both_warning_paths(self):
        self.isolated('''
        import os
        os.environ['SESSION_WARN_EMAIL_ENABLED']='off'
        runtime.update_runtime(consecutive_failures=100)
        session._send_email=mock.Mock()
        session.notify_after_run(FAIL)
        session._send_email.assert_not_called()
        assert not runtime.load_runtime().get('failure_warning_date')
        ''')

    def test_below_threshold_and_dry_run_do_not_escalate(self):
        self.isolated('''
        runtime.update_runtime(consecutive_failures=2,session_warn_date=TODAY)
        session._send_email=mock.Mock()
        session.notify_after_run(FAIL)
        session._send_email.assert_not_called()
        runtime.update_runtime(consecutive_failures=5)
        session.notify_after_run({**FAIL,'dry_run':True})
        session._send_email.assert_not_called()
        ''')

    def test_upgrade_can_be_sent_on_next_day_without_resetting_failures(self):
        self.isolated('''
        runtime.update_runtime(consecutive_failures=6,session_warn_date=TODAY,
                               failure_warning_date='2000-01-01')
        session._send_email=mock.Mock()
        session.notify_after_run(FAIL)
        session._send_email.assert_called_once()
        assert runtime.load_runtime()['failure_warning_date']==TODAY
        assert runtime.load_runtime()['consecutive_failures']==6
        ''')

    def test_concurrent_upgrade_checks_only_send_one_email(self):
        self.isolated('''
        import threading
        runtime.update_runtime(consecutive_failures=3)
        session._send_email=mock.Mock()
        workers=[threading.Thread(target=session._send_failure_warning) for _ in range(4)]
        for worker in workers:worker.start()
        for worker in workers:worker.join(3)
        assert not any(worker.is_alive() for worker in workers)
        session._send_email.assert_called_once()
        ''')

    def test_old_pause_is_ignored_by_status_and_cleared_on_next_runtime_save(self):
        self.isolated('''
        import json
        runtime.DATA_DIR.mkdir(parents=True,exist_ok=True)
        runtime.RUNTIME_PATH.write_text(json.dumps({'auto_paused_until':10**12,
            'consecutive_failures':7}),encoding='utf-8')
        from routers import pages
        pages.get_valid_state_path=lambda:None
        assert pages.api_status(token=__import__('auth_service').AUTH_TOKEN)['auto_paused_until']==0
        runtime.set_running(True)
        saved=json.loads(runtime.RUNTIME_PATH.read_text(encoding='utf-8'))
        assert saved['auto_paused_until']==0
        assert saved['consecutive_failures']==7
        ''')

    def test_background_disabled_still_does_not_start_scheduler_or_account_jobs(self):
        self.isolated('''
        import asyncio
        import app as app_module
        async def verify():
            with mock.patch.object(app_module,'_check_auth_config'), \\
                 mock.patch.object(app_module,'_acquire_instance_lock'), \\
                 mock.patch.object(app_module.scheduler,'configure') as configure, \\
                 mock.patch.object(app_module,'_start_watchdog') as watchdog, \\
                 mock.patch.object(app_module,'migrate_legacy_email_config') as migrate:
                async with app_module.lifespan(app_module.app):
                    configure.assert_not_called()
                    watchdog.assert_not_called()
                    migrate.assert_not_called()
        asyncio.run(verify())
        session._send_email.assert_not_called()
        ''')


if __name__ == '__main__':
    unittest.main()
