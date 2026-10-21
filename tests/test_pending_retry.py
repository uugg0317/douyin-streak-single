"""Isolated continuation tests; never load a user's configuration or browser."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]
PREAMBLE = '''
from unittest import mock
import tasks_service as T
from core import scheduler as S
runtime={}
T.load_runtime=lambda: dict(runtime)
T.update_runtime=lambda **fields: runtime.update(fields)
S.cancel_retry=mock.Mock()
'''


class PendingRetryTests(unittest.TestCase):
    def isolated(self, code):
        with tempfile.TemporaryDirectory() as temporary:
            env = os.environ.copy()
            env.update(DATA_DIR=temporary, ENV_FILE_PATH=str(Path(temporary)/'missing.env'),
                       INSTANCE_LOCK_PATH=str(Path(temporary)/'lock'), SPARKKEEPER_BACKGROUND_TASKS='0',
                       AUTH_TOKEN='isolated-retry-test-token', LOGIN_GATE='on',
                       PASSWORD_PUBLIC_LOGIN='off', PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1')
            result = subprocess.run([sys.executable, '-B', '-c', PREAMBLE+'\n'+textwrap.dedent(code)],
                                    cwd=ROOT, env=env, capture_output=True, text=True, encoding='utf-8', timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr+result.stdout)

    def test_no_scheduler_preserves_manual_queue_without_spending_retry_date(self):
        self.isolated('''
        S._scheduler=None
        result={'deferred':[{'name':'Later','entry_id':'id-later'}]}
        T._schedule_retry(result)
        assert runtime['retry_status']=='manual'
        assert runtime['pending_retry_entries']==[{'name':'Later','entry_id':'id-later'}]
        assert 'retry_date' not in runtime
        assert result['pending_retry_count']==1 and result['retry_status']=='manual'
        ''')

    def test_unknown_rejected_success_and_ambiguous_contacts_are_never_queued(self):
        self.isolated('''
        result={'ok':['Done'],'skipped':[{'name':'Unknown','code':'delivery_unconfirmed'}],
                'failed':[{'name':'Rejected','retryable':False,'code':'delivery_rejected'},
                          {'name':'Unsafe legacy failure'},{'name':'Before Enter','retryable':True}],
                'deferred':[{'name':'Done'},{'name':'Unknown'},{'name':'Ambiguous','code':'identity_ambiguous'},
                            {'name':'Later','entry_id':'later'}]}
        assert T._collect_retry_names(result)==['Before Enter','Later']
        ''')

    def test_date_is_only_committed_when_job_registration_succeeds(self):
        self.isolated('''
        S.background_tasks_enabled=lambda:True
        S.schedule_retry=mock.Mock(return_value=False)
        result={'deferred':[{'name':'Later','entry_id':'later'}]}
        T._schedule_retry(result)
        assert 'retry_date' not in runtime and runtime['retry_status']=='manual'
        S.schedule_retry.return_value=True
        from types import SimpleNamespace
        jobs={}
        S._scheduler=SimpleNamespace(get_job=lambda key:jobs.get(key))
        def registered(func, *, due_at, queue_version):
            jobs['retry_send']=SimpleNamespace(next_run_time=due_at,kwargs={'queue_version':queue_version})
            return True
        S.schedule_retry.side_effect=registered
        T._schedule_retry(result)
        assert runtime['retry_date'] and runtime['retry_status']=='scheduled'
        ''')

    def test_continuation_uses_ids_and_excludes_deselected_or_ambiguous_contacts(self):
        self.isolated('''
        runtime.update(pending_retry_entries=[{'name':'Later','entry_id':'later'},
                       {'name':'Deselected','entry_id':'off'},{'name':'Same'},{'name':'Unsafe','entry_id':'amb'}])
        T.ledger.get_selected=lambda:[{'entry_id':'later','display_name':'Renamed'},
          {'entry_id':'one','display_name':'Same'},{'entry_id':'two','display_name':'Same'},
          {'entry_id':'amb','display_name':'Unsafe','identity_ambiguous':True}]
        T._begin_run_locked=mock.Mock(side_effect=lambda *a,**k:T._release_lock())
        T._start_pending_retry()
        T._begin_run_locked.assert_called_once_with(False,only_ids=['later'])
        ''')

    def test_queue_is_consumed_before_worker_and_restored_if_thread_cannot_start(self):
        self.isolated('''
        original=[{'name':'Later','entry_id':'later'}]
        runtime.update(pending_retry_entries=original,retry_status='manual')
        def fail_start():
            assert runtime['pending_retry_entries']==[]
            raise RuntimeError('thread unavailable')
        with mock.patch.object(T.threading,'Thread') as cls:
            cls.return_value.start.side_effect=fail_start
            try:T._start_run(False,only_ids=['later'])
            except RuntimeError:pass
            else:raise AssertionError('thread failure must be visible')
        assert runtime['pending_retry_entries']==original
        assert not T.app_common.run_lock.locked()
        ''')

    def test_two_continuations_cannot_read_and_replay_the_same_queue(self):
        self.isolated('''
        from fastapi import HTTPException
        runtime.update(pending_retry_entries=[{'name':'Later','entry_id':'later'}],retry_status='manual')
        nested=[]
        def selected():
            try:T._start_pending_retry()
            except HTTPException as error:nested.append(error.status_code)
            else:raise AssertionError('second request must not read a held queue')
            return [{'entry_id':'later','display_name':'Later'}]
        T.ledger.get_selected=selected
        started=[]
        def begin(*args,**kwargs):
            started.append(kwargs['only_ids'])
            runtime.update(pending_retry_entries=[])
            T._release_lock()
        T._begin_run_locked=begin
        T._start_pending_retry()
        T.ledger.get_selected=lambda:[{'entry_id':'later','display_name':'Later'}]
        try:T._start_pending_retry()
        except HTTPException as error:assert error.status_code==400
        else:raise AssertionError('consumed queue must not start again')
        assert nested==[409] and started==[['later']]
        assert not T.app_common.run_lock.locked()
        ''')


if __name__ == '__main__':
    unittest.main()
