import threading
from unittest import TestCase, mock
from fastapi import FastAPI
import app_common
from core import ledger
from routers import contacts_ledger as routes
import test_contact_rename as rename_fixture
from test_r06_selection_snapshot import LocalClient


class DecisionTests(TestCase):
    setUp = rename_fixture.RenameTests.setUp
    sync = rename_fixture.RenameTests.sync

    def client(self):
        app=FastAPI(); app.include_router(routes.router)
        patch=mock.patch.object(routes,'_check_auth'); patch.start(); self.addCleanup(patch.stop)
        patch=mock.patch.object(app_common,'run_lock',threading.Lock()); patch.start(); self.addCleanup(patch.stop)
        return LocalClient(app)

    def decision(self, client, action='confirm', version=None):
        snap=ledger.ledger_snapshot()
        new=next(x for x in snap['entries'] if x['entry_id']!=self.old['entry_id'])
        return client.post('/api/ledger/identity-decision',json={'old_entry_id':self.old['entry_id'],
            'new_entry_id':new['entry_id'],'action':action,'expected_ledger_version':version or snap['ledger_version']})

    def test_confirm_keeps_old_id_selection_history_and_unresolved_intent(self):
        attempt=ledger.begin_send_attempt(self.old['entry_id']); self.sync()
        client=self.client(); response=self.decision(client)
        self.assertEqual(response.status_code,200,response.text)
        rows=ledger.load_ledger(); self.assertEqual(len(rows),1)
        row=rows[0]; self.assertEqual(row['entry_id'],self.old['entry_id'])
        self.assertEqual(row['display_name'],'新名'); self.assertTrue(row['selected'])
        self.assertEqual(row['send_attempt']['attempt_id'],attempt)
        self.assertEqual(row['streak_days'],12)
        self.assertTrue(row['identity_history'])
        with self.assertRaises(ledger.IdentityConflict): ledger.begin_send_attempt(row['entry_id'])

    def test_stale_version_busy_run_and_repeat_are_rejected(self):
        before=ledger.ledger_snapshot()['ledger_version']; self.sync(); client=self.client()
        self.assertEqual(self.decision(client,version=before).status_code,409)
        app_common.run_lock.acquire()
        try: self.assertEqual(self.decision(client).status_code,409)
        finally: app_common.run_lock.release()
        snapshot=ledger.ledger_snapshot(); new=next(x for x in snapshot['entries'] if x['entry_id']!=self.old['entry_id'])
        body={'old_entry_id':self.old['entry_id'],'new_entry_id':new['entry_id'],'action':'confirm','expected_ledger_version':snapshot['ledger_version']}
        self.assertEqual(client.post('/api/ledger/identity-decision',json=body).status_code,200)
        self.assertEqual(client.post('/api/ledger/identity-decision',json=body).status_code,409)

    def test_explicit_different_does_not_transfer_selection_or_erase_old_state(self):
        ledger.begin_send_attempt(self.old['entry_id']); self.sync(); client=self.client()
        response=self.decision(client,action='different'); self.assertEqual(response.status_code,200,response.text)
        rows=ledger.load_ledger(); self.assertEqual(len(rows),2)
        new=next(x for x in rows if x['entry_id']!=self.old['entry_id'])
        self.assertFalse(new['identity_review_pending']); self.assertFalse(new['selected'])
        self.assertTrue(next(x for x in rows if x['entry_id']==self.old['entry_id'])['send_attempt'])
        self.sync(); self.assertFalse(next(x for x in ledger.load_ledger() if x['entry_id']==new['entry_id'])['identity_review_pending'])

    def test_candidate_with_its_own_send_history_cannot_be_discarded(self):
        rows=self.sync(); new=next(x for x in rows if x['entry_id']!=self.old['entry_id'])
        new['last_sent_at']='2026-10-18T09:00:00+08:00'; ledger._save(rows)
        before=ledger.ledger_snapshot(); response=self.decision(self.client())
        self.assertEqual(response.status_code,409)
        self.assertEqual(ledger.ledger_snapshot(),before)

    def test_write_failure_preserves_both_records_and_primary_file(self):
        self.sync(); client=self.client(); before=ledger.ledger_snapshot(); raw=ledger.LEDGER_PATH.read_bytes()
        with mock.patch.object(ledger,'atomic_write_text',side_effect=OSError('synthetic failure')):
            response=self.decision(client)
        self.assertEqual(response.status_code,503,response.text)
        self.assertEqual(ledger.ledger_snapshot(),before)
        self.assertEqual(ledger.LEDGER_PATH.read_bytes(),raw)

    def test_platform_conflict_and_multiple_candidates_are_rejected(self):
        rows=self.sync(); new=next(x for x in rows if x['entry_id']!=self.old['entry_id'])
        rows[0]['user_id']='synthetic-A'; new['user_id']='synthetic-B'; ledger._save(rows)
        self.assertEqual(self.decision(self.client()).status_code,409)
        rows[0]['user_id']=''; new['user_id']=''
        extra=ledger._default_entry('另一个旧名'); extra.update(has_conversation=True,avatar=self.old['avatar'])
        new['rename_candidates'].append({'old_entry_id':extra['entry_id']})
        ledger._save([*rows,extra])
        self.assertEqual(self.decision(self.client()).status_code,409)

    def test_confirmed_rename_uses_new_name_and_rejects_old_send_snapshot(self):
        self.sync(); response=self.decision(self.client()); self.assertEqual(response.status_code,200)
        key=self.old['entry_id']
        with self.assertRaises(ledger.IdentityConflict): ledger.begin_send_attempt(key,expected_display_name='旧名')
        self.assertTrue(ledger.begin_send_attempt(key,expected_display_name='新名'))

    def test_concurrent_review_can_only_commit_once(self):
        self.sync(); client=self.client(); snapshot=ledger.ledger_snapshot()
        new=next(x for x in snapshot['entries'] if x['entry_id']!=self.old['entry_id'])
        body={'old_entry_id':self.old['entry_id'],'new_entry_id':new['entry_id'],'action':'confirm','expected_ledger_version':snapshot['ledger_version']}
        barrier=threading.Barrier(2); codes=[]
        def worker():
            barrier.wait(); codes.append(client.post('/api/ledger/identity-decision',json=body).status_code)
        threads=[threading.Thread(target=worker) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(5)
        self.assertEqual(sorted(codes),[200,409])

    def test_confirmed_name_flows_through_retry_resolution_and_durable_submit(self):
        from core import automation
        import tasks_service
        self.sync(); self.assertEqual(self.decision(self.client()).status_code,200)
        entry=ledger.load_ledger()[0]
        pending=tasks_service._safe_retry_entries([{'entry_id':entry['entry_id'],'name':'旧名'}])
        self.assertEqual(pending,[{'entry_id':self.old['entry_id'],'name':'新名'}])
        observed=[]
        def fake_submit(page,name,message,dry):
            observed.append(name); automation._persist_current_intent(); return True,''
        result={'ok':[],'failed':[],'skipped':[]}
        with mock.patch.object(automation,'send_to_contact',side_effect=fake_submit), \
             mock.patch.object(automation._send_context,'stop_event',None,create=True):
            automation._send_consumer(object(),entry,'synthetic message',False,result)
        self.assertEqual(observed,['新名'])
        current=ledger.load_ledger()[0]
        self.assertEqual(current['entry_id'],self.old['entry_id'])
        self.assertEqual(current['send_attempt']['status'],'accepted')

    def test_atomic_save_keeps_existing_recovery_for_non_utf8_primary(self):
        self.sync()
        original=ledger.LEDGER_PATH.read_bytes()
        ledger.LEDGER_BAK_PATH.write_bytes(original)
        ledger.LEDGER_PATH.write_bytes(b'\xff\xfe broken synthetic primary')
        restored=ledger.load_ledger()
        ledger.set_selected([{'entry_id':restored[0]['entry_id'],'selected':False}])
        self.assertFalse(ledger.load_ledger()[0]['selected'])

    def test_corrupt_primary_cannot_replace_good_backup_before_failed_save(self):
        self.sync()
        original=ledger.LEDGER_PATH.read_bytes()
        ledger.LEDGER_BAK_PATH.write_bytes(original)
        ledger.LEDGER_PATH.write_bytes(b'{broken synthetic json')
        with mock.patch.object(ledger,'atomic_write_text',side_effect=OSError('synthetic failure')):
            with self.assertRaises(OSError):
                ledger.set_selected([{'entry_id':self.old['entry_id'],'selected':False}])
        self.assertEqual(ledger.LEDGER_BAK_PATH.read_bytes(),original)
        self.assertTrue(ledger.load_ledger()[0]['selected'])
