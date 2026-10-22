"""T05：scrypt 资源边界与旧口令兼容；部署只执行纯 Python 预检，绝不安装或部署。"""
from __future__ import annotations

import hashlib
import importlib.util
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock
from fastapi import HTTPException

import auth_config
import password_setup

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / ".review-work" / "t05-password-tests"
PASSWORD = "仅用于隔离测试的-password-123"


def isolated_auth(values):
    common = types.ModuleType("app_common")
    common._env = lambda key: values.get(key, "")
    common.logger = mock.Mock()
    spec = importlib.util.spec_from_file_location("t05_isolated_auth", ROOT / "auth_service.py")
    auth = importlib.util.module_from_spec(spec)
    with mock.patch.dict("sys.modules", {"app_common": common}):
        spec.loader.exec_module(auth)
    return auth, common.logger


class PasswordHashTests(unittest.TestCase):
    def test_generated_scrypt_verifies_unicode_and_wrong_password_is_rejected(self):
        encoded = auth_config.hash_password(PASSWORD)
        self.assertEqual(auth_config.password_hash_kind("", encoded), "scrypt")
        self.assertTrue(auth_config.verify_password(PASSWORD, "", encoded))
        self.assertFalse(auth_config.verify_password(PASSWORD + "wrong", "", encoded))
        self.assertNotIn(PASSWORD, encoded)
        fields = encoded.split("$")
        self.assertEqual(fields[:4], ["scrypt", "32768", "8", "1"])
        self.assertEqual(len(bytes.fromhex(fields[4])), 32)
        self.assertEqual(len(bytes.fromhex(fields[5])), 32)

    def test_new_hashes_use_independent_salts(self):
        first = auth_config.hash_password(PASSWORD)
        second = auth_config.hash_password(PASSWORD)
        self.assertNotEqual(first.split("$")[4], second.split("$")[4])
        self.assertNotEqual(first, second)

    def test_legacy_sha256_uppercase_and_unicode_remain_compatible(self):
        salt = "legacy-text-salt"
        encoded = hashlib.sha256((PASSWORD + salt).encode()).hexdigest()
        for hashed in (encoded, encoded.upper()):
            self.assertEqual(auth_config.password_hash_kind(salt, hashed), "sha256")
            self.assertTrue(auth_config.verify_password(PASSWORD, salt, hashed))
            self.assertFalse(auth_config.verify_password("wrong", salt, hashed))

    def test_legacy_valid_password_and_salt_do_not_gain_new_length_limits(self):
        password, salt = "a" * 5000, "b" * 1500
        encoded = hashlib.sha256((password + salt).encode()).hexdigest()
        self.assertEqual(auth_config.password_hash_kind(salt, encoded), "sha256")
        self.assertTrue(auth_config.verify_password(password, salt, encoded))

    def test_new_generator_rejects_short_or_surrounding_whitespace_before_kdf(self):
        with mock.patch.object(auth_config.hashlib, "scrypt") as kdf:
            for password in ("short", " " + PASSWORD, PASSWORD + " ", " " * 12):
                with self.subTest(password=password), self.assertRaises(ValueError):
                    auth_config.hash_password(password)
            kdf.assert_not_called()

    def test_invalid_types_and_unicode_do_not_raise_or_run_kdf(self):
        encoded = auth_config.hash_password(PASSWORD)
        with mock.patch.object(auth_config.hashlib, "scrypt") as kdf:
            for supplied in (None, 1, [], {}, "", "\ud800", "x" * 4097, "汉" * 2000):
                self.assertFalse(auth_config.verify_password(supplied, "", encoded))
            kdf.assert_not_called()
        for supplied in (None, [], {}, 1, "\ud800"):
            self.assertFalse(auth_config._safe_equal(supplied, "ascii"))
        self.assertTrue(auth_config._safe_equal("中文", "中文"))

    def test_malformed_and_expensive_config_is_rejected_before_kdf(self):
        salt, digest = "11" * 32, "22" * 32
        invalid = [
            f"scrypt$1048576$8$1${salt}${digest}",
            f"scrypt$32767$8$1${salt}${digest}",
            f"scrypt$32768$999999$1${salt}${digest}",
            f"scrypt$32768$8$999999${salt}${digest}",
            f"scrypt$32768$8$2${salt}${digest}",
            f"scrypt$0$8$1${salt}${digest}",
            f"scrypt$-16384$8$1${salt}${digest}",
            f"scrypt$16384$0$1${salt}${digest}",
            f"scrypt$16384$8$0${salt}${digest}",
            f"scrypt$16384$8$1${'11' * 15}${digest}",
            f"scrypt$16384$8$1${'11' * 65}${digest}",
            f"scrypt$16384$8$1${salt}${'22' * 31}",
            f"scrypt$16384$8$1${salt}${'22' * 65}",
            f"scrypt$16384$8$1${salt[:-1]}${digest}",
            f"scrypt$16384$8$1${'zz' * 32}${digest}",
            f"scrypt$16384$8$1${salt}${'zz' * 32}",
            f"scrypt$16384$8$1${salt}${digest}$extra",
            "scrypt$" + "9" * 100000,
        ]
        with mock.patch.object(auth_config.hashlib, "scrypt") as kdf:
            for encoded in invalid:
                with self.subTest(encoded=encoded[:60]):
                    self.assertIsNone(auth_config.password_hash_kind("", encoded))
                    self.assertFalse(auth_config.verify_password(PASSWORD, "", encoded))
            kdf.assert_not_called()

    def test_supported_variant_uses_bounded_memory_and_digest_size(self):
        salt = b"a" * 16
        digest = hashlib.scrypt(PASSWORD.encode(), salt=salt, n=16384, r=8, p=2,
                                dklen=64, maxmem=auth_config.SCRYPT_MAXMEM)
        encoded = f"scrypt$16384$8$2${salt.hex()}${digest.hex().upper()}"
        actual = auth_config.hashlib.scrypt
        with mock.patch.object(auth_config.hashlib, "scrypt", wraps=actual) as kdf:
            self.assertTrue(auth_config.verify_password(PASSWORD, "unused-legacy-salt", encoded))
        self.assertEqual(kdf.call_args.kwargs["maxmem"], 64 * 1024 * 1024)
        self.assertEqual(kdf.call_args.kwargs["dklen"], 64)

    def test_runtime_scrypt_unavailable_or_resource_failure_fails_closed(self):
        encoded = auth_config.hash_password(PASSWORD)
        with mock.patch.object(auth_config.hashlib, "scrypt", None):
            self.assertFalse(auth_config.verify_password(PASSWORD, "", encoded))
            with self.assertRaisesRegex(ValueError, "缺少 scrypt"):
                auth_config.hash_password(PASSWORD)
        for failure in (ValueError("模拟内存限制"), MemoryError("模拟内存不足")):
            with mock.patch.object(auth_config.hashlib, "scrypt", side_effect=failure):
                self.assertFalse(auth_config.verify_password(PASSWORD, "", encoded))

    def test_startup_accepts_self_contained_password_without_external_salt(self):
        encoded = auth_config.hash_password(PASSWORD)
        for entry in ({"URL_ACCESS_TOKEN": "synthetic-url-token"}, {"PASSWORD_PUBLIC_LOGIN": "on"}):
            auth, _ = isolated_auth({"LOGIN_GATE": "on", "URL_PASSWORD_HASH": encoded, **entry})
            auth._check_auth_config()
            self.assertTrue(auth._verify_url_password(PASSWORD))
            self.assertFalse(auth._verify_url_password("wrong"))

    def test_startup_legacy_warning_is_safe_and_password_remains_usable(self):
        salt = "legacy-test-salt"
        encoded = hashlib.sha256((PASSWORD + salt).encode()).hexdigest().upper()
        auth, logger = isolated_auth({"LOGIN_GATE": "on", "PASSWORD_PUBLIC_LOGIN": "on",
                                     "URL_PASSWORD_SALT": salt, "URL_PASSWORD_HASH": encoded})
        auth._check_auth_config()
        warnings = "\n".join(str(call) for call in logger.warning.call_args_list)
        self.assertIn("旧 SHA-256", warnings)
        for secret in (PASSWORD, salt, encoded):
            self.assertNotIn(secret, warnings)
        self.assertTrue(auth._verify_url_password(PASSWORD))

    def test_startup_rejects_expensive_scrypt_without_running_it(self):
        encoded = f"scrypt$999999$8$1${'11' * 32}${'22' * 32}"
        auth, _ = isolated_auth({"LOGIN_GATE": "on", "AUTH_TOKEN": "synthetic-token-that-is-long-enough",
                                "URL_PASSWORD_HASH": encoded})
        with mock.patch.object(auth_config.hashlib, "scrypt") as kdf:
            with self.assertRaises(SystemExit):
                auth._check_auth_config()
            kdf.assert_not_called()

    def test_invalid_session_tokens_are_401_false_or_noop(self):
        auth, _ = isolated_auth({"LOGIN_GATE": "on", "AUTH_TOKEN": "synthetic-token"})
        for token in ([], {}, 1, None):
            with self.subTest(token=token):
                with self.assertRaises(HTTPException) as error:
                    auth._check_auth(token)
                self.assertEqual(error.exception.status_code, 401)
                self.assertFalse(auth._session_valid(token))
                auth._revoke_session(token)

    def test_existing_scrypt_is_preserved_and_generation_failure_does_not_write(self):
        WORK.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=WORK) as temporary:
            target = Path(temporary) / ".env"
            reader = mock.Mock(side_effect=[PASSWORD, PASSWORD])
            password_setup.initialize_env(target, password_reader=reader, emit=lambda text: None)
            original = target.read_bytes()
            reader.reset_mock()
            password_setup.initialize_env(target, password_reader=reader, emit=lambda text: None)
            reader.assert_not_called()
            self.assertEqual(target.read_bytes(), original)
            other = Path(temporary) / "missing.env"
            with mock.patch.object(password_setup, "hash_password", side_effect=ValueError("模拟派生失败")):
                with self.assertRaises(password_setup.PasswordSetupError):
                    password_setup.initialize_env(other, password_reader=mock.Mock(side_effect=[PASSWORD, PASSWORD]),
                                                  emit=lambda text: None)
            self.assertFalse(other.exists())


