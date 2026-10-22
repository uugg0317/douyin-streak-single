"""离线整理台账：默认预览，--apply 需要独占实例锁并先保存可恢复备份。"""
from __future__ import annotations
import argparse
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import sys
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir',required=True)
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args();data=Path(args.data_dir).resolve()
    from core.contact_dedupe import consolidate_duplicates
    path=data/'ledger.json'
    if not args.apply:
        _,report=consolidate_duplicates(json.loads(path.read_text(encoding='utf-8')))
        print(json.dumps({k:v for k,v in report.items() if k!='aliases'},ensure_ascii=False));return
    # 不读取 .env 或登录态；与本目录服务使用同一把操作系统锁。
    os.environ.update(DATA_DIR=str(data),INSTANCE_LOCK_PATH=str(data/'server.pid'),ENV_FILE_PATH=str(data/'__dedupe_missing.env'))
    from instance_lock import _acquire_instance_lock,_release_all
    _acquire_instance_lock()
    try:
        runtime=data/'runtime.json'
        state=json.loads(runtime.read_text(encoding='utf-8')) if runtime.exists() else {}
        if any(state.get(k) for k in ('running','stopping','retry_dispatching','pending_retry_entries')) or state.get('contacts_job_status')=='running':
            raise RuntimeError('存在进行中或待处理任务，未整理台账')
        before=path.read_bytes();entries=json.loads(before.decode('utf-8'))
        result,report=consolidate_duplicates(entries)
        if not report['removed_duplicates']:
            print(json.dumps({k:v for k,v in report.items() if k!='aliases'},ensure_ascii=False));return
        from core.config import atomic_write_bytes,atomic_write_text
        from core.ledger import _mark_ambiguity
        _mark_ambiguity(result)
        backup=data/'backups'/('dedupe-'+datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8])
        backup.mkdir(parents=True,exist_ok=False)
        for name in ('ledger.json','ledger.json.bak','runtime.json'):
            source=data/name
            if source.exists():atomic_write_bytes(backup/name,source.read_bytes())
        if (backup/'ledger.json').read_bytes()!=before or path.read_bytes()!=before:
            raise RuntimeError('台账在整理时发生变化，未写入')
        atomic_write_text(backup/'merge-report.json',json.dumps(report,ensure_ascii=False,indent=2))
        atomic_write_bytes(data/'ledger.json.bak',before)
        atomic_write_text(path,json.dumps(result,ensure_ascii=False,indent=2))
        print(json.dumps({**{k:v for k,v in report.items() if k!='aliases'},'backup':str(backup)},ensure_ascii=False))
    finally:_release_all()

if __name__=='__main__':main()
