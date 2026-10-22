"""整理工具默认只读、先备份、拒绝有待处理任务的数据目录。"""
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from unittest import TestCase

ROOT=Path(__file__).resolve().parents[1]
A='https://p3.douyinpic.com/img/aweme-avatar/fixture~c5_100x100.jpeg'
B='https://p26.douyinpic.com/img/aweme-avatar/fixture~c5_168x168.webp'

class ToolTests(TestCase):
    def invoke(self,data,apply=False):
        args=[sys.executable,str(ROOT/'tools/consolidate_contacts.py'),'--data-dir',str(data)]
        if apply:args.append('--apply')
        return subprocess.run(args,cwd=ROOT,capture_output=True,text=True,encoding='utf-8',timeout=20)
    def fixture(self,data):
        rows=[{'entry_id':'a'*32,'display_name':'合成','avatar':A,'has_conversation':True,'selected':True},
              {'entry_id':'b'*32,'display_name':'合成','avatar':B,'has_conversation':True,'selected':False,'observation_status':'seen'}]
        raw=json.dumps(rows,ensure_ascii=False).encode();(data/'ledger.json').write_bytes(raw);return raw
    def test_preview_never_writes_and_apply_keeps_exact_backup_and_is_idempotent(self):
        with TemporaryDirectory() as tmp:
            data=Path(tmp);raw=self.fixture(data)
            result=self.invoke(data);self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual((data/'ledger.json').read_bytes(),raw);self.assertFalse((data/'backups').exists())
            result=self.invoke(data,True);self.assertEqual(result.returncode,0,result.stderr)
            report=json.loads(result.stdout.strip().splitlines()[-1]);backup=Path(report['backup'])
            self.assertEqual((backup/'ledger.json').read_bytes(),raw)
            rows=json.loads((data/'ledger.json').read_text());self.assertEqual(len(rows),1);self.assertTrue(rows[0]['selected'])
            self.assertEqual(rows[0]['entry_id'],'a'*32)
            result=self.invoke(data,True);self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(json.loads(result.stdout)['removed_duplicates'],0)
    def test_pending_queue_prevents_writes(self):
        with TemporaryDirectory() as tmp:
            data=Path(tmp);raw=self.fixture(data)
            (data/'runtime.json').write_text(json.dumps({'pending_retry_entries':[{'entry_id':'b'*32}]}))
            result=self.invoke(data,True);self.assertNotEqual(result.returncode,0)
            self.assertEqual((data/'ledger.json').read_bytes(),raw);self.assertFalse((data/'backups').exists())