class DeployPasswordPreflightTests(unittest.TestCase):
    def test_actual_deploy_preflight_accepts_scrypt_and_legacy_and_rejects_incomplete_entries(self):
        text = (ROOT / "deploy.sh").read_text(encoding="utf-8")
        match = re.search(r"if ! STREAK_SOURCE_DIR=.*?python3 -c '(.*?)\n'; then", text, re.S)
        self.assertIsNotNone(match)
        code = match.group(1)
        scrypt_hash = auth_config.hash_password(PASSWORD)
        legacy_salt = "synthetic-legacy-salt"
        legacy_hash = hashlib.sha256((PASSWORD + legacy_salt).encode()).hexdigest().upper()
        token = "synthetic-deploy-token-that-is-long-enough"
        cases = [
            ({"URL_ACCESS_TOKEN": token, "URL_PASSWORD_HASH": scrypt_hash}, 0),
            ({"PASSWORD_PUBLIC_LOGIN": "on", "URL_PASSWORD_HASH": scrypt_hash}, 0),
            ({"URL_ACCESS_TOKEN": token, "URL_PASSWORD_SALT": legacy_salt, "URL_PASSWORD_HASH": legacy_hash}, 0),
            ({"AUTH_TOKEN": token}, 0),
            ({"AUTH_TOKEN": token, "PASSWORD_PUBLIC_LOGIN": "on"}, 1),
            ({"AUTH_TOKEN": token, "URL_ACCESS_TOKEN": token}, 1),
            ({"AUTH_TOKEN": token, "URL_PASSWORD_HASH": "scrypt$999999$8$1$" + "11" * 32 + "$" + "22" * 32}, 1),  # SYNTHETIC_SECRET_SHA256=139e70a8233a6d2b90a4eed5cbdf11a346f792d4d61ab3aee144ea16ccde3bcb
            ({"PASSWORD_PUBLIC_LOGIN": "on", "URL_PASSWORD_HASH": scrypt_hash, "LOGIN_GATE": "off"}, 1),
            ({"AUTH_TOKEN": "change_me_token", "PASSWORD_PUBLIC_LOGIN": "on", "URL_PASSWORD_HASH": scrypt_hash}, 1),
        ]
        WORK.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=WORK) as temporary:
            path = Path(temporary) / "service.env"
            for values, status in cases:
                with self.subTest(values=list(values), status=status):
                    path.write_text("\n".join(f"{key}={value}" for key, value in {"LOGIN_GATE": "on", **values}.items()),
                                    encoding="utf-8")
                    env = os.environ.copy()
                    env.update(STREAK_SOURCE_DIR=str(ROOT), STREAK_SERVICE_ENV=str(path), PYTHONDONTWRITEBYTECODE="1")
                    # -S 禁止载入第三方依赖，证明安装 venv 前可执行该标准库预检。
                    result = subprocess.run([sys.executable, "-S", "-B", "-c", code], cwd=WORK, env=env,
                                            text=True, capture_output=True, encoding="utf-8", timeout=10)
                    self.assertEqual(result.returncode, status, result.stdout + result.stderr)
                    self.assertEqual(result.stdout, "")


if __name__ == '__main__':
    unittest.main()
