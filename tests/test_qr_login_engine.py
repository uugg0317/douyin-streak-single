"""Pure tests: no Playwright launch, network, .env, or real account state."""
import json
from pathlib import Path
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core.qr_login import (
    LoginObservation, PNG_SIGNATURE, QrLoginManager, QrLoginBusy,
    QrLoginForbidden, QrLoginInvalid, QrLoginNotFound,
)

STATE={"cookies":[{"name":"sessionid","value":"fixture-cookie-never-public","domain":".douyin.com","path":"/","expires":-1}],"origins":[]}
READY=LoginObservation("saving",authenticated=True,login_signal="creator_identity")
IMAGE=PNG_SIGNATURE+b"fixture-qr"


class FakeBrowser:
    def __init__(self, observations=None):
        self.observations=list(observations or [LoginObservation("waiting",qr_png=IMAGE)])
        self.calls=[]
        self.state=STATE
        self.before_observe=None
        self.before_close=None
        self.refresh_count=0
        self.storage_count=0
        self.closed=threading.Event()
    def record(self,name): self.calls.append((name,threading.get_ident()))
    def open(self): self.record("open")
    def observe(self):
        self.record("observe")
        if self.before_observe: self.before_observe()
        if len(self.observations)>1: return self.observations.pop(0)
        return self.observations[0]
    def refresh(self): self.record("refresh");self.refresh_count+=1
    def storage_state(self): self.record("storage");self.storage_count+=1;return self.state
    def close(self):
        self.record("close")
        if self.before_close:self.before_close()
        self.closed.set()


def wait_for(check, timeout=2):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        result=check()
        if result:return result
        time.sleep(.005)
    raise AssertionError("fixture condition did not become true")


