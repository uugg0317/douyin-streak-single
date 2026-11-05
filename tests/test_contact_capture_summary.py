"""Capture counts use synthetic rows and do not inspect saved account data."""
import unittest

import _isolation  # noqa: F401  先隔离数据目录，再导入项目模块
from core.contact_capture import EXTRACT_JS, SCROLL_STATE_JS, scan_contacts, summarize_streaks


def rows():
    states = [
        {'streak':12, 'streak_status':'confirmed', 'streak_source':'class'},
        {'streak':None, 'streak_status':'unknown', 'streak_label':'重燃中 2/3'},
        {'streak':None, 'streak_status':'unknown', 'streak_label':'1 天后消失'},
        {'streak':0, 'streak_status':'none', 'streak_source':'class', 'streak_none_evidence':True},
        {'streak':None, 'streak_status':'unknown'},
        {'streak':None, 'streak_status':'suspect', 'streak_label':'重燃中 2/3'},
    ]
    return [{'name':f'合成好友-{i}', 'row_key':str(i), **value} for i, value in enumerate(states)]


class SummaryTests(unittest.TestCase):
    def test_categories_partition_this_capture_without_treating_labels_as_days(self):
        data = rows()
        before = [dict(row) for row in data]
        result = summarize_streaks(data)
        self.assertEqual(result, {'normal':1, 'rekindling':1, 'expiring':1, 'none':1, 'unknown':2})
        self.assertEqual(sum(result.values()), len(data))
        self.assertEqual(data, before)

    def test_empty_capture_has_valid_zero_counts(self):
        self.assertEqual(sum(summarize_streaks([]).values()), 0)

    def test_untrusted_or_conflicting_states_remain_unknown(self):
        data = [
            {'streak':12, 'streak_status':'confirmed', 'streak_source':'heuristic'},
            {'streak':True, 'streak_status':'confirmed', 'streak_source':'class'},
            {'streak':0, 'streak_status':'none', 'streak_source':'class'},
            {'streak':12, 'streak_status':'unknown', 'streak_label':'重燃中 2/3'},
            {'streak_status':'reset_pending', 'streak_label':'1 天后消失'},
            {'streak_status':'unknown', 'streak_label':'未读 12 条'},
        ]
        self.assertEqual(summarize_streaks(data)['unknown'], len(data))

    def test_scan_summary_counts_merged_rows_once_and_retains_completeness(self):
        data = rows()
        class Page:
            def evaluate(self, script):
                if script == EXTRACT_JS:
                    return data
                if script == SCROLL_STATE_JS:
                    return {'known':True, 'height':100, 'bottom':True, 'loading':False}
            def wait_for_timeout(self, value):
                pass
        collected = []
        result = scan_contacts(Page(), collected, max_rounds=5)
        self.assertTrue(result['complete'])
        self.assertEqual(result['reason'], 'stable_bottom')
        self.assertEqual(result['rows'], len(data))
        self.assertEqual(result['streak_counts'], summarize_streaks(data))
        self.assertFalse(result['platform_dom_verified'])

    def test_partial_scan_still_reports_only_observed_counts(self):
        class Page:
            def evaluate(self, script):
                if script == EXTRACT_JS:
                    return rows()[:2]
                if script == SCROLL_STATE_JS:
                    return {'known':False, 'height':0, 'bottom':False, 'loading':False}
            def wait_for_timeout(self, value):
                pass
        result = scan_contacts(Page(), [], max_rounds=1)
        self.assertFalse(result['complete'])
        self.assertEqual(result['reason'], 'unknown_scroll_container')
        self.assertEqual(result['rows'], 2)
        self.assertEqual(sum(result['streak_counts'].values()), 2)


if __name__ == '__main__':
    unittest.main()
