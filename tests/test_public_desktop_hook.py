"""桌面路径钩子只准备环境；密码初始化由启动器负责。"""
import os
from pathlib import Path
import runpy
import sys
import tempfile
import unittest
from unittest.mock import patch

HOOK=Path(__file__).resolve().parents[1]/"desktop/runtime_hook_data_dir.py"
class LocalDesktopHookTests(unittest.TestCase):
    def test_path_hook_defers_configuration_to_launcher(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(os.environ, {}, clear=True), patch.object(sys,"frozen",True,create=True), patch.object(sys,"executable",str(Path(temporary)/"app.exe")):
                runpy.run_path(str(HOOK))
                self.assertNotIn("LOGIN_GATE",os.environ)
                self.assertEqual(os.environ["HOST"],"127.0.0.1")
                self.assertEqual(Path(os.environ["ENV_FILE_PATH"]),Path(temporary)/".env")
    def test_existing_config_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            env_path=Path(temporary)/".env"
            env_path.write_text("LOGIN_GATE=on\n",encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True), patch.object(sys,"frozen",True,create=True), patch.object(sys,"executable",str(Path(temporary)/"app.exe")):
                runpy.run_path(str(HOOK))
                self.assertNotIn("LOGIN_GATE",os.environ)
                self.assertEqual(env_path.read_text(encoding="utf-8"),"LOGIN_GATE=on\n")
