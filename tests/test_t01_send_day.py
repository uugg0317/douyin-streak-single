"""T01：业务自然日幂等、待核对保护和 force 透传；只使用假数据与假浏览器。"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]
TEMP_ROOT = ROOT / ".review-work" / "T01"
PRELUDE = r'''
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from core import automation as A, ledger as L
from core.send_day import daily_skip_code, timestamp_date, TZ_NAME

NOW=datetime(2026,10,15,0,30,tzinfo=timezone(timedelta(hours=8)))
TODAY=NOW.isoformat()
YESTERDAY=(NOW-timedelta(days=1)).isoformat()
calls=[]
A.business_now=lambda: NOW
A._now=lambda: TODAY
A.load_config=lambda:{'messages':['仅本地测试'],'max_friends_per_run':0,'send_gap_min':1,'send_gap_max':1}
A.get_valid_state_path=mock.Mock(return_value=Path('未读取的假凭证路径'))
A._get_prewarmed_page=mock.Mock(return_value={'p':None,'browser':None,'context':None,'page':object()})
A.open_browser=mock.Mock(side_effect=AssertionError('测试不得打开真实账号浏览器'))
A.check_login=lambda page:(True,'ok')
A._order_targets_by_list=lambda page,targets:targets
A.send_to_contact=lambda page,name,msg,dry:(calls.append((name,dry)) or (True,'accepted'))
A._keep_prewarmed_alive=lambda *args,**kwargs:True
A._close_prewarmed=lambda:None
A.update_runtime=mock.Mock()
A.load_runtime=lambda:{}
A.SEND_BUDGET_SECONDS=0
A.time.sleep=mock.Mock()

def save(entries):
    L._save([{'display_name':name,'selected':True,'has_conversation':True,**fields} for name,fields in entries])

def fixture():
    save([('已发送',{'last_sent_at':TODAY,'last_status':'success'}),
          ('待核对',{'last_unconfirmed_at':TODAY,'last_status':'unconfirmed'}),
          ('昨天发送',{'last_sent_at':YESTERDAY,'last_status':'success'})])
'''


class SendDayTests(unittest.TestCase):
    def isolated(self, body):
        TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="backend-", dir=TEMP_ROOT) as temporary:
            base = Path(temporary)
            env = dict(os.environ)
            env.update(DATA_DIR=str(base / "data"), ENV_FILE_PATH=str(base / "不存在.env"),
                       STATE_FILE_PATH="", SPARKKEEPER_BACKGROUND_TASKS="0", LOGIN_GATE="on",
                       AUTH_TOKEN="t01-isolated-token-never-real", PASSWORD_PUBLIC_LOGIN="off",
                       PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1", TEMP=str(base), TMP=str(base))
            result = subprocess.run([sys.executable, "-B", "-c", PRELUDE + "\n" + textwrap.dedent(body)],
                                    cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=25)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_timezone_uses_shanghai_day_for_utc_offsets_and_legacy_naive_values(self):
        self.isolated('''
        from core import scheduler
        assert TZ_NAME==scheduler.TZ=='Asia/Shanghai'
        for value in ['2026-10-14T16:10:00Z','2026-10-14T12:10:00-04:00','2026-10-15T00:10:00']:
            assert daily_skip_code({'last_sent_at':value},now=NOW)=='already_sent_today',value
        for value in ['2026-10-14T15:59:59+00:00','2026-10-15T00:10:00+09:00']:
            assert daily_skip_code({'last_sent_at':value},now=NOW) is None,value
        ''')

    def test_missing_or_invalid_optional_timestamps_do_not_crash_or_block(self):
        self.isolated('''
        for value in [None,'','not-a-date',42,{},'999999-01-01T00:00:00']:
            assert timestamp_date(value) is None
            assert daily_skip_code({'last_sent_at':value,'last_unconfirmed_at':value},now=NOW) is None
        assert daily_skip_code({},now=NOW) is None
        ''')

    def test_real_runs_only_send_eligible_friend_once_and_repeat_is_idempotent(self):
        self.isolated('''
        fixture()
        first=A._run_send_impl(dry_run=False)
        second=A._run_send_impl(dry_run=False)
        assert calls==[('昨天发送',False)],calls
        assert first['total']==3 and first['done']==3
        assert {item['code'] for item in first['skipped']}=={'already_sent_today','unconfirmed_today'}
        assert not first['failed'] and not first['deferred']
        assert not second['ok'] and len(second['skipped'])==3 and second['done']==3
        assert all(item['entry_id'] and item['retryable'] is False for item in second['skipped'])
        ''')

    def test_all_today_skips_need_no_credentials_probe_or_browser(self):
        self.isolated('''
        save([('已发送',{'last_sent_at':TODAY})])
        A.get_valid_state_path=mock.Mock(side_effect=AssertionError('当日全部跳过不应探测凭证'))
        A._get_prewarmed_page=mock.Mock(side_effect=AssertionError('不应探测浏览器'))
        result=A._run_send_impl(dry_run=False)
        assert result['total']==result['done']==1 and not result['failed'] and not calls
        assert result['skipped'][0]['code']=='already_sent_today'
        assert not result.get('logged_out') and not result.get('credentials_missing')
        ''')

    def test_force_explicitly_sends_today_records_but_default_does_not(self):
        self.isolated('''
        fixture()
        result=A._run_send_impl(dry_run=False,force=True)
        assert {name for name,dry in calls}=={'已发送','待核对','昨天发送'} and len(calls)==3
        assert not result['skipped'] and len(result['ok_entries'])==3
        ''')

    def test_dry_run_keeps_all_targets_and_does_not_write_send_history(self):
        self.isolated('''
        fixture();before=L.load_ledger()
        result=A._run_send_impl(dry_run=True)
        assert len(calls)==3 and all(dry for name,dry in calls)
        assert not result['skipped'] and L.load_ledger()==before
        ''')

    def test_pending_only_ids_and_legacy_only_names_cannot_bypass_daily_unknown_guard(self):
        self.isolated('''
        fixture();unknown=next(e for e in L.load_ledger() if e['display_name']=='待核对')
        for options in [{'only_ids':[unknown['entry_id']]},{'only_names':['待核对']}]:
            result=A._run_send_impl(dry_run=False,**options)
            assert not calls and not result['deferred'] and not result['failed']
            assert result['skipped'][0]['code']=='unconfirmed_today'
            assert result['skipped'][0]['entry_id']==unknown['entry_id']
        result=A._run_send_impl(dry_run=False,only_ids=[unknown['entry_id']],force=True)
        assert calls==[('待核对',False)] and result['ok']==['待核对']
        ''')

    def test_legacy_unconfirmed_attempt_is_protected_without_new_field(self):
        self.isolated('''
        save([('旧待核对',{'last_status':'unconfirmed','last_attempt_at':TODAY})])
        result=A._run_send_impl(dry_run=False)
        assert not calls and result['skipped'][0]['code']=='unconfirmed_today'
        ''')

    def test_new_unknown_timestamp_is_persisted_and_real_failure_does_not_mark_unknown(self):
        self.isolated('''
        save([('A',{})]);key=L.load_ledger()[0]['entry_id']
        L.update_send_result('A',False,TODAY,entry_id=key,status='unconfirmed')
        assert L.load_ledger()[0]['last_unconfirmed_at']==TODAY
        assert daily_skip_code(L.load_ledger()[0],now=NOW)=='unconfirmed_today'
        save([('B',{})]);key=L.load_ledger()[0]['entry_id']
        L.update_send_result('B',False,TODAY,entry_id=key,status='failed')
        assert not L.load_ledger()[0].get('last_unconfirmed_at')
        assert daily_skip_code(L.load_ledger()[0],now=NOW) is None
        ''')

    def test_compute_pending_has_the_same_daily_filter_and_explicit_overrides(self):
        self.isolated('''
        fixture()
        assert [e['display_name'] for e in A.compute_pending()]==['昨天发送']
        assert len(A.compute_pending(force=True))==3
        assert len(A.compute_pending(dry_run=True))==3
        ''')

    def test_default_limit_is_applied_after_today_records_are_removed(self):
        self.isolated('''
        fixture()
        A.load_config=lambda:{'messages':['假数据'],'max_friends_per_run':1,'send_gap_min':1,'send_gap_max':1}
        result=A._run_send_impl(dry_run=False)
        assert calls==[('昨天发送',False)] and not result['deferred']
        assert result['done']==result['total']==3
        ''')

    def test_public_run_send_preserves_only_id_arguments_and_force(self):
        self.isolated('''
        A._run_in_pw_thread=mock.Mock(return_value={'dry_run':True,'ok':[],'failed':[]})
        A.run_send(dry_run=True,only_names=['A'],only_ids=['id-A'],force=True)
        A._run_in_pw_thread.assert_called_once_with(A._run_send_impl,True,['A'],['id-A'],True)
        ''')

    def test_force_is_optional_in_model_and_propagates_from_run_route(self):
        self.isolated('''
        from routers import run as R
        from models import RunBody
        R._check_auth=mock.Mock();R.ledger.get_selected=mock.Mock(return_value=[{'display_name':'A'}])
        R._start_run=mock.Mock()
        assert RunBody(dry=False).force is False
        R.api_run(RunBody(dry=False),token='假令牌')
        R._start_run.assert_called_once_with(False,force=False)
        R._start_run.reset_mock()
        R.api_run(RunBody(dry=False,force=True),token='假令牌')
        R._start_run.assert_called_once_with(False,force=True)
        ''')

    def test_force_propagates_through_task_worker_and_default_pending_path_does_not_force(self):
        self.isolated('''
        import tasks_service as T
        T._acquire_lock=mock.Mock(return_value=True);T._release_lock=mock.Mock()
        T._mark_run_started=mock.Mock();T.load_runtime=lambda:{}
        T.update_runtime=mock.Mock();T.set_running=mock.Mock()
        T.record_run=mock.Mock();T.notify_after_run=mock.Mock();T._schedule_retry=mock.Mock()
        T.scheduler.cancel_retry=mock.Mock()
        T.automation.run_send=mock.Mock(return_value={'ok':[],'failed':[],'skipped':[],'deferred':[]})
        class InlineThread:
            def __init__(self,target,**kwargs):self.target=target
            def start(self):self.target()
        with mock.patch.object(T.threading,'Thread',InlineThread):
            T._start_run(False,only_names=['A'],only_ids=['id-A'],force=True)
        T.automation.run_send.assert_called_once_with(dry_run=False,only_names=['A'],only_ids=['id-A'],force=True)
        T.automation.run_send.reset_mock()
        with mock.patch.object(T.threading,'Thread',InlineThread):
            T._start_run(False,only_ids=['id-A'])
        T.automation.run_send.assert_called_once_with(dry_run=False,only_names=None,only_ids=['id-A'])
        assert T._release_lock.call_count==2
        ''')


if __name__ == "__main__":
    unittest.main()
