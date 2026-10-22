from datetime import datetime
from unittest import TestCase,mock
from core import streaks,ledger
from routers import contacts_ledger as routes
import test_streak_ledger as fixture

class ConfirmationAgeTests(TestCase):
    def test_unknown_history_uses_shanghai_business_date_and_stale_threshold(self):
        now=datetime.fromisoformat('2026-10-22T01:00:00+08:00')
        for status in ('unknown','suspect','reset_pending'):
            view=streaks.confirmation_age({'streak_status':status,'streak_confirmed_at':'2026-10-17T17:00:00+00:00'},now)
            self.assertEqual(view['streak_confirmed_date'],'2026-10-18');self.assertEqual(view['streak_unconfirmed_days'],4)
            self.assertTrue(view['streak_may_be_stale'])
    def test_missing_invalid_and_future_time_do_not_become_zero_days(self):
        now=datetime.fromisoformat('2026-10-18T12:00:00+08:00')
        for value in (None,'bad','2026-10-19T00:00:00+08:00'):
            view=streaks.confirmation_age({'streak_confirmed_at':value},now)
            self.assertIsNone(view['streak_unconfirmed_days'])
    def test_both_ledger_entries_and_compatible_contacts_include_age(self):
        case=fixture.StreakLedgerTests();case.setUp();self.addCleanup(case.doCleanups)
        rows=ledger.load_ledger();rows[0]['streak_status']='unknown';ledger._save(rows)
        with mock.patch.object(ledger,'business_now',return_value=datetime.fromisoformat('2026-10-22T12:00:00+08:00')),mock.patch.object(routes,'_check_auth'),mock.patch.object(routes,'load_runtime',return_value={}):
            response=routes.api_ledger(token='synthetic')
        for key in ('entries','contacts'):
            self.assertEqual(response[key][0]['streak_unconfirmed_days'],4)
            self.assertEqual(response[key][0]['streak_days'],81)
