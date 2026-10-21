"""Real subprocess lock tests; all PIDs and files belong to synthetic fixtures."""
from __future__ import annotations
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
# Use the actual interpreter, not Windows' venv redirector, so crash tests wait
# for the process that owns the OS file handle rather than its launcher parent.
PYTHON = getattr(sys, '_base_executable', sys.executable)
CODE = '''import sys
from instance_lock import _acquire_instance_lock
if '--barrier' in sys.argv:
    from pathlib import Path
    import os,time
    print('READY', flush=True)
    while not Path(os.environ['START_BARRIER']).exists():
        time.sleep(.01)
try:
    _acquire_instance_lock()
except SystemExit:
    print('REFUSED', flush=True)
    raise SystemExit(0)
print('ACQUIRED', flush=True)
if '--hold' in sys.argv:
    sys.stdin.readline()
'''


class InstanceLockTests(unittest.TestCase):
    def env(self, root):
        env = dict(os.environ)
        env.update(DATA_DIR=str(root / 'data'), ENV_FILE_PATH=str(root / 'missing.env'),
                   INSTANCE_LOCK_PATH=str(root / 'server.pid'), PYTHONDONTWRITEBYTECODE='1')
        return env

    def launch(self, root, barrier=None):
        args = [PYTHON, '-B', '-c', CODE, '--hold']
        env = self.env(root)
        if barrier is not None:
            args.append('--barrier')
            env['START_BARRIER'] = str(barrier)
        p = subprocess.Popen(args, cwd=ROOT,
                             env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, encoding='utf-8')
        self.addCleanup(self.stop, p)
        return p

    @staticmethod
    def stop(p):
        if p.poll() is None:
            if os.name == 'nt':
                subprocess.run(['taskkill', '/PID', str(p.pid), '/T', '/F'], capture_output=True, timeout=15)
            else:
                p.kill()
            p.wait(timeout=10)
        for stream in [p.stdin, p.stdout, p.stderr]:
            if stream:
                stream.close()

    def probe(self, root):
        result = subprocess.run([PYTHON, '-B', '-c', CODE], cwd=ROOT,
                                env=self.env(root), capture_output=True, text=True, encoding='utf-8', timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_stale_unrelated_pid_is_not_a_lock(self):
        with tempfile.TemporaryDirectory(prefix='r05-') as tmp:
            root = Path(tmp)
            (root / 'server.pid').write_text(str(os.getpid()))
            self.assertIn('ACQUIRED', self.probe(root))

    def test_simultaneous_first_acquisition_has_one_winner(self):
        with tempfile.TemporaryDirectory(prefix='r05-') as tmp:
            root = Path(tmp)
            barrier = root / 'start'
            holders = [self.launch(root, barrier) for _ in range(2)]
            try:
                for p in holders:
                    self.assertIn('READY', p.stdout.readline())
                barrier.touch()
                outcomes = [p.stdout.readline().strip() for p in holders]
                self.assertEqual(sorted(outcomes), ['ACQUIRED', 'REFUSED'])
            finally:
                for p in holders:
                    self.stop(p)

    def test_same_directory_exclusive_other_directory_independent_and_crash_recovery(self):
        with tempfile.TemporaryDirectory(prefix='r05-') as tmp:
            a = Path(tmp) / 'a'; b = Path(tmp) / 'b'
            a.mkdir(); b.mkdir()
            holder = self.launch(a)
            try:
                self.assertIn('ACQUIRED', holder.stdout.readline())
                self.assertIn('REFUSED', self.probe(a))
                self.assertIn('ACQUIRED', self.probe(b))
            finally:
                self.stop(holder)
            self.assertIn('ACQUIRED', self.probe(a))

    def test_pid_file_replacement_cannot_bypass_live_os_lock(self):
        with tempfile.TemporaryDirectory(prefix='r05-') as tmp:
            root = Path(tmp)
            holder = self.launch(root)
            try:
                self.assertIn('ACQUIRED', holder.stdout.readline())
                # Diagnostic metadata is not the exclusion primitive.
                (root / 'server.pid').write_text('invalid')
                self.assertIn('REFUSED', self.probe(root))
            finally:
                self.stop(holder)


if __name__ == '__main__':
    unittest.main()
