"""Legacy password migration: synthetic config only, never starts an app."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import _isolation  # noqa: F401  先隔离数据目录，再导入项目模块
import app_common
import auth_config
import auth_service as auth


class PasswordMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='r17-')
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'synthetic.env'
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(app_common, 'ENV_PATH', self.path).start()
        mock.patch.object(auth, 'logger', mock.Mock()).start()
        mock.patch.dict(os.environ, {}, clear=False).start()

    def seed(self, password):
        salt = 'synthetic-migration-salt'
        encoded = hashlib.sha256((password + salt).encode()).hexdigest()
        self.path.write_text('# keep comment\nAUTH_TOKEN=synthetic-do-not-change\n'
                             f'URL_PASSWORD_SALT={salt}\nURL_PASSWORD_HASH={encoded}\nOTHER=keep\n', encoding='utf-8')
        mock.patch.object(auth, 'URL_PASSWORD_SALT', salt).start()
        mock.patch.object(auth, 'URL_PASSWORD_HASH', encoded).start()
        return encoded

    def login(self, password):
        fn = getattr(auth, '_verify_and_migrate_url_password', None)
        self.assertIsNotNone(fn, 'successful legacy login needs a compatible atomic migration path')
        return fn(password)

    def test_short_whitespace_unicode_migrate_without_changing_password(self):
        for password in ['short', ' has spaces ', '中文口令-兼容迁移']:
            with self.subTest(kind=len(password)):
                self.seed(password)
                self.assertTrue(self.login(password))
                self.assertTrue(auth.URL_PASSWORD_HASH.startswith('scrypt$'))
                self.assertTrue(auth._verify_url_password(password))
                self.assertFalse(auth._verify_url_password('wrong'))
                saved = self.path.read_text(encoding='utf-8')
                self.assertIn('# keep comment', saved)
                self.assertIn('AUTH_TOKEN=synthetic-do-not-change', saved)
                self.assertIn('OTHER=keep', saved)
                self.assertIn(auth.URL_PASSWORD_HASH, saved)

    def test_unsupported_long_legacy_password_still_logs_in_without_migration(self):
        password = 'x' * 5000
        old = self.seed(password)
        before = self.path.read_bytes()
        self.assertTrue(self.login(password))
        self.assertEqual(auth.URL_PASSWORD_HASH, old)
        self.assertEqual(self.path.read_bytes(), before)

    def test_incorrect_password_never_writes(self):
        self.seed('fixture-password')
        before = self.path.read_bytes()
        self.assertFalse(self.login('wrong'))
        self.assertEqual(self.path.read_bytes(), before)

    def test_generation_and_write_failure_preserve_verified_login(self):
        for target in ['auth_config.rehash_legacy_password', 'app_common._save_env_values_locked']:
            with self.subTest(target=target):
                old = self.seed('fixture-password')
                before = self.path.read_bytes()
                with mock.patch(target, create=True, side_effect=OSError('synthetic failure')):
                    self.assertTrue(self.login('fixture-password'))
                self.assertEqual(auth.URL_PASSWORD_HASH, old)
                self.assertEqual(self.path.read_bytes(), before)

    def test_concurrent_and_repeated_login_migrate_once(self):
        self.seed('fixture-password')
        fn = getattr(auth_config, 'rehash_legacy_password', None)
        self.assertIsNotNone(fn)
        with mock.patch.object(auth_config, 'rehash_legacy_password', wraps=fn) as derive:
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(self.login, ['fixture-password'] * 4))
            self.assertTrue(all(results))
            self.assertTrue(self.login('fixture-password'))
            self.assertEqual(derive.call_count, 1)
        values = dict(line.split('=', 1) for line in self.path.read_text(encoding='utf-8').splitlines() if '=' in line)
        self.assertTrue(auth_config.verify_password('fixture-password', values['URL_PASSWORD_SALT'], values['URL_PASSWORD_HASH']))

    def test_changed_disk_credentials_are_not_overwritten(self):
        self.seed('fixture-password')
        self.path.write_text('URL_PASSWORD_SALT=another\nURL_PASSWORD_HASH=changed\n', encoding='utf-8')
        before = self.path.read_bytes()
        self.assertTrue(self.login('fixture-password'))
        self.assertEqual(self.path.read_bytes(), before)

    def test_new_password_policy_stays_strict(self):
        for password in ['short', ' space-padding-is-not-new-policy ']:
            with self.assertRaises(ValueError):
                auth_config.hash_password(password)

    def test_password_login_route_uses_migration_before_issuing_session(self):
        from fastapi import Request, Response
        from routers import auth as routes
        self.seed('short')
        request = Request({'type':'http', 'scheme':'http', 'method':'POST',
                           'path':'/api/auth/url-token-login', 'query_string':b'',
                           'server':('127.0.0.1',8000), 'client':('127.0.0.1',1234),
                           'headers':[(b'host',b'127.0.0.1:8000')]})
        with mock.patch.object(routes, 'PASSWORD_PUBLIC_LOGIN', True):
            response = Response()
            result = routes.api_url_token_login(response, request, {'password':'short'})
        self.assertTrue(result['ok'])
        self.assertTrue(auth.URL_PASSWORD_HASH.startswith('scrypt$'))
        self.assertIn('httponly', response.headers['set-cookie'].lower())


if __name__ == '__main__':
    unittest.main()
