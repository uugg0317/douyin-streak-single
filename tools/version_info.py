"""Read the release version without importing the app or private configuration."""
import ast
from pathlib import Path


def read_version(root=None):
    root = Path(root) if root else Path(__file__).resolve().parents[1]
    values = [node.value.value for node in ast.parse((root / 'app_common.py').read_text(encoding='utf-8')).body
              if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'VERSION' for t in node.targets)
              and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)]
    if len(values) != 1:
        raise ValueError('Expected one literal VERSION in app_common.py')
    return values[0]


if __name__ == '__main__':
    print('[版本] 抖音续火花 ' + read_version() + '（有密码门）')
