"""Actual Playwright against explicit synthetic HTML; never a platform claim."""
import json
from pathlib import Path
import unittest
from _browser import managed_chromium, requires_chromium
from core import automation

FIXTURE = Path(__file__).parent / 'fixtures' / 'chat_rows'


@requires_chromium
class CaptureDOMTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._browser_owner = managed_chromium()
        cls.pw, cls.browser = cls._browser_owner.__enter__()
        cls.addClassCleanup(cls._close_browser)

    @classmethod
    def tearDownClass(cls):
        cls._close_browser()

    @classmethod
    def _close_browser(cls):
        owner = getattr(cls, '_browser_owner', None)
        cls._browser_owner = None
        if owner is not None:
            owner.__exit__(None, None, None)

    def setUp(self):
        self.context = self.browser.new_context(service_workers='block')
        self.context.route('**/*', lambda route: route.abort())
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()

    def extract(self, html=None):
        self.page.set_content(html or (FIXTURE/'synthetic.html').read_text(encoding='utf-8'))
        return self.page.evaluate(automation._EXTRACT_JS)

    def test_fixture_declares_synthetic_provenance_and_network_free_images(self):
        expected = json.loads((FIXTURE/'expected.json').read_text(encoding='utf-8'))
        self.assertIn('synthetic', expected['provenance'])
        self.page.set_content((FIXTURE/'synthetic.html').read_text(encoding='utf-8'))
        self.assertEqual(self.page.locator('.conversationConversationItemtitle').count(), 5)
        self.assertTrue(all(url.startswith('data:') for url in self.page.locator('img').evaluate_all('(xs)=>xs.map(x=>x.src)')))

    def test_adjacent_empty_row_never_inherits_streak_or_avatar_in_either_order(self):
        occupied = '<div class="conversationConversationItemwrapper"><span class="conversationConversationItemtitle">A</span><span class="commonStreak">81</span><img style="width:32px;height:32px" src="data:image/svg+xml,%3Csvg/%3E"></div>'
        empty = '<div class="conversationConversationItemwrapper"><span class="conversationConversationItemtitle">B</span></div>'
        for rows in [occupied+empty, empty+occupied]:
            with self.subTest(order=rows[:50]):
                values = self.extract('<div role="list">'+rows+'</div>')
                b = next(row for row in values if row['name']=='B')
                self.assertIn(b.get('streak'), ['', None])
                self.assertEqual(b.get('avatar'), '')

    def test_title_without_unique_row_has_no_streak_or_avatar(self):
        rows = self.extract('<div><span class="commonStreak">88</span><span class="conversationConversationItemtitle">Detached</span></div>')
        self.assertIn(rows[0].get('streak'), ['', None])
        ambiguous = self.extract('<div class="conversationConversationItemwrapper"><span class="conversationConversationItemtitle">A</span><span class="conversationConversationItemtitle">B</span><span class="commonStreak">77</span></div>')
        self.assertTrue(all(row.get('streak') in ['',None] for row in ambiguous))

    def test_unread_preview_broad_classes_and_hidden_badges_never_become_streaks(self):
        for content in ['<span data-unread-count>12</span>', '<span data-message-preview>🔥 23</span>',
                        '<span class="fire-notification">33</span>', '<span data-streak-badge style="display:none">44</span>',
                        '<span data-streak-badge>3 条未读 12</span>', '<span style="opacity:0"><span data-streak-badge>55</span></span>',
                        '<span aria-hidden="true"><span data-streak-badge>55</span></span>']:
            with self.subTest(content=content):
                row = self.extract('<div class="conversationConversationItemwrapper"><span class="conversationConversationItemtitle">A</span>'+content+'</div>')[0]
                self.assertIn(row.get('streak'), ['',None])

    def test_streak_badge_wins_over_unread_and_sources_are_explicit(self):
        rows = self.extract()
        a = next(row for row in rows if row['name']=='合成甲')
        d = next(row for row in rows if row['name']=='合成丁')
        self.assertEqual(int(a['streak']),81)
        self.assertEqual(a.get('streak_source'),'class')
        self.assertEqual(int(d['streak']),12)
        self.assertEqual(d.get('streak_source'),'emoji')

    def test_row_key_does_not_change_when_only_streak_changes(self):
        first=self.extract()[0]
        self.page.locator('.commonStreak').evaluate('(el)=>el.textContent="82"')
        second=self.page.evaluate(automation._EXTRACT_JS)[0]
        self.assertEqual(first['row_key'],second['row_key'])

    def test_real_dom_scroll_waits_for_late_rows_and_reports_complete(self):
        self.page.set_content('''<div id="list" role="list" style="height:80px;overflow-y:auto;width:300px">
          <div class="conversationConversationItemwrapper" style="height:60px"><span class="conversationConversationItemtitle">A</span><span data-streak-badge>12</span></div>
          <div class="conversationConversationItemwrapper" style="height:60px"><span class="conversationConversationItemtitle">B</span></div></div>
          <script>let added=false;list.addEventListener('scroll',()=>{if(added)return;added=true;
          list.setAttribute('aria-busy','true');setTimeout(()=>{const x=document.createElement('div');x.className='conversationConversationItemwrapper';x.style.height='60px';x.innerHTML='<span class="conversationConversationItemtitle">C</span>';list.append(x);list.removeAttribute('aria-busy');},100);});</script>''')
        collected=[]
        summary=automation._scroll_and_extract(self.page,collected,max_rounds=8)
        self.assertEqual({x['name'] for x in collected},{'A','B','C'})
        self.assertTrue(summary['complete'])

    def test_bottom_wait_includes_pagination_that_arrives_after_two_seconds(self):
        self.page.set_content('''<div id="list" role="list" style="height:80px;overflow-y:auto;width:300px">
          <div class="conversationConversationItemwrapper" style="height:60px"><span class="conversationConversationItemtitle">A</span></div>
          <div class="conversationConversationItemwrapper" style="height:60px"><span class="conversationConversationItemtitle">B</span></div></div>
          <script>let added=false;list.addEventListener('scroll',()=>{if(added)return;added=true;
          setTimeout(()=>{const x=document.createElement('div');x.className='conversationConversationItemwrapper';x.style.height='60px';x.innerHTML='<span class="conversationConversationItemtitle">C</span>';list.append(x);},2200);});</script>''')
        collected=[]
        summary=automation._scroll_and_extract(self.page,collected,max_rounds=8)
        self.assertEqual({x['name'] for x in collected},{'A','B','C'})
        self.assertTrue(summary['complete'])

    def test_all_synthetic_expected_states_match(self):
        expected=json.loads((FIXTURE/'expected.json').read_text(encoding='utf-8'))['rows']
        actual=self.extract()
        self.assertEqual([{k:row.get(k) for k in ('name','streak')} for row in actual],
                         [{k:row.get(k) for k in ('name','streak')} for row in expected])
        self.assertEqual([x['streak_status'] for x in actual],[x['status'] for x in expected])

    def test_absence_gate_remains_off_and_missing_badge_is_unknown(self):
        from core.contact_capture import NONE_BY_ABSENCE_ENABLED
        self.assertIs(NONE_BY_ABSENCE_ENABLED,False)
        row=self.extract('<div class="conversationConversationItemwrapper"><span class="conversationConversationItemtitle">合成无标记</span></div>')[0]
        self.assertEqual(row['streak_status'],'unknown');self.assertIsNone(row['streak'])


class StrictStreakParserTests(unittest.TestCase):
    def test_full_value_only_and_explicit_failure(self):
        from core.ledger import _parse_streak
        for value in [81,'81','81天','🔥 81',' 🔥 81天 ',999]:
            with self.subTest(value=value):
                self.assertEqual(_parse_streak(value),999 if value==999 else 81)
        for value in ['',None,False,True,0,-1,1000,'3 条未读 12','abc','12 13','012','12.5',81.5]:
            with self.subTest(value=value):
                self.assertIsNone(_parse_streak(value))
