"""Configuration-order regressions using only temporary synthetic source trees."""
from __future__ import annotations
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class BootstrapTests(unittest.TestCase):
    def run_case(self, entry, override=False):
        with tempfile.TemporaryDirectory(prefix='r04-') as temporary:
            base = Path(temporary)
            source = base / 'source'
            (source / 'core').mkdir(parents=True)
            for name in ['app_common.py', 'env_bootstrap.py', 'core/__init__.py',
                         'core/config.py', 'core/runtime.py', 'core/send_day.py', 'core/run_progress.py']:
                if (ROOT / name).exists():
                    shutil.copyfile(ROOT / name, source / name)
            target = base / 'configured'
            env_file = base / 'synthetic.env'
            env_file.write_text(f'DATA_DIR={target}\nINSTANCE_LOCK_PATH={target}/custom.pid\nR04_VALUE=file\n', encoding='utf-8')
            env = {k:v for k,v in os.environ.items() if k not in {
                'DATA_DIR', 'INSTANCE_LOCK_PATH', 'STATE_FILE_PATH', 'R04_VALUE'}}
            env.update(ENV_FILE_PATH=str(env_file), PYTHONPATH=str(source), PYTHONDONTWRITEBYTECODE='1')
            if override:
                target = base / 'shell'
                env['DATA_DIR'] = str(target)
                env['R04_VALUE'] = 'shell'
            code = f'''import {entry}
import app_common
from core import config, runtime
import json,os
print(json.dumps({{"data":str(config.DATA_DIR),"config":str(config.CONFIG_PATH),"state":str(config.STATE_PATH),"log":str(runtime.LOG_DIR),"pid":str(app_common.PID_PATH),"value":os.environ["R04_VALUE"]}}))
'''
            completed = subprocess.run([sys.executable, '-B', '-c', code], cwd=source,
                                       env=env, capture_output=True, text=True, encoding='utf-8', timeout=15)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            values = json.loads(completed.stdout.splitlines()[-1])
            self.assertEqual(Path(values['data']), target)
            for name in ('config', 'state', 'log'):
                self.assertTrue(Path(values[name]).is_relative_to(target), values)
            self.assertEqual(Path(values['pid']), base / 'configured' / 'custom.pid')
            self.assertEqual(values['value'], 'shell' if override else 'file')
            self.assertFalse((source / 'data').exists())

    def test_app_entry_loads_dotenv_before_all_paths(self):
        self.run_case('app_common')

    def test_direct_core_entry_also_loads_dotenv(self):
        self.run_case('core.config')

    def test_process_environment_still_wins(self):
        self.run_case('core.runtime', override=True)


if __name__ == '__main__':
    unittest.main()
