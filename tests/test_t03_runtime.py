"""T03 运行记录和空目标回归；所有数据、配置、网络与浏览器均隔离。"""

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
from datetime import datetime, timezone
import socket
from unittest import mock
from core import runtime
from core.send_day import SEND_TIMEZONE

def forbidden(*args, **kwargs):
    raise AssertionError('T03 不允许真实联网、凭证读取或浏览器启动')

socket.create_connection=forbidden
socket.socket.connect=forbidden
# UTC 仍是 10 月 14 日，但业务上海时间已经是 10 月 15 日。
NOW=datetime(2026,10,14,16,30,tzinfo=timezone.utc).astimezone(SEND_TIMEZONE)
runtime.business_now=lambda:NOW
runtime.time.time=lambda:12345.25

def result(**fields):
    return dict({'at':'2026-10-14T16:30:00Z','dry_run':False,
                 'ok':[],'failed':[],'skipped':[],'deferred':[]},**fields)

def automation_fixture(targets=()):
    from core import automation as A
    A.load_config=lambda:{'messages':['仅隔离测试'],'friends':[],'max_friends_per_run':0}
    A.ledger.get_selected=lambda:list(targets)
    A.get_valid_state_path=mock.Mock(side_effect=forbidden)
    A._get_prewarmed_page=mock.Mock(side_effect=forbidden)
    A.open_browser=mock.Mock(side_effect=forbidden)
    A.business_now=lambda:NOW
    A._now=lambda:NOW.isoformat()
    return A
