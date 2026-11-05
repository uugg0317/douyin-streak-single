"""没有消息标记的页面：用己方一侧的文字证据确认发送，宽松兜底默认关闭。

真实 Headless Chromium 打开内存中的本地页面，所有网络请求都被拦截。
这些测试只验证判据与控制流，不能证明抖音真实页面一定如此渲染。
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
import json
from functools import partial
from core import automation as A
from core.browser_errors import MessageDeliveryUnconfirmed
requests=[]
page=browser.new_page()
page.route('**/*',lambda route:(requests.append(route.request.url),route.abort()))
A._confirm_delivery=partial(A._confirm_delivery,timeout=0.8,quiet_window=0.25)
BUBBLE='<div class="bubble self" style="margin-left:auto;width:fit-content">fixture-message</div>'
HTML=r"""<div class="conversationConversationItemcurConversation"><span class="conversationConversationItemtitle">Same</span></div>
<section id="chatRoot" style="position:absolute;left:400px;top:10px;width:450px;min-height:160px">
<header style="position:absolute;left:0;top:0">Same</header>
<div id="history" style="padding-top:30px">HISTORY</div>
<div contenteditable="true" id="input" style="min-height:24px"></div>
</section>
<script>
window.enters=0;window.mode='accept';
input.addEventListener('keydown', e => {
 if(e.key!=='Enter')return;
 e.preventDefault();window.enters++;
 const text=input.textContent.trim();input.textContent='';
 if(mode==='empty')return;
 if(mode==='alert'){setTimeout(()=>{const el=document.createElement('div');el.setAttribute('role','alert');el.textContent='发送失败';document.body.append(el)},130);return;}
 const bubble=document.createElement('div');bubble.className='bubble';bubble.style.width='fit-content';
 if(mode!=='other')bubble.style.marginLeft='auto';
 bubble.textContent=text;document.getElementById('history').append(bubble);
});
</script>"""
def load(history=BUBBLE, mode='accept'):
    page.set_content(HTML.replace('HISTORY', history))
    page.evaluate(f"window.mode={json.dumps(mode)}")
'''


@requires_chromium
class DeliveryTextEvidenceTests(unittest.TestCase):
    def isolated(self, body):
        with tempfile.TemporaryDirectory(prefix='spark-delivery-text-') as temporary:
            env = dict(os.environ)
            env.update(DATA_DIR=str(Path(temporary) / 'data'), ENV_FILE_PATH=str(Path(temporary) / 'missing.env'),
                       STATE_FILE_PATH='', SPARKKEEPER_BACKGROUND_TASKS='0', DELIVERY_ACCEPT_INPUT_CLEARED='off',
                       PYTHONUTF8='1', PYTHONDONTWRITEBYTECODE='1')
            env['PYTHONPATH'] = str(ROOT / 'tests') + os.pathsep + env.get('PYTHONPATH', '')
            source = 'from _browser import managed_chromium\nwith managed_chromium() as (pw,browser):\n'
            source += textwrap.indent(PRELUDE + '\n' + textwrap.dedent(body) + '\nassert not requests,requests\n', '    ')
            completed = subprocess.run([sys.executable, '-B', '-c', source], cwd=ROOT, env=env,
                                       capture_output=True, text=True, encoding='utf-8', timeout=30)
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_new_own_side_text_confirms_send_once(self):
        self.isolated('''
        load()
        assert A._send_message(page,'fixture-message',False,target_name='Same')==(True,'accepted_text')
        assert page.evaluate('enters')==1
        ''')

    def test_history_alone_or_other_side_text_never_confirms(self):
        self.isolated('''
        for mode in ('empty','other'):
            load(mode=mode)
            try:
                A._send_message(page,'fixture-message',False,target_name='Same')
            except MessageDeliveryUnconfirmed:
                pass
            else:
                raise AssertionError(mode+' 不能确认发送')
            assert page.evaluate('enters')==1
        ''')

    def test_failure_alert_rejects_even_with_cleared_input(self):
        self.isolated('''
        load(mode='alert')
        try:
            A._send_message(page,'fixture-message',False,target_name='Same')
        except A.MessageDeliveryRejected:
            pass
        else:
            raise AssertionError('失败提示必须判为拒绝')
        ''')

    def test_input_cleared_fallback_is_opt_in(self):
        self.isolated('''
        A.INPUT_CLEARED_QUIET_SECONDS=0.2
        load(history='', mode='empty')
        try:
            A._send_message(page,'fixture-message',False,target_name='Same')
        except MessageDeliveryUnconfirmed:
            pass
        else:
            raise AssertionError('默认不能只凭输入框清空判成功')
        A.DELIVERY_ACCEPT_INPUT_CLEARED=True
        load(history='', mode='empty')
        assert A._send_message(page,'fixture-message',False,target_name='Same')==(True,'accepted_input_cleared')
        load(history='', mode='alert')
        try:
            A._send_message(page,'fixture-message',False,target_name='Same')
        except A.MessageDeliveryRejected:
            pass
        else:
            raise AssertionError('兜底模式下失败提示仍须判为拒绝')
        ''')

    def test_dry_run_reports_text_history_and_saves_structure_without_private_text(self):
        self.isolated('''
        load()
        assert A._send_message(page,'fixture-message',True,target_name='Same')==(True,'dry-run')
        check=A._send_context.delivery_check
        assert check['reason']=='historical_text_observed' and check['history_text_matches']==1, check
        assert page.evaluate('enters')==0
        saved=A.DELIVERY_PROBE_PATH.read_text(encoding='utf-8')
        record=json.loads(saved)
        assert record['adapter']=='structural' and record['structure']['visible_elements']>0
        assert 'fixture-message' not in saved and 'Same' not in saved
        ''')


if __name__ == '__main__':
    unittest.main()
