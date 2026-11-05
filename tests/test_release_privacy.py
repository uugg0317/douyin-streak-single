"""Synthetic secret samples are constructed at runtime; no real secrets are used."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

import _isolation  # noqa: F401  先隔离数据目录，再导入项目模块
from tools import make_release as release
from tools import release_privacy as privacy


class ReleasePrivacyTests(unittest.TestCase):
    def setUp(self):
        self.opaque = 'not-for-release-' + '0123456789abcdef' * 2

    def assignment(self, key='AUTH_TOKEN', value=None):
        return key + ' = ' + repr(self.opaque if value is None else value)

    def assert_rejected(self, name, text, *, kind='credential_literal'):
        findings = privacy.secret_findings(name, text.encode())
        self.assertTrue(any(item['kind'] == kind for item in findings))
        self.assertTrue(all(set(item) == {'line', 'kind'} for item in findings))
        with self.assertRaises(release.ReleaseError) as caught:
            release._scan_public_text(name, text.encode())
        self.assertIn(name + ':', str(caught.exception))
        self.assertIn(kind, str(caught.exception))
        self.assertTrue(self.opaque not in str(caught.exception))

    def test_explicit_secret_assignments_and_environment_defaults_are_rejected(self):
        for key in ('AUTH_TOKEN', 'URL_ACCESS_TOKEN', 'URL_PASSWORD_HASH', 'URL_PASSWORD_SALT',
                    'SMTP_PASS', 'SMTP_PASSWORD', 'API_KEY', 'SECRET_KEY', 'CLIENT_SECRET'):
            with self.subTest(key=key):
                self.assert_rejected('core/settings.py', self.assignment(key))
                self.assert_rejected('tests/test_settings.py', self.assignment(key))
        self.assert_rejected('core/settings.py', 'os.environ[' + repr('AUTH_TOKEN') + '] = ' + repr(self.opaque))
        self.assert_rejected('core/settings.py', 'os.getenv(' + repr('AUTH_TOKEN') + ', ' + repr(self.opaque) + ')')
        self.assert_rejected('.env.example', 'SMTP_PASS' + '=' + self.opaque)

    def test_empty_placeholder_and_declared_synthetic_values_pass(self):
        for value in ('', '<access-token>', '${AUTH_TOKEN}', '...', '[redacted]', '********',
                      'synthetic-token-for-local-test', 'fixture-cookie', 'unit-test-token'):
            self.assertEqual(privacy.secret_findings('tests/test_fixture.py', self.assignment(value=value).encode()), [])
        self.assertEqual(privacy.secret_findings('start.bat', ('set "' + 'AUTH_TOKEN' + '="\nset "HOST=127.0.0.1"').encode()), [])
        self.assertEqual(privacy.secret_findings('.env.example', ('AUTH_TOKEN' + '=\n# empty default').encode()), [])

    def test_chinese_prefix_or_ellipsis_prefix_is_not_a_synthetic_exemption(self):
        for value in ('假期使用的访问口令', '仅本地使用的口令', '测试账户也可能是真密码', '...actual-tail'):
            self.assert_rejected('tests/test_sample.py', self.assignment(value=value))

    def test_fixed_vector_declaration_is_exact_value_same_line_and_tests_only(self):
        digest = hashlib.sha256(self.opaque.encode()).hexdigest()
        declared = self.assignment() + '  # SYNTHETIC_SECRET_SHA256=' + digest
        self.assertEqual(privacy.secret_findings('tests/test_vector.py', declared.encode()), [])
        self.assert_rejected('core/settings.py', declared)
        self.assert_rejected('tests/test_vector.py', declared.replace(self.opaque, self.opaque + '-changed'))
        self.assert_rejected('tests/test_vector.py', '# SYNTHETIC_SECRET_SHA256=' + digest + '\n' + self.assignment())
        second = declared + '\n' + self.assignment('SMTP_PASS')
        self.assertEqual(privacy.secret_findings('tests/test_vector.py', second.encode()), [{'line': 2, 'kind': 'credential_literal'}])

    def test_cookie_headers_and_multiline_exports_are_rejected(self):
        for key in ('sessionid', 'sessionid_ss', 'sid_guard', 'ttwid'):
            with self.subTest(cookie=key):
                self.assert_rejected('docs/request.txt', key + '=' + self.opaque + '; path=/', kind='cookie_value')
                exported = json.dumps({'cookies': [{'name': key, 'value': self.opaque,
                                                     'domain': '.example.invalid', 'path': '/'}]}, indent=2)
                self.assert_rejected('tests/export.json', exported, kind='cookie_value')
        self.assert_rejected('tests/export.py', repr({'name': 'sessionid', 'value': self.opaque}), kind='cookie_value')
        synthetic = json.dumps({'cookies': [{'name': 'sessionid', 'value': 'synthetic-cookie'}]})
        self.assertEqual(privacy.secret_findings('tests/export.json', synthetic.encode()), [])

    def test_authorization_and_private_key_fail_without_echoing_values(self):
        self.assert_rejected('docs/request.txt', 'Authorization: Bearer ' + self.opaque, kind='authorization_value')
        header = '-----BEGIN ' + 'PRIVATE KEY-----'
        self.assert_rejected('tests/key.py', header + '\n' + self.opaque, kind='private_key')

    def test_staging_and_archive_scan_generated_reports_and_test_sources(self):
        with tempfile.TemporaryDirectory(prefix='release-secret-fixture-') as temporary:
            root = Path(temporary)
            for index, name in enumerate(('test-report.json', 'evidence/SUMMARY.md', 'tests/test_leak.py')):
                stage = root / ('stage-' + str(index))
                path = stage / name
                path.parent.mkdir(parents=True)
                payload = (json.dumps({'AUTH_TOKEN': self.opaque}) if name.endswith('.json')
                           else self.assignment())
                path.write_text(payload, encoding='utf-8')
                with self.assertRaises(release.ReleaseError):
                    release.validate_staging(stage)
                archive = root / ('fixture-' + str(index) + '.zip')
                with zipfile.ZipFile(archive, 'w') as package:
                    package.writestr(name, payload)
                with self.assertRaises(release.ReleaseError):
                    release.validate_archive(archive)

    def test_source_writer_rejects_before_any_file_is_written(self):
        with tempfile.TemporaryDirectory(prefix='release-secret-writer-') as temporary:
            root = Path(temporary)
            sources = {'app.py': b'print("safe")', 'tests/test_leak.py': self.assignment().encode()}
            with self.assertRaises(release.ReleaseError):
                release._write_sources(root, sources)
            self.assertEqual(list(root.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
