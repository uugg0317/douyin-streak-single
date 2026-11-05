"""Pure synthetic evidence for historic-value correction and cumulative growth."""
from copy import deepcopy
from datetime import datetime, timedelta
import unittest

import _isolation  # noqa: F401  先隔离数据目录，再导入项目模块
from core import streaks

BASE = datetime.fromisoformat('2026-10-18T08:00:00+08:00')


def trusted(value=12):
    return {'streak_days':value, 'streak_status':'confirmed', 'streak_source':'class',
            'streak_confirmed_at':BASE.isoformat()}


def observe(entry, value, *, hours=1, days=0, sync='one', **fields):
    streaks.observe_streak(entry, {'streak':value, 'streak_status':'confirmed',
                                  'streak_source':'class', **fields},
                          BASE + timedelta(days=days, hours=hours), sync_id=sync)


class StreakCorrectionTests(unittest.TestCase):
    def test_latest_rejected_observation_is_preserved_separately(self):
        entry = trusted()
        observe(entry, 87)
        self.assertEqual(entry['streak_days'], 12)
        self.assertEqual(entry.get('streak_observed_value'), 87)
        self.assertEqual(entry.get('streak_observed_source'), 'class')
        self.assertEqual(entry.get('streak_observed_at'), (BASE + timedelta(hours=1)).isoformat(timespec='seconds'))
        self.assertEqual(entry.get('streak_observed_reason'), 'unexpected_jump')

    def test_same_day_plus_one_cannot_replenish_the_growth_budget(self):
        entry = trusted(100)
        observe(entry, 101)
        observe(entry, 102, hours=2, sync='two')
        self.assertEqual(entry['streak_days'], 101)
        self.assertEqual(entry['streak_status'], 'suspect')

    def test_new_business_day_can_accept_next_increment_without_reopening_same_day_budget(self):
        entry = trusted(100)
        observe(entry, 101)
        observe(entry, 102, days=1, sync='two')
        self.assertEqual(entry['streak_days'], 102)
        self.assertEqual(entry['streak_status'], 'confirmed')
        observe(entry, 103, days=1, hours=2, sync='three')
        self.assertEqual(entry['streak_days'], 102)
        self.assertEqual(entry['streak_status'], 'suspect')

    def test_repeated_same_value_does_not_erase_elapsed_growth_window(self):
        entry = trusted(100)
        observe(entry, 100, days=1)
        observe(entry, 100, days=2, sync='two')
        observe(entry, 103, days=3, sync='three')
        self.assertEqual(entry['streak_days'], 103)
        self.assertEqual(entry.get('streak_growth_base_days'), 100)
        self.assertEqual(entry.get('streak_growth_base_at'), BASE.isoformat())

    def test_legacy_normalization_is_pure_and_never_confirms_old_numbers(self):
        entry = {'streak_days':81, 'selected':True, 'send_attempt':{'status':'unconfirmed'},
                 'streak_label':'重燃中 2/3'}
        original = deepcopy(entry)
        normalize = getattr(streaks, 'normalize_streak_record', None)
        self.assertTrue(callable(normalize), 'normalization helper required')
        normalized = normalize(entry)
        self.assertEqual(entry, original)
        self.assertEqual(normalized['streak_days'], 81)
        self.assertEqual(normalized['streak_status'], 'unknown')
        self.assertEqual(normalized['streak_label'], '重燃中 2/3')
        self.assertEqual(normalized['send_attempt'], original['send_attempt'])
        self.assertNotIn('streak_growth_base_days', normalized)

    def test_unverified_legacy_value_is_not_a_growth_baseline(self):
        entry = streaks.normalize_streak_record({'streak_days':12})
        observe(entry, 87)
        self.assertEqual(entry['streak_days'], 87)
        self.assertEqual(entry['streak_status'], 'confirmed')
        self.assertEqual(entry.get('streak_growth_base_days'), 87)
        self.assertNotIn('streak_history_unverified', entry)

    def test_upper_candidate_requires_two_distinct_syncs_and_explicit_acceptance(self):
        entry = trusted()
        entry['send_attempt'] = {'status':'unconfirmed', 'attempt_id':'synthetic'}
        observe(entry, 87)
        observe(entry, 87, hours=2, sync='one')
        self.assertEqual(entry.get('streak_suspect_count'), 1)
        self.assertEqual(entry['streak_status'], 'suspect')
        observe(entry, 87, hours=3, sync='two')
        self.assertEqual(entry['streak_status'], 'correction_pending')
        self.assertEqual(entry['streak_days'], 12)
        self.assertEqual(entry.get('streak_suspect_direction'), 'up')
        streaks.apply_reset_decision(entry, 'accept', BASE + timedelta(hours=4))
        self.assertEqual(entry['streak_days'], 87)
        self.assertEqual(entry['streak_previous_days'], 12)
        self.assertEqual(entry['streak_growth_base_days'], 87)
        self.assertEqual(entry['streak_growth_base_at'], (BASE + timedelta(hours=3)).isoformat(timespec='seconds'))
        self.assertEqual(entry['send_attempt']['status'], 'unconfirmed')
        self.assertEqual(entry['send_attempt']['attempt_id'], 'synthetic')

    def test_upward_candidates_continue_only_consistently_across_dates(self):
        entry = trusted()
        observe(entry, 87)
        observe(entry, 88, hours=2, sync='two')
        self.assertEqual(entry.get('streak_suspect_count'), 1)
        observe(entry, 89, days=1, sync='three')
        self.assertEqual(entry['streak_status'], 'correction_pending')
        self.assertEqual(entry['streak_days'], 12)

    def test_upward_and_downward_evidence_cannot_be_combined(self):
        entry = trusted()
        observe(entry, 1)
        observe(entry, 87, days=1, sync='two')
        self.assertEqual(entry.get('streak_suspect_count'), 1)
        self.assertEqual(entry.get('streak_suspect_direction'), 'up')
        observe(entry, 2, days=2, sync='three')
        self.assertEqual(entry.get('streak_suspect_count'), 1)
        self.assertEqual(entry.get('streak_suspect_direction'), 'down')
        self.assertEqual(entry['streak_status'], 'suspect')

    def test_keep_upper_correction_retains_history_and_suppresses_repeated_prompt(self):
        entry = trusted()
        observe(entry, 87)
        observe(entry, 87, hours=2, sync='two')
        streaks.apply_reset_decision(entry, 'keep', BASE + timedelta(hours=3))
        self.assertEqual(entry['streak_days'], 12)
        self.assertEqual(entry['streak_reason'], 'correction_kept')
        observe(entry, 88, days=1, sync='three')
        self.assertEqual(entry['streak_days'], 12)
        self.assertNotEqual(entry['streak_status'], 'correction_pending')
        self.assertNotIn('streak_suspect_value', entry)
        self.assertEqual(entry['streak_observed_value'], 88)

    def test_unknown_new_observation_does_not_reuse_old_observed_value(self):
        entry = trusted()
        observe(entry, 87)
        observe(entry, None, hours=2, sync='two', streak_status='unknown', streak_label='1 天后消失')
        self.assertIsNone(entry.get('streak_observed_value'))
        self.assertEqual(entry['streak_days'], 12)
        self.assertEqual(entry['streak_label'], '1 天后消失')
        self.assertEqual(entry['streak_observed_reason'], 'unreadable')

    def test_normalization_rejects_malformed_metadata_and_never_regains_trust(self):
        for changes in ({'streak_status':None}, {'streak_status':[]}, {'streak_source':{}},
                        {'streak_source':None}, {'streak_confirmed_at':'invalid'}, {'streak_status':'none'}):
            with self.subTest(changes=changes):
                entry = dict(trusted(81), **changes)
                first = streaks.normalize_streak_record(entry)
                second = streaks.normalize_streak_record(first)
                self.assertEqual(first, second)
                self.assertEqual(first['streak_status'], 'unknown')
                self.assertEqual(first['streak_days'], 81)
                self.assertNotIn('streak_growth_base_days', first)

    def test_trusted_normalization_preserves_independent_anchor_and_input(self):
        entry = trusted(100)
        first = streaks.normalize_streak_record(entry)
        self.assertNotIn('streak_growth_base_days', entry)
        self.assertEqual(first['streak_growth_base_days'], 100)
        observe(first, 100, days=2)
        second = streaks.normalize_streak_record(first)
        self.assertEqual(second['streak_growth_base_at'], BASE.isoformat())
        self.assertEqual(second['streak_growth_base_days'], 100)

    def test_repeated_explicit_zero_does_not_refresh_numeric_change_time(self):
        entry = trusted()
        observe(entry, 0, streak_status='none', streak_none_evidence=True)
        changed_at = entry['streak_growth_changed_at']
        observe(entry, 0, days=1, sync='two', streak_status='none', streak_none_evidence=True)
        self.assertEqual(entry['streak_growth_changed_at'], changed_at)
        self.assertEqual(entry['streak_growth_base_days'], 0)
        self.assertEqual(entry['streak_status'], 'none')


if __name__ == '__main__':
    unittest.main()
