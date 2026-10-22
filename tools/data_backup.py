"""Offline private-data backup, verification and guarded restore. Never reads .env."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import zipfile
import zlib

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.file_lock import exclusive_lock
from core.restore_guard import LEDGER_RECOVERY_NAME, RESTORE_PAUSE_NAME

MANIFEST = 'backup-manifest.json'
DATA_FILES = ('config.json', 'ledger.json', 'ledger.json.bak', 'runtime.json', 'state.json')
MARKERS = (RESTORE_PAUSE_NAME, LEDGER_RECOVERY_NAME)
ALLOWED_FILES = DATA_FILES + MARKERS
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_BYTES = MAX_TOTAL_BYTES + 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024


class BackupError(ValueError):
    """Safe-to-display validation error, containing no private field values."""


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, indent=2).encode('utf-8')


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _linked(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)


def _safe_path(value):
    """Validate existing lexical components before resolving any links/junctions."""
    requested = Path(os.path.expanduser(str(value)))
    if '..' in requested.parts:
        raise BackupError('路径不能包含上级目录跳转')
    path = Path(os.path.abspath(requested))
    for part in (*reversed(path.parents), path):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if _linked(info):
            raise BackupError('路径包含链接或 Windows 重解析点，未操作')
    return path


def _identity(path):
    info = _safe_path(path).lstat()
    return info.st_dev, info.st_ino


def _read_regular(path, limit=MAX_FILE_BYTES):
    path = _safe_path(path)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit:
        raise BackupError('仅允许限定大小的普通文件；不接受链接或特殊文件')
    with path.open('rb') as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise BackupError('读取时文件已被替换，未操作')
        raw = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    current = _safe_path(path).lstat()
    if len(raw) > limit or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
        raise BackupError('读取时文件发生变化或超过大小限制，未操作')
    return raw


def _validate_data(name, raw):
    # An unreadable marker is still a guard. Preserve its exact bytes.
    if name in MARKERS:
        return
    try:
        value = json.loads(raw.decode('utf-8'))
    except (ValueError, UnicodeError):
        raise BackupError(f'{name} 不是有效 UTF-8 JSON；未修改原数据') from None
    if name.startswith('ledger.'):
        valid = isinstance(value, list) and all(isinstance(row, dict) for row in value)
    else:
        valid = isinstance(value, dict)
    if not valid:
        raise BackupError(f'{name} 的数据结构不符合要求')


def _read_backup(archive):
    raw_archive = _read_regular(archive, MAX_ARCHIVE_BYTES)
    try:
        with zipfile.ZipFile(io.BytesIO(raw_archive)) as source:
            infos = source.infolist()
            names = [info.filename for info in infos]
            if any(info.orig_filename != info.filename for info in infos):
                raise BackupError('备份包含异常文件名')
            if not names or len(names) != len(set(name.casefold() for name in names)) or MANIFEST not in names:
                raise BackupError('备份缺少清单或包含重复文件')
            if any(name not in (*ALLOWED_FILES, MANIFEST) for name in names):
                raise BackupError('备份包含白名单之外的文件或路径')
            if sum(info.file_size for info in infos) > MAX_TOTAL_BYTES + MAX_MANIFEST_BYTES:
                raise BackupError('备份解压总量超过限制')
            payload = {}
            for info in infos:
                mode = info.external_attr >> 16
                limit = MAX_MANIFEST_BYTES if info.filename == MANIFEST else MAX_FILE_BYTES
                if info.is_dir() or stat.S_IFMT(mode) not in (0, stat.S_IFREG) or info.flag_bits & 1 or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED) or info.file_size > limit:
                    raise BackupError('备份含有链接、目录、加密条目或超限文件')
                with source.open(info) as stream:
                    raw = stream.read(limit + 1)
                if len(raw) != info.file_size or len(raw) > limit:
                    raise BackupError('备份条目长度不一致')
                payload[info.filename] = raw
    except (zipfile.BadZipFile, NotImplementedError, RuntimeError, EOFError, zlib.error):
        raise BackupError('备份压缩结构损坏或不受支持') from None
    try:
        def unique_pairs(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError
                result[key] = value
            return result
        manifest = json.loads(payload.pop(MANIFEST).decode('utf-8'), object_pairs_hook=unique_pairs)
        if type(manifest) is not dict or type(manifest.get('schema')) is not int or manifest['schema'] != 1 or manifest.get('kind') != 'sparkkeeper-private-data':
            raise ValueError
        files = manifest['files']
        if type(files) is not dict or set(files) != set(payload) or not any(name in payload for name in DATA_FILES):
            raise ValueError
        for name, raw in payload.items():
            record = files[name]
            if type(record) is not dict or type(record.get('bytes')) is not int or record['bytes'] != len(raw) or record.get('sha256') != hashlib.sha256(raw).hexdigest():
                raise ValueError
            _validate_data(name, raw)
    except (KeyError, TypeError, ValueError, UnicodeError):
        raise BackupError('备份清单、文件摘要或数据结构不一致') from None
    return payload, manifest


def verify(archive):
    payload, manifest = _read_backup(archive)
    return {'ok':True, 'private_backup':True, 'files':sorted(payload), 'bytes':sum(map(len, payload.values())),
            'schema':manifest['schema'], 'sha256':hashlib.sha256(_read_regular(archive, MAX_ARCHIVE_BYTES)).hexdigest()}


def backup(data_dir, archive, *, lock_file=None):
    source, output = _safe_path(data_dir), _safe_path(archive)
    if not source.is_dir() or not output.parent.is_dir() or output.exists():
        raise BackupError('源目录和输出父目录必须存在，输出文件必须尚不存在')
    # Do not create a backup over a live data filename or diagnostic lock.
    if output.is_relative_to(source):
        raise BackupError('备份文件必须放在数据目录之外')
    lock_path = _safe_path(lock_file or source / 'server.pid.lock')
    if not lock_path.name.endswith('.lock'):
        raise BackupError('--lock-file 必须指定服务实际使用的 .lock 文件，不能传 PID 文件')
    if not lock_path.parent.is_dir():
        raise BackupError('实例锁的父目录不存在')
    if lock_path.exists() and (not lock_path.is_file() or lock_path.stat().st_nlink != 1):
        raise BackupError('实例锁必须是普通文件')
    source_id = _identity(source)
    try:
        with exclusive_lock(lock_path):
            payload = {}
            for name in ALLOWED_FILES:
                path = _safe_path(source / name)
                if path.exists():
                    raw = _read_regular(path)
                    _validate_data(name, raw)
                    payload[name] = raw
            if not any(name in payload for name in DATA_FILES):
                raise BackupError('源目录没有可备份的数据文件')
            if sum(map(len, payload.values())) > MAX_TOTAL_BYTES:
                raise BackupError('数据总量超过备份限制')
            # Detect a wrong/custom lock or uncooperative writer before publishing.
            if _identity(source) != source_id or any(_read_regular(source/name) != raw for name, raw in payload.items()):
                raise BackupError('读取期间源目录发生变化，未生成备份')
            if set(payload) != {name for name in ALLOWED_FILES if _safe_path(source/name).exists()}:
                raise BackupError('读取期间数据文件集合发生变化，未生成备份')
            manifest = {'schema':1, 'kind':'sparkkeeper-private-data', 'created_at':_now(),
                        'files':{name:{'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()} for name,raw in payload.items()}}
            fd, temp_name = tempfile.mkstemp(prefix='.spark-backup-', suffix='.tmp', dir=output.parent)
            temporary = Path(temp_name)
            try:
                with os.fdopen(fd, 'w+b') as stream:
                    with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as target:
                        for name, raw in payload.items():
                            target.writestr(name, raw)
                        target.writestr(MANIFEST, _json_bytes(manifest))
                    stream.flush()
                    os.fsync(stream.fileno())
                _read_backup(temporary)
                _safe_path(output)
                # Hard-link creation is atomic and refuses any existing destination.
                os.link(temporary, output)
            finally:
                temporary.unlink(missing_ok=True)
    except OSError as error:
        raise BackupError('备份未完成：实例占用、路径不可用或写入失败；未覆盖原文件') from None
    return verify(output)


def _write_new(path, raw):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def restore(archive, target):
    payload, _ = _read_backup(archive)
    destination = _safe_path(target)
    if destination.exists() or not destination.parent.is_dir():
        raise BackupError('恢复目标必须是尚不存在的新目录，且父目录已存在')
    # Any snapshot can predate a send. These markers are never optional, even
    # when the backed-up ledger was healthy and contained no pending intent.
    payload.setdefault(LEDGER_RECOVERY_NAME, _json_bytes({'version':1,'required':True,
                       'reason':'backup_restored_history_unverified','detected_at':_now()}))
    payload.setdefault(RESTORE_PAUSE_NAME, _json_bytes({'version':1,'paused':True,
                       'reason':'backup_restored_review_required','restored_at':_now()}))
    # Reserve the destination exclusively. Guards are fsynced first, so a hard
    # process interruption leaves a protected partial restore, never sendable data.
    try:
        destination.mkdir(mode=0o700, exist_ok=False)
    except OSError:
        raise BackupError('无法创建新恢复目录；已有目录不会被覆盖') from None
    destination_id = _identity(destination)
    try:
        with exclusive_lock(destination / 'server.pid.lock'):
            for name in (*MARKERS, *DATA_FILES):
                if name not in payload:
                    continue
                if _identity(destination) != destination_id:
                    raise BackupError('恢复目录在写入期间被替换')
                path = _safe_path(destination/name)
                _write_new(path, payload[name])
            if any(_read_regular(destination/name) != raw for name,raw in payload.items()):
                raise BackupError('恢复后的文件验证失败')
    except (OSError, BackupError):
        # Preserve guards on partial output, including after a disk-full failure.
        # Do not clean recursively or remove files another process may have made.
        raise BackupError('恢复未完成，原数据未改动；若留下新目录，请保留恢复保护并检查后重新恢复') from None
    return {'ok':True,'private_backup':True,'files':sorted(payload),'background_paused':True,
            'send_recovery_required':True,'restore_directory':str(destination)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest='action', required=True)
    save = actions.add_parser('backup', help='停止服务后备份私有数据')
    save.add_argument('--data-dir', required=True)
    save.add_argument('--output', required=True)
    save.add_argument('--lock-file', help='仅在服务使用自定义实例锁时指定实际 .lock 文件')
    check = actions.add_parser('verify', help='离线检查备份白名单和摘要')
    check.add_argument('--archive', required=True)
    load = actions.add_parser('restore', help='恢复到新目录并保持后台和发送暂停')
    load.add_argument('--archive', required=True)
    load.add_argument('--target', required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == 'backup':
            result = backup(args.data_dir, args.output, lock_file=args.lock_file)
        elif args.action == 'verify':
            result = verify(args.archive)
        else:
            result = restore(args.archive, args.target)
    except (BackupError, OSError):
        # Never echo OS exception text: a path can itself contain private data.
        error = sys.exc_info()[1]
        print(str(error) if isinstance(error, BackupError) else '操作未完成：文件不可读或路径不可用', file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