class QrLoginEngineTests(unittest.TestCase):
    def manager(self, browser=None, callback=None, **kwargs):
        browser=browser or FakeBrowser()
        manager=QrLoginManager(callback or Mock(),browser_factory=lambda:browser,poll_interval=.01,**kwargs)
        self.addCleanup(manager.shutdown)
        return manager,browser
    def status(self, manager, job):return manager.status(job["job_id"],job["job_token"])
    def completed(self,manager,job):return wait_for(lambda:self.status(manager,job) if self.status(manager,job)["cleanup_complete"] else None)

    def test_idempotent_start_returns_same_job_and_new_request_is_busy(self):
        manager,_=self.manager();job=manager.start("client-1")
        again=manager.start("client-1")
        self.assertEqual(job["job_id"],again["job_id"])
        self.assertEqual(job["job_token"],again["job_token"])
        with self.assertRaises(QrLoginBusy):manager.start("client-2")
        self.assertTrue(manager.is_active())

    def test_authorization_errors_are_explicit_and_unicode_token_is_safe(self):
        manager,_=self.manager();job=manager.start("client-1")
        with self.assertRaises(QrLoginNotFound):manager.status("unknown","token")
        for value in ("wrong","中文",None):
            with self.assertRaises(QrLoginForbidden):manager.status(job["job_id"],value)
        with self.assertRaises(QrLoginInvalid):manager.start("../bad request")

    def test_qr_png_and_safe_status_never_contain_cookie_or_token(self):
        manager,_=self.manager();job=manager.start("client-1")
        wait_for(lambda:self.status(manager,job)["image_ready"])
        image,status=manager.qr(job["job_id"],job["job_token"])
        self.assertEqual(IMAGE,image)
        self.assertNotIn(job["job_token"],json.dumps(status))
        self.assertNotIn("fixture-cookie",json.dumps(status))
        self.assertNotIn("job_token",status)
        self.assertGreater(status["qr_revision"],0)

    def test_strong_identity_saves_once_and_all_browser_operations_use_worker(self):
        browser=FakeBrowser([READY]);threads=[];saved=[]
        def factory():threads.append(threading.get_ident());return browser
        def persist(raw,job):saved.append((raw,job));threads.append(threading.get_ident())
        manager=QrLoginManager(persist,browser_factory=factory,poll_interval=.01);self.addCleanup(manager.shutdown)
        job=manager.start("client-1");status=self.completed(manager,job)
        self.assertEqual("success",status["state"])
        self.assertTrue(status["saved"])
        self.assertFalse(manager.is_active())
        self.assertEqual(1,len(saved))
        self.assertEqual(STATE,json.loads(saved[0][0]))
        self.assertNotIn("job_token",saved[0][1])
        self.assertNotIn("cookies",saved[0][1])
        self.assertEqual(1,len(set(threads+[thread for _,thread in browser.calls])))
        self.assertNotEqual(threading.get_ident(),threads[0])
        self.assertTrue(browser.closed.is_set())

    def test_persist_false_retries_without_reexporting_or_reporting_success(self):
        browser=FakeBrowser([READY]);attempts=[];may_save=threading.Event()
        def persist(raw,job):attempts.append(raw);return may_save.is_set()
        manager,_=self.manager(browser,persist);job=manager.start("client-1")
        wait_for(lambda:len(attempts)>=2)
        status=self.status(manager,job)
        self.assertEqual("saving",status["state"])
        self.assertEqual("waiting_for_runtime",status["code"])
        self.assertFalse(status["saved"])
        self.assertEqual(1,browser.storage_count)
        may_save.set()
        self.assertEqual("success",self.completed(manager,job)["state"])

    def test_busy_candidate_is_revalidated_before_a_later_save(self):
        now=[100.0];browser=FakeBrowser([READY])
        browser.state={"cookies":[{"name":"sessionid","value":"fixture","domain":".douyin.com","path":"/","expires":105.0}],"origins":[]}
        attempts=[]
        def persist(raw,job):attempts.append(raw);return False
        manager,_=self.manager(browser,persist,wall_clock=lambda:now[0]);job=manager.start("client-1")
        wait_for(lambda:len(attempts)>=1);before=len(attempts);now[0]=106.0
        result=self.completed(manager,job)
        self.assertEqual("invalid_state",result["code"])
        self.assertEqual(before,len(attempts))

    def test_cancel_after_commit_began_reports_saved_not_retracted(self):
        entered,release=threading.Event(),threading.Event();writes=[]
        def persist(raw,job):entered.set();release.wait(2);writes.append(raw)
        manager,_=self.manager(FakeBrowser([READY]),persist);job=manager.start("client-1")
        self.assertTrue(entered.wait(1));cancelled=[]
        thread=threading.Thread(target=lambda:cancelled.append(manager.cancel(job["job_id"],job["job_token"])))
        thread.start();release.set();thread.join(1)
        result=self.completed(manager,job)
        self.assertEqual(1,len(writes));self.assertTrue(cancelled[0]["saved"])
        self.assertNotEqual("cancelled",cancelled[0]["state"]);self.assertEqual("success",result["state"])

    def test_cancel_while_save_is_busy_never_writes_later(self):
        attempts=[];writes=[];available=threading.Event()
        def persist(raw,job):
            attempts.append(1)
            if not available.is_set():return False
            writes.append(raw)
        manager,_=self.manager(FakeBrowser([READY]),persist);job=manager.start("client-1")
        wait_for(lambda:attempts)
        manager.cancel(job["job_id"],job["job_token"]);available.set()
        self.assertEqual("cancelled",self.completed(manager,job)["state"])
        self.assertEqual([],writes)

    def test_late_success_after_cancel_is_discarded(self):
        entered,release=threading.Event(),threading.Event();browser=FakeBrowser([READY]);persist=Mock()
        def block():entered.set();release.wait(2)
        browser.before_observe=block
        manager,_=self.manager(browser,persist);job=manager.start("client-1")
        self.assertTrue(entered.wait(1))
        status=manager.cancel(job["job_id"],job["job_token"])
        self.assertEqual("cancelled",status["state"])
        self.assertFalse(status["cleanup_complete"])
        release.set();self.completed(manager,job)
        persist.assert_not_called();self.assertEqual(0,browser.storage_count)

    def test_refresh_discards_old_generation_before_storage_capture(self):
        entered,release=threading.Event(),threading.Event();browser=FakeBrowser([READY,LoginObservation("waiting",qr_png=IMAGE)]);persist=Mock();count=[0]
        def block():
            count[0]+=1
            if count[0]==1:entered.set();release.wait(2)
        browser.before_observe=block
        manager,_=self.manager(browser,persist);job=manager.start("client-1")
        self.assertTrue(entered.wait(1));manager.refresh(job["job_id"],job["job_token"]);release.set()
        wait_for(lambda:browser.refresh_count==1)
        manager.cancel(job["job_id"],job["job_token"]);self.completed(manager,job)
        persist.assert_not_called();self.assertEqual(0,browser.storage_count)

    def test_five_minute_deadline_rejects_late_identity(self):
        now=[10.0];entered,release=threading.Event(),threading.Event();browser=FakeBrowser([READY]);persist=Mock()
        browser.before_observe=lambda:(entered.set(),release.wait(2))
        manager,_=self.manager(browser,persist,clock=lambda:now[0]);job=manager.start("client-1")
        self.assertTrue(entered.wait(1));now[0]=311.0
        self.assertEqual("timeout",self.status(manager,job)["state"])
        release.set();self.completed(manager,job);persist.assert_not_called()

    def test_uid_tt_alone_never_counts_as_a_valid_session(self):
        browser=FakeBrowser([READY]);browser.state={"cookies":[{"name":"uid_tt","value":"not-auth","domain":".douyin.com","path":"/"}],"origins":[]};persist=Mock()
        manager,_=self.manager(browser,persist);job=manager.start("client-1")
        status=self.completed(manager,job)
        self.assertEqual("invalid_state",status["code"]);persist.assert_not_called()

    def test_untrusted_signal_cannot_trigger_storage_capture(self):
        browser=FakeBrowser([LoginObservation(authenticated=True,login_signal="uid_tt")]);persist=Mock()
        manager,_=self.manager(browser,persist);job=manager.start("client-1")
        wait_for(lambda:len(browser.calls)>2)
        manager.cancel(job["job_id"],job["job_token"]);self.completed(manager,job)
        self.assertEqual(0,browser.storage_count);persist.assert_not_called()

    def test_saved_job_remains_active_until_cleanup_finishes(self):
        entered,release=threading.Event(),threading.Event();browser=FakeBrowser([READY]);browser.before_close=lambda:(entered.set(),release.wait(2))
        manager,_=self.manager(browser);job=manager.start("client-1")
        self.assertTrue(entered.wait(1));self.assertTrue(manager.is_active())
        self.assertFalse(self.status(manager,job)["cleanup_complete"])
        with self.assertRaises(QrLoginBusy):manager.start("client-2")
        release.set();self.completed(manager,job);self.assertFalse(manager.is_active())

    def test_new_job_gets_a_fresh_factory_instance_on_same_worker(self):
        browsers=[]
        def factory():browser=FakeBrowser([READY]);browsers.append(browser);return browser
        manager=QrLoginManager(Mock(),browser_factory=factory,poll_interval=.01);self.addCleanup(manager.shutdown)
        first=manager.start("first");self.completed(manager,first)
        second=manager.start("second");self.completed(manager,second)
        self.assertEqual(2,len(browsers));self.assertIsNot(browsers[0],browsers[1])
        self.assertEqual(browsers[0].calls[0][1],browsers[1].calls[0][1])

    def test_shutdown_is_bounded_then_cleans_on_worker_after_unblock(self):
        entered,release=threading.Event(),threading.Event();browser=FakeBrowser();browser.before_observe=lambda:(entered.set(),release.wait(2))
        manager,_=self.manager(browser);job=manager.start("client-1");self.assertTrue(entered.wait(1))
        started=time.monotonic();self.assertFalse(manager.shutdown(timeout=.02));self.assertLess(time.monotonic()-started,.3)
        release.set();self.assertTrue(manager.shutdown(timeout=1));self.assertTrue(browser.closed.is_set())
        self.assertFalse(manager.is_active())
        with self.assertRaises(QrLoginInvalid):manager.start("after-shutdown")

    def test_save_and_factory_failures_do_not_expose_exception_credentials(self):
        secret="Cookie: private-error-body"
        manager,_=self.manager(FakeBrowser([READY]),Mock(side_effect=RuntimeError(secret)));job=manager.start("save-error")
        result=self.completed(manager,job);self.assertEqual("save_failed",result["code"]);self.assertNotIn(secret,json.dumps(result))
        manager2=QrLoginManager(Mock(),browser_factory=Mock(side_effect=RuntimeError(secret)),poll_interval=.01);self.addCleanup(manager2.shutdown)
        second=manager2.start("factory-error");result=self.completed(manager2,second)
        self.assertEqual("browser_failed",result["code"]);self.assertNotIn(secret,json.dumps(result))

    def test_captcha_and_empty_component_are_states_not_success(self):
        for code in ("verification_required","component_not_ready"):
            with self.subTest(code=code):
                manager,browser=self.manager(FakeBrowser([LoginObservation(code)]));job=manager.start(code)
                wait_for(lambda:self.status(manager,job)["code"]==code)
                self.assertFalse(self.status(manager,job)["saved"])
                self.assertEqual(0,browser.storage_count)
                manager.cancel(job["job_id"],job["job_token"]);self.completed(manager,job)

    def test_missing_qr_ends_job_and_closes_browser_without_export(self):
        persist=Mock()
        manager,browser=self.manager(FakeBrowser([LoginObservation("login_component_failed")]),persist)
        job=manager.start("missing-qr");status=self.completed(manager,job)
        self.assertEqual("error",status["state"])
        self.assertEqual("login_component_failed",status["code"])
        self.assertFalse(manager.is_active())
        self.assertTrue(browser.closed.is_set())
        self.assertEqual(0,browser.storage_count)
        persist.assert_not_called()

    def test_navigation_timeout_has_a_specific_safe_message(self):
        browser=FakeBrowser();browser.open=Mock(side_effect=TimeoutError('private navigation details'))
        manager,_=self.manager(browser);job=manager.start('timeout-fixture')
        result=self.completed(manager,job)
        self.assertEqual('login_page_timeout',result['code'])
        self.assertNotIn('private',str(result))

    def test_cleanup_failure_blocks_new_jobs_and_retries_on_original_worker(self):
        browser=FakeBrowser([READY]);persist=Mock();allow_close=threading.Event();calls=[]
        def close():
            calls.append(threading.get_ident())
            if not allow_close.is_set():raise RuntimeError('offline cleanup failure')
            browser.closed.set()
        browser.close=close
        manager,_=self.manager(browser,persist)
        job=manager.start('cleanup-pending')
        try:
            pending=wait_for(lambda:self.status(manager,job) if self.status(manager,job)['state']=='cleanup_pending' else None)
            self.assertTrue(pending['saved']);self.assertFalse(pending['cleanup_complete'])
            self.assertTrue(manager.is_active())
            with self.assertRaises(QrLoginBusy):manager.start('must-stay-blocked')
            self.assertEqual('cleanup_pending',manager.refresh(job['job_id'],job['job_token'])['state'])
            wait_for(lambda:len(calls)>=3 and not self.status(manager,job)['cleanup_retrying'])
            before=len(calls);time.sleep(.03);self.assertEqual(len(calls),before)
        finally:
            allow_close.set()
            if hasattr(manager,'retry_cleanup'):manager.retry_cleanup(job['job_id'],job['job_token'])
        result=self.completed(manager,job)
        self.assertEqual('success',result['state']);self.assertTrue(result['saved'])
        persist.assert_called_once();self.assertEqual(len(set(calls)),1)
        self.assertEqual(calls[0],browser.calls[0][1])

    def test_cancelled_job_waits_for_confirmed_cleanup_without_losing_outcome(self):
        browser=FakeBrowser();allow_close=threading.Event()
        def close():
            if not allow_close.is_set():raise RuntimeError('offline cleanup failure')
        browser.close=close
        manager,_=self.manager(browser);job=manager.start('cancel-cleanup-pending')
        wait_for(lambda:browser.calls)
        manager.cancel(job['job_id'],job['job_token'])
        try:
            pending=wait_for(lambda:self.status(manager,job) if self.status(manager,job)['state']=='cleanup_pending' else None)
            self.assertFalse(pending['saved']);self.assertFalse(pending['cleanup_complete'])
            self.assertEqual('cleanup_pending',manager.cancel(job['job_id'],job['job_token'])['state'])
        finally:
            allow_close.set()
            if hasattr(manager,'retry_cleanup'):manager.retry_cleanup(job['job_id'],job['job_token'])
        self.assertEqual('cancelled',self.completed(manager,job)['state'])

    def test_worker_start_failure_rolls_back_and_shutdown_is_safe(self):
        manager,_=self.manager()
        with patch.object(threading.Thread,'start',side_effect=RuntimeError('fixture start failure')):
            with self.assertRaises(QrLoginInvalid):manager.start('start-failed')
        self.assertFalse(manager.is_active())
        job=manager.start('start-again')
        manager.cancel(job['job_id'],job['job_token']);self.completed(manager,job)
        self.assertTrue(manager.shutdown(timeout=1))

    def test_reloaded_client_can_recover_only_pending_cleanup_without_auto_retry(self):
        browser=FakeBrowser([READY]);persist=Mock();allow_close=threading.Event();calls=[]
        def close():
            calls.append(1)
            if not allow_close.is_set():raise RuntimeError('offline close failure')
        browser.close=close
        manager,_=self.manager(browser,persist);job=manager.start('before-reload')
        try:
            wait_for(lambda:len(calls)>=3 and not self.status(manager,job)['cleanup_retrying'])
            before=len(calls)
            recovered=manager.recover_cleanup()
            self.assertEqual(recovered['job_id'],job['job_id'])
            self.assertEqual(recovered['job_token'],job['job_token'])
            self.assertEqual(recovered['state'],'cleanup_pending')
            self.assertTrue(recovered['saved']);self.assertFalse(recovered['cleanup_complete'])
            time.sleep(.03);self.assertEqual(len(calls),before)
            self.assertNotIn('fixture-cookie',json.dumps(recovered))
            # The original page keeps its capability after the new page recovers.
            self.assertEqual(self.status(manager,job)['state'],'cleanup_pending')
        finally:
            allow_close.set()
            if hasattr(manager,'retry_cleanup'):manager.retry_cleanup(job['job_id'],job['job_token'])
        self.assertEqual(self.completed(manager,job)['state'],'success')
        persist.assert_called_once()
        with self.assertRaises(QrLoginInvalid):manager.recover_cleanup()

    def test_cleanup_recovery_never_returns_an_active_login_capability(self):
        manager,browser=self.manager();job=manager.start('still-scanning')
        wait_for(lambda:browser.calls)
        with self.assertRaises(QrLoginInvalid):manager.recover_cleanup()
        manager.cancel(job['job_id'],job['job_token']);self.completed(manager,job)


if __name__=='__main__':unittest.main()
