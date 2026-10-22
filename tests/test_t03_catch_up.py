"""T03 补跑触发与通知回归：假时钟、假任务、假 SMTP，不访问账号。"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / ".review-work" / "T03"
PREAMBLE = r'''
import asyncio, copy, os, socket, threading
from datetime import datetime, timedelta, timezone
from unittest import mock
import tasks_service as T
import session_service as S
from fastapi import HTTPException
from core.send_day import SEND_TIMEZONE

def forbidden(*args,**kwargs):
    raise AssertionError('T03 测试禁止账号网络、真实浏览器或真实SMTP')
socket.create_connection=forbidden
T.automation._pw_executor.submit=forbidden
S._send_email=forbidden
NOW=datetime(2026,10,14,16,30,tzinfo=timezone.utc).astimezone(SEND_TIMEZONE)
TODAY=NOW.date().isoformat()
runtime={}
config={'auto_run_enabled':True,'schedule_time':'00:00'}
T.load_runtime=lambda:copy.deepcopy(runtime)
T.update_runtime=lambda **fields:runtime.update(fields)
T.load_config=lambda:dict(config)
T.business_now=lambda:NOW
S.load_runtime=T.load_runtime
S.update_runtime=T.update_runtime
S.business_now=lambda:NOW
T.scheduler.background_tasks_enabled=lambda:True
T._qr_login_active=mock.Mock(return_value=False)
T._catch_up_started_date=''
T._start_run=mock.Mock()
T.notify_catch_up_started=mock.Mock(return_value=False)
'''


class CatchUpTests(unittest.TestCase):
    def isolated(self, body):
        WORK.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="catch-up-", dir=WORK) as temporary:
            base = Path(temporary)
            scrub = {"DATA_DIR", "ENV_FILE_PATH", "STATE_FILE_PATH", "INSTANCE_LOCK_PATH",
                     "AUTH_TOKEN", "URL_ACCESS_TOKEN", "URL_PASSWORD_SALT", "URL_PASSWORD_HASH",
                     "LOGIN_GATE", "PASSWORD_PUBLIC_LOGIN", "QR_PUBLIC_ORIGIN",
                     "CATCH_UP_MISSED_RUN", "SESSION_WARN_EMAIL_ENABLED"}
            env = {key: value for key, value in os.environ.items() if key not in scrub}
            env.update(DATA_DIR=str(base / "data"), ENV_FILE_PATH=str(base / "不存在.env"),
                       INSTANCE_LOCK_PATH=str(base / "server.pid"), STATE_FILE_PATH="",
                       AUTH_TOKEN="t03-isolated-token", LOGIN_GATE="on", PASSWORD_PUBLIC_LOGIN="off",
                       SPARKKEEPER_BACKGROUND_TASKS="0", SESSION_WARN_EMAIL_ENABLED="off",
                       PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1", TEMP=str(base), TMP=str(base))
            result = subprocess.run([sys.executable, "-B", "-c", PREAMBLE + "\n" + textwrap.dedent(body)],
                                    cwd=ROOT, env=env, text=True, encoding="utf-8", capture_output=True, timeout=25)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_default_on_starts_once_after_midnight_and_marks_only_after_start(self):
        self.isolated('''
        assert 'CATCH_UP_MISSED_RUN' not in os.environ
        def start(*args,**kwargs):assert 'catch_up_date' not in runtime
        T._start_run.side_effect=start
        assert T.ensure_today_sent() is True
        assert T.ensure_today_sent() is False
        T._start_run.assert_called_once_with(False,require_qr_idle=True)
        assert runtime['catch_up_date']==TODAY
        T.notify_catch_up_started.assert_called_once_with(TODAY)
        ''')

    def test_background_auto_and_catch_up_switches_all_prevent_start(self):
        self.isolated('''
        T.scheduler.background_tasks_enabled=lambda:False
        assert T.ensure_today_sent() is False
        T.scheduler.background_tasks_enabled=lambda:True
        config['auto_run_enabled']=False
        assert T.ensure_today_sent() is False
        config['auto_run_enabled']=True
        for value in ['off','OFF','0','false','no']:
            os.environ['CATCH_UP_MISSED_RUN']=value
            assert T.ensure_today_sent() is False,value
        T._start_run.assert_not_called();T.notify_catch_up_started.assert_not_called()
        assert 'catch_up_date' not in runtime
        ''')

    def test_schedule_two_minute_boundary_and_invalid_configuration(self):
        self.isolated('''
        T.business_now=lambda:NOW.replace(hour=0,minute=1,second=59)
        assert T.ensure_today_sent() is False
        config['schedule_time']='23:59'
        T.business_now=lambda:NOW.replace(hour=23,minute=59,second=59)
        assert T.ensure_today_sent() is False
        for value in ['bad','25:00','12:99','12:30:00']:
            config['schedule_time']=value
            assert T.ensure_today_sent() is False,value
        config['schedule_time']='00:00'
        T.business_now=lambda:NOW.replace(hour=0,minute=2,second=0)
        assert T.ensure_today_sent() is True
        T._start_run.assert_called_once()
        ''')

    def test_today_real_record_or_prior_catch_up_prevents_start(self):
        self.isolated('''
        for fields in [{'last_real_run_date':TODAY},{'catch_up_date':TODAY},
                       {'last_run':{'dry_run':False,'at':'2026-10-14T16:05:00Z'}},
                       {'history':[{'dry_run':False,'at':'2026-10-15T00:05:00+08:00'}]}]:
            runtime.clear();runtime.update(fields)
            assert T.ensure_today_sent() is False,fields
        T._start_run.assert_not_called();T.notify_catch_up_started.assert_not_called()
        ''')

    def test_dry_record_and_previous_day_do_not_block_today(self):
        self.isolated('''
        runtime.update(last_real_run_date='2026-10-14',catch_up_date='2026-10-14',
                       last_run={'dry_run':True,'at':NOW.isoformat()},
                       history=[{'dry_run':False,'at':'2026-10-14T15:59:59Z'}])
        assert T.ensure_today_sent() is True
        T._start_run.assert_called_once()
        ''')

    def test_runtime_lock_and_qr_login_busy_prevent_catch_up(self):
        self.isolated('''
        runtime['running']=True
        assert T.ensure_today_sent() is False
        runtime.clear()
        assert T.app_common.run_lock.acquire(blocking=False)
        try:assert T.ensure_today_sent() is False
        finally:T.app_common.run_lock.release()
        T._qr_login_active.return_value=True
        assert T.ensure_today_sent() is False
        T._start_run.assert_not_called();assert 'catch_up_date' not in runtime
        ''')

    def test_conflict_or_thread_start_failure_never_consumes_catch_up_day(self):
        self.isolated('''
        for error in [HTTPException(status_code=409,detail='并发任务先取得锁'),RuntimeError('假线程未启动')]:
            T._start_run.side_effect=error
            assert T.ensure_today_sent() is False
            assert 'catch_up_date' not in runtime and not T._catch_up_started_date
        T.notify_catch_up_started.assert_not_called()
        T._start_run.side_effect=None
        assert T.ensure_today_sent() is True
        assert runtime['catch_up_date']==TODAY
        ''')

    def test_concurrent_checks_only_start_one_job(self):
        self.isolated('''
        entered=threading.Event();release=threading.Event();answers=[]
        def start(*args,**kwargs):
            entered.set();assert release.wait(3)
        T._start_run.side_effect=start
        leader=threading.Thread(target=lambda:answers.append(T.ensure_today_sent()))
        leader.start();assert entered.wait(2)
        others=[threading.Thread(target=lambda:answers.append(T.ensure_today_sent())) for _ in range(6)]
        for thread in others:thread.start()
        for thread in others:thread.join(2)
        release.set();leader.join(2)
        assert answers.count(True)==1 and answers.count(False)==6,answers
        T._start_run.assert_called_once();T.notify_catch_up_started.assert_called_once()
        ''')

    def test_failed_date_persistence_or_mail_does_not_relaunch_started_job(self):
        self.isolated('''
        T.update_runtime=mock.Mock(side_effect=OSError('假磁盘故障'))
        T.notify_catch_up_started.side_effect=OSError('假SMTP故障')
        assert T.ensure_today_sent() is True
        assert T.ensure_today_sent() is False
        T._start_run.assert_called_once()
        assert T._catch_up_started_date==TODAY
        ''')

    def test_qr_start_between_precheck_and_run_lock_is_rejected_without_marking(self):
        self.isolated('''
        # 恢复真实启动器，但替换 worker；两个 QR 检查之间模拟另一次注册完成。
        import importlib
        T=importlib.reload(T)
        T.load_runtime=lambda:dict(runtime);T.update_runtime=lambda **fields:runtime.update(fields)
        T.load_config=lambda:dict(config);T.business_now=lambda:NOW
        T.scheduler.background_tasks_enabled=lambda:True
        T.notify_catch_up_started=mock.Mock()
        T._qr_login_active=mock.Mock(side_effect=[False,True])
        T._begin_run_locked=mock.Mock(side_effect=AssertionError('不应启动worker'))
        assert T.ensure_today_sent() is False
        assert not T.app_common.run_lock.locked() and 'catch_up_date' not in runtime
        T._begin_run_locked.assert_not_called();T.notify_catch_up_started.assert_not_called()
        ''')

    def test_session_check_calls_catch_up_only_after_releasing_its_lock(self):
        self.isolated('''
        def check():assert T.app_common.run_lock.locked()
        def catch_up():assert not T.app_common.run_lock.locked()
        S._run_session_check_locked=mock.Mock(side_effect=check)
        T.ensure_today_sent=mock.Mock(side_effect=catch_up)
        S.run_session_check()
        S._run_session_check_locked.assert_called_once();T.ensure_today_sent.assert_called_once()
        assert T.app_common.run_lock.acquire(blocking=False)
        try:S.run_session_check();assert T.app_common.run_lock.locked()
        finally:T.app_common.run_lock.release()
        assert T.ensure_today_sent.call_count==1
        ''')

    def test_startup_background_off_creates_no_catch_up_task(self):
        self.isolated('''
        import app
        app.scheduler.background_tasks_enabled=lambda:False
        app._check_auth_config=mock.Mock();app._acquire_instance_lock=mock.Mock()
        app.set_running=mock.Mock();app.update_runtime=mock.Mock()
        app.scheduler.shutdown=mock.Mock();app.qr_credentials_router.shutdown_qr_login=mock.Mock()
        app.ensure_today_sent=mock.Mock()
        async def start_stop():
            async with app.lifespan(app.app):pass
        with mock.patch.object(app.asyncio,'create_task') as create:
            asyncio.run(start_stop())
        create.assert_not_called();app.ensure_today_sent.assert_not_called()
        ''')

    def test_startup_check_runs_off_event_loop_and_shutdown_waits_for_it(self):
        self.isolated('''
        import app
        app.scheduler.background_tasks_enabled=lambda:True
        for name in ['_check_auth_config','_acquire_instance_lock','migrate_legacy_email_config',
                     '_start_watchdog','set_running','update_runtime']:
            setattr(app,name,mock.Mock())
        app.scheduler.configure=mock.Mock();app.scheduler.shutdown=mock.Mock()
        entered=threading.Event();release=threading.Event();completed=threading.Event();owner=threading.get_ident()
        def check(**kwargs):
            assert threading.get_ident()!=owner
            entered.set();assert release.wait(3);completed.set()
        app.ensure_today_sent=mock.Mock(side_effect=check)
        def close_qr():assert completed.is_set()
        app.qr_credentials_router.shutdown_qr_login=mock.Mock(side_effect=close_qr)
        async def start_stop():
            try:
                async with app.lifespan(app.app):
                    assert await asyncio.to_thread(entered.wait,2)
                    # 补跑线程仍在等；事件循环能继续推进，不会阻塞接口处理。
                    await asyncio.sleep(0)
                    assert not completed.is_set()
                    release.set()
            finally:release.set()
        asyncio.run(start_stop())
        assert completed.is_set();app.ensure_today_sent.assert_called_once()
        ''')

    def test_immediate_shutdown_does_not_execute_queued_startup_check(self):
        self.isolated('''
        import app
        app.scheduler.background_tasks_enabled=lambda:True
        for name in ['_check_auth_config','_acquire_instance_lock','migrate_legacy_email_config',
                     '_start_watchdog','set_running','update_runtime']:
            setattr(app,name,mock.Mock())
        app.scheduler.configure=mock.Mock();app.scheduler.shutdown=mock.Mock()
        app.qr_credentials_router.shutdown_qr_login=mock.Mock()
        app.ensure_today_sent=mock.Mock()
        async def start_stop():
            async with app.lifespan(app.app):
                # 不让出事件循环：补跑检查尚未执行就立即关闭。
                pass
        asyncio.run(start_stop())
        app.ensure_today_sent.assert_not_called();T._start_run.assert_not_called()
        ''')

    def test_stop_event_or_midnight_after_checks_prevents_start(self):
        self.isolated('''
        stopped=threading.Event();stopped.set()
        assert T.ensure_today_sent(stop_event=stopped) is False
        stopped.clear()
        T._qr_login_active=mock.Mock(side_effect=lambda:(stopped.set() or False))
        assert T.ensure_today_sent(stop_event=stopped) is False
        T._qr_login_active=mock.Mock(return_value=False)
        T.business_now=mock.Mock(side_effect=[NOW,NOW+timedelta(days=1)])
        assert T.ensure_today_sent() is False
        T._start_run.assert_not_called();assert 'catch_up_date' not in runtime
        ''')

    def test_stop_after_precheck_releases_new_lock_without_starting_worker(self):
        self.isolated('''
        import importlib
        T=importlib.reload(T)
        T.load_runtime=lambda:dict(runtime);T.update_runtime=lambda **fields:runtime.update(fields)
        T.load_config=lambda:dict(config);T.business_now=lambda:NOW
        T.scheduler.background_tasks_enabled=lambda:True
        T.notify_catch_up_started=mock.Mock()
        stopped=threading.Event();reads=[]
        def qr_active():
            reads.append(1)
            if len(reads)==2:stopped.set()
            return False
        T._qr_login_active=qr_active
        T._begin_run_locked=mock.Mock(side_effect=AssertionError('关闭后不得启动worker'))
        assert T.ensure_today_sent(stop_event=stopped) is False
        assert stopped.is_set() and not T.app_common.run_lock.locked()
        assert 'catch_up_date' not in runtime
        T._begin_run_locked.assert_not_called();T.notify_catch_up_started.assert_not_called()
        ''')


class CatchUpNoticeTests(unittest.TestCase):
    isolated = CatchUpTests.isolated

    def test_notice_uses_independent_day_key_and_only_claims_started(self):
        self.isolated('''
        os.environ['SESSION_WARN_EMAIL_ENABLED']='on'
        runtime.update(session_warn_date=TODAY,failure_warning_date=TODAY)
        S.load_email_config=lambda:{'mail_to':'fake@example.invalid'}
        S._send_email=mock.Mock()
        assert S.notify_catch_up_started(TODAY) is True
        assert S.notify_catch_up_started(TODAY) is False
        S._send_email.assert_called_once()
        cfg,subject,body=S._send_email.call_args.args
        assert '已启动今日补跑' in body and '不代表消息已投递' in body
        assert runtime['catch_up_notice_date']==TODAY
        assert runtime['session_warn_date']==runtime['failure_warning_date']==TODAY
        ''')

    def test_notice_switch_off_and_failed_smtp_do_not_mark_day(self):
        self.isolated('''
        S.load_email_config=mock.Mock(return_value={'mail_to':'fake@example.invalid'})
        S._send_email=mock.Mock(side_effect=OSError('假SMTP失败'))
        assert S.notify_catch_up_started(TODAY) is False
        S.load_email_config.assert_not_called();S._send_email.assert_not_called()
        os.environ['SESSION_WARN_EMAIL_ENABLED']='on'
        try:S.notify_catch_up_started(TODAY)
        except OSError:pass
        else:raise AssertionError('假SMTP错误应留给上层日志处理')
        assert 'catch_up_notice_date' not in runtime
        S._send_email.side_effect=None
        assert S.notify_catch_up_started(TODAY) is True
        ''')


if __name__ == "__main__":
    unittest.main()
