"""无配置读取副作用的口令格式与验证：兼容旧 SHA-256，新口令使用 scrypt。"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import re
import secrets

SCRYPT_N = 32768
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_MAXMEM = 64 * 1024 * 1024
_MAX_PASSWORD_BYTES = 4096
_PLACEHOLDERS = ("change_me", "changeme", "replace_me", "your_")


@dataclass(frozen=True)
class _ScryptSpec:
    n: int
    r: int
    p: int
    salt: bytes
    digest: bytes


def _safe_equal(supplied: str, expected: str) -> bool:
    """非文本和畸形 Unicode 视为不等；字节比较不受 ASCII 限制。"""
    if not isinstance(supplied, str) or not isinstance(expected, str):
        return False
    try:
        return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))
    except UnicodeError:
        return False


def _parse_scrypt(encoded: str) -> _ScryptSpec | None:
    # 先限制文本及每个字段，再转整数或分配字节，超限配置绝不进入 KDF。
    if not isinstance(encoded, str) or len(encoded) > 512:
        return None
    fields = encoded.split("$")
    if len(fields) != 6 or fields[0] != "scrypt":
        return None
    if any(not re.fullmatch(r"[1-9][0-9]{0,5}", field) for field in fields[1:4]):
        return None
    n, r, p = (int(field) for field in fields[1:4])
    if not (16384 <= n <= 32768 and n & (n - 1) == 0 and 1 <= r <= 8 and 1 <= p <= 2):
        return None
    if n * r * p > SCRYPT_N * SCRYPT_R:
        return None
    salt_hex, digest_hex = fields[4:6]
    if (not 32 <= len(salt_hex) <= 128 or len(salt_hex) % 2
            or not 64 <= len(digest_hex) <= 128 or len(digest_hex) % 2
            or not re.fullmatch(r"[0-9a-fA-F]+", salt_hex)
            or not re.fullmatch(r"[0-9a-fA-F]+", digest_hex)):
        return None
    return _ScryptSpec(n, r, p, bytes.fromhex(salt_hex), bytes.fromhex(digest_hex))


def password_hash_kind(salt: str, encoded: str) -> str | None:
    """仅检查格式和资源边界，不运行昂贵的口令派生。"""
    if not isinstance(encoded, str):
        return None
    if encoded.startswith("scrypt$"):
        return "scrypt" if _parse_scrypt(encoded) is not None else None
    if (isinstance(salt, str) and salt
            and not salt.lower().startswith(_PLACEHOLDERS)
            and re.fullmatch(r"[0-9a-fA-F]{64}", encoded)):
        try:
            salt.encode("utf-8")
        except UnicodeError:
            return None
        return "sha256"
    return None


def _password_bytes(password: str, *, bounded: bool = True) -> bytes | None:
    if not isinstance(password, str) or not password or (bounded and len(password) > _MAX_PASSWORD_BYTES):
        return None
    try:
        encoded = password.encode("utf-8")
    except UnicodeError:
        return None
    return encoded if not bounded or len(encoded) <= _MAX_PASSWORD_BYTES else None


def hash_password(password: str) -> str:
    """生成自含盐的标准 scrypt 格式，不读取任何文件或环境变量。"""
    if not isinstance(password, str) or len(password) < 12:
        raise ValueError("新密码至少需要 12 个字符")
    if password != password.strip():
        raise ValueError("新密码首尾不能包含空格")
    password_bytes = _password_bytes(password)
    if password_bytes is None:
        raise ValueError("密码必须是非空 UTF-8 文本，且不能超过 4096 字节")
    return _derive_scrypt(password_bytes)


def rehash_legacy_password(password: str) -> str | None:
    """Preserve an already verified legacy password, without new-password policy.

    Unsupported lengths remain on their existing hash instead of creating a
    scrypt hash that the bounded verifier would subsequently reject.
    """
    password_bytes = _password_bytes(password)
    return _derive_scrypt(password_bytes) if password_bytes is not None else None


def _derive_scrypt(password_bytes: bytes) -> str:
    if not callable(getattr(hashlib, "scrypt", None)):
        raise ValueError("当前 Python 缺少 scrypt 支持，请使用包含 OpenSSL 的 Python")
    salt = secrets.token_bytes(32)
    try:
        digest = hashlib.scrypt(password_bytes, salt=salt, n=SCRYPT_N, r=SCRYPT_R,
                                p=SCRYPT_P, dklen=32, maxmem=SCRYPT_MAXMEM)
    except (ValueError, MemoryError) as error:
        raise ValueError("scrypt 口令生成失败，配置未修改，请检查 Python 和可用内存") from error
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, salt: str, encoded: str) -> bool:
    """错误格式或超过上限的参数直接失败，不回退到其它哈希算法。"""
    kind = password_hash_kind(salt, encoded)
    if kind is None:
        return False
    # 旧 SHA-256 配置不新增长度限制；资源上限针对新 scrypt 路径。
    password_bytes = _password_bytes(password, bounded=kind != "sha256")
    if password_bytes is None:
        return False
    if kind == "sha256":
        computed = hashlib.sha256(password_bytes + salt.encode("utf-8")).hexdigest()
        return _safe_equal(computed.lower(), encoded.lower())
    if kind != "scrypt" or not callable(getattr(hashlib, "scrypt", None)):
        return False
    spec = _parse_scrypt(encoded)
    try:
        computed = hashlib.scrypt(password_bytes, salt=spec.salt, n=spec.n, r=spec.r,
                                  p=spec.p, dklen=len(spec.digest), maxmem=SCRYPT_MAXMEM)
    except (ValueError, MemoryError):
        return False
    return hmac.compare_digest(computed, spec.digest)
