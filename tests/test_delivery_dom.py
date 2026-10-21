"""Real headless Playwright on in-memory local pages, with all network blocked.

These tests verify our DOM contract and control flow. They do not establish that
the current Douyin DOM exposes those markers or that a real message is delivered.
"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from _browser import requires_chromium

ROOT = Path(__file__).resolve().parents[1]
PRELUDE = r'''
from core import automation as A
from core.browser_errors import MessageDeliveryUnconfirmed
from playwright.sync_api import sync_playwright
from functools import partial
requests=[]
pw=sync_playwright().start()
browser=pw.chromium.launch(headless=True)
page=browser.new_page()
page.route('**/*',lambda route:(requests.append(route.request.url),route.abort()))
real_confirm=A._confirm_delivery
A._confirm_delivery=partial(real_confirm,timeout=0.8,quiet_window=0.25)
HTML=r"""<style>.conversationConversationItemtitle{display:block}#history>div{display:block}</style>
<div class="conversationConversationItemtitle">Same</div>
<section id="current" data-conversation-active="true" data-conversation-id="fixture-conversation-Same">
<h2 data-conversation-title>Same</h2>
<section id="history" data-message-list><div data-message-id="old" data-is-self="true"><span data-message-text>fixture-message</span></div></section>
<div contenteditable="true" id="input"></div>
</section>
<section id="other-conversation" data-conversation-active="false" data-conversation-id="fixture-conversation-Other">
<h2 data-conversation-title>Other</h2><section id="other-history" data-message-list></section>
</section>
<script>
window.enters=0;window.total_enters=0;window.mode='accept';
document.addEventListener('keydown',e=>{if(e.key==='Enter')window.total_enters++});
input.addEventListener('keydown', e => {
 if(e.key!=='Enter')return;
 e.preventDefault();window.enters++;
 const text=input.textContent.trim();input.textContent='';
 if(mode==='empty'||mode==='old')return;
 const bubble=document.createElement('div');bubble.setAttribute('data-message-id','new-'+enters);
 if(mode!=='unsupported')bubble.setAttribute('data-is-self',mode==='other'?'false':'true');
 const content=document.createElement('span');content.setAttribute('data-message-text','');content.textContent=text;bubble.append(content);
 const list=mode==='other-conversation'?document.getElementById('other-history'):mode==='outside-scope'?document.body:document.getElementById('history');
 list.append(bubble);
 if(mode==='pending'||mode==='pending-resolve')bubble.setAttribute('data-send-status','pending');
 if(mode==='pending-resolve')setTimeout(()=>bubble.setAttribute('data-send-status','sent'),400);
 if(mode==='reject')setTimeout(()=>bubble.setAttribute('data-send-status','failed'),130);
 if(mode==='alert'||mode==='outside-alert'||mode==='inline-alert')setTimeout(()=>{const el=document.createElement('div');el.setAttribute('role','alert');el.textContent='发送失败';(mode==='inline-alert'?(document.getElementById('current')||document.getElementById('chatRoot')):document.body).append(el)},130);
});
</script>"""

# 同一fixture的结构适配分支：不使用任何data-conversation-*或data-message-list。
import re
STRUCTURAL_HTML=HTML.replace('<div class="conversationConversationItemtitle">Same</div>',
    '<div class="conversationConversationItemcurConversation"><span class="conversationConversationItemtitle">Same</span></div>')
STRUCTURAL_HTML=STRUCTURAL_HTML.replace('<section id="current"',
    '<section id="chatRoot" style="position:absolute;left:400px;top:10px;width:450px;min-height:140px"')
STRUCTURAL_HTML=STRUCTURAL_HTML.replace('<h2 data-conversation-title>Same</h2>',
    '<header style="position:absolute;left:0;top:0">Same</header>')
STRUCTURAL_HTML=STRUCTURAL_HTML.replace('<div contenteditable="true" id="input">',
    '<div contenteditable="true" id="input" style="margin-top:45px;min-height:24px">')
STRUCTURAL_HTML=re.sub(r'\sdata-conversation-(?:active|id|title)(?:="[^"]*")?', '', STRUCTURAL_HTML)
STRUCTURAL_HTML=STRUCTURAL_HTML.replace(' data-message-list','')

page.set_content(HTML)
'''


@requires_chromium
class DeliveryDomTests(unittest.TestCase):
    def isolated(self, body):
        with tempfile.TemporaryDirectory(prefix='spark-delivery-dom-') as temporary:
            env = dict(os.environ)
            env.update(DATA_DIR=str(Path(temporary)/'data'), ENV_FILE_PATH=str(Path(temporary)/'missing.env'),
                       STATE_FILE_PATH='', SPARKKEEPER_BACKGROUND_TASKS='0', PYTHONUTF8='1', PYTHONDONTWRITEBYTECODE='1')
            setup = PRELUDE.replace('pw=sync_playwright().start()\n','').replace('browser=pw.chromium.launch(headless=True)\n','')
            source = 'from _browser import managed_chromium\nwith managed_chromium() as (pw,browser):\n'
            source += textwrap.indent(setup + '\n' + textwrap.dedent(body) + '\nassert not requests,requests\n', '    ')
            env['PYTHONPATH'] = str(ROOT/'tests') + os.pathsep + env.get('PYTHONPATH','')
            completed = subprocess.run([sys.executable, '-B', '-c', source], cwd=ROOT, env=env,
                                       capture_output=True, text=True, encoding='utf-8', timeout=25)
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_new_own_bubble_and_quiet_window_accept_once(self):
        self.isolated('''
        assert A._send_message(page,'fixture-message',False,target_name='Same')==(True,'accepted')
        assert page.evaluate('enters')==1
        ''')

    def test_cleared_input_without_new_bubble_remains_unconfirmed(self):
        self.isolated('''
        page.evaluate("mode='empty'")
        try:A._send_message(page,'fixture-message',False,target_name='Same')
        except MessageDeliveryUnconfirmed:pass
        else:raise AssertionError('empty input was accepted')
        assert page.evaluate('enters')==1
        ''')

    def test_historical_matching_bubble_cannot_confirm_new_send(self):
        self.isolated('''
        page.evaluate("mode='old'")
        try:A._send_message(page,'fixture-message',False,target_name='Same')
        except MessageDeliveryUnconfirmed:pass
        else:raise AssertionError('historical matching bubble was accepted')
        assert page.evaluate('enters')==1
        ''')

    def test_new_bubble_then_failure_is_rejected_without_second_enter(self):
        self.isolated('''
        page.evaluate("mode='reject'")
        try:A._send_message(page,'fixture-message',False,target_name='Same')
        except A.MessageDeliveryRejected:pass
        else:raise AssertionError('rejected new bubble was accepted')
        assert page.evaluate('enters')==1
        ''')

    def test_failure_alert_after_clear_is_rejected(self):
        self.isolated('''
        page.evaluate("mode='alert'")
        try:A._send_message(page,'fixture-message',False,target_name='Same')
        except A.MessageDeliveryRejected:pass
        else:raise AssertionError('post-send failure alert was accepted')
        assert page.evaluate('enters')==1
        ''')

    def test_other_sender_and_unsupported_layout_never_confirm(self):
        self.isolated('''
        for mode in ['other','unsupported']:
            page.set_content(HTML);page.evaluate(f"window.mode='{mode}'")
            try:A._send_message(page,'fixture-message',False,target_name='Same')
            except MessageDeliveryUnconfirmed:pass
            else:raise AssertionError('unidentified sender/layout was accepted')
            assert page.evaluate('enters')==1
        ''')


    def test_pending_bubble_does_not_confirm_until_sent(self):
        self.isolated("""
        page.evaluate("mode='pending-resolve'")
        assert A._send_message(page,'fixture-message',False,target_name='Same')==(True,'accepted')
        assert page.locator('[data-message-id="new-1"]').get_attribute('data-send-status')=='sent'
        assert page.evaluate('enters')==1
        """)

    def test_perpetually_pending_bubble_remains_unconfirmed(self):
        self.isolated("""
        page.evaluate("mode='pending'")
        try:A._send_message(page,'fixture-message',False,target_name='Same')
        except MessageDeliveryUnconfirmed:pass
        else:raise AssertionError('pending bubble was accepted')
        assert page.evaluate('enters')==1
        """)

    def test_same_name_rows_are_preserved_and_target_lookup_refuses_guessing(self):
        self.isolated('''
        page.set_content('<div class="conversationConversationItemtitle">Same</div><div class="conversationConversationItemtitle">Same</div>')
        assert A._find_contact(page,'Same') is None
        contacts=page.evaluate(A._EXTRACT_JS)
        assert len(contacts)==2 and contacts[0]['row_key']!=contacts[1]['row_key']
        ''')

    def test_left_highlight_does_not_send_to_unchanged_right_conversation(self):
        self.isolated('''
        from unittest.mock import Mock
        page.set_content(HTML)
        page.evaluate("document.querySelector('.conversationConversationItemtitle').classList.add('conversationConversationItemcurConversation')")
        page.evaluate("document.querySelector('.conversationConversationItemtitle').outerHTML='<div class=conversationConversationItemcurConversation><span class=conversationConversationItemtitle>Alpha</span></div><div style=position:absolute;left:400px;top:10px>Beta</div>'")
        A._locate_contact=Mock(return_value=True)
        ok,why=A.send_to_contact(page,'Alpha','fixture-message',False)
        assert ok is False and '校验失败' in why
        assert page.evaluate('enters')==0
        ''')



    def test_matching_own_bubble_in_another_visible_conversation_cannot_confirm(self):
        self.isolated("""
        page.evaluate("mode='other-conversation'")
        try:A._send_message(page,'fixture-message',False,target_name='Same')
        except MessageDeliveryUnconfirmed:pass
        else:raise AssertionError('another conversation confirmed current send')
        assert page.evaluate('enters')==1 and page.evaluate('total_enters')==1
        assert page.locator('#other-history [data-message-id="new-1"]').count()==1
        """)

    def test_matching_bubble_outside_all_conversation_scopes_cannot_confirm(self):
        self.isolated("""
        page.evaluate("mode='outside-scope'")
        try:A._send_message(page,'fixture-message',False,target_name='Same')
        except MessageDeliveryUnconfirmed:pass
        else:raise AssertionError('unbound global bubble confirmed current send')
        assert page.evaluate('enters')==1
        """)

    def test_new_global_failure_alert_vetoes_current_bubble_without_second_enter(self):
        self.isolated("""
        page.evaluate("mode='outside-alert'")
        try:A._send_message(page,'fixture-message',False,target_name='Same')
        except A.MessageDeliveryRejected:pass
        else:raise AssertionError('global post-send failure portal was accepted')
        assert page.evaluate('enters')==1
        assert page.locator('body > [role="alert"]').count()==1
        """)

    def test_missing_ambiguous_or_wrong_current_scope_never_presses_enter(self):
        self.isolated("""
        changes=[
            "current.removeAttribute('data-conversation-active')",
            "current.removeAttribute('data-conversation-id')",
            "current.querySelector('[data-conversation-title]').textContent='Different'",
            "document.getElementById('other-conversation').setAttribute('data-conversation-active','true')",
            "const second=input.cloneNode(true);second.id='second-input';current.append(second)",
        ]
        for change in changes:
            page.set_content(HTML);page.evaluate(change)
            try:
                result=A._send_message(page,'fixture-message',False,target_name='Same')
                assert result[0] is False,result
            except MessageDeliveryUnconfirmed as error:
                assert getattr(error,'enter_attempted',None) is False
            assert page.evaluate('enters')==0 and page.evaluate('total_enters')==0
            assert page.locator('#input').inner_text()==''
        """)

    def test_missing_or_duplicate_message_list_never_presses_enter(self):
        self.isolated("""
        for change in ("document.getElementById('history').removeAttribute('data-message-list')",
                       "const list=document.getElementById('history');const copy=list.cloneNode(true);copy.id='second-history';current.append(copy)"):
            page.set_content(HTML);page.evaluate(change)
            try:
                result=A._send_message(page,'fixture-message',False,target_name='Same')
                assert result[0] is False,result
            except MessageDeliveryUnconfirmed as error:
                assert getattr(error,'enter_attempted',None) is False
            assert page.evaluate('enters')==0 and page.evaluate('total_enters')==0
        """)

    def test_switching_conversation_id_after_enter_remains_unconfirmed(self):
        self.isolated("""
        page.evaluate("input.addEventListener('keydown',e=>{if(e.key==='Enter')setTimeout(()=>current.setAttribute('data-conversation-id','changed-conversation'),130)})")
        try:A._send_message(page,'fixture-message',False,target_name='Same')
        except MessageDeliveryUnconfirmed:pass
        else:raise AssertionError('changed conversation ID was accepted')
        assert page.evaluate('enters')==1
        """)

    def test_switching_current_active_scope_after_enter_remains_unconfirmed(self):
        self.isolated("""
        page.evaluate("input.addEventListener('keydown',e=>{if(e.key==='Enter')setTimeout(()=>{current.setAttribute('data-conversation-active','false');document.getElementById('other-conversation').setAttribute('data-conversation-active','true')},130)})")
        try:A._send_message(page,'fixture-message',False,target_name='Same')
        except MessageDeliveryUnconfirmed:pass
        else:raise AssertionError('different active scope was accepted')
        assert page.evaluate('enters')==1
        """)

    def test_replaced_scope_with_identical_attributes_is_not_the_bound_scope(self):
        self.isolated("""
        page.evaluate("input.addEventListener('keydown',e=>{if(e.key==='Enter')setTimeout(()=>current.replaceWith(current.cloneNode(true)),130)})")
        try:A._send_message(page,'fixture-message',False,target_name='Same')
        except MessageDeliveryUnconfirmed:pass
        else:raise AssertionError('replaced scope was accepted')
        assert page.evaluate('enters')==1
        """)

    def test_replaced_message_list_cannot_confirm_against_new_dom(self):
        self.isolated("""
        page.evaluate("input.addEventListener('keydown',e=>{if(e.key==='Enter')setTimeout(()=>document.getElementById('history').replaceWith(document.getElementById('history').cloneNode(true)),130)})")
        try:A._send_message(page,'fixture-message',False,target_name='Same')
        except MessageDeliveryUnconfirmed:pass
        else:raise AssertionError('replaced message list was accepted')
        assert page.evaluate('enters')==1
        """)

    def test_replaced_input_cannot_confirm_against_new_dom(self):
        self.isolated("""
        page.evaluate("input.addEventListener('keydown',e=>{if(e.key==='Enter')setTimeout(()=>input.replaceWith(input.cloneNode(true)),130)})")
        try:A._send_message(page,'fixture-message',False,target_name='Same')
        except MessageDeliveryUnconfirmed:pass
        else:raise AssertionError('replaced editor was accepted')
        assert page.evaluate('enters')==1
        """)

    def test_changed_title_after_enter_remains_unconfirmed(self):
        self.isolated("""
        page.evaluate("input.addEventListener('keydown',e=>{if(e.key==='Enter')setTimeout(()=>current.querySelector('[data-conversation-title]').textContent='Different',130)})")
        try:A._send_message(page,'fixture-message',False,target_name='Same')
        except MessageDeliveryUnconfirmed:pass
        else:raise AssertionError('changed target title was accepted')
        assert page.evaluate('enters')==1
        """)

    def test_focus_transfer_after_click_never_edits_or_enters_another_editor(self):
        self.isolated("""
        page.evaluate("const other=document.createElement('div');other.id='outside-editor';other.contentEditable='true';document.getElementById('other-conversation').append(other);input.addEventListener('focus',()=>other.focus())")
        try:A._send_message(page,'fixture-message',False,target_name='Same')
        except MessageDeliveryUnconfirmed as error:assert error.enter_attempted is False
        else:raise AssertionError('lost editor focus after click was not detected')
        assert page.evaluate('total_enters')==0 and page.evaluate('enters')==0
        assert page.locator('#outside-editor').inner_text()==''
        """)

    def test_focus_transfer_after_typing_blocks_global_enter_on_another_editor(self):
        self.isolated("""
        page.evaluate("const other=document.createElement('div');other.id='outside-editor';other.contentEditable='true';document.getElementById('other-conversation').append(other);input.addEventListener('input',()=>{if(input.textContent==='fixture-message')other.focus()})")
        try:A._send_message(page,'fixture-message',False,target_name='Same')
        except MessageDeliveryUnconfirmed as error:assert error.enter_attempted is False
        else:raise AssertionError('focus transfer before Enter was not detected')
        assert page.evaluate('total_enters')==0 and page.evaluate('enters')==0
        assert page.locator('#input').inner_text()=='fixture-message'
        assert page.locator('#outside-editor').inner_text()==''
        """)



    def test_structural_adapter_without_test_scope_attributes_accepts_current_own_message(self):
        self.isolated("""
        page.set_content(STRUCTURAL_HTML)
        assert page.locator('[data-conversation-active],[data-conversation-id],[data-conversation-title],[data-message-list]').count()==0
        assert A._send_message(page,'fixture-message',False,target_name='Same')==(True,'accepted')
        assert page.evaluate('enters')==1 and page.evaluate('total_enters')==1
        """)

    def test_structural_adapter_ignores_matching_bubble_in_adjacent_conversation(self):
        self.isolated("""
        page.set_content(STRUCTURAL_HTML);page.evaluate("mode='other-conversation'")
        try:A._send_message(page,'fixture-message',False,target_name='Same')
        except MessageDeliveryUnconfirmed:pass
        else:raise AssertionError('adjacent conversation confirmed structural send')
        assert page.evaluate('enters')==1
        assert page.locator('#other-history [data-message-id="new-1"]').count()==1
        """)

    def test_structural_adapter_rejects_replaced_root_after_enter(self):
        self.isolated("""
        page.set_content(STRUCTURAL_HTML)
        page.evaluate("input.addEventListener('keydown',e=>{if(e.key==='Enter')setTimeout(()=>document.getElementById('chatRoot').replaceWith(document.getElementById('chatRoot').cloneNode(true)),130)})")
        try:A._send_message(page,'fixture-message',False,target_name='Same')
        except MessageDeliveryUnconfirmed:pass
        else:raise AssertionError('replaced structural root was accepted')
        assert page.evaluate('enters')==1
        """)

    def test_structural_adapter_does_not_keep_confirming_after_left_selection_changes(self):
        self.isolated("""
        page.set_content(STRUCTURAL_HTML)
        page.evaluate("input.addEventListener('keydown',e=>{if(e.key==='Enter')setTimeout(()=>document.querySelector('.conversationConversationItemcurConversation').classList.remove('conversationConversationItemcurConversation'),130)})")
        try:A._send_message(page,'fixture-message',False,target_name='Same')
        except MessageDeliveryUnconfirmed:pass
        else:raise AssertionError('changed structural left selection was accepted')
        assert page.evaluate('enters')==1
        """)



    def test_inline_current_scope_failure_also_vetoes_new_bubble(self):
        self.isolated("""
        page.evaluate("mode='inline-alert'")
        try:A._send_message(page,'fixture-message',False,target_name='Same')
        except A.MessageDeliveryRejected:pass
        else:raise AssertionError('inline post-send failure was accepted')
        assert page.evaluate('enters')==1
        assert page.locator('#current [role="alert"]').count()==1
        """)

    def test_existing_global_failure_signal_cannot_turn_a_new_bubble_green(self):
        self.isolated("""
        page.evaluate("const alert=document.createElement('div');alert.setAttribute('role','alert');alert.textContent='发送失败';document.body.append(alert)")
        try:A._send_message(page,'fixture-message',False,target_name='Same')
        except (MessageDeliveryUnconfirmed,A.MessageDeliveryRejected):pass
        else:raise AssertionError('existing global failure signal was ignored')
        assert page.evaluate('enters')==1
        """)


if __name__ == '__main__':
    unittest.main()
