from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, mock
import inspect
from core import ledger, automation


class RenameTests(TestCase):
    def setUp(self):
        temp=TemporaryDirectory(); self.addCleanup(temp.cleanup); root=Path(temp.name)
        for name,value in {'DATA_DIR':root,'LEDGER_PATH':root/'ledger.json','LEDGER_BAK_PATH':root/'ledger.json.bak'}.items():
            patch=mock.patch.object(ledger,name,value); patch.start(); self.addCleanup(patch.stop)
        old=ledger._default_entry('旧名')
        old.update(avatar='https://example.invalid/face?token=one',selected=True,has_conversation=True,streak_days=12)
        ledger._save([old]); self.old=old

    def sync(self, name='新名', avatar=None, complete=True, **fields):
        options={'capture':{'complete':complete}} if 'capture' in inspect.signature(ledger.merge_consumer_contacts).parameters else {}
        ledger.merge_consumer_contacts([{'name':name,'avatar':avatar or self.old['avatar'],**fields}],**options)
        return ledger.load_ledger()

    def test_ordinary_candidate_no_longer_requires_identity_confirmation(self):
        rows=self.sync(); old=next(x for x in rows if x['entry_id']==self.old['entry_id']); new=next(x for x in rows if x is not old)
        self.assertTrue(old['selected']); self.assertFalse(new['selected'])
        self.assertEqual(old['observation_status'],'not_observed')
        self.assertEqual(new['rename_candidates'][0]['old_entry_id'],old['entry_id'])
        self.assertEqual(automation.compute_pending(selected_snapshot=[old]),[])
        with self.assertRaises(ledger.IdentityConflict): ledger.begin_send_attempt(old['entry_id'],force=True)
        self.assertIsNone(ledger.identity_block_reason(new))
        self.assertTrue(ledger.begin_send_attempt(new['entry_id']))

    def test_unresolved_old_intent_cannot_be_bypassed_via_unmatched_new_shell(self):
        attempt=ledger.begin_send_attempt(self.old['entry_id'])
        rows=self.sync(avatar='https://example.invalid/completely-different')
        new=next(x for x in rows if x['entry_id']!=self.old['entry_id'])
        self.assertTrue(new['identity_review_pending'])
        self.assertTrue(new['rename_candidates'][0]['unresolved_send'])
        with self.assertRaises(ledger.IdentityConflict): ledger.begin_send_attempt(new['entry_id'])
        self.assertEqual(next(x for x in rows if x['entry_id']==self.old['entry_id'])['send_attempt']['attempt_id'],attempt)

    def test_partial_capture_is_not_evidence_of_deleted_or_renamed_identity(self):
        rows=self.sync(complete=False)
        old=next(x for x in rows if x['entry_id']==self.old['entry_id'])
        self.assertEqual(old['observation_status'],'not_observed')
        self.assertFalse(old['last_capture_complete'])
        self.assertEqual(old['streak_status'],'unknown')
        self.assertEqual(old['streak_days'],12)
        self.assertNotIn('deleted',old)

    def test_query_parameters_are_preserved_without_manual_identity_gate(self):
        rows=self.sync(name='旧名',avatar='https://example.invalid/face?token=two')
        self.assertEqual(len(rows),2)
        self.assertTrue(next(x for x in rows if x['entry_id']!=self.old['entry_id'])['identity_review_pending'])
        self.assertIsNone(ledger.identity_block_reason(next(x for x in rows if x['entry_id']!=self.old['entry_id'])))

    def test_verified_platform_identity_updates_name_without_changing_entry_id(self):
        self.old.update(user_id='synthetic-id',platform_id_verified=True); ledger._save([self.old])
        rows=self.sync(user_id='synthetic-id',identity_verified=True)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['entry_id'],self.old['entry_id'])
        self.assertEqual(rows[0]['display_name'],'新名')
        self.assertFalse(rows[0].get('identity_review_pending',False))

    def test_selected_old_identity_with_changed_name_and_avatar_requires_explicit_exclusion(self):
        self.old.update(last_status='success',last_sent_at='2026-10-18T09:00:00+08:00')
        ledger._save([self.old])
        rows=self.sync(avatar='https://example.invalid/new-avatar')
        new=next(x for x in rows if x['entry_id']!=self.old['entry_id'])
        self.assertTrue(new['identity_review_pending'])
        self.assertIn('selected_identity_needs_exclusion',new['rename_candidates'][0]['evidence'])
