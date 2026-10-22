"""Deletion must not erase the sole durable record of an uncertain submission."""
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
from fastapi import HTTPException
from core import ledger
from models import DeleteFriendBody, BatchDeleteBody
from routers import contacts_ledger as routes
routes._check_auth = lambda token: None
ledger.set_selected([{'display_name': 'Safe', 'selected': True},
                     {'display_name': 'Pending', 'selected': True}])
entries = ledger.load_ledger()
safe = next(e['entry_id'] for e in entries if e['display_name'] == 'Safe')
pending = next(e['entry_id'] for e in entries if e['display_name'] == 'Pending')
def conflict(call):
    try:
        call()
    except HTTPException as error:
        assert error.status_code == 409
        assert '核对' in error.detail
    else:
        raise AssertionError('unresolved intent was deleted')
'''


class IntentDeletionTests(unittest.TestCase):
    def isolated(self, body):
        temporary_root = ROOT / 'dist' / 'test-tmp'
        temporary_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='intent-delete-', dir=temporary_root) as directory:
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

    def test_single_sending_delete_keeps_intent_for_restart_recovery(self):
        self.isolated('''
        attempt = ledger.begin_send_attempt(pending)
        conflict(lambda: routes.api_ledger_delete(DeleteFriendBody(entry_id=pending)))
        assert ledger.recover_interrupted_attempts() == 1
        row = ledger.resolve_entry(ledger.load_ledger(), entry_id=pending)
        assert row['send_attempt']['attempt_id'] == attempt
        assert row['send_attempt']['status'] == 'unconfirmed'
        ''')

    def test_unconfirmed_and_legacy_unconfirmed_cannot_be_deleted(self):
        self.isolated('''
        attempt = ledger.begin_send_attempt(pending)
        ledger.update_send_result('Pending', False, entry_id=pending, attempt_id=attempt, status='unconfirmed')
        conflict(lambda: routes.api_ledger_delete(DeleteFriendBody(entry_id=pending)))
        ledger.update_send_result('Safe', False, entry_id=safe, status='unconfirmed')
        conflict(lambda: routes.api_ledger_delete(DeleteFriendBody(display_name='Safe')))
        assert len(ledger.load_ledger()) == 2
        ''')

    def test_mixed_batch_is_rejected_before_any_delete_or_save(self):
        self.isolated('''
        ledger.begin_send_attempt(pending)
        before = ledger.ledger_snapshot()
        with mock.patch.object(ledger, '_save', wraps=ledger._save) as save:
            conflict(lambda: routes.api_ledger_batch_delete(
                BatchDeleteBody(entry_ids=[safe], display_names=['Pending'])))
            save.assert_not_called()
        assert ledger.ledger_snapshot() == before
        ''')

    def test_confirmed_and_ordinary_rows_still_delete_in_one_commit(self):
        self.isolated('''
        attempt = ledger.begin_send_attempt(pending)
        ledger.update_send_result('Pending', True, entry_id=pending, attempt_id=attempt)
        with mock.patch.object(ledger, '_save', wraps=ledger._save) as save:
            result = routes.api_ledger_batch_delete(BatchDeleteBody(entry_ids=[safe, pending, safe]))
            assert result == {'ok': True, 'deleted': 2, 'requested': 2}
            assert save.call_count == 1
        assert ledger.load_ledger() == []
        ''')


if __name__ == '__main__':
    unittest.main()
