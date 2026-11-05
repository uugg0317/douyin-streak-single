"""测试统一隔离：不读取真实 .env，不写真实 data/、日志或登录态。

必须在导入任何项目模块之前导入。test_00_isolation.py 按文件名排序最先加载，
保证 `python -m unittest discover -s tests` 整套运行时生效；直接在进程内导入
项目代码的测试模块也在文件开头导入本模块，单独运行时同样隔离。
子进程测试仍各自设置自己的临时目录，不受影响。
"""
from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from pathlib import Path

if not os.environ.get("SPARKKEEPER_TEST_SANDBOX"):
    SANDBOX = Path(tempfile.mkdtemp(prefix="sparkkeeper-tests-"))
    os.environ["SPARKKEEPER_TEST_SANDBOX"] = str(SANDBOX)
    os.environ["DATA_DIR"] = str(SANDBOX / "data")
    os.environ["ENV_FILE_PATH"] = str(SANDBOX / "missing.env")
    os.environ["INSTANCE_LOCK_PATH"] = str(SANDBOX / "server.pid")
    os.environ["STATE_FILE_PATH"] = ""
    # 测试默认不启动后台任务；需要后台的用例自行设置或替换判断函数。
    os.environ["SPARKKEEPER_BACKGROUND_TASKS"] = "0"
    os.environ["SESSION_WARN_EMAIL_ENABLED"] = "off"
    # Windows 上日志文件可能仍被占用，清理失败不影响测试结果。
    atexit.register(shutil.rmtree, SANDBOX, True)
else:
    SANDBOX = Path(os.environ["SPARKKEEPER_TEST_SANDBOX"])
