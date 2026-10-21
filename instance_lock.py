"""Process-lifetime OS locks; PID files are diagnostic metadata only."""
from __future__ import annotations

import atexit
import os
from pathlib import Path
import threading

from app_common import PID_PATH, logger
from core.config import atomic_write_text

_guard = threading.RLock()
_held: dict[str, tuple[object, Path]] = {}


def _lock(handle) -> None:
    handle.seek(0)
    if os.name == 'nt':
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(handle) -> None:
    handle.seek(0)
    if os.name == 'nt':
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _remove_own_pid(path: Path) -> None:
    try:
        if path.read_text(encoding='utf-8').strip() == str(os.getpid()):
            path.unlink(missing_ok=True)
    except (OSError, UnicodeError):
        pass  # Missing diagnostic metadata does not release the separate lock.


def _clear_instance_metadata() -> None:
    """Server stop is not process exit: other worker threads may still be alive."""
    with _guard:
        for _, path in _held.values():
            _remove_own_pid(path)


def _release_all() -> None:
    with _guard:
        for handle, path in list(_held.values()):
            try:
                _remove_own_pid(path)
                _unlock(handle)
            except OSError:
                pass  # Process exit still closes the descriptor and releases it.
            finally:
                handle.close()
        _held.clear()


def _acquire_instance_lock() -> None:
    path = Path(PID_PATH).expanduser().resolve()
    key = os.path.normcase(str(path))
    with _guard:
        if key in _held:
            atomic_write_text(path, str(os.getpid()))
            return  # Same process can restart its desktop server.
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = path.with_name(path.name + '.lock')
        handle = lock_path.open('a+b')
        os.set_inheritable(handle.fileno(), False)
        try:
            _lock(handle)
        except OSError as error:
            handle.close()
            logger.error('检测到已有 sparkkeeper 实例在运行，拒绝启动；请先停止旧实例，避免多实例重复发送。')
            raise SystemExit('已有实例在运行，请先停止旧实例') from error
        try:
            atomic_write_text(path, str(os.getpid()))
        except BaseException:
            _unlock(handle)
            handle.close()
            raise
        _held[key] = (handle, path)
        # Never unlink this stable lock file: replacing its inode could permit
        # another lock on POSIX. The OS closes our handle even after a hard crash.


atexit.register(_release_all)
