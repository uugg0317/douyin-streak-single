"""按文件名排序最先加载：在其它测试模块导入项目代码之前隔离数据目录与配置文件。"""
from __future__ import annotations

import os
from pathlib import Path
import unittest

import _isolation  # noqa: F401  必须先于任何项目模块导入

ROOT = Path(__file__).resolve().parents[1]


class SuiteIsolationTests(unittest.TestCase):
    def test_suite_never_points_at_real_data_or_env(self):
        data_dir = Path(os.environ["DATA_DIR"]).resolve()
        env_file = Path(os.environ["ENV_FILE_PATH"]).resolve()
        self.assertNotEqual(data_dir, (ROOT / "data").resolve())
        self.assertNotEqual(env_file, (ROOT / ".env").resolve())
        self.assertFalse(env_file.exists())


if __name__ == "__main__":
    unittest.main()
