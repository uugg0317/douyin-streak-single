"""Offline T07 control-flow tests; the actual DOM contract is tested separately.

All imports and fake state live in an isolated DATA_DIR. No real account,
platform connection, or SMTP operation is used.
"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / '.review-work' / 'T07'
PRELUDE = r'''
from types import SimpleNamespace
from unittest.mock import Mock
from core import automation as A
from core.browser_errors import MessageDeliveryUnconfirmed

page=Mock();box=Mock();box.inner_text.return_value=''
scope=SimpleNamespace(input_box=box,dispose=Mock())
A.detect_rate_limit=Mock(return_value=None)
A._bind_delivery_scope=Mock(return_value=scope)
A._delivery_scope_current=Mock(return_value=True)
A._delivery_snapshot=Mock(return_value={'messages':[],'failures':[]})
A._wait_text_in_box=Mock(return_value=True)
A.time.sleep=Mock()
A.progress=Mock()

def enter_count():
    return sum(call.args==('Enter',) for call in page.keyboard.press.call_args_list)

def fake_clock():
    clock=SimpleNamespace(now=0.0)
    A.time.monotonic=Mock(side_effect=lambda:clock.now)
    A.time.sleep=Mock(side_effect=lambda seconds:setattr(clock,'now',clock.now+seconds))
    A.time.time=Mock(side_effect=AssertionError('confirmation must not use the wall clock'))
    return clock

def unknown(call,attempted):
    try:call()
    except MessageDeliveryUnconfirmed as error:
        assert error.enter_attempted is attempted,error
        if not attempted:assert str(error).startswith('未尝试发送：') and '已尝试' not in str(error)
        return error
    raise AssertionError('expected conservative unconfirmed result')
'''


class T07DeliveryScopeTests(unittest.TestCase):
    def isolated(self, body):
        WORK.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='unit-', dir=WORK) as temporary:
            env = dict(os.environ)
            env.update(DATA_DIR=str(Path(temporary) / 'data'),
                       ENV_FILE_PATH=str(Path(temporary) / 'missing.env'), STATE_FILE_PATH='',
                       SPARKKEEPER_BACKGROUND_TASKS='0', PYTHONUTF8='1', PYTHONDONTWRITEBYTECODE='1')
            result = subprocess.run([sys.executable, '-B', '-c', PRELUDE + textwrap.dedent(body)],
                                    cwd=ROOT, env=env, encoding='utf-8', text=True,
                                    capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_missing_scope_is_explicitly_unattempted_and_never_uses_global_input(self):
        self.isolated('''
        A._bind_delivery_scope.return_value=None
        unknown(lambda:A._send_message(page,'fixture-message',False,target_name='Alpha'),False)
        page.locator.assert_not_called()
        page.keyboard.press.assert_not_called();page.keyboard.type.assert_not_called()
        A._delivery_snapshot.assert_not_called()
        ''')

    def test_unavailable_baseline_prevents_enter_and_disposes_owned_scope(self):
        self.isolated('''
        A._delivery_snapshot.return_value={'messages':[],'failures':[],'unavailable':True}
        unknown(lambda:A._send_message(page,'fixture-message',False,target_name='Alpha'),False)
        page.keyboard.press.assert_not_called();page.keyboard.type.assert_not_called()
        scope.dispose.assert_called_once()
        ''')

    def test_scope_or_focus_lost_after_click_does_not_edit_or_enter(self):
        self.isolated('''
        A._delivery_scope_current.side_effect=[True,False]
        unknown(lambda:A._send_message(page,'fixture-message',False,target_name='Alpha'),False)
        box.click.assert_called_once()
        page.keyboard.press.assert_not_called();page.keyboard.type.assert_not_called()
        assert A._delivery_scope_current.call_args.kwargs['require_focus'] is True
        scope.dispose.assert_called_once()
        ''')

    def test_scope_or_focus_lost_after_typing_does_not_press_enter(self):
        self.isolated('''
        A._delivery_scope_current.side_effect=[True,True,False]
        unknown(lambda:A._send_message(page,'fixture-message',False,target_name='Alpha'),False)
        page.keyboard.type.assert_called_once()
        assert enter_count()==0
        assert A._delivery_scope_current.call_args.kwargs['require_focus'] is True
        scope.dispose.assert_called_once()
        ''')

    def test_confirmed_candidate_uses_monotonic_time_and_final_scope_check(self):
        self.isolated('''
        clock=fake_clock()
        A._delivery_snapshot.return_value={'messages':[{'id':'new','text':'fixture-message'}],'failures':[]}
        result=A._confirm_delivery(page,box,'fixture-message',{'messages':[],'failures':[]},
                                   timeout=.8,quiet_window=.2,scope=scope)
        assert result==(True,'accepted')
        assert clock.now>=.2
        A.time.time.assert_not_called();A._delivery_scope_current.assert_called_once_with(scope)
        A.progress.assert_called_once()
        ''')

    def test_scope_invalid_at_final_acceptance_remains_attempted_unknown(self):
        self.isolated('''
        fake_clock()
        A._delivery_snapshot.return_value={'messages':[{'id':'new','text':'fixture-message'}],'failures':[]}
        A._delivery_scope_current.return_value=False
        unknown(lambda:A._confirm_delivery(page,box,'fixture-message',{'messages':[],'failures':[]},
                                            timeout=.8,quiet_window=.2,scope=scope),True)
        A.progress.assert_not_called()
        ''')

    def test_old_message_id_cannot_become_new_evidence_and_timeout_is_monotonic(self):
        self.isolated('''
        clock=fake_clock()
        bubble={'id':'old','text':'fixture-message'}
        A._delivery_snapshot.return_value={'messages':[bubble],'failures':[]}
        unknown(lambda:A._confirm_delivery(page,box,'fixture-message',{'messages':[bubble],'failures':[]},
                                            timeout=.3,quiet_window=.1,scope=scope),True)
        assert clock.now>=.3
        A.time.time.assert_not_called();A.progress.assert_not_called()
        ''')

    def test_global_failure_vetoes_new_own_bubble(self):
        self.isolated('''
        fake_clock()
        A._delivery_snapshot.return_value={'messages':[{'id':'new','text':'fixture-message'}],
                                           'failures':['发送失败']}
        try:A._confirm_delivery(page,box,'fixture-message',{'messages':[],'failures':[]},scope=scope)
        except A.MessageDeliveryRejected:pass
        else:raise AssertionError('global failure was ignored')
        A.progress.assert_not_called()
        ''')

    def test_existing_global_failure_prevents_acceptance_without_new_rejection(self):
        self.isolated('''
        fake_clock()
        A._delivery_snapshot.return_value={'messages':[{'id':'new','text':'fixture-message'}],
                                           'failures':['发送失败']}
        unknown(lambda:A._confirm_delivery(page,box,'fixture-message',{'messages':[],'failures':['发送失败']},
                                            timeout=.3,quiet_window=.1,scope=scope),True)
        A.progress.assert_not_called()
        ''')

    def test_send_to_contact_passes_exact_target_into_scope_binding(self):
        self.isolated('''
        A._locate_contact=Mock(return_value=True)
        A._verify_item_active=Mock(return_value=True);A._verify_in_conversation=Mock(return_value=True)
        A._send_message=Mock(return_value=(True,'accepted'))
        assert A.send_to_contact(page,'Alpha','fixture-message',False)==(True,'accepted')
        A._send_message.assert_called_once_with(page,'fixture-message',False,target_name='Alpha')
        ''')


if __name__ == '__main__':
    unittest.main()
