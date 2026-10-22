"""Small OS-lock primitives with no configuration, data or logging imports."""
from contextlib import contextmanager
import os


def lock(handle) -> None:
    handle.seek(0)
    if os.name == 'nt':
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def unlock(handle) -> None:
    handle.seek(0)
    if os.name == 'nt':
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def exclusive_lock(path):
    """Use the same stable lock inode as the running application; never unlink it."""
    with path.open('a+b') as handle:
        os.set_inheritable(handle.fileno(), False)
        lock(handle)
        try:
            yield
        finally:
            unlock(handle)
