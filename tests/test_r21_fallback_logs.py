"""Fallback diagnostics must not expose exception payloads or interrupt cleanup."""
import ast
import logging
from pathlib import Path
from unittest import TestCase, mock


SOURCE = Path(__file__).resolve().parents[1] / 'core' / 'automation.py'


class FallbackLogTests(TestCase):
    def test_no_silent_exception_only_pass_remains(self):
        tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
        silent = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)
                  and isinstance(n.type, ast.Name) and n.type.id == 'Exception'
                  and len(n.body) == 1 and isinstance(n.body[0], ast.Pass)]
        self.assertEqual(silent, [])

    def function(self, name, **namespace):
        tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
        exec(compile(ast.Module(body=[fn],type_ignores=[]), str(SOURCE), 'exec'), namespace)
        return namespace[name]

    def test_screenshot_failure_preserves_flow_and_does_not_log_payload(self):
        logger = logging.getLogger('r21-synthetic')
        page = mock.Mock()
        page.screenshot.side_effect = RuntimeError('SYNTHETIC_PRIVATE_PAYLOAD')
        function = self.function('_screenshot', logger=logger, SCREENSHOT_PATH=Path('synthetic.png'))
        with self.assertLogs(logger, level='DEBUG') as captured:
            self.assertIsNone(function(page))
        self.assertNotIn('SYNTHETIC_PRIVATE_PAYLOAD', '\n'.join(captured.output))
        self.assertIn('截图', '\n'.join(captured.output))

    def test_initializer_failure_remains_nonfatal_and_has_context(self):
        logger = logging.getLogger('r21-synthetic')
        event_loop = mock.Mock()
        event_loop.new_event_loop.side_effect = RuntimeError('SYNTHETIC_PRIVATE_PAYLOAD')
        function = self.function('_pw_thread_initializer', logger=logger, asyncio=event_loop)
        with self.assertLogs(logger, level='DEBUG') as captured:
            self.assertIsNone(function())
        self.assertNotIn('SYNTHETIC_PRIVATE_PAYLOAD', '\n'.join(captured.output))
        self.assertIn('事件循环', '\n'.join(captured.output))
