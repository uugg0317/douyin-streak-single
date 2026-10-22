"""Budgeting must defer work instead of violating the configured minimum gap."""
from __future__ import annotations
from pathlib import Path
import unittest
from unittest import mock
from core import automation as a


class IntervalTests(unittest.TestCase):
    def test_budget_interval_respects_configured_bounds(self):
        for low, high in [(1, 1), (6, 12), (20, 30)]:
            for left in [1, 4, 30, 600]:
                for jitter in [.75, 1.25]:
                    with self.subTest(low=low, left=left, jitter=jitter), \
                         mock.patch.object(a.time, 'time', return_value=100), \
                         mock.patch.object(a.random, 'uniform', return_value=jitter):
                        value = a._send_interval(100 + left, 4, high, gap_min=low)
                        self.assertGreaterEqual(value, low)
                        self.assertLessEqual(value, high)

    def test_insufficient_budget_defers_without_a_short_wait(self):
        now = [0.0]
        waits = []
        targets = [{'entry_id':'a'*32, 'display_name':'A', 'selected':True, 'has_conversation':True},
                   {'entry_id':'b'*32, 'display_name':'B', 'selected':True, 'has_conversation':True}]
        cfg = {'messages':['fixture'], 'max_friends_per_run':0, 'send_gap_min':6, 'send_gap_max':12}
        def send(page, entry, message, dry, result):
            now[0] += 2
            result['ok'].append(entry['display_name'])
            result.setdefault('ok_entries', []).append({'entry_id':entry['entry_id'], 'name':entry['display_name']})
        def sleep(seconds):
            waits.append(seconds); now[0] += seconds
        patches = [mock.patch.object(a, 'load_config', return_value=cfg),
                   mock.patch.object(a.ledger, 'get_selected', return_value=targets),
                   mock.patch.object(a.ledger, 'load_ledger', return_value=[]),
                   mock.patch.object(a, 'get_valid_state_path', return_value=Path('synthetic-state')),
                   mock.patch.object(a, '_get_prewarmed_page', return_value={'p':None,'browser':None,'context':None,'page':object()}),
                   mock.patch.object(a, 'check_login', return_value=(True,'fixture')),
                   mock.patch.object(a, '_order_targets_by_list', side_effect=lambda page, items:items),
                   mock.patch.object(a, '_send_consumer', side_effect=send),
                   mock.patch.object(a, '_keep_prewarmed_alive', return_value=True),
                   mock.patch.object(a, '_close_prewarmed'),
                   mock.patch.object(a, 'update_runtime'), mock.patch.object(a, 'load_runtime', return_value={}),
                   mock.patch.object(a, 'SEND_BUDGET_SECONDS', 5),
                   mock.patch.object(a.time, 'time', side_effect=lambda:now[0]),
                   mock.patch.object(a.time, 'sleep', side_effect=sleep)]
        for p in patches:p.start()
        try:
            result = a._run_send_impl(False)
        finally:
            for p in reversed(patches):p.stop()
        self.assertEqual(result['ok'], ['A'])
        self.assertEqual([row['name'] for row in result['deferred']], ['B'])
        self.assertEqual(waits, [], 'Budget cannot fit the minimum gap plus next action; defer immediately')


if __name__ == '__main__':
    unittest.main()
