"""An offline restore must not inherit permission to start background jobs."""
from pathlib import Path

RESTORE_PAUSE_NAME = 'restore-paused.json'
LEDGER_RECOVERY_NAME = 'ledger-recovery-required.json'


def restore_paused(data_dir) -> bool:
    # Content is diagnostic only; malformed files and links cannot lift the pause.
    try:
        (Path(data_dir) / RESTORE_PAUSE_NAME).lstat()
    except FileNotFoundError:
        return False
    return True
