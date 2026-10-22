"""取消人工关联后的当前会话与历史发送保护。"""
from datetime import timedelta
from unittest import TestCase
from core import ledger
from core.send_day import business_now

class NoIdentityReviewTests(TestCase):
    def pair(self):
        old=ledger._default_entry('相同名字');old.update(has_conversation=True,observation_status='not_observed',last_status='pending')
        new=ledger._default_entry('相同名字');new.update(has_conversation=True,observation_status='seen',identity_review_pending=True,rename_candidates=[{'old_entry_id':old['entry_id']}])
        return old,new
    def test_historical_duplicate_does_not_block_unique_observed_row(self):
        old,new=self.pair();ledger._mark_ambiguity([old,new])
        self.assertFalse(new['identity_ambiguous']);self.assertIsNone(ledger.identity_block_reason(new))
        self.assertEqual(ledger.identity_block_reason(old),'not_observed')
    def test_two_current_same_name_rows_still_cannot_be_guessed(self):
        old,new=self.pair();old['observation_status']='seen';ledger._mark_ambiguity([old,new])
        self.assertTrue(old['identity_ambiguous']);self.assertTrue(new['identity_ambiguous'])
    def test_old_unresolved_send_is_not_bypassed_and_refreshes_after_resolution(self):
        old,new=self.pair();old['last_status']='unconfirmed';ledger._mark_ambiguity([old,new])
        self.assertEqual(ledger.identity_block_reason(new),'related_send_unconfirmed')
        old['last_status']='failed';ledger._mark_ambiguity([old,new])
        self.assertIsNone(ledger.identity_block_reason(new))
    def test_today_success_is_protected_but_yesterday_does_not_require_review(self):
        old,new=self.pair();old['last_sent_at']=business_now().isoformat();ledger._mark_ambiguity([old,new])
        self.assertEqual(ledger.identity_block_reason(new),'related_sent_today')
        old['last_sent_at']=(business_now()-timedelta(days=1)).isoformat();ledger._mark_ambiguity([old,new])
        self.assertIsNone(ledger.identity_block_reason(new))
