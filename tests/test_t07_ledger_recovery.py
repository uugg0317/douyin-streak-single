"""T07：待核对/拒绝结果落真实台账并贯穿当日保护和队列；所有页面动作均为假桩。"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / ".review-work" / "t07-ledger-tests"

PREAMBLE = '''
from datetime import datetime,timedelta,timezone
from pathlib import Path
from unittest import mock
import app_common,auth_service
from core import automation as A, ledger as L, runtime as R, scheduler as S
from core.send_day import daily_skip_code
from core.browser_errors import MessageDeliveryUnconfirmed
import tasks_service as T
NOW=[datetime(2026,10,15,0,30,tzinfo=timezone(timedelta(hours=8)))]
A.business_now=lambda:NOW[0]
R.business_now=lambda:NOW[0]
A._now=lambda:NOW[0].isoformat()
A.load_config=lambda:{'messages':['合成消息'],'max_friends_per_run':0,
                      'send_gap_min':1,'send_gap_max':1}
A.get_valid_state_path=mock.Mock(return_value=Path('未读取的合成凭证路径'))
A.open_browser=mock.Mock(side_effect=AssertionError('禁止真实账号浏览器'))
A._get_prewarmed_page=mock.Mock(return_value={'p':None,'browser':None,'context':None,'page':object()})
A.check_login=lambda page:(True,'假登录')
A._order_targets_by_list=lambda page,targets:targets
A._keep_prewarmed_alive=lambda *args,**kwargs:True
A._close_prewarmed=mock.Mock()
A.SEND_BUDGET_SECONDS=0
A.time.sleep=mock.Mock()
S.cancel_retry=mock.Mock()
from types import SimpleNamespace
JOBS={}
S._scheduler=SimpleNamespace(get_job=lambda key:JOBS.get(key))
def registered_retry(func, *, due_at, queue_version):
    JOBS['retry_send']=SimpleNamespace(next_run_time=due_at,kwargs={'queue_version':queue_version})
    return True
S.schedule_retry=mock.Mock(side_effect=registered_retry)
def seed(names):
    entries=[]
    for index,name in enumerate(names):
        entry=L._default_entry(name)
        entry.update(selected=True,selected_order=index,has_conversation=True)
        if index==0:
            entry.update(last_status='success',last_ok=True,
                         last_sent_at=(NOW[0]-timedelta(days=1)).isoformat(),last_msg='旧成功文案')
        entries.append(entry)
    L._save(entries)
    return L.load_ledger()
def result(dry=False):
    return {'at':A._now(),'dry_run':dry,'ok':[],'ok_entries':[],
            'failed':[],'skipped':[],'deferred':[],'logged_out':False,'rate_limited':False}
def finish(outcome):
    T._schedule_retry(outcome)
    R.record_run(outcome)
'''


class LedgerRecoveryTests(unittest.TestCase):
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
                "LOGIN_GATE": "on", "AUTH_TOKEN": "t07-isolated-management-token-not-real",
                "PASSWORD_PUBLIC_LOGIN": "off", "SPARKKEEPER_BACKGROUND_TASKS": "0",
                "SESSION_WARN_EMAIL_ENABLED": "off", "QR_PUBLIC_ORIGIN": "", "ALLOWED_HOSTS": "",
                "WHITELIST_IPS": "", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1",
            })
            source = PREAMBLE + "\n" + textwrap.dedent(code) + "\nprint('T07台账链路验证通过')\n"
            completed = subprocess.run([sys.executable, "-B", "-c", source], cwd=ROOT, env=env,
                                       text=True, encoding="utf-8", capture_output=True, timeout=20)
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertIn("T07台账链路验证通过", completed.stdout)

    def test_unknown_persists_review_state_over_old_success_and_keeps_attempt_meaning(self):
        self.isolated('''
        for entered in (True,False):
            entry=seed(['待核对合成好友'])[0]
            old_sent=entry['last_sent_at']
            R.update_runtime(consecutive_failures=7,session_status='ok',session_checked_at=123)
            error=MessageDeliveryUnconfirmed() if entered else MessageDeliveryUnconfirmed(enter_attempted=False)
            A.send_to_contact=mock.Mock(side_effect=error)
            outcome=result()
            A._send_consumer(object(),entry,'合成消息',False,outcome)
            row=L.load_ledger()[0]
            assert row['last_status']=='unconfirmed' and row['last_ok'] is None
            assert row['last_unconfirmed_at']==A._now()==row['last_attempt_at']
            assert row['last_sent_at']==old_sent and row['last_msg']=='旧成功文案'
            assert outcome['skipped'][0]['entry_id']==entry['entry_id']
            assert outcome['skipped'][0]['code']=='delivery_unconfirmed'
            assert outcome['skipped'][0]['retryable'] is False
            assert outcome['skipped'][0]['enter_attempted'] is entered
            if not entered:assert '未尝试' in outcome['skipped'][0]['reason']
            finish(outcome)
            assert R.load_runtime()['consecutive_failures']==7
            assert R.load_runtime()['session_checked_at']==123
            assert not R.load_runtime()['pending_retry_entries']
            from routers import contacts_ledger
            public=contacts_ledger.api_ledger(token=auth_service.AUTH_TOKEN)
            assert public['entries'][0]['last_status']=='unconfirmed'
            assert public['contacts'][0]['last_status']=='unconfirmed'
        S.schedule_retry.assert_not_called()
        ''')

    def test_same_day_unknown_is_skipped_before_credentials_for_all_normal_entry_paths(self):
        self.isolated('''
        entry=seed(['待核对合成好友'])[0]
        L.update_send_result(entry['display_name'],False,A._now(),entry_id=entry['entry_id'],status='unconfirmed')
        A.send_to_contact=mock.Mock(side_effect=AssertionError('同日未知结果不得重发'))
        A.get_valid_state_path=mock.Mock(side_effect=AssertionError('全跳过时不读取凭证'))
        for options in ({},{'only_names':[entry['display_name']]},{'only_ids':[entry['entry_id']]}):
            outcome=A._run_send_impl(dry_run=False,**options)
            assert not outcome['ok'] and not outcome['failed'] and not outcome['deferred']
            assert outcome['skipped'][0]['code']=='unconfirmed_today'
            assert outcome['skipped'][0]['retryable'] is False
            finish(outcome)
        A.send_to_contact.assert_not_called()
        A.open_browser.assert_not_called()
        A.get_valid_state_path.assert_not_called()
        S.schedule_retry.assert_not_called()
        assert not R.load_runtime()['pending_retry_entries']
        ''')

    def test_unknown_contact_never_enters_automatic_retry_but_later_unsent_id_can(self):
        self.isolated('''
        entries=seed(['待核对合成好友','尚未尝试合成好友'])
        attempted=[]
        def send(page,name,message,dry):
            attempted.append(name)
            raise MessageDeliveryUnconfirmed()
        A.send_to_contact=mock.Mock(side_effect=send)
        outcome=A._run_send_impl(dry_run=False)
        assert attempted==['待核对合成好友']
        assert [item['entry_id'] for item in outcome['deferred']]==[entries[1]['entry_id']]
        S.background_tasks_enabled=mock.Mock(return_value=True)
        finish(outcome)
        queued=R.load_runtime()['pending_retry_entries']
        assert queued==[{'entry_id':entries[1]['entry_id'],'name':'尚未尝试合成好友'}]
        assert all(item['entry_id']!=entries[0]['entry_id'] for item in queued)
        assert R.load_runtime()['retry_status']=='scheduled'
        assert outcome['pending_retry_count']==1
        assert S.schedule_retry.call_count==1
        assert S.schedule_retry.call_args.args==(T._scheduled_pending_retry,)
        assert S.schedule_retry.call_args.kwargs['due_at'].isoformat()==R.load_runtime()['retry_due_at']
        assert S.schedule_retry.call_args.kwargs['queue_version']==R.load_runtime()['retry_queue_version']
        assert T._pending_ids_locked()==[entries[1]['entry_id']]
        ''')

    def test_rejected_is_persisted_without_unknown_timestamp_or_automatic_retry(self):
        self.isolated('''
        entry=seed(['被拒绝合成好友'])[0]
        previous=entry['last_sent_at']
        A.send_to_contact=mock.Mock(side_effect=A.MessageDeliveryRejected())
        outcome=result()
        A._send_consumer(object(),entry,'合成消息',False,outcome)
        row=L.load_ledger()[0]
        assert row['last_status']=='rejected' and row['last_ok'] is False
        assert row['last_sent_at']==previous and row['last_attempt_at']==A._now()
        assert row.get('last_unconfirmed_at') is None
        assert outcome['failed'][0]['code']=='delivery_rejected'
        assert outcome['failed'][0]['retryable'] is False
        S.background_tasks_enabled=mock.Mock(return_value=True)
        finish(outcome)
        assert not R.load_runtime()['pending_retry_entries']
        S.schedule_retry.assert_not_called()
        ''')

    def test_dry_run_never_mutates_unknown_ledger_and_next_day_is_one_new_normal_run(self):
        self.isolated('''
        entry=seed(['待核对合成好友'])[0]
        L.update_send_result(entry['display_name'],False,A._now(),entry_id=entry['entry_id'],status='unconfirmed')
        before=L.LEDGER_PATH.read_bytes()
        A.send_to_contact=mock.Mock(return_value=(True,'dry-run'))
        A._send_consumer(object(),L.load_ledger()[0],'合成演练消息',True,result(True))
        assert L.LEDGER_PATH.read_bytes()==before
        assert daily_skip_code(L.load_ledger()[0],now=NOW[0])=='unconfirmed_today'
        unknown=result();unknown['delivery_unconfirmed']=True
        unknown['skipped']=[{'entry_id':entry['entry_id'],'name':entry['display_name'],
                            'code':'delivery_unconfirmed','retryable':False}]
        finish(unknown)
        assert not R.load_runtime()['pending_retry_entries']
        NOW[0]+=timedelta(days=1)
        assert daily_skip_code(L.load_ledger()[0],now=NOW[0]) is None
        assert [item['entry_id'] for item in A.compute_pending()]==[entry['entry_id']]
        A.send_to_contact=mock.Mock(return_value=(True,'accepted'))
        first=A._run_send_impl(dry_run=False)
        finish(first)
        second=A._run_send_impl(dry_run=False)
        assert first['ok']==[entry['display_name']]
        assert second['skipped'][0]['code']=='already_sent_today'
        A.send_to_contact.assert_called_once()
        assert R.load_runtime()['last_real_run_date']==NOW[0].date().isoformat()
        assert not R.load_runtime()['pending_retry_entries']
        S.schedule_retry.assert_not_called()
        ''')


if __name__ == '__main__':
    unittest.main()
