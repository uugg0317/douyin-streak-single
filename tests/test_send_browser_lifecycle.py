"""Offline lifecycle regressions: real contextmanager, fake browser and temporary data."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]
PRELUDE = r'''
import gc, json, threading, weakref
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from core import automation as A, browser as B
from core.browser_errors import BrowserSessionUnavailable, MessageDeliveryUnconfirmed, raise_if_browser_unavailable

drivers=[];owners=[];send_calls=[];collect_between_contacts=False
class Driver:
    def __init__(self):
        self.stopped=False;self.stops=0
        self.thread=threading.get_ident()
        self.chromium=SimpleNamespace(launch=lambda **kwargs: self.browser)
        self.browser=Browser(self)
    def stop(self):self.stopped=True;self.stops+=1
class Browser:
    def __init__(self,driver):self.driver=driver;self.closes=0
    def close(self):self.closes+=1
    def new_context(self,**kwargs):return SimpleNamespace(new_page=lambda:Page(self.driver))
class Page:
    def __init__(self,driver):self.driver=driver
    def live(self):
        assert threading.get_ident()==self.driver.thread,'browser crossed its owner thread'
        if self.driver.stopped:raise RuntimeError('Event loop is closed! Is Playwright already stopped?')
    def wait_for_load_state(self,*args,**kwargs):self.live()
    def wait_for_selector(self,*args,**kwargs):self.live()
def start_driver():
    driver=Driver();drivers.append(driver);return driver
def open_owned_browser():
    owner=B.open_browser(use_state=False)
    # A cycle deliberately delays generator finalization until explicit GC,
    # reproducing a cached Page that dies between two consumer contacts.
    owner._fixture_cycle=owner
    owners.append(weakref.ref(owner))
    return owner
def logged(page):page.live();return True,'ok'
def send(page,name,msg,dry):
    try:page.live()
    except Exception as error:raise_if_browser_unavailable(error);raise
    send_calls.append(name)
    if collect_between_contacts:gc.collect()
    return True,'dry-run'

B.sync_playwright=Mock(return_value=SimpleNamespace(start=start_driver))
B._apply_stealth=Mock()
A.open_browser=open_owned_browser
A.get_valid_state_path=Mock(return_value=Path('offline-state-not-read.json'))
A.load_config=Mock(return_value={'messages':['offline-message'],'max_friends_per_run':0,'send_gap_min':1,'send_gap_max':1})
A.update_runtime=Mock();A.load_runtime=Mock(return_value={})
A.check_login=Mock(side_effect=logged);A._open_chat_page=Mock(return_value=True)
A._order_targets_by_list=Mock(side_effect=lambda page,targets:targets)
A.time.sleep=Mock();A.SEND_BUDGET_SECONDS=0;A.KEEP_BROWSER_ALWAYS=True
A.KEEP_PREWARMED_SECONDS=600;A._prewarmed_browser=None
real_send_to_contact=A.send_to_contact
A.send_to_contact=Mock(side_effect=send)
A.ledger.confirm_join=Mock();A.ledger.mark_no_consumer_conversation=Mock();A.ledger.update_send_result=Mock()
A.ledger.get_selected=Mock(return_value=[])
def targets(*names):
    A.ledger.get_selected.return_value=[{'display_name':name,'has_conversation':True,'channel':'consumer'} for name in names]
'''


class SendBrowserLifecycleTests(unittest.TestCase):
    def isolated(self, body):
        with tempfile.TemporaryDirectory(prefix='send-lifecycle-offline-') as temporary:
            env = dict(os.environ)
            env.update(DATA_DIR=str(Path(temporary)/'data'), ENV_FILE_PATH=str(Path(temporary)/'missing.env'),
                       STATE_FILE_PATH='', SPARKKEEPER_BACKGROUND_TASKS='0',
                       PYTHONUTF8='1', PYTHONDONTWRITEBYTECODE='1')
            result = subprocess.run([sys.executable, '-B', '-c', PRELUDE + '\n' + textwrap.dedent(body)],
                                    cwd=ROOT, env=env, text=True, encoding='utf-8', capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def test_cold_cache_keeps_real_contextmanager_alive_until_explicit_close(self):
        self.isolated('''
        targets('Alpha');result=A._run_send_impl(dry_run=True)
        gc.collect()
        assert result['ok']==['Alpha']
        assert owners[0]() is not None,'cached browser lost its contextmanager owner'
        assert not drivers[0].stopped,'contextmanager GC stopped cached Playwright'
        A._close_prewarmed();A._close_prewarmed();gc.collect()
        assert drivers[0].stops==1 and drivers[0].browser.closes==1
        assert A._prewarmed_browser is None
        ''')

    def test_single_cold_run_then_multi_reuse_survives_gc_after_first_contact(self):
        self.isolated('''
        targets('First');first=A._run_send_impl(dry_run=True)
        assert first['ok']==['First']
        collect_between_contacts=True
        targets('Alpha','Beta','Gamma');second=A._run_send_impl(dry_run=True)
        assert second['ok']==['Alpha','Beta','Gamma'],second
        assert second['done']==3 and not second['failed'] and not second['deferred'],second
        assert len(drivers)==1,'reuse unexpectedly replaced the owned browser'
        assert not drivers[0].stopped
        A._close_prewarmed()
        ''')

    def test_direct_start_prewarm_without_contextmanager_closes_once(self):
        self.isolated('''
        driver=start_driver();page=Page(driver)
        A._prewarmed_browser={'p':driver,'browser':driver.browser,'context':object(),'page':page,'ready':True,'created_at':0}
        A._close_prewarmed();A._close_prewarmed()
        assert driver.stops==1 and driver.browser.closes==1
        ''')

    def test_public_runs_reuse_one_driver_on_the_same_dedicated_worker(self):
        self.isolated('''
        targets('First');A.run_send(dry_run=True)
        collect_between_contacts=True
        targets('Alpha','Beta','Gamma');result=A.run_send(dry_run=True)
        assert result['ok']==['Alpha','Beta','Gamma'] and result['done']==3,result
        assert len(drivers)==1 and drivers[0].thread!=threading.get_ident()
        A._run_in_pw_thread(A._close_prewarmed)
        assert drivers[0].stops==1
        A._pw_executor.shutdown(wait=True)
        ''')

    def test_dead_loop_during_locate_is_not_reported_as_missing_friend(self):
        self.isolated('''
        page=Mock();page.get_by_text.side_effect=RuntimeError('Event loop is closed! private-fixture-details')
        A.logger=Mock()
        try:A._locate_contact(page,'Alpha')
        except BrowserSessionUnavailable as error:
            assert 'private-fixture' not in str(error)
        else:raise AssertionError('dead Playwright was swallowed as contact-not-found')
        assert page.get_by_text.call_count==1
        assert not A.logger.info.called
        ''')

    def test_header_active_and_input_preflight_propagate_closed_session(self):
        self.isolated('''
        for operation in ('header','active','input'):
            page=Mock();dead=RuntimeError('Event loop is closed! private-fixture-details')
            if operation=='header':
                page.get_by_text.side_effect=dead;call=lambda:A._verify_in_conversation(page,'Alpha')
            elif operation=='active':
                page.locator.side_effect=dead;call=lambda:A._verify_item_active(page,'Alpha')
            else:
                A.detect_rate_limit=Mock(return_value=None)
                page.locator.return_value.first.count.side_effect=dead
                call=lambda:A._send_message(page,'offline-message',dry_run=True)
            try:call()
            except BrowserSessionUnavailable:pass
            else:raise AssertionError(operation+' hid dead browser')
            assert not page.keyboard.press.called
        ''')

    def test_mid_batch_session_failure_stops_once_defers_only_unattempted_names(self):
        self.isolated('''
        def failing(page,name,msg,dry):
            send_calls.append(name)
            if name=='Beta':raise BrowserSessionUnavailable()
            return True,'dry-run'
        A.send_to_contact=Mock(side_effect=failing)
        targets('Alpha','Beta','Gamma');result=A._run_send_impl(dry_run=True)
        assert result['ok']==['Alpha'],result
        assert result['done']==1 and result['total']==3,result
        assert {item['name'] for item in result['deferred']}=={'Beta','Gamma'},result
        assert result.get('browser_unavailable') is True,result
        assert len(result['failed'])==1 and result['failed'][0]['name']=='_system',result
        assert '浏览器会话已关闭' in result['failed'][0]['reason']
        assert send_calls==['Alpha','Beta']
        assert A._prewarmed_browser is None and drivers[0].stopped
        assert len(drivers)==1,'failure must not restart or replay within the same batch'
        ''')

    def test_dry_run_never_changes_join_confirmation_or_consumer_route(self):
        self.isolated('''
        targets('Alpha');A._run_send_impl(dry_run=True)
        A.ledger.confirm_join.assert_not_called();A.ledger.update_send_result.assert_not_called()
        entry={'display_name':'CreatorFixture','has_conversation':True,'channel':'creator'}
        A.send_to_contact=Mock(return_value=(False,'ordinary-not-found'))
        result={'ok':[],'failed':[],'skipped':[],'rate_limited':False}
        A._send_consumer(Mock(),entry,'offline-message',True,result)
        A.ledger.mark_no_consumer_conversation.assert_not_called()
        A._close_prewarmed()
        ''')

    def test_exact_name_and_final_target_guard_remain_fail_closed(self):
        self.isolated('''
        page=Mock();empty=Mock();empty.count.return_value=0
        page.get_by_text.return_value=empty
        candidates=Mock();candidates.all_text_contents.return_value=['AlphaBeta']
        page.locator.return_value=candidates
        assert A._find_contact(page,'Alpha') is None
        active=Mock();active.count.return_value=1;active.inner_text.return_value='AlphaBeta'
        page.locator.return_value.first=active
        assert A._verify_item_active(page,'Alpha') is False
        active.inner_text.return_value='Friend .fixture'
        assert A._verify_item_active(page,'Friend\u00a0.fixture') is True
        A._locate_contact=Mock(return_value=True)
        A._verify_item_active=Mock(return_value=False);A._verify_in_conversation=Mock(return_value=False)
        A._send_message=Mock(side_effect=AssertionError('must not type in wrong conversation'))
        ok,why=real_send_to_contact(page,'Alpha','offline-message',True)
        assert ok is False and '校验失败' in why
        A._send_message.assert_not_called();page.keyboard.press.assert_not_called()
        ''')

    def test_skipped_and_successful_names_are_not_requeued_after_driver_failure(self):
        self.isolated('''
        targets('CreatorFixture','Alpha','Beta')
        A.ledger.get_selected.return_value[0]['channel']='creator'
        def sending(page,name,msg,dry):
            if name=='CreatorFixture':return False,'ordinary-not-found'
            if name=='Beta':raise BrowserSessionUnavailable()
            return True,'dry-run'
        A.send_to_contact=Mock(side_effect=sending)
        result=A._run_send_impl(dry_run=True)
        assert result['done']==2,result
        assert {item['name'] for item in result['deferred']}=={'Beta'},result
        assert result['ok']==['Alpha'] and result['skipped'][0]['name']=='CreatorFixture'
        ''')

    def test_dead_cached_session_is_rebuilt_before_any_contact_attempt(self):
        self.isolated('''
        targets('Previous');A._run_send_impl(dry_run=True)
        drivers[0].stop();send_calls.clear()
        targets('Alpha','Beta');result=A._run_send_impl(dry_run=True)
        assert result['ok']==['Alpha','Beta'] and result['done']==2,result
        assert not result['failed'] and not result['deferred'],result
        assert not result.get('browser_unavailable')
        assert send_calls==['Alpha','Beta'] and len(drivers)==2
        assert A._prewarmed_browser['p'] is drivers[1]
        A._close_prewarmed()
        ''')

    def test_second_pre_send_probe_can_rebuild_without_replaying_any_contact(self):
        self.isolated('''
        targets('Previous');A._run_send_impl(dry_run=True);send_calls.clear()
        A.check_login=Mock(side_effect=[(True,'ok'),RuntimeError('Event loop is closed! private-fixture'),(True,'ok')])
        targets('Alpha','Beta');result=A._run_send_impl(dry_run=True)
        assert result['ok']==['Alpha','Beta'] and not result['failed'],result
        assert send_calls==['Alpha','Beta'] and len(drivers)==2
        assert not result.get('browser_unavailable')
        A._close_prewarmed()
        ''')

    def test_system_failure_does_not_cache_a_half_initialized_browser(self):
        self.isolated('''
        def sending(page,name,msg,dry):
            if name=='Beta':raise ValueError('ordinary offline system failure')
            return True,'dry-run'
        A.send_to_contact=Mock(side_effect=sending)
        targets('Alpha','Beta','Gamma');result=A._run_send_impl(dry_run=True)
        assert result['ok']==['Alpha'] and result['done']==1,result
        assert {item['name'] for item in result['deferred']}=={'Beta','Gamma'},result
        assert A._prewarmed_browser is None and drivers[0].stopped
        ''')

    def test_enter_call_exception_is_unconfirmed_and_enter_is_attempted_once(self):
        self.isolated('''
        page=Mock();input_box=Mock();A._wait_text_in_box=Mock(return_value=True)
        def press(key):
            if key=='Enter':raise RuntimeError('Event loop is closed! private fixture details')
        page.keyboard.press.side_effect=press
        try:A._type_and_send(page,input_box,'offline-message')
        except MessageDeliveryUnconfirmed as error:
            assert error.code=='delivery_unconfirmed' and 'private fixture' not in str(error)
        else:raise AssertionError('Enter uncertainty was treated as confirmed failure')
        assert sum(call.args==('Enter',) for call in page.keyboard.press.call_args_list)==1
        ''')

    def test_input_confirmation_timeout_never_replays_enter(self):
        self.isolated('''
        page=Mock();box=page.locator.return_value.first
        box.count.return_value=1;box.bounding_box.return_value={'x':0,'y':0}
        A.detect_rate_limit=Mock(return_value=None);A._wait_text_in_box=Mock(return_value=True)
        A._wait_input_cleared=Mock(return_value=False)
        try:A._send_message(page,'offline-message',False)
        except MessageDeliveryUnconfirmed:pass
        else:raise AssertionError('confirmation timeout must remain unknown')
        assert sum(call.args==('Enter',) for call in page.keyboard.press.call_args_list)==1
        assert A._wait_input_cleared.call_count==1
        ''')

    def test_driver_failure_while_confirming_is_unknown_not_unattempted(self):
        self.isolated('''
        page=Mock();box=page.locator.return_value.first
        box.count.return_value=1;box.bounding_box.return_value={'x':0,'y':0}
        A.detect_rate_limit=Mock(return_value=None);A._wait_text_in_box=Mock(return_value=True)
        A._wait_input_cleared=Mock(side_effect=RuntimeError('Event loop is closed! private fixture details'))
        try:A._send_message(page,'offline-message',False)
        except MessageDeliveryUnconfirmed as error:assert 'private fixture' not in str(error)
        else:raise AssertionError('post-Enter failure must not be replayable')
        assert sum(call.args==('Enter',) for call in page.keyboard.press.call_args_list)==1
        ''')

    def test_unknown_contact_is_skipped_for_review_and_only_later_contact_deferred(self):
        self.isolated('''
        def sending(page,name,msg,dry):
            send_calls.append(name)
            if name=='Beta':raise MessageDeliveryUnconfirmed()
            return True,'ok'
        A.send_to_contact=Mock(side_effect=sending)
        targets('Alpha','Beta','Gamma');result=A._run_send_impl(dry_run=False)
        assert result['ok']==['Alpha'] and not result['failed'],result
        assert result.get('delivery_unconfirmed') is True,result
        assert result['skipped'][0]['name']=='Beta' and result['skipped'][0]['code']=='delivery_unconfirmed'
        assert {item['name'] for item in result['deferred']}=={'Gamma'},result
        assert result['done']==2 and result['total']==3,result
        assert send_calls==['Alpha','Beta']
        assert [call.args[0] for call in A.ledger.update_send_result.call_args_list]==['Alpha']
        assert [call.args[0] for call in A.ledger.confirm_join.call_args_list]==['Alpha']
        assert A._prewarmed_browser is None and drivers[0].stopped
        ''')

    def test_message_dry_run_never_types_or_checks_post_enter_confirmation(self):
        self.isolated('''
        page=Mock();box=page.locator.return_value.first
        box.count.return_value=1;box.bounding_box.return_value={'x':0,'y':0}
        A.detect_rate_limit=Mock(return_value=None)
        A._type_and_send=Mock(side_effect=AssertionError('dry run must never type'))
        A._wait_input_cleared=Mock(side_effect=AssertionError('dry run must never wait for delivery'))
        assert A._send_message(page,'offline-message',True)==(True,'dry-run')
        A._type_and_send.assert_not_called();A._wait_input_cleared.assert_not_called()
        page.keyboard.press.assert_not_called();page.keyboard.type.assert_not_called()
        ''')

    def test_closed_browser_before_enter_remains_unattempted_session_failure(self):
        self.isolated('''
        page=Mock();box=Mock();box.click.side_effect=RuntimeError('Event loop is closed! private fixture')
        try:A._type_and_send(page,box,'offline-message')
        except BrowserSessionUnavailable:pass
        else:raise AssertionError('pre-Enter dead session was hidden')
        page.keyboard.press.assert_not_called()
        ''')


if __name__ == '__main__':
    unittest.main()
