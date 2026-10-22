import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

class FixtureValidationTests(TestCase):
    def root(self):
        temp=TemporaryDirectory();self.addCleanup(temp.cleanup);return Path(temp.name)
    def sample(self,root,html=None):
        sample=root/'real/2026-10-18-01';sample.mkdir(parents=True)
        (sample/'meta.json').write_text(json.dumps({'source':'real','collected_at':'2026-10-18','row_type':'with_streak',
            'expected':{'name':'合成甲','streak_status':'confirmed','streak_days':12}},ensure_ascii=False),encoding='utf-8')
        (sample/'row.html').write_text(html or '<div><span class="conversationConversationItemtitle">合成甲</span><img src="placeholder-avatar.svg"><span>12天</span></div>',encoding='utf-8')
        return sample
    def check(self,root):
        from tools.check_fixtures import check_fixtures
        return check_fixtures(root)
    def test_no_real_directory_is_explicitly_pending_not_fake_verification(self):
        report=self.check(self.root());self.assertEqual(report['real_samples'],0)
        self.assertFalse(report['platform_dom_verified']);self.assertIn('尚无真实样本',report['message'])
    def test_sanitized_declared_real_sample_passes_but_does_not_prove_origin(self):
        root=self.root();self.sample(root);report=self.check(root)
        self.assertEqual(report['real_samples'],1);self.assertFalse(report['platform_dom_verified'])
    def test_private_patterns_and_external_references_are_rejected(self):
        from tools.check_fixtures import FixtureError
        for html in ['<img src="https://example.invalid/avatar.png">','<span>13800138000</span>',
                     '<span>person@example.invalid</span>','<div data-token="synthetic"></div>',
                     '<span class="conversationConversationItemtitle">真实名称</span>',
                     '<script>alert(1)</script>']:
            with self.subTest(kind=html[:20]):
                root=self.root();self.sample(root,html)
                with self.assertRaises(FixtureError):self.check(root)
    def test_missing_meta_and_synthetic_claiming_real_are_rejected(self):
        from tools.check_fixtures import FixtureError
        root=self.root();sample=self.sample(root);(sample/'meta.json').unlink()
        with self.assertRaises(FixtureError):self.check(root)
        root=self.root();(root/'synthetic.html').write_text('<script>{"source":"real"}</script>',encoding='utf-8')
        with self.assertRaises(FixtureError):self.check(root)
