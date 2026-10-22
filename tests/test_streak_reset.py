from datetime import datetime
import inspect
import threading
from unittest import TestCase,mock
from core import streaks,ledger
from fastapi import FastAPI
from routers import contacts_ledger as routes
import app_common
import test_streak_ledger as fixture
from test_r06_selection_snapshot import LocalClient

def observe(entry,value,day,sync,**fields):
    now=datetime.fromisoformat(day+'T10:00:00+08:00')
    options={'sync_id':sync} if 'sync_id' in inspect.signature(streaks.observe_streak).parameters else {}
    streaks.observe_streak(entry,{'streak':value,'streak_status':'confirmed','streak_source':'class',**fields},now,**options)

def old_entry():
    return {'streak_days':81,'streak_status':'confirmed','streak_confirmed_at':'2026-10-17T08:00:00+08:00'}

class ResetEvidenceTests(TestCase):
    def test_distinct_syncs_across_days_propose_but_never_overwrite(self):
        entry=old_entry();observe(entry,1,'2026-10-18','one');observe(entry,1,'2026-10-18','one')
        self.assertEqual(entry['streak_suspect_count'],1)
        observe(entry,2,'2026-10-19','two')
        self.assertEqual(entry['streak_status'],'reset_pending');self.assertEqual(entry['streak_days'],81)
        self.assertEqual(entry['streak_suspect_value'],2);self.assertEqual(entry['streak_suspect_count'],2)
    def test_same_business_day_does_not_qualify(self):
        entry=old_entry();observe(entry,1,'2026-10-18','one');observe(entry,1,'2026-10-18','two')
        self.assertEqual(entry['streak_status'],'suspect');self.assertEqual(entry['streak_suspect_count'],2)
    def test_inconsistent_growth_restarts_evidence(self):
        entry=old_entry();observe(entry,1,'2026-10-18','one');observe(entry,3,'2026-10-19','two')
        self.assertEqual(entry['streak_suspect_count'],1)
        observe(entry,4,'2026-10-20','three');self.assertEqual(entry['streak_status'],'reset_pending')
    def test_unknown_and_unsupported_sources_do_not_count(self):
        entry=old_entry();observe(entry,1,'2026-10-18','one')
        observe(entry,None,'2026-10-19','two',streak_status='unknown')
        observe(entry,2,'2026-10-19','three',streak_source='heuristic')
        self.assertEqual(entry['streak_suspect_count'],1);self.assertNotEqual(entry['streak_status'],'reset_pending')
    def test_upper_jump_never_proposes_a_reset(self):
        entry=old_entry();entry['streak_days']=12
        observe(entry,87,'2026-10-18','one');observe(entry,88,'2026-10-19','two')
        self.assertEqual(entry['streak_status'],'suspect');self.assertFalse(entry.get('streak_suspect_count'))
    def test_optional_null_cooldown_remains_compatible(self):
        entry=old_entry();entry['streak_reset_dismissed_until']=None
        observe(entry,1,'2026-10-18','one')
        self.assertEqual(entry['streak_suspect_count'],1)

class ResetDecisionTests(TestCase):
    setUp=fixture.StreakLedgerTests.setUp
    def prepare(self):
        rows=ledger.load_ledger();entry=rows[0]
        observe(entry,1,'2026-10-18','one');observe(entry,2,'2026-10-19','two')
        entry['send_attempt']={'attempt_id':'synthetic-keep','status':'unconfirmed'}
        ledger._save(rows)
        patch=mock.patch.object(ledger,'business_now',return_value=datetime.fromisoformat('2026-10-19T12:00:00+08:00'));patch.start();self.addCleanup(patch.stop)
        patch=mock.patch.object(routes,'_check_auth');patch.start();self.addCleanup(patch.stop)
        patch=mock.patch.object(app_common,'run_lock',threading.Lock());patch.start();self.addCleanup(patch.stop)
        app=FastAPI();app.include_router(routes.router);return LocalClient(app),entry['entry_id']
    def call(self,client,key,action='accept',version=None):
        return client.post('/api/ledger/streak-decision',json={'entry_id':key,'action':action,'expected_ledger_version':version or ledger.ledger_snapshot()['ledger_version']})
    def test_accept_requires_user_action_and_keeps_audit_and_send_protection(self):
        client,key=self.prepare();self.assertEqual(ledger.load_ledger()[0]['streak_days'],81)
        result=self.call(client,key);self.assertEqual(result.status_code,200,result.text)
        entry=ledger.load_ledger()[0];self.assertEqual(entry['streak_days'],2);self.assertEqual(entry['streak_status'],'confirmed')
        self.assertEqual(entry['streak_previous_days'],81);self.assertTrue(entry['streak_reset_accepted_at'])
        self.assertEqual(entry['send_attempt']['attempt_id'],'synthetic-keep')
    def test_keep_clears_candidate_and_suppresses_repeat_prompt(self):
        client,key=self.prepare();result=self.call(client,key,'keep');self.assertEqual(result.status_code,200,result.text)
        entry=ledger.load_ledger()[0];self.assertEqual(entry['streak_days'],81);self.assertNotIn('streak_suspect_value',entry)
        observe(entry,3,'2026-10-20','three');self.assertNotEqual(entry['streak_status'],'reset_pending');self.assertFalse(entry.get('streak_suspect_count'))
    def test_stale_version_or_running_task_is_rejected(self):
        client,key=self.prepare();self.assertEqual(self.call(client,key,version='stale').status_code,409)
        app_common.run_lock.acquire()
        try:self.assertEqual(self.call(client,key).status_code,409)
        finally:app_common.run_lock.release()
    def test_unknown_cannot_be_accepted_and_failed_write_preserves_state(self):
        client,key=self.prepare();before=ledger.ledger_snapshot()
        with mock.patch.object(ledger,'atomic_write_text',side_effect=OSError('synthetic')):
            self.assertEqual(self.call(client,key).status_code,503)
        self.assertEqual(ledger.ledger_snapshot(),before)
        rows=ledger.load_ledger();rows[0]['streak_status']='unknown';ledger._save(rows)
        self.assertEqual(self.call(client,key).status_code,409)
