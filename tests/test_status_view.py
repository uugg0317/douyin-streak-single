"""Status presentation never invents credential expiry or task progress."""
import json
import unittest

from core.status_view import session_check_view, task_phase_view, TASK_PHASES


class SessionCheckViewTests(unittest.TestCase):
    NOW = 2_000_000.0

    def test_freshness_threshold_is_exactly_twenty_four_hours(self):
        for age, stale in ((0, False), (86399.999, False), (86400, True), (86400.001, True)):
            with self.subTest(age=age):
                view = session_check_view(self.NOW - age, now=self.NOW)
                self.assertAlmostEqual(view['session_check_age_seconds'], age)
                self.assertEqual(view['session_check_stale'], stale)
                self.assertEqual(view['session_check_reason'], 'stale' if stale else 'fresh')
                self.assertNotIn('session_status', view)

    def test_small_future_clock_skew_is_tolerated_with_zero_age(self):
        for skew in (0.01, 299.999, 300):
            with self.subTest(skew=skew):
                view = session_check_view(self.NOW + skew, now=self.NOW)
                self.assertEqual(view['session_check_reason'], 'fresh')
                self.assertEqual(view['session_check_age_seconds'], 0)
                self.assertFalse(view['session_check_stale'])

    def test_larger_future_time_is_unknown_without_claiming_expiry(self):
        view = session_check_view(self.NOW + 300.001, now=self.NOW)
        self.assertEqual(view['session_check_reason'], 'future')
        self.assertIsNone(view['session_check_age_seconds'])
        self.assertIsNone(view['session_checked_at'])
        self.assertFalse(view['session_check_stale'])
        self.assertNotIn('session_status', view)

    def test_missing_time_remains_unchecked(self):
        for value in (None, ''):
            view = session_check_view(value, now=self.NOW)
            self.assertEqual(view['session_check_reason'], 'missing')
            self.assertIsNone(view['session_check_age_seconds'])

    def test_invalid_timestamp_is_json_safe_and_unknown(self):
        for value in (True, False, 'garbage', 'NaN', float('nan'), float('inf'), -1, 0, {}, [], complex(1)):
            with self.subTest(value=repr(value)):
                view = session_check_view(value, now=self.NOW)
                self.assertEqual(view['session_check_reason'], 'invalid')
                self.assertIsNone(view['session_checked_at'])
                self.assertIsNone(view['session_check_age_seconds'])
                json.dumps(view, allow_nan=False)

    def test_legacy_numeric_string_is_supported(self):
        view = session_check_view(str(self.NOW - 60), now=self.NOW)
        self.assertEqual(view['session_check_age_seconds'], 60)
        self.assertEqual(view['session_check_reason'], 'fresh')

    def test_phase_requires_active_task_and_known_enum(self):
        for phase in TASK_PHASES:
            self.assertEqual(task_phase_view(phase, active=True), phase)
            self.assertIsNone(task_phase_view(phase, active=False))
        for phase in ('old-phase', '', None, {}, ['locating']):
            self.assertIsNone(task_phase_view(phase, active=True))


if __name__ == '__main__':
    unittest.main()
