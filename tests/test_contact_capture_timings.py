"""Deterministic scan timing; fake pages never sleep or use account data."""
import unittest

from core.contact_capture import (scan_contacts, EXTRACT_JS, SCROLL_STATE_JS, SCROLL_NEXT_JS,
                                  READ_PAIR_INTERVAL_MS, SCROLL_SETTLE_MS, BOTTOM_SETTLE_MS)


class Clock:
    def __init__(self):
        self.seconds = 100.0

    def __call__(self):
        return self.seconds

    def advance(self, seconds):
        self.seconds += seconds


class Page:
    def __init__(self, clock, *, bottom=True, pending_reads=0):
        self.clock = clock
        self.bottom = bottom
        self.pending_reads = pending_reads
        self.reads = 0
        self.calls = []

    def evaluate(self, script):
        self.calls.append(script)
        if script == EXTRACT_JS:
            self.clock.advance(.010)
            self.reads += 1
            return [{'name':'Synthetic', 'row_key':'synthetic', 'streak':12,
                     'streak_status':'confirmed', 'streak_source':'class',
                     'profile_pending':self.reads <= self.pending_reads}]
        if script == SCROLL_STATE_JS:
            self.clock.advance(.020)
            return {'known':True, 'bottom':self.bottom, 'loading':False, 'height':100}
        if script == SCROLL_NEXT_JS:
            self.clock.advance(.030)
            return None
        raise AssertionError('unexpected browser operation')

    def wait_for_timeout(self, milliseconds):
        self.calls.append(milliseconds)
        self.clock.advance(milliseconds / 1000)


class CaptureTimingTests(unittest.TestCase):
    def test_bottom_scan_keeps_waits_and_separates_operation_buckets(self):
        clock = Clock()
        page = Page(clock)
        progress_calls = []
        def progress():
            progress_calls.append(True)
            clock.advance(.050)
        rows = []
        result = scan_contacts(page, rows, max_rounds=5, clock=clock, progress=progress)
        self.assertTrue(result['complete'])
        self.assertEqual(result['rounds'], 3)
        self.assertEqual(result['reason'], 'stable_bottom')
        self.assertEqual(result['timings_ms'], {'read':120.0, 'stabilize':5540.0, 'scroll':60.0, 'total':5770.0})
        self.assertEqual([call for call in page.calls if isinstance(call, int)],
                         [READ_PAIR_INTERVAL_MS, BOTTOM_SETTLE_MS,
                          READ_PAIR_INTERVAL_MS, BOTTOM_SETTLE_MS, READ_PAIR_INTERVAL_MS])
        self.assertEqual(len(progress_calls), 1)
        self.assertEqual(len(rows), 1)

    def test_non_bottom_round_preserves_original_operation_order(self):
        clock = Clock()
        page = Page(clock, bottom=False)
        result = scan_contacts(page, [], max_rounds=1, clock=clock)
        self.assertFalse(result['complete'])
        self.assertEqual(result['reason'], 'round_limit')
        self.assertEqual(page.calls, [EXTRACT_JS, READ_PAIR_INTERVAL_MS, EXTRACT_JS,
                                     SCROLL_STATE_JS, SCROLL_NEXT_JS, SCROLL_SETTLE_MS])
        self.assertEqual(result['timings_ms'], {'read':40.0, 'stabilize':980.0, 'scroll':30.0, 'total':1050.0})

    def test_profile_readiness_wait_is_included_without_changing_retry_behavior(self):
        clock = Clock()
        page = Page(clock, pending_reads=2)
        result = scan_contacts(page, [], max_rounds=1, clock=clock)
        self.assertFalse(result['profiles_unresolved'])
        self.assertEqual(page.reads, 4)
        self.assertEqual(result['timings_ms'], {'read':60.0, 'stabilize':3360.0, 'scroll':30.0, 'total':3450.0})
        self.assertEqual([call for call in page.calls if isinstance(call, int)],
                         [READ_PAIR_INTERVAL_MS, 500, READ_PAIR_INTERVAL_MS, BOTTOM_SETTLE_MS])

    def test_empty_scan_has_nonnegative_complete_timing_shape(self):
        result = scan_contacts(Page(Clock()), [], max_rounds=0, clock=Clock())
        self.assertEqual(result['timings_ms'], {'read':0.0, 'stabilize':0.0, 'scroll':0.0, 'total':0.0})
        self.assertEqual(result['rounds'], 0)
        self.assertFalse(result['complete'])

    def test_read_error_is_propagated_without_retry_or_extra_progress(self):
        clock = Clock()
        page = Page(clock)
        calls = []
        def failing(script):
            calls.append(script)
            clock.advance(.025)
            raise RuntimeError('synthetic DOM failure')
        page.evaluate = failing
        with self.assertRaisesRegex(RuntimeError, 'synthetic DOM failure'):
            scan_contacts(page, [], clock=clock, progress=lambda: self.fail('unexpected progress'))
        self.assertEqual(calls, [EXTRACT_JS])


if __name__ == '__main__':
    unittest.main()
