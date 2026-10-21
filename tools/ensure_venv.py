"""Validate or recover the local install environment, without loading the app.

Run this with an available external Python 3.10+ interpreter. Existing invalid
environments are renamed in the project, never removed or overwritten.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import stat
import subprocess
import sys


MINIMUM_PYTHON = (3, 10)
VERSION_PROBE = (
    "import sys; print('.'.join(map(str, sys.version_info[:3]))); "
    "raise SystemExit(0 if sys.version_info >= (3, 10) else 3)"
)


class EnvironmentSetupError(RuntimeError):
    """An installation must stop; previously existing files are preserved."""


def venv_python(environment: Path) -> Path:
    return environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _process_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for key in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"):
        environment.pop(key, None)
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONNOUSERSITE"] = "1"
    return environment


def _run(argv: list[str], project: Path, runner, timeout: int = 30):
    return runner(
        argv, cwd=project, env=_process_environment(),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, check=False,
    )


def _output(result) -> str:
    return "\n".join(part.strip() for part in (result.stdout or "", result.stderr or "") if part.strip())[-2000:]


def _invalid_reason(environment: Path, project: Path, runner) -> str | None:
    executable = venv_python(environment)
    if not executable.is_file():
        return "未找到虚拟环境解释器。"
    try:
        version = _run([str(executable), "-c", VERSION_PROBE], project, runner)
        if version.returncode:
            return f"解释器无法执行或版本低于 Python 3.10（退出码 {version.returncode}）：{_output(version)}"
        dependencies = _run([str(executable), "-m", "pip", "check"], project, runner, timeout=120)
        if dependencies.returncode:
            return f"pip check 未通过（退出码 {dependencies.returncode}）：{_output(dependencies)}"
    except subprocess.TimeoutExpired as error:
        return f"环境检查超时（{error.timeout} 秒）。"
    except OSError as error:
        return f"无法运行环境检查：{error}"
    return None


def _is_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        # Junctions need the reparse bit on Python 3.10/3.11, which lack is_junction.
        return bool(getattr(path.lstat(), "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    except FileNotFoundError:
        return False


def ensure_venv(project: Path, *, runner=None, emit=print) -> Path:
    """Return a checked interpreter, preserving an invalid environment in place."""
    project = Path(project).resolve(strict=True)
    if not project.is_dir():
        raise EnvironmentSetupError("项目路径不是目录。")
    runner = runner or subprocess.run
    environment = project / "venv"
    if _is_link(environment):
        raise EnvironmentSetupError("venv 是目录链接；请使用项目内的独立虚拟环境，未进行重命名或重建。")
    if environment.exists():
        reason = _invalid_reason(environment, project, runner)
        if reason is None:
            emit("[环境] 已有 venv 的解释器、Python 3.10+ 版本和 pip check 均通过。")
            return venv_python(environment)
        emit(f"[环境] 已有 venv 无效：{reason}")
        if Path(sys.executable).resolve().is_relative_to(environment.resolve()):
            raise EnvironmentSetupError("安装引导正在使用待重建的 venv；请退出当前虚拟环境后使用系统 Python 重新运行，原目录未改动。")
        sequence = 1
        backup = project / f"venv.invalid-{sequence}"
        while backup.exists() or backup.is_symlink():
            sequence += 1
            backup = project / f"venv.invalid-{sequence}"
        try:
            environment.rename(backup)
        except OSError as error:
            raise EnvironmentSetupError(f"无法保留失效 venv，未开始重建：{error}") from error
        emit(f"[环境] 已将旧环境保留为 {backup.name}；不会删除它。")
    emit("[环境] 正在创建项目内的独立 venv。")
    try:
        result = _run([sys.executable, "-m", "venv", str(environment)], project, runner, timeout=300)
    except subprocess.TimeoutExpired as error:
        raise EnvironmentSetupError(f"创建 venv 超时（{error.timeout} 秒），已有保留目录未删除。") from error
    except OSError as error:
        raise EnvironmentSetupError(f"无法创建 venv，已有保留目录未删除：{error}") from error
    if result.returncode:
        raise EnvironmentSetupError(f"创建 venv 失败（退出码 {result.returncode}），已有保留目录未删除：{_output(result)}")
    reason = _invalid_reason(environment, project, runner)
    if reason is not None:
        raise EnvironmentSetupError(f"新建 venv 未通过检查，安装已停止：{reason}")
    emit("[环境] 新建 venv 检查通过。")
    return venv_python(environment)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="检查并保留重建失效的项目虚拟环境。")
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    if sys.version_info < MINIMUM_PYTHON:
        print("[错误] 请使用 Python 3.10 或更新版本运行安装器。", file=sys.stderr)
        return 1
    try:
        ensure_venv(args.project_root)
    except (EnvironmentSetupError, OSError) as error:
        print(f"[错误] {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
