"""会话列表分批加载：滚到底部后才加载下一批，定位必须等新一批出现再判断是否到底。

真实 Headless Chromium 打开内存页面，所有网络请求都被拦截。
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
from core import automation as A, contact_capture as C
requests=[]
page=browser.new_page()
page.route('**/*',lambda route:(requests.append(route.request.url),route.abort()))
C.SCROLL_SETTLE_MS=150
C.BOTTOM_SETTLE_MS=700
page.set_content(r"""
<div id="list" style="position:absolute;left:0;top:0;width:280px;height:300px;overflow-y:auto"><div id="inner"></div></div>
<section id="chatRoot" style="position:absolute;left:400px;top:10px;width:450px;height:200px">
<header id="header" style="position:absolute;left:0;top:0">nobody</header>
<div contenteditable="true" id="input" style="margin-top:60px;min-height:24px"></div>
</section>
<script>
window.loaded=0;const inner=document.getElementById('inner');const list=document.getElementById('list');
function add(n){for(let i=0;i<n;i++){const k=window.loaded++;const name=k===14?'Target':'Friend'+k;
 const row=document.createElement('div');row.className='conversationConversationItemwrapper';row.style.height='60px';
 row.innerHTML='<span class="conversationConversationItemtitle">'+name+'</span>';
 row.addEventListener('click',()=>{document.querySelectorAll('.conversationConversationItemcurConversation').forEach(e=>e.classList.remove('conversationConversationItemcurConversation'));
  row.classList.add('conversationConversationItemcurConversation');document.getElementById('header').textContent=name;});
 inner.append(row);}}
add(10);
list.addEventListener('scroll',()=>{if(list.scrollTop+list.clientHeight>=list.scrollHeight-2&&window.loaded<20&&!window.loadingMore){
 window.loadingMore=true;setTimeout(()=>{add(10);window.loadingMore=false;},600);}});
</script>""")
'''


@requires_chromium
class LazyListLocateTests(unittest.TestCase):
    def isolated(self, body):
        with tempfile.TemporaryDirectory(prefix='spark-locate-lazy-') as temporary:
            env = dict(os.environ)
            env.update(DATA_DIR=str(Path(temporary) / 'data'), ENV_FILE_PATH=str(Path(temporary) / 'missing.env'),
                       STATE_FILE_PATH='', SPARKKEEPER_BACKGROUND_TASKS='0', PYTHONUTF8='1', PYTHONDONTWRITEBYTECODE='1')
            env['PYTHONPATH'] = str(ROOT / 'tests') + os.pathsep + env.get('PYTHONPATH', '')
            source = 'from _browser import managed_chromium\nwith managed_chromium() as (pw,browser):\n'
            source += textwrap.indent(PRELUDE + '\n' + textwrap.dedent(body) + '\nassert not requests,requests\n', '    ')
            completed = subprocess.run([sys.executable, '-B', '-c', source], cwd=ROOT, env=env,
                                       capture_output=True, text=True, encoding='utf-8', timeout=60)
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_friend_in_a_later_batch_is_found_after_waiting_at_the_bottom(self):
        self.isolated('''
        assert A._locate_contact(page,'Target') is True
        assert page.evaluate("document.getElementById('header').textContent")=='Target'
        assert page.evaluate('window.loaded')==20
        ''')

    def test_waits_for_the_real_list_to_replace_the_placeholder_list(self):
        self.isolated('''
        page.set_content("""<div id="inner"></div><script>
        const fill=names=>{document.getElementById('inner').innerHTML=names.map(n=>'<div class="conversationConversationItemwrapper"><span class="conversationConversationItemtitle">'+n+'</span></div>').join('')};
        fill(['Cached0','Cached1','Cached2']);setTimeout(()=>fill(['Friend0','Target','Friend2']),800);
        </script>""")
        assert A._find_contact(page,'Target') is None
        assert A._wait_list_stable(page,quiet=1.0,limit=5) is True
        assert A._find_contact(page,'Target') is not None
        ''')

    def test_failed_search_fallback_clears_the_search_box(self):
        self.isolated('''
        page.set_content("""<input placeholder="搜索" id="search"><div id="inner"></div><script>
        const names=['Friend0','Friend1'];
        const render=q=>{document.getElementById('inner').innerHTML=names.filter(n=>!q||n.includes(q)).map(n=>'<div class="conversationConversationItemwrapper"><span class="conversationConversationItemtitle">'+n+'</span></div>').join('')};
        render('');document.getElementById('search').addEventListener('input',e=>render(e.target.value));
        </script>""")
        assert A._search_and_open(page,'Nobody') is False
        assert page.locator('#search').input_value()==''
        assert A._find_contact(page,'Friend1') is not None
        ''')

    def test_absent_friend_stops_after_the_list_height_settles(self):
        self.isolated('''
        assert A._locate_contact(page,'Nobody') is False
        assert page.evaluate('window.loaded')==20
        assert page.evaluate("document.getElementById('header').textContent")=='nobody'
        ''')


if __name__ == '__main__':
    unittest.main()
