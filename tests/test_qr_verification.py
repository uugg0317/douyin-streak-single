"""Human verification controls, using synthetic pixels only; no platform access."""
import threading
import unittest
import uuid
from unittest.mock import Mock

from core.qr_login import LoginObservation, QrLoginInvalid, QrLoginBusy
import test_qr_login_engine as fixtures
from test_qr_login_engine import FakeBrowser, IMAGE, wait_for

VERIFY = LoginObservation("verification_required", verification_png=IMAGE,
                          verification_width=320, verification_height=240,
                          verification_generation=1)
POINTS = [{"x":.2,"y":.5,"t":0},{"x":.8,"y":.5,"t":0}]


class HumanBrowser(FakeBrowser):
    def __init__(self):
        super().__init__([VERIFY]);self.action_calls=[];self.before_action=None
    def interact(self, action, points, generation, still_current):
        self.record("interact")
        if self.before_action:self.before_action()
        if not still_current():return False
        self.action_calls.append((action,points,generation))
        self.observations=[LoginObservation("waiting",qr_png=IMAGE)]
        return True


class VerificationTests(unittest.TestCase):
    manager=fixtures.QrLoginEngineTests.manager
    status=fixtures.QrLoginEngineTests.status
    completed=fixtures.QrLoginEngineTests.completed
    def ready(self, browser=None):
        browser=browser or HumanBrowser();persist=Mock()
        manager,_=self.manager(browser,persist);job=manager.start("verification-fixture")
        status=wait_for(lambda:self.status(manager,job) if self.status(manager,job)["verification_ready"] else None)
        return manager,job,browser,persist,status
    def action(self, manager, job, revision, action_id=None, **kwargs):
        return manager.verification_action(job['job_id'],job['job_token'],action_id or str(uuid.uuid4()),
                                           revision,kwargs.get('action','drag'),kwargs.get('points',POINTS))

    def test_human_action_uses_worker_and_returns_to_qr_without_export(self):
        manager,job,browser,persist,status=self.ready()
        png,public=manager.verification(job['job_id'],job['job_token'])
        self.assertEqual(IMAGE,png);self.assertNotIn(job['job_token'],str(public))
        self.action(manager,job,status['verification_revision'])
        result=wait_for(lambda:self.status(manager,job) if self.status(manager,job)['image_ready'] else None)
        self.assertFalse(result['verification_ready']);self.assertFalse(result['verification_busy'])
        self.assertEqual(1,len(browser.action_calls));persist.assert_not_called()
        self.assertEqual(0,browser.storage_count)
        self.assertEqual(1,len({thread for _,thread in browser.calls}))
        self.assertNotEqual(threading.get_ident(),browser.calls[0][1])

    def test_same_action_id_is_idempotent_even_after_verification_disappears(self):
        manager,job,browser,_,status=self.ready();action_id=str(uuid.uuid4())
        self.action(manager,job,status['verification_revision'],action_id)
        wait_for(lambda:browser.action_calls)
        self.action(manager,job,status['verification_revision'],action_id)
        self.assertEqual(1,len(browser.action_calls))

    def test_bad_revision_and_invalid_coordinates_never_enter_queue(self):
        manager,job,browser,_,status=self.ready()
        with self.assertRaises(QrLoginInvalid):self.action(manager,job,status['verification_revision']+1)
        for points in ([{'x':float('nan'),'y':.5,'t':0}], [{'x':2,'y':.5,'t':0}],
                       [{'x':True,'y':.5,'t':0}], [{'x':.5,'y':.5,'t':3001}],
                       [{'x':.5,'y':.5,'t':0,'selector':'body'}]):
            with self.subTest(points=points),self.assertRaises(QrLoginInvalid):
                self.action(manager,job,status['verification_revision'],action='click',points=points)
        with self.assertRaises(QrLoginInvalid):self.action(manager,job,status['verification_revision'],action='type')
        self.assertEqual([],browser.action_calls)

    def test_second_action_is_rejected_while_first_is_running_and_cancel_aborts_it(self):
        entered,release=threading.Event(),threading.Event();browser=HumanBrowser()
        browser.before_action=lambda:(entered.set(),release.wait(2))
        manager,job,_,persist,status=self.ready(browser)
        self.action(manager,job,status['verification_revision']);self.assertTrue(entered.wait(1))
        with self.assertRaises(QrLoginBusy):self.action(manager,job,status['verification_revision'])
        manager.cancel(job['job_id'],job['job_token']);release.set();self.completed(manager,job)
        self.assertEqual([],browser.action_calls);persist.assert_not_called()
        png,status=manager.verification(job['job_id'],job['job_token'])
        self.assertIsNone(png);self.assertFalse(status['verification_ready'])

    def test_cancel_discards_a_queued_gesture_before_browser_execution(self):
        entered,release=threading.Event(),threading.Event();browser=HumanBrowser();observes=[0]
        def block():
            observes[0]+=1
            if observes[0]>=2:entered.set();release.wait(2)
        browser.before_observe=block
        manager,job,_,_,status=self.ready(browser);self.assertTrue(entered.wait(1))
        self.action(manager,job,status['verification_revision'])
        manager.cancel(job['job_id'],job['job_token']);release.set();self.completed(manager,job)
        self.assertEqual([],browser.action_calls)
        self.assertNotIn('interact',[name for name,_ in browser.calls])


if __name__=='__main__':unittest.main()
