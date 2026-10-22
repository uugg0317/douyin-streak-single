from datetime import datetime
from zoneinfo import ZoneInfo
from unittest import TestCase
from core.streaks import observe_streak


class GrowthTests(TestCase):
    def observe(self, old=12, value=13, confirmed='2026-10-18T08:00:00+08:00', now='2026-10-18T12:00:00+08:00', **fields):
        entry={'streak_days':old}
        if confirmed is not None: entry['streak_confirmed_at']=confirmed
        observe_streak(entry,{'streak':value,'streak_status':'confirmed','streak_source':'class',**fields},datetime.fromisoformat(now))
        return entry

    def test_same_day_unchanged_and_one_increment_are_allowed(self):
        for value in [12,13]: self.assertEqual(self.observe(value=value)['streak_days'],value)

    def test_multiday_growth_uses_elapsed_business_dates(self):
        self.assertEqual(self.observe(value=16,now='2026-10-22T12:00:00+08:00')['streak_days'],16)
        self.assertEqual(self.observe(value=17,now='2026-10-22T12:00:00+08:00')['streak_days'],12)

    def test_large_jump_and_positive_drop_are_suspect(self):
        for value in [87,11]:
            row=self.observe(value=value)
            self.assertEqual(row['streak_days'],12)
            self.assertEqual(row['streak_status'],'suspect')
            self.assertEqual(row['streak_confirmed_at'],'2026-10-18T08:00:00+08:00')

    def test_first_observation_and_legacy_without_date(self):
        self.assertEqual(self.observe(old=0,value=81,confirmed=None)['streak_days'],81)
        self.assertEqual(self.observe(value=13,confirmed=None)['streak_days'],13)
        self.assertEqual(self.observe(value=87,confirmed=None)['streak_days'],12)

    def test_missing_and_heuristic_sources_cannot_confirm(self):
        for source in [None,'heuristic','unread']:
            row=self.observe(value=13,streak_source=source)
            self.assertEqual(row['streak_days'],12)
            self.assertEqual(row['streak_status'],'unknown')

    def test_invalid_and_future_baseline_time_are_not_silently_accepted(self):
        for date in ['bad','2026-10-19T08:00:00+08:00','2026-10-18T18:00:00+08:00']:
            row=self.observe(confirmed=date)
            self.assertEqual(row['streak_days'],12)
            self.assertEqual(row['streak_status'],'suspect')

    def test_explicit_none_resets_and_unknown_does_not_refresh_confirmation(self):
        row=self.observe(value=0,streak_status='none',streak_none_evidence=True)
        self.assertEqual(row['streak_days'],0)
        observe_streak(row,{'streak_status':'unknown'},datetime(2026,10,19,tzinfo=ZoneInfo('Asia/Shanghai')))
        self.assertEqual(row['streak_confirmed_at'],'2026-10-18T12:00:00+08:00')
