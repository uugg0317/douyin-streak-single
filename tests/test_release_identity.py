"""R02: synthetic committed sources, evidence binding and byte-identical builds."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock
import zipfile

from tools import make_release as release


PROJECT = Path(__file__).resolve().parents[1]
TEST_TMP = PROJECT / "dist" / "test-tmp"


class ReleaseIdentityTests(unittest.TestCase):
    def setUp(self):
        TEST_TMP.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="identity-", dir=TEST_TMP)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.put("app_common.py", b"VERSION = '2.8'\nraise RuntimeError('must never import')\n")
        self.put("app.py", b"print('synthetic')\n")
        self.put("README.md", b"Synthetic source\n")
        self.put("安装依赖.bat", b"@echo off\r\necho synthetic\r\n")
        self.put(".gitignore", b".env\ndata/\ndist/\n")
        self.put(".gitattributes", b"* text=auto\n*.png binary\n")
        self.put("tests/test_example.py", b"# synthetic test\n")
        self.put("tests/test_example.cjs", b"// synthetic test\n")
        self.put("static/pixel.png", b"\x89PNG\r\n\x1a\n\x00\xff\r\n")
        self.git("init", "-q")
        self.git("config", "user.name", "Synthetic Release Test")
        self.git("config", "user.email", "synthetic@example.invalid")
        self.git("config", "core.autocrlf", "false")
        self.git("add", "--", "app.py", "app_common.py", "README.md", "安装依赖.bat", ".gitignore", ".gitattributes", "tests", "static")
        self.git("commit", "-qm", "synthetic baseline")

    def put(self, name, data):
        target = self.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return target

    def git(self, *args):
        result = subprocess.run(["git", "-C", str(self.root), *args], capture_output=True, check=True)
        return result.stdout.decode("utf-8").strip()

    def commands(self):
        records = [
            {"command": ["python", "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py", "-v"],
             "exit_code": 0, "passed": 1, "failed": 0, "errors": 0, "skipped": 0, "skip_reasons": []},
            {"command": ["node", "tests/test_example.cjs"], "exit_code": 0,
             "passed": 1, "failed": 0, "errors": 0, "skipped": 0, "skip_reasons": []},
        ]
        for index,record in enumerate(records):
            relative=f'dist/evidence/synthetic/command-{index}.log'
            raw=f'synthetic command {index} output\n'.encode()
            self.put(relative,raw)
            record.update(evidence_log=relative,sha256=hashlib.sha256(raw).hexdigest(),bytes=len(raw))
        return records

    def report(self):
        before = release.capture_snapshot(self.root)
        prepared = release.prepare_test_snapshot(self.root, before)
        after = release.capture_snapshot(self.root)
        return release.write_test_report(
            self.root, before=before, after=after, tested_root=prepared,
            commands=self.commands(), environment={"python": "synthetic Python", "node": "synthetic Node", "platform": "synthetic"},
            not_executed=[{"scope": "real platform", "reason": "synthetic offline tests only"}],
        )

    def test_ast_version_does_not_import_application(self):
        snapshot = release.capture_snapshot(self.root)
        self.assertEqual(snapshot["version"], "2.8")
        self.assertFalse(snapshot["dirty"])
        self.assertEqual(len(snapshot["git_head"]), 40)
        self.assertEqual(len(snapshot["source_tree_sha256"]), 64)

    def test_missing_browser_report_cannot_create_verified_release(self):
        before=release.capture_snapshot(self.root)
        prepared=release.prepare_test_snapshot(self.root,before)
        commands=self.commands()
        commands[0].update(skipped=1,skip_reasons=['synthetic browser: no_chromium_headless_shell'],skipped_browser=1)
        report=release.write_test_report(self.root,before=before,after=before,tested_root=prepared,
            commands=commands,environment={'python':'synthetic Python','node':'synthetic Node','platform':'synthetic'})
        data=json.loads(report.read_text(encoding='utf-8'))
        self.assertEqual(data['skipped_browser'],1)
        self.assertEqual(data['verification'],'browser_tests_not_executed')
        with self.assertRaisesRegex(release.ReleaseError,'browser tests not executed'):
            release.build_final_release(self.root,test_report=report)

    def test_public_report_redacts_commands_and_summary_binds_raw_logs(self):
        before=release.capture_snapshot(self.root);prepared=release.prepare_test_snapshot(self.root,before)
        commands=self.commands()
        commands[0]['command'][0]=str(self.root/'runtime/python.exe')
        commands[1]['command'][0]=str(self.root/'runtime/node.exe')
        report=release.write_test_report(self.root,before=before,after=before,tested_root=prepared,
            commands=commands,environment={'python':'synthetic Python','node':'synthetic Node','platform':'synthetic','python_executable':str(self.root/'runtime/python.exe')})
        data=json.loads(report.read_text(encoding='utf-8'))
        self.assertEqual(data['commands'][0]['command'][0],'<python>')
        self.assertEqual(data['commands'][1]['command'][0],'<node>')
        self.assertNotIn(str(self.root),report.read_text(encoding='utf-8'))
        output=release.build_final_release(self.root,test_report=report)
        with zipfile.ZipFile(output/(output.name+'.zip')) as archive:
            summary=archive.read('evidence/SUMMARY.md').decode('utf-8')
            self.assertFalse(any(name.endswith('.log') for name in archive.namelist()))
            for record in data['commands']:
                raw=(self.root/record['evidence_log']).read_bytes()
                self.assertEqual(record['sha256'],hashlib.sha256(raw).hexdigest())
                self.assertEqual(record['bytes'],len(raw))
                self.assertIn(record['sha256'],summary)

    def test_changed_raw_log_is_rejected_after_report(self):
        report=self.report();data=json.loads(report.read_text(encoding='utf-8'))
        (self.root/data['commands'][0]['evidence_log']).write_bytes(b'changed synthetic log')
        with self.assertRaisesRegex(release.ReleaseError,'hash/size mismatch'):
            release.build_final_release(self.root,test_report=report)

    def test_supplemental_missing_browser_report_stays_separate_from_verified_main(self):
        main=self.report();snapshot=release.capture_snapshot(self.root)
        commands=self.commands();commands[0].update(skipped=1,skip_reasons=['synthetic: no_chromium_headless_shell'],skipped_browser=1)
        extra=release.write_test_report(self.root,before=snapshot,after=snapshot,tested_root=self.root/'dist'/('verify-'+snapshot['git_head'][:8]),
            commands=commands,environment={'python':'synthetic Python','node':'synthetic Node','platform':'synthetic','browser_mode':'absent'})
        output=release.build_final_release(self.root,test_report=main,supplemental_report=extra)
        with zipfile.ZipFile(output/(output.name+'.zip')) as archive:
            supplemental=json.loads(archive.read('test-report-no-browser.json'))
            self.assertEqual(supplemental['skipped_browser'],1)
            self.assertIn('browser_tests_not_executed',archive.read('evidence/SUMMARY.md').decode())

    def test_dynamic_or_multiple_version_definitions_are_rejected(self):
        for value in (b"VERSION = str(2.8)\n", b"VERSION = '2.8'\nVERSION = '2.9'\n"):
            self.put("app_common.py", value)
            self.git("add", "app_common.py")
            self.git("commit", "-qm", "synthetic version")
            with self.assertRaises(release.ReleaseError):
                release.capture_snapshot(self.root)

    def test_dirty_staged_untracked_and_ignored_sources_are_rejected(self):
        for index, (name, stage) in enumerate((("app.py", False), ("app.py", True), ("core/new.py", False))):
            with self.subTest(name=name, staged=stage):
                self.put(name, f"# changed source {index}\n".encode())
                if stage:
                    self.git("add", "--", name)
                with self.assertRaises(release.ReleaseError):
                    release.capture_snapshot(self.root)
                self.git("add", "--", name)
                self.git("commit", "-qm", "synthetic changed source")
        self.put(".gitignore", b".env\ndata/\ndist/\ncore/ignored.py\n")
        self.git("add", ".gitignore")
        self.git("commit", "-qm", "synthetic ignored rule")
        self.put("core/ignored.py", b"# still source\n")
        with self.assertRaises(release.ReleaseError):
            release.capture_snapshot(self.root)

    def test_private_and_generated_files_do_not_affect_identity_or_get_opened(self):
        before = release.capture_snapshot(self.root)
        private = self.put(".env", b"SYNTHETIC_PRIVATE=do_not_read\n")
        self.put("data/state.json", b'{"synthetic": true}')
        self.put("SOURCE_VERSION.json", b"stale synthetic metadata")
        original = Path.open
        def guarded(path, *args, **kwargs):
            if path == private or path.is_relative_to(self.root / "data"):
                raise AssertionError("private contents opened")
            return original(path, *args, **kwargs)
        with mock.patch.object(Path, "open", guarded):
            self.assertEqual(before, release.capture_snapshot(self.root))

    def test_crlf_is_normalized_and_binary_bytes_are_preserved(self):
        before = release.capture_snapshot(self.root)
        self.put("app.py", b"print('synthetic')\r\n")
        after = release.capture_snapshot(self.root)
        self.assertEqual(before, after)
        stage = release.prepare_test_snapshot(self.root, before)
        self.assertEqual((stage / "app.py").read_bytes(), b"print('synthetic')\n")
        self.assertEqual((stage / "安装依赖.bat").read_bytes(), b"@echo off\r\necho synthetic\r\n")
        self.assertEqual((stage / "static/pixel.png").read_bytes(), b"\x89PNG\r\n\x1a\n\x00\xff\r\n")
        self.assertEqual(release.source_manifest(stage), before["files"])

    def test_test_snapshot_and_source_mutation_invalidate_report(self):
        before = release.capture_snapshot(self.root)
        stage = release.prepare_test_snapshot(self.root, before)
        (stage / "app.py").write_bytes(b"changed test bytes\n")
        with self.assertRaises(release.ReleaseError):
            release.write_test_report(self.root, before=before, after=before, tested_root=stage,
                                      commands=self.commands(), environment={"python": "synthetic"})
        after = dict(before, source_tree_sha256="0" * 64)
        with self.assertRaises(release.ReleaseError):
            release.write_test_report(self.root, before=before, after=after, tested_root=stage,
                                      commands=self.commands(), environment={"python": "synthetic"})

    def test_final_archive_report_metadata_and_checksums_match(self):
        report_path = self.report()
        snapshot = release.capture_snapshot(self.root)
        output = release.build_final_release(self.root, test_report=report_path)
        archive = output / (f"douyin-streak-2.8-{snapshot['git_head'][:8]}.zip")
        with zipfile.ZipFile(archive) as package:
            metadata = json.loads(package.read("SOURCE_VERSION.json"))
            manifest = json.loads(package.read("source-manifest.json"))
            self.assertEqual(metadata["git_head"], snapshot["git_head"])
            self.assertEqual(metadata["source_tree_sha256"], snapshot["source_tree_sha256"])
            self.assertEqual(manifest["files"], snapshot["files"])
            self.assertFalse(set(release.GENERATED_FILES) & set(manifest["files"]))
            for name, digest in manifest["files"].items():
                self.assertEqual(hashlib.sha256(package.read(name)).hexdigest(), digest)
            self.assertEqual(package.read("test-report.json"), (output / "test-report.json").read_bytes())
        checksums = (output / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(checksums), 2)
        for line in checksums:
            digest, name = line.split("  ", 1)
            self.assertNotEqual(name, "SHA256SUMS.txt")
            self.assertEqual(hashlib.sha256((output / name).read_bytes()).hexdigest(), digest)

    def test_stale_report_rejected_after_source_commit(self):
        report = self.report()
        self.put("app.py", b"print('second version')\n")
        self.git("add", "app.py")
        self.git("commit", "-qm", "synthetic next commit")
        with self.assertRaises(release.ReleaseError):
            release.build_final_release(self.root, test_report=report)

    def test_missing_failed_or_incomplete_test_commands_rejected(self):
        report_path = self.report()
        original = json.loads(report_path.read_text(encoding="utf-8"))
        variants = []
        for field, value in (("exit_code", 1), ("failed", 1), ("errors", 1), ("skipped", 1)):
            changed = json.loads(json.dumps(original))
            changed["commands"][0][field] = value
            variants.append(changed)
        variants.append(dict(original, commands=[]))
        variants.append(dict(original, commands=original["commands"][:1]))
        filtered = json.loads(json.dumps(original))
        filtered["commands"][0]["command"].extend(["-k", "only_one"])
        variants.append(filtered)
        variants.append(dict(original, environment={}))
        variants.append(dict(original, not_executed=[{"scope": "real platform", "reason": ""}]))
        for changed in variants:
            with self.subTest(changed=changed):
                report_path.write_text(json.dumps(changed), encoding="utf-8")
                with self.assertRaises(release.ReleaseError):
                    release.build_final_release(self.root, test_report=report_path)

    def test_source_changes_during_final_build_are_rejected(self):
        report = self.report()
        original = release.validate_archive
        def mutate(archive):
            original(archive)
            self.put("app.py", b"# concurrent source change\n")
        with mock.patch.object(release, "validate_archive", side_effect=mutate):
            with self.assertRaises(release.ReleaseError):
                release.build_final_release(self.root, test_report=report)

    def test_report_must_stay_in_dist_and_existing_outputs_are_preserved(self):
        report = self.report()
        with self.assertRaises(release.ReleaseError):
            release.build_final_release(self.root, test_report=self.root / ".env")
        output = release.build_final_release(self.root, test_report=report)
        before = (output / "SHA256SUMS.txt").read_bytes()
        with self.assertRaises(release.ReleaseError):
            release.build_final_release(self.root, test_report=report)
        self.assertEqual((output / "SHA256SUMS.txt").read_bytes(), before)

    def test_tracked_private_blob_is_never_requested(self):
        before = release.capture_snapshot(self.root)
        self.put(".env", b"SYNTHETIC_PRIVATE_IN_GIT=not_read\n")
        self.git("add", "-f", ".env")
        self.git("commit", "-qm", "synthetic historical private fixture")
        private_id = self.git("rev-parse", "HEAD:.env")
        original = release._git
        def guard(root, *args, input_bytes=None):
            if args[0] == "cat-file":
                self.assertNotIn(private_id.encode(), input_bytes)
            return original(root, *args, input_bytes=input_bytes)
        with mock.patch.object(release, "_git", side_effect=guard):
            current = release.capture_snapshot(self.root)
        self.assertEqual(current["files"], before["files"])
        self.assertEqual(current["source_tree_sha256"], before["source_tree_sha256"])


if __name__ == "__main__":
    unittest.main()
