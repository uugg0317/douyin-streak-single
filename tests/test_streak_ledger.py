from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, mock
from core import ledger


class StreakLedgerTests(TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        for name, value in {'DATA_DIR':root,'LEDGER_PATH':root/'ledger.json','LEDGER_BAK_PATH':root/'ledger.json.bak'}.items():
            patch = mock.patch.object(ledger,name,value)
            patch.start(); self.addCleanup(patch.stop)
        self.entry = ledger._default_entry('A')
        self.entry.update(has_conversation=True, avatar='fixture', streak_days=81,
                          streak_status='confirmed', streak_confirmed_at='2026-10-18T08:00:00+08:00')
        ledger._save([self.entry])

    def observe(self, **fields):
        ledger.merge_consumer_contacts([{'name':'A','avatar':'fixture',**fields}])
        return ledger.load_ledger()[0]

    def test_unknown_preserves_value_and_confirmation_time(self):
        row = self.observe(streak=None, streak_status='unknown')
        self.assertEqual(row['streak_days'],81)
        self.assertEqual(row['streak_status'],'unknown')
        self.assertEqual(row['streak_confirmed_at'],self.entry['streak_confirmed_at'])
        self.assertTrue(row.get('streak_seen_at'))
        self.assertEqual(ledger.stats()['top_streak'],[])

    def test_none_requires_explicit_evidence_before_writing_zero(self):
        row = self.observe(streak=0, streak_status='none')
        self.assertEqual(row['streak_days'],81)
        row = self.observe(streak=0, streak_status='none', streak_none_evidence=True, streak_source='class')
        self.assertEqual(row['streak_days'],0)
        self.assertEqual(row['streak_status'],'none')

    def test_confirmed_value_and_suspect_preserve_their_meanings(self):
        row = self.observe(streak=82, streak_status='confirmed', streak_source='class')
        self.assertEqual(row['streak_days'],82)
        when = row['streak_confirmed_at']
        row = self.observe(streak=999, streak_status='suspect', streak_source='class')
        self.assertEqual(row['streak_days'],82)
        self.assertEqual(row['streak_status'],'suspect')
        self.assertEqual(row['streak_confirmed_at'],when)

    def test_legacy_entries_load_without_new_fields(self):
        self.entry.pop('streak_status'); self.entry.pop('streak_confirmed_at')
        ledger._save([self.entry])
        self.assertEqual(ledger.load_ledger()[0]['streak_days'],81)
        self.assertEqual(ledger.stats()['top_streak'][0]['streak_days'],81)
