from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, mock
import inspect
import _isolation  # noqa: F401  先隔离数据目录，再导入项目模块
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
        self.assertFalse(new['selected'])
        self.assertTrue(next(x for x in rows if x['entry_id']==self.old['entry_id'])['selected'])

    def test_same_name_avatar_change_carries_selection_and_never_unselects_old(self):
        old_avatar='https://p3.douyinpic.com/img/aweme-avatar/tos-cn-avt-0015_oldresource~c5_168x168.webp'
        new_avatar='https://p3.douyinpic.com/img/aweme-avatar/tos-cn-avt-0015_newresource~c5_168x168.webp'
        stale_avatar='https://p3.douyinpic.com/img/aweme-avatar/tos-cn-avt-0015_stale~c5_168x168.webp'
        stale=ledger._default_entry('秋亦')
        stale.update(avatar=stale_avatar,selected=True,selected_order=2,has_conversation=True,
                     source={'consumer':True},consumer_key='秋亦|'+stale_avatar+'|1',streak_days=215,
                     streak_status='unknown',observation_status='not_observed',
                     last_sent_at='2026-10-27T15:11:12+08:00',last_status='success')
        current=ledger._default_entry('秋亦')
        current.update(avatar=old_avatar,selected=True,selected_order=1,has_conversation=True,
                       source={'consumer':True},consumer_key='秋亦|'+old_avatar+'|1',streak_days=227,
                       streak_status='confirmed',observation_status='seen',
                       last_sent_at='2026-11-08T00:00:22+08:00',last_status='success')
        untouched=ledger._default_entry('马')
        untouched.update(avatar='https://p3.douyinpic.com/img/aweme-avatar/tos-cn-avt-0015_horse~c5_168x168.webp',
                         selected=True,has_conversation=True,source={'consumer':True},
                         streak_days=399,streak_status='confirmed',observation_status='seen')
        unselected=ledger._default_entry('路人')
        unselected.update(avatar='https://p3.douyinpic.com/img/aweme-avatar/tos-cn-avt-0015_other~c5_168x168.webp',
                          selected=False,has_conversation=True,source={'consumer':True},
                          streak_days=3,streak_status='confirmed',observation_status='seen')
        ledger._save([stale,current,untouched,unselected])
        ledger.merge_consumer_contacts([
            {'name':'秋亦','avatar':new_avatar,'streak':235,'streak_status':'confirmed','streak_source':'class'},
            {'name':'马','avatar':untouched['avatar'],'streak':399,'streak_status':'confirmed','streak_source':'class'},
            {'name':'路人','avatar':unselected['avatar'],'streak':3,'streak_status':'confirmed','streak_source':'class'},
        ],capture={'complete':True})
        rows=ledger.load_ledger()
        by_id={e['entry_id']:e for e in rows}
        live=[e for e in rows if e.get('display_name')=='秋亦' and e.get('observation_status')=='seen']
        self.assertEqual(len(live),1)
        self.assertNotIn(live[0]['entry_id'],{stale['entry_id'],current['entry_id']})
        self.assertTrue(live[0]['selected'])
        self.assertEqual(live[0]['selected_order'],1)
        self.assertEqual(live[0]['streak_days'],235)
        self.assertTrue(by_id[current['entry_id']]['selected'])
        self.assertEqual(by_id[current['entry_id']]['observation_status'],'not_observed')
        self.assertTrue(by_id[stale['entry_id']]['selected'])
        self.assertEqual(by_id[stale['entry_id']]['observation_status'],'not_observed')
        self.assertTrue(by_id[untouched['entry_id']]['selected'])
        self.assertFalse(by_id[unselected['entry_id']]['selected'])

    def test_unselected_history_does_not_invent_selection_on_new_row(self):
        self.old['selected']=False
        ledger._save([self.old])
        rows=self.sync(name='旧名',avatar='https://example.invalid/completely-different')
        new=next(x for x in rows if x['entry_id']!=self.old['entry_id'])
        self.assertFalse(new['selected'])
        self.assertFalse(next(x for x in rows if x['entry_id']==self.old['entry_id'])['selected'])
