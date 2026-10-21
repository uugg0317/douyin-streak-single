"""Isolated 2.8 UI fixture. Serves only bundled static assets and fake API states.
No application imports, real accounts, network clients, login browsers or files.
Run: python tests/qr_console_fixture.py --port 8766
Panel demo: /?panels=1. Changes fake global DATA only; all mutations use LOCK.
"""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit, unquote, parse_qs
ROOT=Path(__file__).resolve().parents[1]
LOCK=threading.RLock()
DATA={'status':'idle','image_failure':False,'refresh_failure':False,'saved':False,
      'credential_saved':False,'session_checked_at':None,'cleanup_complete':True,
      'revision':1,'deadline':0,'checks':[],'qr_status_polls':0,'app_status_polls':0,
      'scenarios':[],'panels':False,'panel_mode':'normal','panel_progress':None,
      'panel_failures':0,'panel_paused_until':0}
QR=(ROOT/'static/fire-icon.png').read_bytes() # Static placeholder, never a login QR.

BANNER='''<aside aria-label="隔离测试控制" style="position:fixed;bottom:calc(108px + env(safe-area-inset-bottom));right:12px;z-index:99999;background:#fff7ed;border:1px solid #b45309;border-radius:10px;padding:10px;max-width:calc(100vw - 44px);max-height:calc(100dvh - 220px);overflow:auto;font:12px system-ui;color:#78350f"><details><summary style="cursor:pointer;min-height:24px;line-height:24px">隔离模拟 · 不连接抖音</summary><div style="display:flex;flex-wrap:wrap;gap:7px;margin-top:8px"><button onclick="qa('scanned')">模拟扫码确认</button><button onclick="qa('saving')">模拟保存中</button><button onclick="qa('success')">模拟完成</button><button onclick="qa('refresh_failure')">模拟刷新断网</button><button onclick="qa('image_failure')">模拟图片失败</button><button onclick="qa('image_recover')">恢复图片</button><button onclick="qa('cleanup_pending')">模拟关闭未完成</button></div><p style="margin:8px 0 0;line-height:1.6">先开始扫码，再按顺序推进；也可从等待直接点“模拟完成”检查进度跳步。后台任务始终暂停。</p></details></aside><script>function qa(scenario){return fetch('/__test/control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({scenario})});}</script>'''

PANEL_CONTROLS='''<div aria-label="状态面板演示" style="margin-top:12px;padding-top:10px;border-top:1px solid #d4a865"><strong>状态面板演示 · 全部虚构</strong><div style="display:flex;flex-wrap:wrap;gap:7px;margin-top:8px"><button onclick="qa('panels_sending')">失败＋发送</button><button onclick="qa('panels_cooldown')">模拟冷却</button><button onclick="qa('panels_normal')">普通空闲</button><button onclick="qa('panels_progress')">更新进度</button><button onclick="qa('panels_preparing')">模拟准备中</button><button onclick="qa('panels_off')">关闭面板演示</button></div><p style="margin:8px 0 0;line-height:1.6">只修改假状态，随后由页面状态轮询更新；不会发消息、操作账号或修改凭据。</p></div>'''

def configure_panels(scenario):
    """Caller holds LOCK; no real scheduler, account or browser is involved."""
    if not isinstance(scenario,str):return False
    if scenario=='panels_off':
        DATA.update(panels=False,panel_mode='normal',panel_progress=None,panel_failures=0,panel_paused_until=0)
        return True
    if scenario not in {'panels_sending','panels_cooldown','panels_normal','panels_progress','panels_preparing'}:
        return False
    DATA['panels']=True
    if scenario=='panels_cooldown':
        DATA.update(panel_mode='cooldown',panel_progress=None,panel_failures=2,panel_paused_until=time.time()+1800)
    elif scenario=='panels_normal':
        DATA.update(panel_mode='normal',panel_progress=None,panel_failures=0,panel_paused_until=0)
    elif scenario=='panels_preparing':
        DATA.update(panel_mode='preparing',panel_progress={'current':'','total':0,'done':0,'ok':0,'failed':0},panel_failures=2,panel_paused_until=0)
    else:
        progress={'current':'示例好友 · 这是用于手机布局检查的一条很长的中文好友名称','total':12,'done':5,'ok':4,'failed':1}
        if scenario=='panels_progress':
            previous=DATA.get('panel_progress')
            done=min(12,(previous.get('done',5) if isinstance(previous,dict) and previous.get('total')==12 else 5)+3)
            progress.update(done=done,ok=max(0,done-1))
        DATA.update(panel_mode='sending',panel_progress=progress,panel_failures=2,panel_paused_until=0)
    return True

def app_snapshot():
    """The default QR fixture remains idle; panel mode overrides fake UI fields."""
    panels=DATA['panels']
    return {'version':'2.8','state_file_exists':DATA['credential_saved'],
            'session_status':'ok' if DATA['credential_saved'] else 'unknown',
            'session_checked_at':DATA['session_checked_at'],
            'running':panels and DATA['panel_mode'] in {'sending','preparing'},
            'background_tasks_enabled':False,'next_run':None,
            'consecutive_failures':DATA['panel_failures'] if panels else 0,
            'auto_paused_until':DATA['panel_paused_until'] if panels else 0,
            'send_progress':DATA['panel_progress'] if panels else None}

