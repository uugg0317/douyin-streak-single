"""每日可用性：防重复保护只管当天，跨天自动失效；卡住的状态都有出口。全部使用临时数据目录。"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / ".review-work" / "daily-reliability-tests"

PREAMBLE = '''
import json, os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import core.send_day as D, core.contact_identity as I
from core import ledger as L, restore_guard as G
TZ=timezone(timedelta(hours=8))
NOW=[datetime(2026,10,15,9,0,tzinfo=TZ)]
for module in (D, L, I):
    module.business_now=lambda:NOW[0]
YESTERDAY=(NOW[0]-timedelta(days=1))
def seed(names, **fields):
    entries=[]
    for index,name in enumerate(names):
        entry=L._default_entry(name)
        entry.update(selected=True,selected_order=index,has_conversation=True,**fields)
        entries.append(entry)
    L._save(entries)
    return L.load_ledger()
def intent(status, when):
    return {'send_attempt':{'attempt_id':'a'*32,'entry_id':'x','business_date':when.date().isoformat(),
                            'status':status,'started_at':when.isoformat(timespec='seconds')},
            'last_status':status,'last_attempt_at':when.isoformat(timespec='seconds')}
'''


class DailyReliabilityTests(unittest.TestCase):
    def isolated(self, code):
        WORK.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="case-", dir=WORK) as temporary:
            folder = Path(temporary)
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith("SMTP_") and key not in {
                       "URL_ACCESS_TOKEN", "URL_PASSWORD_SALT", "URL_PASSWORD_HASH", "STATE_FILE_PATH"}}
            env.update({
                "DATA_DIR": str(folder / "data"), "ENV_FILE_PATH": str(folder / "missing.env"),
                "INSTANCE_LOCK_PATH": str(folder / "pid"), "SPARKKEEPER_BACKGROUND_TASKS": "0",
                "SESSION_WARN_EMAIL_ENABLED": "off", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1",
            })
            source = PREAMBLE + "\n" + textwrap.dedent(code) + "\nprint('每日可用性验证通过')\n"
            completed = subprocess.run([sys.executable, "-B", "-c", source], cwd=ROOT, env=env,
                                       text=True, encoding="utf-8", capture_output=True, timeout=30)
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertIn("每日可用性验证通过", completed.stdout)

    def test_unconfirmed_and_rejected_only_block_their_own_business_day(self):
        self.isolated('''
        for status, code in (('unconfirmed','unconfirmed_today'), ('sending','unconfirmed_today'),
                             ('rejected','rejected_unreviewed')):
            assert D.daily_skip_code(intent(status, NOW[0]))==code
            assert D.daily_skip_code(intent(status, YESTERDAY)) is None
        assert I.identity_block_reason(intent('rejected', NOW[0]))=='rejected_unreviewed'
        assert I.identity_block_reason(intent('rejected', YESTERDAY)) is None
        legacy={'last_status':'unconfirmed','last_attempt_at':YESTERDAY.isoformat()}
        assert D.daily_skip_code(legacy) is None
        unknown_date={'last_status':'unconfirmed'}
        assert D.daily_skip_code(unknown_date)=='unconfirmed_today'  # 无法证明已过期，等用户核对
        assert D.daily_skip_code({'last_sent_at':NOW[0].isoformat()})=='already_sent_today'
        ''')

    def test_next_day_attempt_needs_no_force_and_keeps_the_old_intent_for_review(self):
        self.isolated('''
        entry=seed(['跨天合成好友'], **intent('unconfirmed', YESTERDAY))[0]
        attempt=L.begin_send_attempt(entry['entry_id'])
        row=L.load_ledger()[0]
        assert row['send_attempt']['attempt_id']==attempt and row['send_attempt']['status']=='sending'
        assert row['expired_send_attempt']['status']=='unconfirmed'
        try:
            L.begin_send_attempt(entry['entry_id'])
        except L.IdentityConflict:
            pass
        else:
            raise AssertionError('同一天的未决意图仍须阻止重复提交')
        ''')

    def test_rename_candidate_protection_expires_with_the_old_intent(self):
        self.isolated('''
        old, new = seed(['旧名合成好友','新名合成好友'])
        rows=L.load_ledger()
        rows[0].update(intent('unconfirmed', NOW[0]))
        rows[1]['rename_candidates']=[{'old_entry_id':rows[0]['entry_id'],'unresolved_send':True}]
        L._save(rows)
        assert L.load_ledger()[1]['send_block_reason']=='related_send_unconfirmed'
        rows=L.load_ledger()
        rows[0].update(intent('unconfirmed', YESTERDAY))
        L._save(rows)
        assert L.load_ledger()[1]['send_block_reason'] is None
        ''')

    def test_review_send_records_user_decision_and_unblocks(self):
        self.isolated('''
        sent, unsent = seed(['已见合成好友','未发合成好友'], **intent('unconfirmed', NOW[0]))
        version=L.ledger_snapshot()['ledger_version']
        result=L.review_send(sent['entry_id'],'sent',version)
        row=L.resolve_entry(L.load_ledger(), entry_id=sent['entry_id'])
        assert row['last_status']=='success' and row['send_attempt']['status']=='accepted'
        assert D.daily_skip_code(row)=='already_sent_today'
        L.review_send(unsent['entry_id'],'not_sent',result['ledger_version'])
        row=L.resolve_entry(L.load_ledger(), entry_id=unsent['entry_id'])
        assert row['last_status']=='failed' and D.daily_skip_code(row) is None
        assert row['send_reviews'][-1]['action']=='not_sent'
        try:
            L.review_send(unsent['entry_id'],'sent',L.ledger_snapshot()['ledger_version'])
        except L.IdentityConflict:
            pass
        else:
            raise AssertionError('没有待核对记录时不能凭空记为已发')
        ''')

    def test_stale_unconfirmed_entry_can_be_deleted(self):
        self.isolated('''
        stale, fresh = seed(['昨日合成好友','今日合成好友'])
        rows=L.load_ledger()
        rows[0].update(intent('unconfirmed', YESTERDAY)); rows[1].update(intent('unconfirmed', NOW[0]))
        L._save(rows)
        assert L.remove_entries(entry_ids=[stale['entry_id']])['deleted']==1
        try:
            L.remove_entries(entry_ids=[fresh['entry_id']])
        except L.IdentityConflict:
            pass
        else:
            raise AssertionError('当天未决记录仍不能删除')
        ''')

    def test_recovery_marker_expires_next_day_and_repairs_primary(self):
        self.isolated('''
        seed(['恢复合成好友'])
        L._save(L.load_ledger())  # 生成可用备份
        L.LEDGER_PATH.write_bytes(b'\\xff not json')
        rows=L.load_ledger()
        assert L.recovery_required() and rows[0]['send_block_reason']=='ledger_recovery_required'
        marker=L.recovery_marker_path()
        data=json.loads(marker.read_text(encoding='utf-8'))
        data['detected_at']=YESTERDAY.isoformat(timespec='seconds')
        marker.write_text(json.dumps(data), encoding='utf-8')
        assert L.recovery_required()  # 文件是今天写的，仍按今天生效
        os.utime(marker, (YESTERDAY.timestamp(), YESTERDAY.timestamp()))
        assert not L.recovery_required()
        rows=L.load_ledger()
        assert not marker.exists() and rows[0]['send_block_reason'] is None
        assert 'ledger_recovery_required' not in rows[0]
        assert json.loads(L.LEDGER_PATH.read_text(encoding='utf-8'))[0]['display_name']=='恢复合成好友'
        folder=L.LEDGER_PATH.parent
        assert list(folder.glob('ledger.json.corrupt-*')) and list(folder.glob('ledger-recovery-expired-*.json'))
        ''')

    def test_manual_recovery_clear_and_saved_flag_does_not_linger(self):
        self.isolated('''
        seed(['手动合成好友'])
        L._save(L.load_ledger())
        L.LEDGER_PATH.unlink()
        rows=L.load_ledger()
        assert L.recovery_required()
        L._save(rows)  # 保存时带着当天标志
        result=L.clear_recovery()
        assert result['cleared'] is True and not L.recovery_required()
        row=L.load_ledger()[0]
        assert 'ledger_recovery_required' not in row and row['send_block_reason'] is None
        assert L.clear_recovery()['cleared'] is False
        ''')

    def test_review_email_only_when_action_needed_and_once_per_day(self):
        self.isolated('''
        from unittest import mock
        import session_service as S
        sent=[]
        S._warn_email_enabled=lambda:True
        S.load_email_config=lambda:{'mail_to':'synthetic-review@example.invalid'}
        S._send_email=lambda cfg,subject,body:sent.append((subject,body))
        S.business_now=lambda:NOW[0]
        quiet={'dry_run':False,'ok':['合成好友'],'failed':[],'skipped':[]}
        assert S._send_review_warning(quiet) is False and not sent
        review={'dry_run':False,'ok':[],'failed':[{'name':'拒绝合成好友','code':'delivery_rejected'}],
                'skipped':[{'name':'待核对合成好友','code':'delivery_unconfirmed'}]}
        assert S._send_review_warning({**review,'dry_run':True}) is False and not sent
        assert S._send_review_warning(review) is True
        assert len(sent)==1 and '待核对合成好友' in sent[0][1] and '拒绝合成好友' in sent[0][1]
        assert S._send_review_warning(review) is False and len(sent)==1
        NOW[0]=NOW[0]+timedelta(days=1)
        assert S._send_review_warning({'dry_run':False,'ledger_recovery_required':True}) is True
        assert '解除发送保护' in sent[-1][1]
        ''')

    def test_restore_pause_is_scoped_to_its_day(self):
        self.isolated('''
        folder=Path(os.environ['DATA_DIR']); folder.mkdir(parents=True, exist_ok=True)
        marker=folder/G.RESTORE_PAUSE_NAME
        marker.write_text(json.dumps({'paused':True,'restored_at':NOW[0].isoformat()}), encoding='utf-8')
        os.utime(marker, (NOW[0].timestamp(), NOW[0].timestamp()))
        assert G.marker_active(marker, today=NOW[0].date())
        assert not G.marker_active(marker, today=(NOW[0]+timedelta(days=1)).date())
        marker.write_text('{broken', encoding='utf-8')
        assert G.marker_business_day(marker) is not None
        # 备份里带出的旧标记按原字节还原：内容日期很早，但文件是恢复当天写的，当天仍然生效。
        marker.write_text(json.dumps({'paused':True,'restored_at':(NOW[0]-timedelta(days=30)).isoformat()}), encoding='utf-8')
        os.utime(marker, (NOW[0].timestamp(), NOW[0].timestamp()))
        assert G.marker_active(marker, today=NOW[0].date())
        assert not G.marker_active(marker, today=(NOW[0]+timedelta(days=1)).date())
        ''')


if __name__ == "__main__":
    unittest.main()
