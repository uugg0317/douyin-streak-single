"""Private-data recovery drills use temporary files and synthetic accounts only."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest import mock
import warnings
import zipfile

import _isolation  # noqa: F401  先隔离数据目录，再导入项目模块
from tools import data_backup as B
from core.file_lock import exclusive_lock

ROOT = Path(__file__).resolve().parents[1]


class DataBackupTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix='private-backup-fixture-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root/'source'
        self.source.mkdir()
        self.archive = self.root/'private.zip'
        self.target = self.root/'restored'
        self.entry_id = 'a'*32
        self.rows = [{'entry_id':self.entry_id,'display_name':'synthetic friend','selected':True,
                      'has_conversation':True,'last_sent_at':'2026-10-20T10:00:00+08:00',
                      'send_attempt':{'attempt_id':'synthetic-attempt','entry_id':self.entry_id,
                                      'status':'sending','started_at':'2026-10-21T10:00:00+08:00'}}]
        self.payload = {
            'config.json':{'auto_run_enabled':True,'friends':['synthetic friend']},
            'ledger.json':self.rows,
            'ledger.json.bak':self.rows,
            'runtime.json':{'running':True,'pending_retry_entries':[{'entry_id':self.entry_id}],
                            'session_status':'valid','history':[{'synthetic':True}]},
            'state.json':{'cookies':[{'name':'sessionid','value':'synthetic-cookie-not-a-real-account'}]},
        }
        for name, value in self.payload.items():
            (self.source/name).write_bytes(B._json_bytes(value))
        (self.source/'.env').write_text('synthetic private environment: must never be read',encoding='utf-8')
        (self.source/'logs').mkdir()
        (self.source/'logs'/'runtime.log').write_text('synthetic private message',encoding='utf-8')

    def make_backup(self):
        return B.backup(self.source, self.archive)

    def rewrite_archive(self, change):
        with zipfile.ZipFile(self.archive) as source:
            parts = [(info,source.read(info)) for info in source.infolist()]
        parts = change(parts)
        with warnings.catch_warnings(), zipfile.ZipFile(self.archive,'w') as target:
            warnings.simplefilter('ignore',UserWarning)
            for info, raw in parts:
                target.writestr(info,raw)

    def invoke(self, source, *args, env=None):
        values = dict(os.environ)
        values.update(DATA_DIR=str(self.root/'process-data'),ENV_FILE_PATH=str(self.root/'missing.env'),
                      INSTANCE_LOCK_PATH=str(self.root/'process.pid'),STATE_FILE_PATH='',
                      SPARKKEEPER_BACKGROUND_TASKS='0',LOGIN_GATE='off',PYTHONUTF8='1')
        values.update(env or {})
        return subprocess.run([sys.executable,'-B','-c',source,*map(str,args)],cwd=ROOT,env=values,
                              capture_output=True,text=True,encoding='utf-8',timeout=30)

    def test_backup_whitelist_exact_bytes_and_private_values_never_in_manifest(self):
        before = {name:(self.source/name).read_bytes() for name in self.payload}
        original = B._read_regular
        def guarded(path,*args):
            self.assertNotEqual(Path(path).name,'.env')
            self.assertNotIn('logs',Path(path).parts)
            return original(path,*args)
        with mock.patch.object(B,'_read_regular',side_effect=guarded):
            result = self.make_backup()
        self.assertEqual(result['files'],sorted(self.payload))
        with zipfile.ZipFile(self.archive) as saved:
            self.assertEqual(set(saved.namelist()),{*self.payload,B.MANIFEST})
            for name, raw in before.items():
                self.assertEqual(saved.read(name),raw)
            manifest = saved.read(B.MANIFEST).decode()
            self.assertNotIn('synthetic friend',manifest)
            self.assertNotIn('synthetic-cookie',manifest)
            self.assertNotIn(str(self.source),manifest)
        self.assertFalse((self.source/'logs'/'douyin-cloud-streak.log').exists())
        self.assertEqual(before,{name:(self.source/name).read_bytes() for name in self.payload})

    def test_restore_exact_data_and_guards_even_without_original_marker(self):
        self.make_backup()
        result = B.restore(self.archive,self.target)
        self.assertTrue(result['background_paused'] and result['send_recovery_required'])
        for name in self.payload:
            self.assertEqual((self.target/name).read_bytes(),(self.source/name).read_bytes())
        marker = json.loads((self.target/B.LEDGER_RECOVERY_NAME).read_text())
        self.assertEqual(marker['reason'],'backup_restored_history_unverified')
        self.assertTrue((self.target/B.RESTORE_PAUSE_NAME).exists())

    def test_malformed_original_guards_preserve_exact_bytes_and_still_block(self):
        for name in B.MARKERS:
            (self.source/name).write_bytes(b'\xff invalid synthetic guard')
        self.make_backup()
        B.restore(self.archive,self.target)
        for name in B.MARKERS:
            self.assertEqual((self.target/name).read_bytes(),b'\xff invalid synthetic guard')
        self.assert_restart_blocked()

    def assert_restart_blocked(self):
        result = self.invoke('''
from core import ledger as L, automation as A, scheduler as S, runtime as R
assert L.recovery_required()
assert not S.background_tasks_enabled(), 'inherited background flag bypassed restore pause'
assert L.recover_interrupted_attempts()==1
entry=L.load_ledger()[0]
assert entry['selected'] and entry['last_sent_at']=='2026-10-20T10:00:00+08:00'
assert entry['send_attempt']['status']=='unconfirmed'
assert R.load_runtime()['pending_retry_entries']
for force in (False,True):
    assert A.compute_pending(cfg={},force=force)==[]
    try:L.begin_send_attempt(entry['entry_id'],force=force)
    except L.IdentityConflict:pass
    else:raise AssertionError('restore protection bypassed')
L.set_selected([{'entry_id':entry['entry_id'],'selected':False}])
assert L.recovery_required()
print('synthetic restore guards verified')
''',env={'DATA_DIR':str(self.target),'INSTANCE_LOCK_PATH':str(self.target/'server.pid'),
         'SPARKKEEPER_BACKGROUND_TASKS':'1'})
        self.assertEqual(result.returncode,0,result.stderr)

    def test_real_restart_preserves_pending_intent_and_blocks_force(self):
        self.make_backup()
        B.restore(self.archive,self.target)
        self.assert_restart_blocked()

    def test_clean_old_snapshot_still_blocks_force_across_restarts(self):
        clean=[{**self.rows[0]}]
        clean[0].pop('send_attempt')
        for name in ('ledger.json','ledger.json.bak'):
            (self.source/name).write_bytes(B._json_bytes(clean))
        self.make_backup()
        B.restore(self.archive,self.target)
        script='''
from core import ledger as L, automation as A, scheduler as S
assert not S.background_tasks_enabled()
assert L.recover_interrupted_attempts()==0
entry=L.load_ledger()[0]
assert 'send_attempt' not in entry
assert not A.compute_pending(cfg={},force=True)
try:L.begin_send_attempt(entry['entry_id'],force=True)
except L.IdentityConflict:pass
else:raise AssertionError('healthy old snapshot bypassed send protection')
L.set_selected([{'entry_id':entry['entry_id'],'selected':True}])
'''
        for _ in range(2):
            result=self.invoke(script,env={'DATA_DIR':str(self.target),
                'INSTANCE_LOCK_PATH':str(self.target/'server.pid'),'SPARKKEEPER_BACKGROUND_TASKS':'1'})
            self.assertEqual(result.returncode,0,result.stderr)

    def test_hard_process_exit_leaves_guarded_partial_restore(self):
        self.make_backup()
        result=self.invoke('''
import os,sys
from tools import data_backup as B
write=B._write_new
def interrupted(path,raw):
    if path.name=='ledger.json':os._exit(76)
    write(path,raw)
B._write_new=interrupted
B.restore(sys.argv[1],sys.argv[2])
''',self.archive,self.target)
        self.assertEqual(result.returncode,76,result.stderr)
        self.assertTrue(all((self.target/name).exists() for name in B.MARKERS))
        self.assertFalse((self.target/'ledger.json').exists())
        self.assertNotIn('"ok": true',result.stdout)
        result=self.invoke('''
from core import ledger as L, scheduler as S
assert not S.background_tasks_enabled()
assert L.recovery_required()
assert not L.legacy_import_allowed()
''',env={'DATA_DIR':str(self.target),'SPARKKEEPER_BACKGROUND_TASKS':'1'})
        self.assertEqual(result.returncode,0,result.stderr)

    def test_existing_output_and_restore_target_are_never_overwritten(self):
        self.make_backup()
        before = self.archive.read_bytes()
        with self.assertRaises(B.BackupError):
            self.make_backup()
        self.assertEqual(self.archive.read_bytes(),before)
        self.target.mkdir()
        with self.assertRaises(B.BackupError):
            B.restore(self.archive,self.target)
        (self.target/'keep').write_bytes(b'synthetic owner file')
        with self.assertRaises(B.BackupError):
            B.restore(self.archive,self.target)
        self.assertEqual((self.target/'keep').read_bytes(),b'synthetic owner file')

    def test_concurrent_target_created_at_reservation_is_preserved(self):
        self.make_backup()
        original = Path.mkdir
        def race(path,*args,**kwargs):
            if path == self.target and not path.exists():
                original(path)
                (path/'owner.txt').write_bytes(b'synthetic concurrent owner')
            return original(path,*args,**kwargs)
        with mock.patch.object(Path,'mkdir',race),self.assertRaises(B.BackupError):
            B.restore(self.archive,self.target)
        self.assertEqual(list(self.target.iterdir()),[self.target/'owner.txt'])

    def test_concurrent_output_at_publication_is_never_replaced(self):
        original = os.link
        def race(source,target,*args,**kwargs):
            Path(target).write_bytes(b'synthetic concurrent archive')
            return original(source,target,*args,**kwargs)
        with mock.patch.object(os,'link',race),self.assertRaises(B.BackupError):
            self.make_backup()
        self.assertEqual(self.archive.read_bytes(),b'synthetic concurrent archive')
        self.assertFalse(list(self.root.glob('.spark-backup-*.tmp')))

    def test_actual_application_lock_refuses_backup_until_released(self):
        code='''
import sys
from instance_lock import _acquire_instance_lock
_acquire_instance_lock()
print('READY',flush=True)
sys.stdin.readline()
'''
        env=dict(os.environ,DATA_DIR=str(self.root/'lock-process-data'),
                 ENV_FILE_PATH=str(self.root/'missing.env'),INSTANCE_LOCK_PATH=str(self.source/'server.pid'))
        proc=subprocess.Popen([sys.executable,'-B','-c',code],cwd=ROOT,env=env,stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8')
        try:
            self.assertEqual(proc.stdout.readline().strip(),'READY')
            with self.assertRaises(B.BackupError):self.make_backup()
            self.assertFalse(self.archive.exists())
        finally:
            proc.communicate('\n',timeout=15)
        self.assertTrue(self.make_backup()['ok'])

    def test_custom_os_lock_path_is_explicit_and_pid_path_rejected(self):
        custom=self.root/'custom-instance.pid.lock'
        with exclusive_lock(custom):
            with self.assertRaises(B.BackupError):
                B.backup(self.source,self.archive,lock_file=custom)
        self.assertFalse(self.archive.exists())
        with self.assertRaises(B.BackupError):
            B.backup(self.source,self.archive,lock_file=self.root/'custom-instance.pid')
        self.assertTrue(B.backup(self.source,self.archive,lock_file=custom)['ok'])

    def test_failed_data_write_retains_guards_and_never_changes_source(self):
        self.make_backup()
        before=self.archive.read_bytes()
        original=B._write_new
        def fail(path,raw):
            if path.name=='ledger.json':raise OSError('synthetic full disk')
            return original(path,raw)
        with mock.patch.object(B,'_write_new',side_effect=fail),self.assertRaises(B.BackupError):
            B.restore(self.archive,self.target)
        self.assertTrue(all((self.target/name).exists() for name in B.MARKERS))
        self.assertFalse((self.target/'ledger.json').exists())
        self.assertEqual(self.archive.read_bytes(),before)
        self.assertEqual(json.loads((self.source/'ledger.json').read_text()),self.rows)

    def test_failed_first_guard_never_writes_account_data(self):
        self.make_backup()
        with mock.patch.object(B,'_write_new',side_effect=OSError('synthetic marker error')),self.assertRaises(B.BackupError):
            B.restore(self.archive,self.target)
        self.assertFalse(any((self.target/name).exists() for name in B.DATA_FILES))

    def test_archive_tamper_rejected_before_target_creation(self):
        self.make_backup()
        self.rewrite_archive(lambda items:[(info,b'{}' if info.filename=='state.json' else raw) for info,raw in items])
        with self.assertRaises(B.BackupError):B.restore(self.archive,self.target)
        self.assertFalse(self.target.exists())

    def test_extra_paths_duplicate_case_and_symlink_archive_members_rejected(self):
        self.make_backup()
        original=self.archive.read_bytes()
        for name in ('../escape','/absolute','C:/drive','dir\\state.json','state.json','STATE.JSON','.env'):
            with self.subTest(name=name):
                self.archive.write_bytes(original)
                self.rewrite_archive(lambda items:items+[(name,b'{}')])
                with self.assertRaises(B.BackupError):B.verify(self.archive)
        self.archive.write_bytes(original)
        def linked(items):
            for info,raw in items:
                if info.filename=='state.json':
                    info.create_system=3;info.external_attr=(stat.S_IFLNK|0o777)<<16
            return items
        self.rewrite_archive(linked)
        with self.assertRaises(B.BackupError):B.verify(self.archive)

    def test_manifest_missing_hash_size_and_schema_changes_rejected(self):
        self.make_backup()
        original=self.archive.read_bytes()
        for mutation in ('missing','hash','size','schema'):
            with self.subTest(mutation=mutation):
                self.archive.write_bytes(original)
                def corrupt(items):
                    output=[]
                    for info,raw in items:
                        if info.filename==B.MANIFEST:
                            if mutation=='missing':continue
                            value=json.loads(raw)
                            if mutation=='schema':value['schema']=False
                            elif mutation=='hash':value['files']['state.json']['sha256']='0'*64
                            else:value['files']['state.json']['bytes']+=1
                            raw=B._json_bytes(value)
                        output.append((info,raw))
                    return output
                self.rewrite_archive(corrupt)
                with self.assertRaises(B.BackupError):B.verify(self.archive)

    def test_limits_and_bad_json_fail_without_disclosing_values(self):
        secret='synthetic private invalid json value'
        (self.source/'config.json').write_text(secret,encoding='utf-8')
        with self.assertRaises(B.BackupError) as raised:self.make_backup()
        self.assertNotIn(secret,str(raised.exception))
        (self.source/'config.json').write_bytes(B._json_bytes(self.payload['config.json']))
        with mock.patch.object(B,'MAX_TOTAL_BYTES',1),self.assertRaises(B.BackupError):self.make_backup()
        self.make_backup()
        with mock.patch.object(B,'MAX_FILE_BYTES',8),self.assertRaises(B.BackupError):B.verify(self.archive)

    def test_source_hardlinks_and_paths_through_parent_hops_rejected(self):
        path=self.source/'config.json'
        alias=self.root/'private-alias'
        os.link(path,alias)
        with self.assertRaises(B.BackupError):self.make_backup()
        with self.assertRaises(B.BackupError):B.backup(self.source/'..'/'source',self.archive)

    def test_simulated_windows_reparse_path_is_rejected(self):
        original=Path.lstat
        def reparse(path,*args,**kwargs):
            result=original(path,*args,**kwargs)
            if path==self.source:
                return type('Reparse',(),{'st_mode':result.st_mode,'st_file_attributes':0x400})()
            return result
        with mock.patch.object(Path,'lstat',reparse),self.assertRaises(B.BackupError):self.make_backup()

    def test_cli_does_not_load_environment_or_runtime_services(self):
        result=self.invoke('''
import sys
from tools import data_backup as B
assert 'app_common' not in sys.modules
assert 'core.config' not in sys.modules
assert 'core.runtime' not in sys.modules
assert B.main(['backup','--data-dir',sys.argv[1],'--output',sys.argv[2]])==0
assert 'app_common' not in sys.modules
''',self.source,self.archive)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertNotIn('synthetic-cookie',result.stdout+result.stderr)


if __name__=='__main__':
    unittest.main()
