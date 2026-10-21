"""Sync result identity and terminal state use synthetic data and controlled workers."""
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock
import app_common
from core import ledger, runtime
from fastapi import FastAPI
from routers import contacts_ledger
import tasks_service as tasks
from test_r06_selection_snapshot import LocalClient


class SyncJobTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name)
        for module, fields in [(runtime, {'DATA_DIR':root,'RUNTIME_PATH':root/'runtime.json','_rt_cache':{'mtime':None,'data':None}}),
                               (ledger, {'DATA_DIR':root,'LEDGER_PATH':root/'ledger.json','LEDGER_BAK_PATH':root/'ledger.json.bak'}),
                               (app_common, {'run_lock':threading.Lock(),'contacts_fetching':False})]:
            for key,value in fields.items():
                p=mock.patch.object(module,key,value);p.start();self.addCleanup(p.stop)
        self.targets=[]
        owner=self
        class DeferredThread:
            def __init__(self, target, **kwargs):owner.targets.append(target)
            def start(self):pass
        p=mock.patch.object(tasks,'threading',SimpleNamespace(Thread=DeferredThread));p.start();self.addCleanup(p.stop)
        p=mock.patch.object(contacts_ledger,'_check_auth');p.start();self.addCleanup(p.stop)
        self.old=ledger._default_entry('旧合成好友');ledger._save([self.old])
        app=FastAPI();app.include_router(contacts_ledger.router);self.client=LocalClient(app)

    def start(self):
        result=self.client.post('/api/sync',json={})
        self.assertEqual(result.status_code,200)
        self.assertTrue(result.json().get('job_id'),'sync must return its stable job identity')
        return result.json()['job_id']

    def finish(self,data=None,error=None):
        with mock.patch.object(tasks.automation,'fetch_chat_contacts',return_value=data,side_effect=error):
            self.targets.pop(0)()

    def test_running_identity_published_before_worker_starts(self):
        job=self.start();rt=runtime.load_runtime()
        self.assertEqual(rt['contacts_job_id'],job)
        self.assertEqual(rt['contacts_job_status'],'running')
        self.assertTrue(app_common.contacts_fetching)
        self.finish({'names':[],'error':None})

    def test_empty_success_is_a_real_terminal_result(self):
        job=self.start();self.finish({'names':[],'error':None})
        result=self.client.get('/api/ledger').json()
        self.assertEqual(result['contacts_job_id'],job)
        self.assertEqual(result['contacts_job_status'],'succeeded')
        self.assertFalse(result['fetching'])
        self.assertFalse(result['contacts_error'])

    def test_failed_sync_keeps_old_ledger_but_marks_current_job_failed(self):
        job=self.start();self.finish({'names':[],'error':'synthetic login expired'})
        result=self.client.get('/api/ledger').json()
        self.assertEqual(result['contacts_job_id'],job)
        self.assertEqual(result['contacts_job_status'],'failed')
        self.assertTrue(result['contacts_error'])
        self.assertEqual(len(result['entries']),1)

    def test_exception_and_merge_failure_cannot_report_success(self):
        for merge in [False,True]:
            with self.subTest(merge=merge):
                self.start()
                if merge:
                    with mock.patch.object(ledger,'merge_consumer_contacts',side_effect=OSError('fixture merge')):
                        self.finish({'names':['new synthetic friend'],'error':None})
                else:self.finish(error=RuntimeError('fixture failure'))
                self.assertEqual(runtime.load_runtime()['contacts_job_status'],'failed')
                self.assertFalse(app_common.contacts_fetching)
                self.assertFalse(app_common.run_lock.locked())

    def test_old_completion_cannot_overwrite_new_job(self):
        job=self.start()
        runtime.record_contacts({'names':[],'error':None},job_id='older-job')
        self.assertEqual(runtime.load_runtime()['contacts_job_id'],job)
        self.assertEqual(runtime.load_runtime()['contacts_job_status'],'running')
        self.finish({'names':[],'error':None})

    def test_worker_start_failure_publishes_failure_and_releases_lock(self):
        class FailedThread:
            def __init__(self, **kwargs):pass
            def start(self):raise RuntimeError('synthetic thread start failure')
        with mock.patch.object(tasks,'threading',SimpleNamespace(Thread=FailedThread)):
            with self.assertRaises(RuntimeError):tasks._start_fetch_contacts()
        self.assertEqual(runtime.load_runtime()['contacts_job_status'],'failed')
        self.assertFalse(app_common.contacts_fetching)
        self.assertFalse(app_common.run_lock.locked())

    def test_job_metadata_write_failure_does_not_start_worker(self):
        with mock.patch.object(tasks,'update_runtime',side_effect=OSError('fixture metadata write')):
            with self.assertRaises(OSError):tasks._start_fetch_contacts()
        self.assertFalse(self.targets)
        self.assertFalse(app_common.contacts_fetching)
        self.assertFalse(app_common.run_lock.locked())


if __name__=='__main__':unittest.main()
