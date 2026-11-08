from unittest import TestCase, mock
import _isolation  # noqa: F401  先隔离数据目录，再导入项目模块
from core import automation as A


def row(name='A',value=12):
    return {'name':name,'avatar':'fixture-'+name,'row_key':name+'|1','streak':value,'streak_status':'confirmed','streak_source':'class'}


class Page:
    def __init__(self, frames, loading=False, known=True):
        self.frames=frames; self.reads=0; self.loading=loading; self.known=known
        self.mouse=mock.Mock()
    def evaluate(self, script):
        if script==A._EXTRACT_JS:
            value=self.frames[min(self.reads,len(self.frames)-1)]; self.reads+=1
            return value
        return {'known':self.known,'height':100,'bottom':True,'loading':self.loading,'empty':False}
    def wait_for_timeout(self, value): pass


class ScrollTests(TestCase):
    def test_streak_changes_update_existing_row_not_duplicate_it(self):
        page=Page([[row(value=12)],[row(value=12)],[row(value=13)],[row(value=13)]])
        collected=[]
        result=A._scroll_and_extract(page,collected,max_rounds=4)
        self.assertEqual(len(collected),1)
        self.assertEqual(collected[0]['streak'],13)
        self.assertTrue(result['complete'])

    def test_unstable_pair_is_unknown_and_cannot_claim_complete(self):
        page=Page([[row(value=12)],[row(value=87)]])
        collected=[]; result=A._scroll_and_extract(page,collected,max_rounds=1)
        self.assertEqual(collected[0]['streak_status'],'unknown')
        self.assertIsNone(collected[0]['streak'])
        self.assertFalse(result['complete'])

    def test_loading_or_unknown_container_never_claims_complete(self):
        for options in [{'loading':True},{'known':False}]:
            with self.subTest(options=options):
                result=A._scroll_and_extract(Page([[row()]],**options),[],max_rounds=3)
                self.assertFalse(result['complete'])

    def test_late_batch_is_included_before_stability(self):
        frames=[[row()],[row()],[row(),row('B')],[row(),row('B')]]
        collected=[]; result=A._scroll_and_extract(Page(frames),collected,max_rounds=5)
        self.assertEqual([x['name'] for x in collected],['A','B'])
        self.assertTrue(result['complete'])

    def test_incomplete_pair_keeps_previous_confirmed_reading(self):
        frames = [[row(value=12)], [row(value=12)], [row(value=12)], [row('B', value=5)]]
        collected = []
        result = A._scroll_and_extract(Page(frames), collected, max_rounds=2)
        by_name = {item['name']: item for item in collected}
        self.assertEqual(by_name['A']['streak'], 12)
        self.assertEqual(by_name['A']['streak_status'], 'confirmed')
        self.assertFalse(result['complete'])

