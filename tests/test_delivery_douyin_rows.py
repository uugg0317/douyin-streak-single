"""按真实抖音结构摘要搭的本地页面：消息行带 data-index，己方消息带 isFromMe 类名，
文案渲染成表情图片（没有文字）；按下发送后左侧会话列表会重排、换掉选中行节点。

真实 Headless Chromium 打开内存页面，所有网络请求都被拦截。只验证判据与控制流。
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
ROW='<div class="conversationConversationItemwrapper ACTIVE"><span class="conversationConversationItemtitle">NAME</span></div>'
HTML=r"""<div id="list">LIST</div>
<section id="chatRoot" style="position:absolute;left:400px;top:10px;width:450px;min-height:200px">
<header style="position:absolute;left:0;top:0">Same</header>
<div id="history" style="padding-top:30px">
<div data-index="0"><div class="messageMessageBoxmessageBox messageMessageBoxisFromMe"><img class="TextMessageTextemoji" style="width:20px;height:20px" alt=""></div></div>
<div data-index="1"><div class="messageMessageBoxmessageBox"><span>synthetic reply</span></div></div>
</div>
<div contenteditable="true" id="input" style="min-height:24px"></div>
</section>
<script>
window.enters=0;window.mode='accept';window.nextIndex=2;
input.addEventListener('keydown', e => {
 if(e.key!=='Enter')return;
 e.preventDefault();window.enters++;input.textContent='';
 const active=mode==='switch'?'Other':'Same';
 document.getElementById('list').innerHTML=REORDERED.replace(/CURRENT_FRIEND/g, active);
 if(mode==='empty'||mode==='switch')return;
 const history=document.getElementById('history');
 if(mode==='rerender-old'){const old=history.querySelector('[data-index="0"]');const copy=old.cloneNode(true);old.replaceWith(copy);return;}
 if(mode==='reversed'){for(const item of history.querySelectorAll('[data-index]'))item.setAttribute('data-index',String(Number(item.getAttribute('data-index'))+1));}
 const row=document.createElement('div');row.setAttribute('data-index',mode==='reversed'?'0':String(window.nextIndex++));
 const own=mode==='other'?'':' messageMessageBoxisFromMe';
 const state=mode==='fail'?' messageMessageBoxsendFail':mode==='pending'?' messageMessageBoxsending':'';
 row.innerHTML='<div class="messageMessageBoxmessageBox'+own+state+'"><img class="TextMessageTextemoji" style="width:20px;height:20px" alt=""></div>';
 document.getElementById('history').append(row);
});
</script>"""
def row(name, active):
    return ROW.replace('NAME', name).replace('ACTIVE', 'conversationConversationItemcurConversation' if active else '')
LIST=row('Same', True)+row('Other', False)
REORDERED=(row('CURRENT_FRIEND', True)+row('Peer', False)).replace('"', '\\"')
def load(mode='accept'):
    page.set_content(HTML.replace('LIST', LIST).replace('REORDERED', '"'+REORDERED+'"'))
    page.evaluate(f"window.mode={json.dumps(mode)}")
def unconfirmed(mode):
    load(mode)
    try:
        A._send_message(page,'[synthetic-emoji]',False,target_name='Same')
    except MessageDeliveryUnconfirmed as error:
        assert page.evaluate('enters')==1
        return getattr(error,'diagnostic',None)
    raise AssertionError(mode+' 不能确认发送')
'''


@requires_chromium
class DouyinRowEvidenceTests(unittest.TestCase):
    def isolated(self, body):
        with tempfile.TemporaryDirectory(prefix='spark-delivery-rows-') as temporary:
            env = dict(os.environ)
            env.update(DATA_DIR=str(Path(temporary) / 'data'), ENV_FILE_PATH=str(Path(temporary) / 'missing.env'),
                       STATE_FILE_PATH='', SPARKKEEPER_BACKGROUND_TASKS='0', DELIVERY_ACCEPT_INPUT_CLEARED='off',
                       PYTHONUTF8='1', PYTHONDONTWRITEBYTECODE='1')
            env['PYTHONPATH'] = str(ROOT / 'tests') + os.pathsep + env.get('PYTHONPATH', '')
            source = 'from _browser import managed_chromium\nwith managed_chromium() as (pw,browser):\n'
            source += textwrap.indent(PRELUDE + '\n' + textwrap.dedent(body) + '\nassert not requests,requests\n', '    ')
            completed = subprocess.run([sys.executable, '-B', '-c', source], cwd=ROOT, env=env,
                                       capture_output=True, text=True, encoding='utf-8', timeout=40)
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_new_own_row_confirms_even_when_left_list_rerenders(self):
        self.isolated('''
        load()
        assert A._send_message(page,'[synthetic-emoji]',False,target_name='Same')==(True,'accepted_own_row')
        assert page.evaluate('enters')==1
        ''')

    def test_other_side_failed_pending_or_missing_rows_never_confirm(self):
        self.isolated('''
        assert unconfirmed('other')=='no_new_message'
        assert unconfirmed('empty')=='no_new_message'
        assert unconfirmed('fail')=='new_message_pending_or_failed'
        assert unconfirmed('pending')=='new_message_pending_or_failed'
        ''')

    def test_reversed_indexes_still_confirm_by_new_bottom_row(self):
        self.isolated('''
        load('reversed')
        assert A._send_message(page,'[synthetic-emoji]',False,target_name='Same')==(True,'accepted_own_row')
        ''')

    def test_rerendered_old_own_row_is_not_a_new_message(self):
        self.isolated('''
        assert unconfirmed('rerender-old')=='no_new_message'
        saved=json.loads(A.AFTER_SEND_PROBE_PATH.read_text(encoding='utf-8'))
        assert saved['reason']=='no_new_message' and saved['before'] and saved['after']
        assert 'synthetic' not in json.dumps(saved)
        ''')

    def test_left_selection_switched_to_another_friend_is_scope_change(self):
        self.isolated('''
        assert unconfirmed('switch')=='scope_changed'
        ''')


if __name__ == '__main__':
    unittest.main()
