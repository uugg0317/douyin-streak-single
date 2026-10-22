"""T04：实际配置保存与假 SMTP 传输的安全边界，不外发邮件、不读取真实配置。"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / ".review-work" / "t04-tests"
TOKEN = "t04-isolated-management-token-not-real"
FAKE_PASS = "fakeauthorizationcodeabcdefghijklmnop"

PREAMBLE = '''
import asyncio, json, os, smtplib, ssl, threading
from pathlib import Path
from unittest import mock
import app_common
import email_service as email
from core import config
from fastapi import FastAPI
from middleware import register_middleware
from routers import email as routes
from models import EmailConfigBody
TOKEN='t04-isolated-management-token-not-real'
FAKE_PASS='fakeauthorizationcodeabcdefghijklmnop'
smtplib.SMTP_SSL=mock.Mock(side_effect=AssertionError('禁止真实 SMTP'))
smtplib.SMTP=mock.Mock(side_effect=AssertionError('禁止真实 SMTP'))
config.save_config({'mail_to':'old-to@example.invalid','email_subject':'旧主题','email_body':'旧正文'})
def stored():return app_common.ENV_PATH.read_bytes(),config.CONFIG_PATH.read_bytes()
def verifying(ctx):
    return isinstance(ctx,ssl.SSLContext) and ctx.verify_mode==ssl.CERT_REQUIRED and ctx.check_hostname
app=FastAPI()
register_middleware(app)
app.include_router(routes.router)
async def invoke(body):
    raw=json.dumps(body).encode()
    scope={'type':'http','http_version':'1.1','asgi':{'version':'3.0','spec_version':'2.4'},
       'scheme':'http','method':'POST','path':'/api/email-config','raw_path':b'/api/email-config',
       'query_string':b'','root_path':'','headers':[(b'host',b'127.0.0.1:8000'),
       (b'origin',b'http://127.0.0.1:8000'),(b'x-auth-token',TOKEN.encode()),
       (b'content-type',b'application/json'),(b'x-requested-with',b'sparkkeeper')],
       'client':('127.0.0.1',43210),'server':('127.0.0.1',8000)}
    sent=[];received=False
    async def receive():
        nonlocal received
        if not received:
            received=True
            return {'type':'http.request','body':raw,'more_body':False}
        await asyncio.Event().wait()
    async def send(message):sent.append(message)
    await app(scope,receive,send)
    start=next(item for item in sent if item['type']=='http.response.start')
    payload=b''.join(item.get('body',b'') for item in sent if item['type']=='http.response.body')
    return start['status'],json.loads(payload)
def request(body):return asyncio.run(invoke(body))
'''


class SmtpSecurityTests(unittest.TestCase):
    def isolated(self, body):
        WORK.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="case-", dir=WORK) as temporary:
            folder = Path(temporary)
            env_path = folder / "service.env"
            env_path.write_text(
                "SMTP_HOST=smtp.old.example.invalid\nSMTP_PORT=587\n"
                "SMTP_USER=old-user@example.invalid\nSMTP_PASS=" + FAKE_PASS + "\n",
                encoding="utf-8",
            )
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith("SMTP_") and key not in {
                       "STATE_FILE_PATH", "URL_ACCESS_TOKEN", "URL_PASSWORD_SALT", "URL_PASSWORD_HASH",
                   }}
            env.update({
                "DATA_DIR": str(folder / "data"), "ENV_FILE_PATH": str(env_path),
                "INSTANCE_LOCK_PATH": str(folder / "pid"), "HOST": "127.0.0.1", "PORT": "8000",
                "LOGIN_GATE": "on", "AUTH_TOKEN": TOKEN, "PASSWORD_PUBLIC_LOGIN": "off",
                "SPARKKEEPER_BACKGROUND_TASKS": "0", "SESSION_WARN_EMAIL_ENABLED": "off",
                "QR_PUBLIC_ORIGIN": "", "WHITELIST_IPS": "",
                "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1",
            })
            code = PREAMBLE + "\n" + textwrap.dedent(body) + "\nprint('T04隔离验证通过')\n"
            completed = subprocess.run([sys.executable, "-B", "-c", code], cwd=ROOT, env=env,
                                       text=True, encoding="utf-8", capture_output=True, timeout=20)
            self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
            self.assertIn("T04隔离验证通过", completed.stdout)
            self.assertNotIn(FAKE_PASS, completed.stderr + completed.stdout)

    def test_ssl_and_starttls_both_verify_certificates_and_hostnames(self):
        self.isolated('''
        seen=[]
        class FakeSMTP:
            def __init__(self,*args,**kwargs):seen.append(('connect',kwargs.get('context')))
            def ehlo(self):pass
            def starttls(self,**kwargs):seen.append(('starttls',kwargs.get('context')))
            def login(self,*args):seen.append(('login',None))
            def send_message(self,msg):seen.append(('send',None))
            def quit(self):pass
            def close(self):pass
        cfg=email.load_email_config()
        for port in (465,587):
            seen.clear()
            with mock.patch.object(smtplib,'SMTP_SSL',FakeSMTP),mock.patch.object(smtplib,'SMTP',FakeSMTP):
                email._send_email({**cfg,'smtp_port':port},'测试','测试正文')
            name='connect' if port==465 else 'starttls'
            assert verifying(next(ctx for stage,ctx in seen if stage==name))
            assert len([stage for stage,ctx in seen if stage=='login'])==1
        ''')

    def test_certificate_failure_on_either_tls_path_never_logs_in_or_falls_back(self):
        self.isolated('''
        logins=[];connections=[];closed=[]
        class FakeSMTP:
            def __init__(self,*args,**kwargs):
                connections.append(args[1])
                if args[1]==465:
                    assert verifying(kwargs['context'])
                    raise ssl.SSLCertVerificationError(1,'模拟证书校验失败')
            def ehlo(self):pass
            def starttls(self,**kwargs):
                assert verifying(kwargs['context'])
                raise ssl.SSLCertVerificationError(1,'模拟主机名不匹配')
            def login(self,*args):logins.append(1)
            def send_message(self,msg):raise AssertionError('不得发送')
            def quit(self):pass
            def close(self):closed.append(1)
        for port in (465,587):
            connections.clear()
            with mock.patch.object(smtplib,'SMTP_SSL',FakeSMTP),mock.patch.object(smtplib,'SMTP',FakeSMTP):
                try:email._send_email({**email.load_email_config(),'smtp_port':port},'测试','测试')
                except ssl.SSLCertVerificationError:pass
                else:raise AssertionError('必须拒绝证书异常')
            assert connections==[port]
        assert not logins and len(closed)==1
        ''')

    def test_host_port_user_changes_require_reentered_actual_password_without_partial_writes(self):
        self.isolated('''
        before=stored()
        for changed in ({'smtp_host':'smtp.new.example.invalid'},{'smtp_port':465},
                        {'smtp_user':'new-user@example.invalid'}):
            for password in (None,'','******','********','   '):
                raw={**changed,'mail_to':'new-to@example.invalid','subject':'新主题'}
                if password is not None:raw['smtp_pass']=password
                try:email.save_email_config(raw)
                except ValueError as error:
                    assert '重新输入' in str(error)
                    assert FAKE_PASS not in str(error)
                else:raise AssertionError('连接变更不得复用旧授权码')
                assert stored()==before
        ''')

    def test_same_connection_accepts_blank_or_mask_and_preserves_password(self):
        self.isolated('''
        for password in ('','******','********','   '):
            result=email.save_email_config({'smtp_host':'smtp.old.example.invalid','smtp_port':587,
                'smtp_user':'old-user@example.invalid','smtp_pass':password,
                'mail_to':'changed-to@example.invalid'})
            assert result['smtp_pass']==FAKE_PASS
            assert result['smtp_port']==587
        ''')

    def test_actual_reentry_may_match_original_password(self):
        self.isolated('''
        result=email.save_email_config({'smtp_host':'smtp.new.example.invalid','smtp_pass':FAKE_PASS})
        assert result['smtp_host']=='smtp.new.example.invalid'
        assert result['smtp_pass']==FAKE_PASS
        ''')

    def test_real_partial_http_request_changes_only_business_settings_and_keeps_587(self):
        self.isolated('''
        before_env=app_common.ENV_PATH.read_bytes()
        status,payload=request({'mail_to':'new-to@example.invalid','subject':'新主题'})
        assert status==200,(status,payload)
        assert payload['config']['smtp_port']==587
        assert payload['config']['smtp_pass']=='******'
        assert payload['config']['mail_to']=='new-to@example.invalid'
        assert FAKE_PASS not in json.dumps(payload)
        assert app_common.ENV_PATH.read_bytes()==before_env
        assert email.load_email_config()['smtp_pass']==FAKE_PASS
        ''')

    def test_real_http_connection_change_without_code_returns_400_and_saves_nothing(self):
        self.isolated('''
        before=stored()
        status,payload=request({'smtp_host':'smtp.other.example.invalid',
                                'smtp_pass':'******','mail_to':'other-to@example.invalid'})
        assert status==400
        assert '重新输入' in payload['detail']
        assert FAKE_PASS not in json.dumps(payload)
        assert stored()==before
        ''')

    def test_reentered_valid_password_and_all_connection_changes_are_saved(self):
        self.isolated('''
        entered='newfakeauthorizationcode'
        result=email.save_email_config({'smtp_host':'smtp.new.example.invalid','smtp_port':465,
            'smtp_user':'new-user@example.invalid','smtp_pass':entered,'mail_to':'new-to@example.invalid'})
        assert result['smtp_host']=='smtp.new.example.invalid'
        assert result['smtp_port']==465 and result['smtp_user']=='new-user@example.invalid'
        assert result['smtp_pass']==entered
        ''')

    def test_invalid_port_or_code_never_partially_changes_env_or_business_config(self):
        self.isolated('''
        before=stored()
        cases=[{'smtp_port':0,'smtp_pass':FAKE_PASS},{'smtp_port':65536,'smtp_pass':FAKE_PASS},
               {'smtp_port':'not-number','smtp_pass':FAKE_PASS},
               {'smtp_host':'smtp.new.example.invalid','smtp_pass':'123456'},
               {'smtp_host':'smtp.new.example.invalid','smtp_pass':'short'},
               {'smtp_host':'smtp.new.example.invalid','smtp_pass':'invalid\\ncodeletters'}]
        for changed in cases:
            try:email.save_email_config({**changed,'mail_to':'new-to@example.invalid','subject':'新主题'})
            except ValueError as error:assert FAKE_PASS not in str(error)
            else:raise AssertionError('无效参数必须拒绝')
            assert stored()==before
        ''')

    def test_load_snapshot_waits_for_complete_connection_and_password_save(self):
        self.isolated('''
        entered=threading.Event();resume=threading.Event();read_started=threading.Event()
        observed=[];errors=[]
        actual_save=email.save_env_values
        def staged_save(updates):
            assert 'SMTP_HOST' in updates and 'SMTP_PASS' in updates
            os.environ['SMTP_HOST']=updates['SMTP_HOST']
            entered.set()
            assert resume.wait(3)
            actual_save(updates)
        def write():
            try:email.save_email_config({'smtp_host':'smtp.a.example.invalid','smtp_pass':'a-fake-authorization-code'})
            except Exception as error:errors.append(error)
        def read():
            read_started.set()
            observed.append(email.load_email_config())
        with mock.patch.object(email,'save_env_values',side_effect=staged_save):
            writer=threading.Thread(target=write);writer.start();assert entered.wait(3)
            reader=threading.Thread(target=read);reader.start();assert read_started.wait(3)
            reader.join(0.05)
            assert reader.is_alive() and not observed
            resume.set();writer.join(3);reader.join(3)
        assert not errors and not writer.is_alive() and not reader.is_alive()
        assert len(observed)==1
        assert (observed[0]['smtp_host'],observed[0]['smtp_pass'])==('smtp.a.example.invalid','a-fake-authorization-code')
        ''')

    def test_two_saves_and_snapshot_cannot_mix_one_host_with_another_code(self):
        self.isolated('''
        entered=threading.Event();resume=threading.Event();second_started=threading.Event()
        snapshots=[];errors=[]
        actual_save=email.save_env_values
        def staged_save(updates):
            if updates.get('SMTP_HOST')=='smtp.a.example.invalid':
                os.environ['SMTP_HOST']=updates['SMTP_HOST']
                entered.set();assert resume.wait(3)
            actual_save(updates)
        def write(host,password,started=None):
            if started:started.set()
            try:snapshots.append(email.save_email_config({'smtp_host':host,'smtp_pass':password}))
            except Exception as error:errors.append(error)
        with mock.patch.object(email,'save_env_values',side_effect=staged_save):
            one=threading.Thread(target=write,args=('smtp.a.example.invalid','a-fake-authorization-code'))
            one.start();assert entered.wait(3)
            two=threading.Thread(target=write,args=('smtp.b.example.invalid','b-fake-authorization-code',second_started))
            two.start();assert second_started.wait(3)
            two.join(0.05);assert two.is_alive()
            resume.set();one.join(3);two.join(3)
        assert not errors and not one.is_alive() and not two.is_alive()
        pairs={(item['smtp_host'],item['smtp_pass']) for item in snapshots}
        assert pairs=={('smtp.a.example.invalid','a-fake-authorization-code'),
                       ('smtp.b.example.invalid','b-fake-authorization-code')}
        final=email.load_email_config()
        assert (final['smtp_host'],final['smtp_pass'])==('smtp.b.example.invalid','b-fake-authorization-code')
        ''')

    def test_smtp_network_operation_does_not_hold_config_lock(self):
        self.isolated('''
        observed=[]
        class FakeSMTP:
            def __init__(self,*args,**kwargs):
                reader=threading.Thread(target=lambda:observed.append(email.load_email_config()))
                reader.start();reader.join(3)
                assert not reader.is_alive(),'SMTP网络期间不能占用配置锁'
            def ehlo(self):pass
            def starttls(self,**kwargs):pass
            def login(self,*args):pass
            def send_message(self,msg):pass
            def quit(self):pass
            def close(self):pass
        with mock.patch.object(smtplib,'SMTP_SSL',FakeSMTP):
            cfg=email.load_email_config()
            email._send_email({**cfg,'smtp_port':465},'测试','测试')
        assert len(observed)==1
        ''')


if __name__ == '__main__':
    unittest.main()