def mark_saved():
    """Confirm once per fake credential commit, never on a status poll."""
    if not DATA['saved']:
        DATA['session_checked_at']=time.time()
    DATA['saved']=True
    DATA['credential_saved']=True
def snapshot():
    return {'job_id':'qa-job','job_token':'fixture-capability-not-real','status':DATA['status'],'code':DATA['status'],'saved':DATA['saved'],'cleanup_complete':DATA['cleanup_complete'],'cleanup_attempts':3 if DATA['status']=='cleanup_pending' else 0,'cleanup_retrying':False,'qr_revision':DATA['revision'],'image_ready':DATA['status']=='waiting','deadline':DATA['deadline'],'message':''}
class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def reply(self,data,code=200,ctype=None):
        if not isinstance(data,bytes):data=json.dumps(data).encode();ctype='application/json'
        self.send_response(code);self.send_header('Content-Type',ctype or 'application/octet-stream');self.send_header('Content-Length',str(len(data)));self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(data)
    def do_GET(self):
        request=urlsplit(self.path)
        path=unquote(request.path)
        with LOCK:
            if path=='/':
                query=parse_qs(request.query)
                panel_demo=query.get('panels',[''])[0].lower() in {'1','true','yes'}
                if panel_demo:configure_panels('panels_sending')
                banner=BANNER.replace('</details>',PANEL_CONTROLS+'</details>') if panel_demo or DATA['panels'] else BANNER
                return self.reply((ROOT/'static/index.html').read_text(encoding='utf-8').replace('</body>',banner+'</body>').encode(),ctype='text/html; charset=utf-8')
            if path.startswith('/static/'):
                file=(ROOT/path.lstrip('/')).resolve()
                if not file.is_relative_to((ROOT/'static').resolve()) or not file.is_file():return self.reply({},404)
                return self.reply(file.read_bytes(),ctype=mimetypes.guess_type(str(file))[0] or 'application/octet-stream')
            if path=='/api/health':return self.reply({'ok':True,'version':'2.8'})
            if path=='/api/status':
                DATA['app_status_polls']+=1
                return self.reply(app_snapshot())
            if path=='/api/config':return self.reply({'auto_run_enabled':True,'messages':['模拟消息，不会发送'],'schedule_time':'00:00','friends':[]})
            if path=='/api/ledger':return self.reply({'items':[]})
            if path=='/api/logs':return self.reply({'lines':['隔离模拟：没有真实登录或发送操作。']})
            if path=='/api/email-config':return self.reply({})
            if path=='/api/credentials/qr/status':
                DATA['qr_status_polls']+=1
                return self.reply(snapshot())
            if path=='/api/credentials/qr/image':
                if DATA['image_failure']:return self.reply({'detail':'模拟二维码图片暂不可用'},503)
                return self.reply(QR,ctype='image/png')
            if path=='/__test/state':return self.reply({'fixture':True,**DATA})
            return self.reply({},404)
    def do_POST(self):
        try:body=json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))) or b'{}')
        except Exception:return self.reply({},400)
        path=urlsplit(self.path).path
        with LOCK:
            if path=='/__test/control':
                scenario=body.get('scenario')
                DATA['scenarios'].append(scenario)
                if configure_panels(scenario):pass
                elif scenario=='refresh_failure':DATA['refresh_failure']=True
                elif scenario=='image_failure':DATA['image_failure']=True;DATA['revision']+=1
                elif scenario=='image_recover':DATA['image_failure']=False
                elif scenario=='scanned':DATA.update(status='scanned',cleanup_complete=False)
                elif scenario=='saving':DATA.update(status='saving',cleanup_complete=False)
                elif scenario=='cleanup_pending':mark_saved();DATA.update(status='cleanup_pending',cleanup_complete=False)
                elif scenario=='success':mark_saved();DATA.update(status='success',cleanup_complete=True)
                return self.reply({'ok':True})
            DATA['checks'].append(path)
            if path=='/api/credentials/qr/start':
                if DATA['status']=='cleanup_pending':return self.reply({'detail':'上一任务仍在等待清理'},409)
                DATA.update(status='waiting',saved=False,cleanup_complete=False,deadline=time.time()+300);return self.reply(snapshot())
            if path=='/api/credentials/qr/recover-cleanup':
                if DATA['status']!='cleanup_pending':return self.reply({'detail':'没有待恢复清理任务'},409)
                return self.reply(snapshot())
            if path=='/api/credentials/qr/refresh':
                DATA['revision']+=1
                if DATA['refresh_failure']:DATA['refresh_failure']=False;return self.reply({'detail':'模拟刷新响应丢失'},503)
                return self.reply(snapshot())
            if path=='/api/credentials/qr/cancel':
                if DATA['status']!='cleanup_pending':DATA.update(status='cancelled',cleanup_complete=True)
                return self.reply(snapshot())
            if path=='/api/credentials/qr/retry-cleanup':mark_saved();DATA.update(status='success',cleanup_complete=True);return self.reply(snapshot())
            if path=='/api/auth/logout':return self.reply({'ok':True})
            return self.reply({'detail':'隔离环境不执行此操作'},400)
if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=8766);args=parser.parse_args()
    server=ThreadingHTTPServer(('127.0.0.1',args.port),Handler)
    print('Isolated QR UI fixture on port',args.port,flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()
