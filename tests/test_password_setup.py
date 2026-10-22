"""首次密码初始化的文件边界、失败行为和实际认证兼容性。"""

from __future__ import annotations

import importlib.util
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import password_setup
from auth_config import password_hash_kind


class PasswordSetupTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.destination = Path(self.directory.name) / ".env"
        self.messages = []

    def initialize(self, passwords):
        reader = mock.Mock(side_effect=passwords)
        result = password_setup.initialize_env(self.destination, password_reader=reader, emit=self.messages.append)
        return result, reader

    def values(self):
        return dict(line.split("=", 1) for line in self.destination.read_text(encoding="utf-8").splitlines() if line and not line.startswith("#"))

    def test_creates_safe_local_config_and_real_auth_verifier_accepts_password(self):
        password = "这是仅测试使用的密码123!"
        created, reader = self.initialize([password, password])
        self.assertTrue(created)
        self.assertEqual(reader.call_count, 2)
        values = self.values()
        self.assertEqual(values["LOGIN_GATE"], "on")
        self.assertEqual(values["PASSWORD_PUBLIC_LOGIN"], "on")
        self.assertEqual(values["HOST"], "127.0.0.1")
        self.assertEqual(values["PORT"], "8000")
        self.assertEqual(values["SPARKKEEPER_BACKGROUND_TASKS"], "0")
        self.assertGreaterEqual(len(values["AUTH_TOKEN"]), 48)
        self.assertGreaterEqual(len(values["URL_ACCESS_TOKEN"]), 48)
        self.assertNotEqual(values["AUTH_TOKEN"], values["URL_ACCESS_TOKEN"])
        self.assertEqual(values["URL_PASSWORD_SALT"], "")
        self.assertEqual(password_hash_kind("", values["URL_PASSWORD_HASH"]), "scrypt")
        self.assertNotIn(password, self.destination.read_text(encoding="utf-8"))
        self.assertNotIn(password, "\n".join(self.messages))
        # 执行生产 auth_service 的真实验证函数，避免只在测试里重写同一公式。
        common = types.ModuleType("app_common")
        common._env = lambda key: values.get(key, "")
        common.logger = mock.Mock()
        spec = importlib.util.spec_from_file_location("isolated_setup_auth", Path(password_setup.__file__).parent / "auth_service.py")
        auth = importlib.util.module_from_spec(spec)
        with mock.patch.dict("sys.modules", {"app_common": common}):
            spec.loader.exec_module(auth)
        self.assertTrue(auth._verify_url_password(password))
        self.assertFalse(auth._verify_url_password(password + "wrong"))
        auth._check_auth_config()

    def test_existing_config_is_never_changed_or_prompted(self):
        self.initialize(["test-long-password", "test-long-password"])
        original = self.destination.read_bytes()
        created, reader = self.initialize([])
        self.assertFalse(created)
        reader.assert_not_called()
        self.assertEqual(self.destination.read_bytes(), original)

    def test_mismatch_does_not_create_config(self):
        with self.assertRaisesRegex(password_setup.PasswordSetupError, "不一致"):
            self.initialize(["sample-long-password", "different-password"])
        self.assertFalse(self.destination.exists())
        self.assertEqual(list(Path(self.directory.name).iterdir()), [])

    def test_short_password_does_not_request_confirmation_or_create_config(self):
        reader = mock.Mock(return_value="short")
        with self.assertRaisesRegex(password_setup.PasswordSetupError, "至少"):
            password_setup.initialize_env(self.destination, password_reader=reader, emit=self.messages.append)
        self.assertEqual(reader.call_count, 1)
        self.assertFalse(self.destination.exists())

    def test_surrounding_whitespace_is_rejected(self):
        for password in (" long-password-value", "long-password-value "):
            with self.subTest(password=password), self.assertRaisesRegex(password_setup.PasswordSetupError, "首尾"):
                self.initialize([password])
            self.assertFalse(self.destination.exists())

    def test_competing_writer_is_preserved(self):
        real_publish = password_setup._publish_without_replace
        def competing_writer(source, target):
            self.destination.write_text("existing-racer\n", encoding="utf-8")
            return real_publish(source, target)
        with mock.patch.object(password_setup, "_publish_without_replace", side_effect=competing_writer):
            with self.assertRaisesRegex(password_setup.PasswordSetupError, "另一进程"):
                self.initialize(["test-long-password", "test-long-password"])
        self.assertEqual(self.destination.read_text(encoding="utf-8"), "existing-racer\n")
        self.assertEqual([item.name for item in Path(self.directory.name).iterdir()], [".env"])

    def test_legacy_token_only_config_gets_password_without_losing_other_settings(self):
        original = "# preserve this comment\r\nLOGIN_GATE=off\r\nAUTH_TOKEN=existing-valid-management-token-123\r\nSMTP_SERVER=private-example.invalid\r\n"
        self.destination.write_bytes(original.encode("utf-8"))
        changed, reader = self.initialize(["new-local-password-123", "new-local-password-123"])
        self.assertTrue(changed)
        self.assertEqual(reader.call_count, 2)
        values = self.values()
        self.assertEqual(values["AUTH_TOKEN"], "existing-valid-management-token-123")
        self.assertEqual(values["LOGIN_GATE"], "off")
        self.assertEqual(values["SMTP_SERVER"], "private-example.invalid")
        self.assertIn(b"# preserve this comment\r\n", self.destination.read_bytes())
        self.assertEqual(password_hash_kind(values["URL_PASSWORD_SALT"], values["URL_PASSWORD_HASH"]), "scrypt")

    def test_empty_and_template_configs_are_completed(self):
        for original in ("", "AUTH_TOKEN=change_me_to_random\nURL_PASSWORD_SALT=\nURL_PASSWORD_HASH=\n"):
            with self.subTest(original=original):
                self.destination.write_text(original, encoding="utf-8")
                changed, reader = self.initialize(["new-local-password-123", "new-local-password-123"])
                self.assertTrue(changed)
                self.assertEqual(reader.call_count, 2)
                self.assertFalse(self.values()["AUTH_TOKEN"].startswith("change_me"))
                self.assertEqual(password_hash_kind(self.values()["URL_PASSWORD_SALT"], self.values()["URL_PASSWORD_HASH"]), "scrypt")

    def test_invalid_password_input_preserves_legacy_configuration(self):
        original = b"AUTH_TOKEN=existing-management-token\nSMTP_SERVER=preserved\n"
        self.destination.write_bytes(original)
        with self.assertRaises(password_setup.PasswordSetupError):
            self.initialize(["new-local-password-123", "different-password-123"])
        self.assertEqual(self.destination.read_bytes(), original)

    def test_existing_valid_password_is_never_replaced_when_other_defaults_are_missing(self):
        import hashlib
        password = "preserved-password-123"
        salt = "preserved-salt"
        hashed = hashlib.sha256((password + salt).encode()).hexdigest()
        self.destination.write_text(f"URL_PASSWORD_SALT={salt}\nURL_PASSWORD_HASH={hashed}\n", encoding="utf-8")
        _, reader = self.initialize([])
        reader.assert_not_called()
        self.assertEqual(self.values()["URL_PASSWORD_HASH"], hashed)
        self.assertEqual(self.values()["URL_PASSWORD_SALT"], salt)

    def test_modified_existing_config_is_not_overwritten_during_prompt(self):
        self.destination.write_text("AUTH_TOKEN=legacy-token\n", encoding="utf-8")
        def read_password(prompt):
            self.destination.write_text("AUTH_TOKEN=another-writer-token\n", encoding="utf-8")
            return "new-local-password-123"
        with self.assertRaisesRegex(password_setup.PasswordSetupError, "已改变"):
            password_setup.initialize_env(self.destination, password_reader=read_password, emit=self.messages.append)
        self.assertEqual(self.destination.read_text(encoding="utf-8"), "AUTH_TOKEN=another-writer-token\n")

    def test_local_launch_clears_only_inherited_auth_before_actual_env_loader(self):
        import ast
        import hashlib
        import os
        new_password = "new-local-password-123"
        self.initialize([new_password, new_password])
        old_password, old_salt = "old-inherited-password-123", "old-salt"
        inherited = {
            "AUTH_TOKEN": "synthetic-old-inherited-management-token",
            "URL_ACCESS_TOKEN": "synthetic-old-inherited-url-token",
            "URL_PASSWORD_SALT": old_salt,
            "URL_PASSWORD_HASH": hashlib.sha256((old_password + old_salt).encode()).hexdigest(),
            "SMTP_SERVER": "inherited-example.invalid",
        }
        source = Path(password_setup.__file__).parent / "app_common.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        actual_loader = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_load_env")
        from env_bootstrap import load_environment
        namespace = {"os": os, "ENV_PATH": self.destination,
                     "load_environment": load_environment}
        exec(compile(ast.Module(body=[actual_loader], type_ignores=[]), str(source), "exec"), namespace)
        with mock.patch.dict(os.environ, inherited, clear=True):
            # 本机 BAT 的最小契约；一般服务器仍保留 setdefault 优先规则。
            for key in ("AUTH_TOKEN", "URL_ACCESS_TOKEN", "URL_PASSWORD_SALT", "URL_PASSWORD_HASH"):
                os.environ.pop(key, None)
            namespace["_load_env"]()
            values = dict(os.environ)
        self.assertEqual(values["URL_PASSWORD_HASH"], self.values()["URL_PASSWORD_HASH"])
        self.assertEqual(values["SMTP_SERVER"], "inherited-example.invalid")
        common = types.ModuleType("app_common")
        common._env = lambda key: values.get(key, "")
        common.logger = mock.Mock()
        spec = importlib.util.spec_from_file_location("isolated_local_launch_auth", Path(password_setup.__file__).parent / "auth_service.py")
        auth = importlib.util.module_from_spec(spec)
        with mock.patch.dict("sys.modules", {"app_common": common}):
            spec.loader.exec_module(auth)
        self.assertTrue(auth._verify_url_password(new_password))
        self.assertFalse(auth._verify_url_password(old_password))

    def test_link_failure_leaves_no_partial_configuration(self):
        with mock.patch.object(password_setup, "_publish_without_replace", side_effect=OSError("not supported")):
            with self.assertRaisesRegex(password_setup.PasswordSetupError, "安全创建"):
                self.initialize(["test-long-password", "test-long-password"])
        self.assertFalse(self.destination.exists())
        self.assertEqual(list(Path(self.directory.name).iterdir()), [])

    def test_repeated_initialization_generates_independent_secrets(self):
        self.initialize(["test-long-password", "test-long-password"])
        first = self.values()
        other = Path(self.directory.name) / "other.env"
        password_setup.initialize_env(other, password_reader=mock.Mock(side_effect=["test-long-password", "test-long-password"]), emit=self.messages.append)
        second = dict(line.split("=", 1) for line in other.read_text(encoding="utf-8").splitlines() if line and not line.startswith("#"))
        for key in ("AUTH_TOKEN", "URL_ACCESS_TOKEN", "URL_PASSWORD_HASH"):
            self.assertNotEqual(first[key], second[key])
        self.assertNotEqual(first["URL_PASSWORD_HASH"].split("$")[4], second["URL_PASSWORD_HASH"].split("$")[4])

    def test_getpass_plaintext_fallback_is_rejected(self):
        import getpass
        import warnings
        def fallback(prompt):
            warnings.warn("can not hide input", getpass.GetPassWarning)
            return "test-long-password"
        with mock.patch.object(password_setup.getpass, "getpass", side_effect=fallback):
            with self.assertRaisesRegex(password_setup.PasswordSetupError, "隐藏"):
                password_setup._read_password("prompt")


if __name__ == "__main__":
    unittest.main()
