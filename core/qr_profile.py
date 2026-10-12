"""Dedicated QR browser profile: retain resource caches, remove login data.

Call prepare_profile before launching Chromium and clear_profile_login_data only
after its context is closed. Both are fail-closed: unsafe paths and incomplete
cleanup raise QrProfileError, so callers must not launch/reuse that profile.
No account files or browser profile contents are read here.
"""
from __future__ import annotations

import fnmatch
import os
from pathlib import Path
import stat


PROFILE_NAME = "qr-login-profile"
LOGIN_DATA_PATHS = (
    "Default/Cookies*",
    "Default/Network/Cookies*",
    "Default/Network/Trust Tokens*",
    "Default/Local Storage",
    "Default/Session Storage",
    "Default/Sessions",
    "Default/IndexedDB",
    "Default/Service Worker",
    "Default/WebStorage",
    "Default/SharedStorage*",
    "Default/File System",
    "Default/Storage",
    "Default/databases",
    "Default/blob_storage",
    "Default/Login Data*",
    "Default/Web Data*",
    "Default/Account Web Data*",
)


class QrProfileError(RuntimeError):
    """The dedicated profile cannot safely be prepared or cleared."""


def _lstat(path: Path):
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _is_link(metadata) -> bool:
    # Windows junctions and other reparse points need the attribute check;
    # is_symlink() alone does not reject junctions on every Python version.
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _assert_no_links(path: Path) -> None:
    for component in reversed((path, *path.parents)):
        metadata = _lstat(component)
        if metadata is not None and _is_link(metadata):
            raise QrProfileError("QR profile cleanup refuses symbolic links or junctions")


def _checked_root(profile_dir: Path) -> Path:
    root = Path(profile_dir).expanduser().absolute()
    if root.name != PROFILE_NAME:
        raise QrProfileError("QR cleanup requires the dedicated qr-login-profile directory")
    _assert_no_links(root)
    if root.resolve(strict=False) != root:
        raise QrProfileError("QR profile resolved outside its designated directory")
    metadata = _lstat(root)
    if metadata is not None and not stat.S_ISDIR(metadata.st_mode):
        raise QrProfileError("QR profile path is not a directory")
    return root


def _checked_target(root: Path, target: Path):
    # Recheck the root too, immediately before each filesystem mutation.
    if _checked_root(root) != root:
        raise QrProfileError("QR profile directory changed")
    target = Path(target).absolute()
    try:
        relative = target.relative_to(root)
    except ValueError:
        raise QrProfileError("QR cleanup target is outside the dedicated profile") from None
    if not relative.parts:
        raise QrProfileError("QR cleanup must never remove the whole profile")
    _assert_no_links(target)
    resolved = target.resolve(strict=False)
    if resolved != target or root not in resolved.parents:
        raise QrProfileError("QR cleanup target resolved outside the dedicated profile")
    return _lstat(target)


def _walk_checked(root: Path, target: Path) -> None:
    """Preflight each descendant before any selected target is removed."""
    metadata = _checked_target(root, target)
    if metadata is None or not stat.S_ISDIR(metadata.st_mode):
        return
    with os.scandir(target) as entries:
        children = [target / entry.name for entry in entries]
    for child in children:
        _walk_checked(root, child)


def _remove_checked(root: Path, target: Path) -> None:
    """Delete only checked descendants; never call an unchecked recursive remover."""
    metadata = _checked_target(root, target)
    if metadata is None:
        return
    if stat.S_ISDIR(metadata.st_mode):
        with os.scandir(target) as entries:
            children = [target / entry.name for entry in entries]
        for child in children:
            _remove_checked(root, child)
        _checked_target(root, target)
        target.rmdir()
    else:
        _checked_target(root, target)
        target.unlink()


def clear_profile_login_data(profile_dir: Path) -> None:
    """Clear auth/personal data only, preserving HTTP/code cache and other files.

    A missing dedicated profile is already clean. A symlink/junction anywhere
    along a deletion path is rejected, including links whose target is inside
    the profile. Deletion failures are never ignored.
    """
    try:
        root = _checked_root(profile_dir)
        if _lstat(root) is None:
            return
        targets = set()
        for pattern in LOGIN_DATA_PATHS:
            parent_name, filename_pattern = pattern.rsplit("/", 1)
            parent = root / parent_name
            metadata = _checked_target(root, parent)
            if metadata is None:
                continue
            if not stat.S_ISDIR(metadata.st_mode):
                raise QrProfileError("QR login-data parent is not a directory")
            with os.scandir(parent) as entries:
                candidates = [parent / entry.name for entry in entries if fnmatch.fnmatch(entry.name, filename_pattern)]
            for target in candidates:
                _walk_checked(root, target)
                targets.add(target)
        for target in sorted(targets, key=lambda path: str(path)):
            _remove_checked(root, target)
        # A concurrently created auth file must not be silently retained.
        for pattern in LOGIN_DATA_PATHS:
            parent_name, filename_pattern = pattern.rsplit("/", 1)
            parent = root / parent_name
            metadata = _checked_target(root, parent)
            if metadata is not None:
                with os.scandir(parent) as entries:
                    if any(fnmatch.fnmatch(entry.name, filename_pattern) for entry in entries):
                        raise QrProfileError("QR login data remained after cleanup")
    except QrProfileError:
        raise
    except (OSError, RuntimeError, ValueError) as error:
        raise QrProfileError("QR profile login-data cleanup failed") from error


def prepare_profile(data_dir: Path) -> Path:
    """Create/clean only data_dir/qr-login-profile; never copy another profile."""
    try:
        root = _checked_root(Path(data_dir).expanduser().absolute() / PROFILE_NAME)
        root.mkdir(parents=True, exist_ok=True)
        root = _checked_root(root)
        clear_profile_login_data(root)
        if _lstat(root) is None:
            raise QrProfileError("QR profile disappeared during preparation")
        return root
    except QrProfileError:
        raise
    except (OSError, RuntimeError, ValueError) as error:
        raise QrProfileError("QR profile preparation failed") from error
