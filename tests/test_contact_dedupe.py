"""重复同步与保守整理回归，仅使用合成联系人。"""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, mock
from core import ledger
from core.contact_identity import avatar_key
from core.contact_dedupe import consolidate_duplicates
from core.contact_capture import scan_contacts, EXTRACT_JS

A='https://p3.douyinpic.com/img/aweme-avatar/synthetic_resource~c5_100x100.jpeg?x=one'
B='https://p26.douyinpic.com/img/aweme-avatar/synthetic_resource~c5_168x168.webp?x=two'

def entry(key,avatar,**fields):
    row=ledger._default_entry('合成好友');row.update(entry_id=key*32,avatar=avatar,has_conversation=True,source={'consumer':True},consumer_key='合成好友|'+avatar+'|1',**fields);return row

class DedupeTests(TestCase):
    def test_cdn_and_transform_are_not_identity_but_other_resources_remain_distinct(self):
        self.assertEqual(avatar_key(A),avatar_key(B))
        self.assertNotEqual(avatar_key(A),avatar_key(B.replace('synthetic_resource','another_resource')))
        self.assertNotEqual(avatar_key('https://example.invalid/a?x=1'),avatar_key('https://example.invalid/a?x=2'))
        self.assertNotEqual(avatar_key(A),avatar_key(A.replace('p3.douyinpic.com','p3.douyinpic.com.evil.invalid')))
        self.assertEqual(avatar_key(A.replace('resource~','resource.png~')),avatar_key(B.replace('resource~','resource.png~')))

    def test_merge_preserves_selection_id_and_latest_observation_without_mutating_input(self):
        old=entry('a',A,selected=True,selected_order=2,observation_status='not_observed')
        new=entry('b',B,observation_status='seen',streak_days=215,streak_status='confirmed')
        original=[old,new];saved=deepcopy(original);out,report=consolidate_duplicates(original)
        self.assertEqual(original,saved);self.assertEqual(len(out),1)
        self.assertEqual(out[0]['entry_id'],old['entry_id']);self.assertTrue(out[0]['selected'])
        self.assertEqual(out[0]['streak_days'],215);self.assertEqual(out[0]['observation_status'],'seen')
        self.assertIn(new['entry_id'],out[0]['merged_entry_ids']);self.assertEqual(report['removed_duplicates'],1)
        again,report=consolidate_duplicates(out);self.assertEqual(again,out);self.assertEqual(report['removed_duplicates'],0)

    def test_completed_history_is_retained_and_unknown_history_is_not_merged(self):
        old=entry('a',A,last_sent_at='2026-10-18T08:00:00+08:00',last_status='success')
        new=entry('b',B,last_sent_at='2026-10-19T08:00:00+08:00',last_status='success')
        out,_=consolidate_duplicates([old,new]);self.assertEqual(out[0]['last_sent_at'],new['last_sent_at'])
        self.assertEqual(len(out[0]['merged_record_history']),2)
        old['last_status']='unconfirmed';out,_=consolidate_duplicates([old,new]);self.assertEqual(len(out),2)

    def test_distinct_resources_platform_conflicts_and_simultaneous_duplicates_are_preserved(self):
        for kind in ('avatar','platform','ordinal','conflict'):
            old,new=entry('a',A),entry('b',B)
            if kind=='avatar':new['avatar']=B.replace('synthetic_resource','different')
            elif kind=='platform':old['user_id']='A';new['user_id']='B'
            elif kind=='ordinal':new['consumer_key']='same|resource|2'
            else:new['identity_conflict']=True
            out,_=consolidate_duplicates([old,new]);self.assertEqual(len(out),2,kind)

    def test_self_candidate_is_removed_but_external_history_link_survives(self):
        old,new=entry('a',A),entry('b',B)
        new['rename_candidates']=[{'old_entry_id':old['entry_id']},{'old_entry_id':'c'*32,'unresolved_send':True}]
        out,_=consolidate_duplicates([old,new]);self.assertEqual([x['old_entry_id'] for x in out[0]['rename_candidates']],['c'*32])

    def test_repeated_sync_with_changed_cdn_does_not_add_or_change_selection(self):
        with TemporaryDirectory() as td:
            root=Path(td)
            with mock.patch.multiple(ledger,DATA_DIR=root,LEDGER_PATH=root/'ledger.json',LEDGER_BAK_PATH=root/'ledger.json.bak'):
                ledger.merge_consumer_contacts([{'name':'合成好友','avatar':A,'streak':12,'streak_status':'confirmed','streak_source':'class'}],capture={'complete':True})
                first=ledger.load_ledger()[0];ledger.set_selected([{'entry_id':first['entry_id'],'selected':True}])
                for avatar in (B,A,B):
                    result=ledger.merge_consumer_contacts([{'name':'合成好友','avatar':avatar,'streak':12,'streak_status':'confirmed','streak_source':'class'}],capture={'complete':True})
                    self.assertEqual(result['added'],0);rows=ledger.load_ledger();self.assertEqual(len(rows),1)
                    self.assertEqual(rows[0]['entry_id'],first['entry_id']);self.assertTrue(rows[0]['selected'])

class PendingProfileTests(TestCase):
    class Page:
        def __init__(self,ready):self.reads=0;self.ready=ready
        def evaluate(self,js):
            if js==EXTRACT_JS:
                self.reads+=1
                if not self.ready or self.reads<=2:return [{'name':'123456789','avatar':'data:image/png;base64,fixture','profile_pending':True}]
                return [{'name':'合成资料已加载','avatar':A,'streak_status':'unknown','row_key':'ready'}]
            return {'known':True,'height':100,'bottom':True,'loading':False}
        def wait_for_timeout(self,ms):pass
    def test_waits_for_profile_before_collecting(self):
        rows=[];result=scan_contacts(self.Page(True),rows,max_rounds=5)
        self.assertEqual([r['name'] for r in rows],['合成资料已加载']);self.assertTrue(result['complete'])
    def test_unresolved_profiles_are_not_written_or_claimed_complete(self):
        rows=[];result=scan_contacts(self.Page(False),rows,max_rounds=2)
        self.assertEqual(rows,[]);self.assertFalse(result['complete']);self.assertTrue(result['profiles_unresolved'])