'''


class RuntimeRunTests(unittest.TestCase):
    def isolated(self, body):
        WORK.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="runtime-", dir=WORK) as temporary:
            folder = Path(temporary)
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith("SMTP_") and key not in {
                       "STATE_FILE_PATH", "URL_ACCESS_TOKEN", "URL_PASSWORD_SALT", "URL_PASSWORD_HASH",
                   }}
            env.update({
                "DATA_DIR": str(folder / "data"), "ENV_FILE_PATH": str(folder / "missing.env"),
                "INSTANCE_LOCK_PATH": str(folder / "pid"), "STATE_FILE_PATH": "",
                "SPARKKEEPER_NO_ROOT_STATE_FALLBACK": "1", "SPARKKEEPER_BACKGROUND_TASKS": "0",
                "LOGIN_GATE": "on", "AUTH_TOKEN": "t03-isolated-token-never-real",
                "PASSWORD_PUBLIC_LOGIN": "off", "SESSION_WARN_EMAIL_ENABLED": "off",
                "HOST": "127.0.0.1", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1",
                "TEMP": str(folder), "TMP": str(folder),
            })
            code = PREAMBLE + "\n" + textwrap.dedent(body)
            completed = subprocess.run([sys.executable, "-B", "-c", code], cwd=ROOT, env=env,
                                       capture_output=True, text=True, encoding="utf-8", timeout=20)
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_new_runtime_has_no_fabricated_real_run_date(self):
        self.isolated('''
        assert runtime.load_runtime()['last_real_run_date'] is None
        ''')

    def test_every_real_outcome_records_business_shanghai_day(self):
        self.isolated('''
        cases=[{}, {'ok':['A']}, {'failed':[{'name':'A'}]},
               {'skipped':[{'name':'A','code':'already_sent_today'}]},
               {'skipped':[{'name':'A','code':'unconfirmed_today'}]},
               {'deferred':[{'name':'A'}]}, {'logged_out':True}]
        for fields in cases:
            runtime.update_runtime(last_real_run_date=None)
            runtime.record_run(result(**fields))
            assert runtime.load_runtime()['last_real_run_date']=='2026-10-15',fields
        ''')

    def test_dry_run_preserves_real_date_failure_count_and_credential_evidence(self):
        self.isolated('''
        for fields in [{}, {'ok':['A']}, {'failed':[{'name':'A'}]}, {'logged_out':True}]:
            runtime.update_runtime(last_real_run_date='2026-10-13',consecutive_failures=4,
                                   session_status='expired',session_checked_at=111.0)
            runtime.record_run(result(**dict(fields,dry_run=True)))
            state=runtime.load_runtime()
            assert state['last_real_run_date']=='2026-10-13'
            assert state['consecutive_failures']==4
            assert state['session_status']=='expired' and state['session_checked_at']==111.0
        assert len(state['history'])==4
        ''')

    def test_pure_empty_real_run_increments_failure_without_fabricating_login_check(self):
        self.isolated('''
        runtime.update_runtime(consecutive_failures=4,session_status='ok',session_checked_at=111.0)
        runtime.record_run(result())
        state=runtime.load_runtime()
        assert state['consecutive_failures']==5
        assert state['session_status']=='ok' and state['session_checked_at']==111.0
        assert state['history'][0]['ok_count']==state['history'][0]['failed_count']==0
        ''')

    def test_missing_result_lists_are_also_empty_and_cannot_claim_expiry(self):
        self.isolated('''
        runtime.record_run({'dry_run':False})
        state=runtime.load_runtime()
        assert state['consecutive_failures']==1
        assert state['session_status']=='unknown' and state['session_checked_at'] is None
        ''')

    def test_all_already_sent_today_is_healthy_without_new_credential_evidence(self):
        self.isolated('''
        runtime.update_runtime(consecutive_failures=5,session_status='expired',session_checked_at=111.0)
        runtime.record_run(result(skipped=[{'entry_id':'A','code':'already_sent_today'}]))
        state=runtime.load_runtime()
        assert state['consecutive_failures']==0
        assert state['session_status']=='expired' and state['session_checked_at']==111.0
        ''')

    def test_unconfirmed_today_is_not_a_healthy_empty_success(self):
        self.isolated('''
        runtime.update_runtime(consecutive_failures=3,session_status='unknown',session_checked_at=111.0)
        runtime.record_run(result(skipped=[{'entry_id':'A','code':'unconfirmed_today'}]))
        state=runtime.load_runtime()
        assert state['consecutive_failures']==3
        assert state['session_status']=='unknown' and state['session_checked_at']==111.0
        ''')

    def test_mix_of_today_sent_and_unconfirmed_still_requires_attention(self):
        self.isolated('''
        runtime.update_runtime(consecutive_failures=2)
        runtime.record_run(result(skipped=[{'entry_id':'A','code':'already_sent_today'},
                                           {'entry_id':'B','code':'unconfirmed_today'}]))
        assert runtime.load_runtime()['consecutive_failures']==2
        ''')

    def test_delivery_unknown_without_explicit_failure_preserves_failure_count(self):
        self.isolated('''
        runtime.update_runtime(consecutive_failures=3,session_status='unknown',session_checked_at=111.0)
        runtime.record_run(result(delivery_unconfirmed=True))
        state=runtime.load_runtime()
        assert state['consecutive_failures']==3
        assert state['session_status']=='unknown' and state['session_checked_at']==111.0
        assert state['last_real_run_date']=='2026-10-15'
        ''')

    def test_deferred_only_keeps_existing_non_hard_failure_contract(self):
        self.isolated('''
        runtime.update_runtime(consecutive_failures=3,session_checked_at=111.0)
        runtime.record_run(result(deferred=[{'entry_id':'A','name':'A','retryable':True}]))
        state=runtime.load_runtime()
        assert state['consecutive_failures']==0
        assert state['session_status']=='failed' and state['session_checked_at']==111.0
        ''')

    def test_partial_success_preserves_existing_credential_and_failure_contract(self):
        self.isolated('''
        runtime.update_runtime(consecutive_failures=3)
        runtime.record_run(result(ok=['A'],failed=[{'entry_id':'B','name':'B'}]))
        state=runtime.load_runtime()
        assert state['consecutive_failures']==0
        assert state['session_status']=='partial' and state['session_checked_at']==12345.25
        ''')

    def test_explicit_logged_out_is_still_confirmed_expiry(self):
        self.isolated('''
        runtime.update_runtime(consecutive_failures=3)
        runtime.record_run(result(logged_out=True))
        state=runtime.load_runtime()
        assert state['consecutive_failures']==4
        assert state['session_status']=='expired' and state['session_checked_at']==12345.25
        ''')

    def test_real_no_targets_reports_system_failure_before_any_credential_probe(self):
        self.isolated('''
        A=automation_fixture()
        outcome=A._run_send_impl(dry_run=False)
        assert outcome['total']==outcome['done']==0
        assert len(outcome['failed'])==1
        failure=outcome['failed'][0]
        assert failure['name']=='_system' and failure['code']=='no_selected_contacts'
        assert failure['retryable'] is False
        assert not outcome.get('credentials_missing') and not outcome.get('logged_out')
        A.get_valid_state_path.assert_not_called()
        A._get_prewarmed_page.assert_not_called()
        A.open_browser.assert_not_called()
        runtime.record_run(outcome)
        state=runtime.load_runtime()
        assert state['consecutive_failures']==1
        assert state['session_status']=='failed' and state['session_checked_at'] is None
        ''')

    def test_dry_no_targets_has_no_failure_and_does_not_probe_credentials(self):
        self.isolated('''
        A=automation_fixture()
        outcome=A._run_send_impl(dry_run=True)
        assert not outcome['ok'] and not outcome['failed'] and not outcome['skipped']
        assert not outcome['deferred'] and outcome['total']==outcome['done']==0
        A.get_valid_state_path.assert_not_called()
        A.open_browser.assert_not_called()
        ''')

    def test_only_id_filter_leaving_no_targets_does_not_claim_missing_credentials(self):
        self.isolated('''
        A=automation_fixture([{'entry_id':'A','display_name':'A'}])
        outcome=A._run_send_impl(dry_run=False,only_ids=['not-selected'])
        assert outcome['failed'][0]['code']=='no_selected_contacts'
        assert not outcome.get('credentials_missing') and outcome['total']==0
        A.get_valid_state_path.assert_not_called()
        ''')

    def test_all_today_skips_still_return_before_no_contacts_or_credential_probe(self):
        self.isolated('''
        for code,entry in [('already_sent_today',{'last_sent_at':NOW.isoformat()}),
                           ('unconfirmed_today',{'last_unconfirmed_at':NOW.isoformat()})]:
            A=automation_fixture([dict(entry,entry_id='A',display_name='A')])
            outcome=A._run_send_impl(dry_run=False)
            assert not outcome['failed'] and outcome['skipped'][0]['code']==code
            assert outcome['total']==outcome['done']==1
            A.get_valid_state_path.assert_not_called()
        ''')

    def test_existing_selected_target_still_reports_missing_credentials(self):
        self.isolated('''
        A=automation_fixture([{'entry_id':'A','display_name':'A'}])
        A.get_valid_state_path=mock.Mock(return_value=None)
        outcome=A._run_send_impl(dry_run=False)
        assert outcome['failed'][0]['code']=='credentials_missing'
        assert outcome['credentials_missing'] is True
        assert outcome['deferred'][0]['entry_id']=='A'
        assert outcome['total']==1 and outcome['done']==0
        A.open_browser.assert_not_called()
        ''')


if __name__ == "__main__":
    unittest.main()
