"""以已观察到的网页类名重建脱敏夹具；不含账号、头像或私信。"""
from datetime import datetime, timezone
import unittest
from _browser import managed_chromium, requires_chromium
from core.contact_capture import EXTRACT_JS
from core.streaks import observe_streak


def row(name, text=None, icon='normal/normal_normal.png', label=False, extra=''):
    badge = '' if text is None else f'''<div class="commonStreakstreakContainer">
    <img class="commonStreakicon" style="width:16px;height:16px" src="https://fixture.invalid/flame_icon/{icon}">
    <div class="commonStreaknormalText {'commonStreaklabelText' if label else ''}">{text}</div></div>'''
    return f'<div class="conversationConversationItemwrapper"><span class="conversationConversationItemtitle">{name}</span>{badge}{extra}</div>'


@requires_chromium
class RealBadgeContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.owner=managed_chromium();cls.pw,cls.browser=cls.owner.__enter__()
        cls.addClassCleanup(lambda:cls.owner.__exit__(None,None,None))

    def setUp(self):
        self.context=self.browser.new_context(service_workers='block')
        self.context.route('**/*',lambda route:route.abort())
        self.addCleanup(self.context.close)
        self.page=self.context.new_page()

    def read(self,html):
        self.page.set_content(html)
        return self.page.evaluate(EXTRACT_JS)

    def test_observed_numeric_variants_and_no_badge(self):
        actual=self.read(row('A','215')+row('B','90')+row('C','348')+row('D','565','soulmate/normal_soulmate.png')+row('E'))
        self.assertEqual([x['streak'] for x in actual],[215,90,348,565,None])
        self.assertEqual([x['streak_status'] for x in actual],['confirmed']*4+['unknown'])
        self.assertTrue(all(x['avatar']=='' for x in actual),'火花图片不得充当好友头像')

    def test_special_labels_never_become_days(self):
        actual=self.read(row('A','重燃中 2/3','mate/gray_mate.png',True)+row('B','1 天后消失','normal/gray_normal.png',True))
        self.assertEqual([x['streak_label'] for x in actual],['重燃中 2/3','1 天后消失'])
        self.assertTrue(all(x['streak'] is None and x['streak_status']=='unknown' for x in actual))

    def test_gray_unknown_or_missing_icon_is_not_numeric_evidence(self):
        for icon in ['mate/gray_mate.png','unrecognized.png']:
            self.assertEqual(self.read(row('A','81',icon))[0]['streak_status'],'unknown')
        html=row('A','81').replace('class="commonStreakicon"','class="other-icon"')
        self.assertEqual(self.read(html)[0]['streak_status'],'unknown')

    def test_hidden_preview_and_unread_never_supply_days(self):
        content=row('A','215',extra='<span data-unread-count>8</span><span data-message-preview>🔥 99</span>')
        self.assertEqual(self.read(content)[0]['streak'],215)
        hidden=content.replace('class="commonStreakstreakContainer"','class="commonStreakstreakContainer" style="display:none"')
        self.assertEqual(self.read(hidden)[0]['streak_status'],'unknown')

    def test_ambiguous_or_nested_row_badges_do_not_confirm_parent(self):
        child=row('B','90')
        actual=self.read(row('A',extra=child))
        self.assertEqual(actual[0]['streak_status'],'unknown')
        self.assertEqual(actual[1]['streak'],90)
        duplicated=row('A','215').replace('</div></div>','<div class="commonStreaknormalText">90</div></div></div>')
        self.assertNotEqual(self.read(duplicated)[0]['streak_status'],'confirmed')


    def test_row_identity_survives_cdn_and_thumbnail_changes(self):
        a='https://p3.douyinpic.com/img/aweme-avatar/fixture_resource.png~c5_100x100.jpeg?first=1'
        b='https://p26.douyinpic.com/img/aweme-avatar/fixture_resource.png~c5_168x168.webp?second=2'
        first=self.read(row('合成',extra=f'<img class="avatar" style="width:32px;height:32px" src="{a}">'))[0]
        second=self.read(row('合成',extra=f'<img class="avatar" style="width:32px;height:32px" src="{b}">'))[0]
        self.assertEqual(first['row_key'],second['row_key'])
        self.assertNotEqual(first['avatar'],second['avatar'])

    def test_numeric_placeholder_is_pending_not_a_resolved_contact(self):
        value=self.read(row('123456789',extra='<img style="width:32px;height:32px" src="data:image/png;base64,fixture">'))[0]
        self.assertTrue(value['profile_pending'])


class SpecialBadgePersistenceTests(unittest.TestCase):
    def test_special_label_preserves_history_then_clears_on_normal_observation(self):
        entry={'streak_days':81,'streak_status':'confirmed'}
        now=datetime(2026,10,19,tzinfo=timezone.utc)
        observe_streak(entry,{'streak_status':'unknown','streak_label':'重燃中 2/3'},now)
        self.assertEqual(entry['streak_days'],81)
        self.assertEqual(entry['streak_label'],'重燃中 2/3')
        self.assertEqual(entry['streak_status'],'unknown')
        observe_streak(entry,{'streak_status':'unknown'},now)
        self.assertIsNone(entry['streak_label'])
        observe_streak(entry,{'streak_status':'unknown','streak_label':'任意私信内容'},now)
        self.assertIsNone(entry['streak_label'])
