"""Build a source-only release without importing or opening runtime configuration.

The source selection is a whitelist. Private paths are excluded before descent or
open; the stricter staging/archive validators reject any injected extra content.
Only this module's explicit output paths under root/dist are written.
"""
from __future__ import annotations

import argparse
import ast
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import zipfile
if __package__:
    from .release_privacy import user_path_lines, public_commands, public_value, evidence_summary
else:
    from release_privacy import user_path_lines, public_commands, public_value, evidence_summary


class ReleaseError(ValueError):
    """Unsafe input or release content; messages contain paths, never file data."""


SOURCE_DIRECTORIES = frozenset({"core", "routers", "static", "desktop", "docs", "extra", "tests", "tools"})
ROOT_FILES = frozenset({
    ".env.example", ".gitattributes", ".gitignore", "README.md", "NOTICE.md", "LICENSE", "LICENSE.md",
    "requirements.txt", "requirements.lock", "安装依赖.bat", "启动单账号本机版.bat",
    "启动本地Cookie助手.bat", "使用说明.txt", "deploy.sh", "start.sh", "douyin-streak.service",
})
# Generated files are deliberately omitted from the source manifest (R02).
GENERATED_FILES = frozenset({"SOURCE_VERSION.json", "source-manifest.json", "test-report.json", "test-report.txt", "test-report-no-browser.json", "evidence/SUMMARY.md", "SHA256SUMS.txt"})
FORBIDDEN_DIRECTORIES = frozenset({".git", "venv", "__pycache__", ".review-work", "dist", "node_modules"})
STATIC_SUFFIXES = frozenset({".html", ".css", ".js", ".svg", ".png", ".ico", ".webp", ".jpg", ".jpeg", ".gif", ".woff", ".woff2"})
BINARY_SUFFIXES = frozenset({".png", ".ico", ".webp", ".jpg", ".jpeg", ".gif", ".woff", ".woff2"})
NORMALIZATION = "text-lf-bat-crlf-v1"


def is_link_or_reparse(info: os.stat_result) -> bool:
    """Reject Unix links and all Windows reparse points, including junctions."""
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _root_path(root: Path | str) -> Path:
    path = Path(root).absolute()
    if is_link_or_reparse(path.lstat()) or not path.is_dir():
        raise ReleaseError("unsafe directory: " + str(path))
    return path.resolve()


def _safe_path(root: Path, relative: Path) -> Path:
    candidate = root
    for part in relative.parts:
        candidate = candidate / part
        if is_link_or_reparse(candidate.lstat()):
            raise ReleaseError("link/reparse point is forbidden: " + relative.as_posix())
    if not candidate.resolve().is_relative_to(root):
        raise ReleaseError("path escapes root: " + relative.as_posix())
    return candidate


def _parse_entry(name: str) -> PurePosixPath:
    if not name or "\\" in name or ":" in name or "\x00" in name or name.startswith("/"):
        raise ReleaseError("unsafe entry path: " + name)
    parts = name.rstrip("/").split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ReleaseError("unsafe entry path: " + name)
    return PurePosixPath(*parts)


def _forbidden(path: PurePosixPath, is_dir: bool = False) -> bool:
    parts = tuple(part.casefold() for part in path.parts)
    if any(part in FORBIDDEN_DIRECTORIES or part.startswith("venv.invalid-") for part in parts):
        return True
    if any(part == ".env" or (part.startswith(".env.") and part != ".env.example") for part in parts):
        return True
    if "data" in parts:
        if parts == ("data",) and is_dir:
            return False
        if parts != ("data", ".gitkeep") or is_dir:
            return True
    name = parts[-1]
    return name == "state.json" or name.endswith((".pid", ".log", ".pem", ".key", ".zip", ".tar", ".tar.gz", ".tgz"))


def _allowed_file(path: PurePosixPath, *, generated: bool = False) -> bool:
    name = path.name
    if generated and path.as_posix() in GENERATED_FILES:
        return True
    if len(path.parts) == 1:
        return name in ROOT_FILES or (generated and name in GENERATED_FILES) or (path.suffix == ".py" and not name.startswith("_test_"))
    if path.as_posix() == "data/.gitkeep":
        return generated
    if any(part.startswith(".") for part in path.parts):
        return False
    top = path.parts[0]
    suffix = path.suffix.lower()
    if top in {"core", "routers", "tools"}:
        return suffix == ".py" or path.as_posix() in {"core/sparkflow_login/LICENSE", "core/sparkflow_login/manifest.json"}
    if top == "static":
        return suffix in STATIC_SUFFIXES
    if top == "tests":
        return suffix in {".py", ".cjs", ".js", ".html", ".json", ".md"}
    if top == "desktop":
        return suffix in {".py", ".md", ".ico", ".png"}
    if top == "docs":
        return suffix in {".md", ".txt"}
    if top == "extra":
        return suffix == ".example"
    return False


def collect_source_files(root: Path | str) -> tuple[Path, ...]:
    """Return sorted relative paths; never descend into private/runtime roots."""
    root = _root_path(root)
    selected: list[Path] = []

    def visit(relative: Path) -> None:
        folder = root if relative == Path(".") else _safe_path(root, relative)
        with os.scandir(folder) as entries:
            for entry in entries:
                rel = relative / entry.name
                public = PurePosixPath(rel.as_posix())
                # Name-only checks happen before stat, descent, or any content read.
                if _forbidden(public, is_dir=True):
                    continue
                if relative == Path(".") and entry.name not in SOURCE_DIRECTORIES and not _allowed_file(public):
                    continue
                if relative != Path(".") and entry.name.startswith("."):
                    continue
                info = entry.stat(follow_symlinks=False)
                if is_link_or_reparse(info):
                    raise ReleaseError("link/reparse point is forbidden: " + rel.as_posix())
                if stat.S_ISDIR(info.st_mode):
                    if public.parts[0] in SOURCE_DIRECTORIES:
                        visit(rel)
                elif stat.S_ISREG(info.st_mode) and not _forbidden(public) and _allowed_file(public):
                    selected.append(rel)
                elif not stat.S_ISREG(info.st_mode):
                    raise ReleaseError("special file is forbidden: " + rel.as_posix())

    visit(Path("."))
    return tuple(sorted(selected, key=lambda path: path.as_posix()))


def source_manifest(root: Path | str, files: tuple[Path, ...] | None = None) -> dict[str, str]:
    """Hash release bytes: BAT uses CRLF, other text LF; preserve binary bytes."""
    root = _root_path(root)
    allowed = collect_source_files(root)
    if files is not None and tuple(files) != allowed:
        raise ReleaseError("source selection no longer matches whitelist")
    result = {}
    for relative in allowed:
        candidate = _safe_path(root, relative)
        result[relative.as_posix()] = hashlib.sha256(
            _canonical_bytes(relative, candidate.read_bytes())
        ).hexdigest()
    return result


def _canonical_bytes(relative: Path | PurePosixPath, content: bytes) -> bytes:
    if relative.suffix.lower() in BINARY_SUFFIXES:
        return content
    text = content.replace(b"\r\n", b"\n")
    return text.replace(b"\n", b"\r\n") if relative.suffix.lower() == ".bat" else text


def _exact_manifest(root: Path) -> dict[str, str]:
    """Test/build snapshots must already contain the exact canonical bytes."""
    return {path.as_posix(): hashlib.sha256(_safe_path(root, path).read_bytes()).hexdigest()
            for path in collect_source_files(root)}


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _scan_public_text(name: str, raw: bytes) -> None:
    if PurePosixPath(name).suffix.lower() in BINARY_SUFFIXES or name == 'data/.gitkeep':
        return
    try:
        lines=user_path_lines(name,raw)
    except UnicodeError as error:
        raise ReleaseError('non-UTF8 release text: '+name) from error
    if lines:
        raise ReleaseError('local username path in release text: '+', '.join(name+':'+str(line) for line in lines))


def _verify_command_evidence(root: Path, commands: list[dict]) -> None:
    for record in commands:
        relative=Path(str(record.get('evidence_log','')))
        if relative.parts[:2] != ('dist','evidence') or relative.suffix != '.log':
            raise ReleaseError('raw test evidence must be a .log under dist/evidence')
        path=_dist_path(root,relative)
        try:
            raw=path.read_bytes()
        except OSError as error:
            raise ReleaseError('raw test evidence unavailable: '+relative.as_posix()) from error
        if record.get('sha256') != hashlib.sha256(raw).hexdigest() or type(record.get('bytes')) is not int or record['bytes'] != len(raw):
            raise ReleaseError('raw test evidence hash/size mismatch: '+relative.as_posix())


def _tree_hash(files: dict[str, str]) -> str:
    # Fixed path ordering and JSON encoding make the identity portable.
    return hashlib.sha256(_json_bytes({"normalization": NORMALIZATION, "files": files})).hexdigest()


def _git(root: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env["GIT_OPTIONAL_LOCKS"] = "0"
    result = subprocess.run(["git", "-C", str(root), *args], input=input_bytes,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    if result.returncode:
        # Git/filter errors can contain local paths or file content. Do not echo them.
        raise ReleaseError("git source inspection failed: " + args[0])
    return result.stdout


def _selected_git_path(name: str) -> bool:
    path = _parse_entry(name)
    return not _forbidden(path) and _allowed_file(path)


def _head_sources(root: Path) -> tuple[str, dict[str, bytes]]:
    top = Path(os.fsdecode(_git(root, "rev-parse", "--show-toplevel").strip())).resolve()
    if top != root:
        raise ReleaseError("release root must be the Git repository root")
    head = _git(root, "rev-parse", "--verify", "HEAD").decode("ascii").strip()
    entries: dict[str, str] = {}
    for entry in _git(root, "ls-tree", "-rz", "--full-tree", head).split(b"\0"):
        if not entry:
            continue
        metadata, raw_name = entry.split(b"\t", 1)
        name = raw_name.decode("utf-8")
        if not _selected_git_path(name):
            continue
        mode, kind, object_id = metadata.decode("ascii").split()
        if mode not in {"100644", "100755"} or kind != "blob":
            raise ReleaseError("non-regular committed source: " + name)
        entries[name] = object_id
    indexed = {}
    for entry in _git(root, "ls-files", "--stage", "-z").split(b"\0"):
        if not entry:
            continue
        metadata, raw_name = entry.split(b"\t", 1)
        name = raw_name.decode("utf-8")
        if not _selected_git_path(name):
            continue
        mode, object_id, stage = metadata.decode("ascii").split()
        if mode not in {"100644", "100755"} or stage != "0":
            raise ReleaseError("unresolved or non-regular indexed source: " + name)
        indexed[name] = object_id
    if indexed != entries:
        raise ReleaseError("whitelisted sources have staged changes")
    # Read only selected source blobs. No private blob is requested from Git.
    names = sorted(entries)
    response = _git(root, "cat-file", "--batch", input_bytes=("\n".join(entries[name] for name in names) + "\n").encode("ascii"))
    position = 0
    contents = {}
    for name in names:
        end = response.index(b"\n", position)
        object_id, kind, length = response[position:end].split()
        if object_id.decode("ascii") != entries[name] or kind != b"blob":
            raise ReleaseError("committed source blob is unavailable: " + name)
        position = end + 1
        size = int(length)
        contents[name] = _canonical_bytes(PurePosixPath(name), response[position:position + size])
        position += size + 1
    return head, contents


def _version(content: bytes) -> str:
    try:
        tree = ast.parse(content, filename="app_common.py")
        values = []
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "VERSION" for target in node.targets):
                values.append(ast.literal_eval(node.value))
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == "VERSION":
                values.append(ast.literal_eval(node.value))
    except (ValueError, SyntaxError, TypeError) as exc:
        raise ReleaseError("VERSION must be one literal in app_common.py") from exc
    if len(values) != 1 or not isinstance(values[0], str) or not re.fullmatch(r"[0-9]+(?:\.[0-9A-Za-z]+)+(?:[-+][0-9A-Za-z.-]+)?", values[0]):
        raise ReleaseError("VERSION must be one safe version string in app_common.py")
    return values[0]


def capture_snapshot(root: Path | str) -> dict:
    """Require clean selected sources and return a stable, JSON-safe identity."""
    root = _root_path(root)
    head, content = _head_sources(root)
    files = {name: hashlib.sha256(data).hexdigest() for name, data in content.items()}
    if source_manifest(root) != files:
        raise ReleaseError("whitelisted sources differ from HEAD (changed, missing or untracked source)")
    if "app_common.py" not in content:
        raise ReleaseError("committed app_common.py is required")
    return {"version": _version(content["app_common.py"]), "git_head": head, "dirty": False,
            "normalization": NORMALIZATION, "files": files, "source_tree_sha256": _tree_hash(files)}


def _require_snapshot(actual: dict, expected: dict) -> None:
    if actual != expected:
        raise ReleaseError("source snapshot changed; repeat final tests for the current committed sources")


def _dist_path(root: Path, relative: Path, *, create_parent: bool = False) -> Path:
    if relative.is_absolute() or not relative.parts or relative.parts[0] != "dist" or any(part in {".", ".."} for part in relative.parts):
        raise ReleaseError("output/evidence path must stay under root/dist")
    candidate = root
    for part in relative.parts:
        candidate = candidate / part
        if candidate.exists() or candidate.is_symlink():
            if is_link_or_reparse(candidate.lstat()):
                raise ReleaseError("link/reparse point in output/evidence path")
    if create_parent:
        candidate.parent.mkdir(parents=True, exist_ok=True)
    return candidate


def _write_sources(staging: Path, contents: dict[str, bytes]) -> None:
    for name, data in contents.items():
        target = staging / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(data)


def prepare_test_snapshot(root: Path | str, snapshot: dict | None = None) -> Path:
    """Create exact release bytes in dist for final offline tests, with no secrets."""
    root = _root_path(root)
    current = capture_snapshot(root)
    if snapshot is not None:
        _require_snapshot(current, snapshot)
    target = _dist_path(root, Path("dist") / ("verify-" + current["git_head"][:8]), create_parent=True)
    if target.exists():
        raise ReleaseError("verification snapshot already exists; preserve previous evidence")
    _, contents = _head_sources(root)
    target.mkdir()
    _write_sources(target, contents)
    if _exact_manifest(target) != current["files"]:
        raise ReleaseError("verification source bytes do not match manifest")
    _require_snapshot(capture_snapshot(root), current)
    return target


def _validate_report(report: dict, snapshot: dict, *, require_browser: bool = False) -> None:
    if not isinstance(report, dict) or report.get("schema_version") != 1:
        raise ReleaseError("unsupported final test report")
    for key in ("git_head", "source_tree_sha256", "version", "normalization"):
        if report.get(key) != snapshot[key]:
            raise ReleaseError("test report does not match source identity: " + key)
    for key in ("source_before", "source_after", "tested_source"):
        if report.get(key) != snapshot["source_tree_sha256"]:
            raise ReleaseError("test report has missing or changed snapshot evidence: " + key)
    environment = report.get("environment")
    if not isinstance(environment, dict) or not {"python", "node", "platform"}.issubset(environment) or not all(isinstance(key, str) and isinstance(value, str) and value.strip() for key, value in environment.items()):
        raise ReleaseError("test report must record nonempty interpreter environment")
    commands = report.get("commands")
    if not isinstance(commands, list) or not commands:
        raise ReleaseError("test report must contain final test commands")
    python_suite = False
    covered_js = set()
    skipped_browser = 0
    for record in commands:
        if not isinstance(record, dict):
            raise ReleaseError("invalid command evidence")
        command = record.get("command")
        if not isinstance(command, list) or not command or not all(isinstance(arg, str) and arg for arg in command):
            raise ReleaseError("test command must be a nonempty argument list")
        counts = [record.get(field) for field in ("passed", "failed", "errors", "skipped")]
        if any(type(value) is not int or value < 0 for value in counts):
            raise ReleaseError("test report must record valid pass/fail/error/skip counts")
        if record.get("exit_code") != 0 or record["failed"] or record["errors"]:
            raise ReleaseError("final test report contains failed or errored commands")
        reasons = record.get("skip_reasons")
        if not isinstance(reasons, list) or len(reasons) != record["skipped"] or not all(isinstance(reason, str) and reason.strip() for reason in reasons):
            raise ReleaseError("every skipped test must have a reason")
        actual_browser_skips = sum('no_chromium_headless_shell' in reason for reason in reasons)
        if record.get('skipped_browser',actual_browser_skips) != actual_browser_skips:
            raise ReleaseError('browser skip count does not match reasons')
        skipped_browser += actual_browser_skips
        if record["passed"] + record["skipped"] == 0:
            raise ReleaseError("final test command ran no tests")
        executable = Path(command[0]).stem.lower()
        if (executable.startswith("python") or executable=='<python>') and command[1:] == ["-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py", "-v"]:
            python_suite = True
        if executable in {'node','<node>'} and len(command) == 2:
            script = command[1].replace("\\", "/")
            script=script.removeprefix('<项目根>/')
            if script.startswith("./"):
                script = script[2:]
            covered_js.add(script)
    if not python_suite:
        raise ReleaseError("final Python unittest discovery evidence is missing")
    expected_js = {name for name in snapshot["files"] if name.startswith("tests/") and name.endswith(".cjs") and name.count("/") == 1}
    if not expected_js.issubset(covered_js):
        raise ReleaseError("final report does not cover every tests/*.cjs script")
    if report.get('skipped_browser',skipped_browser) != skipped_browser:
        raise ReleaseError('browser skip total does not match command evidence')
    if require_browser and skipped_browser:
        raise ReleaseError('browser tests not executed: cannot publish a fully verified release')
    not_executed = report.get("not_executed")
    if not isinstance(not_executed, list) or any(not isinstance(record, dict) or not all(isinstance(record.get(key), str) and record[key].strip() for key in ("scope", "reason")) for record in not_executed):
        raise ReleaseError("unexecuted scopes must include a reason")


def write_test_report(root: Path | str, *, before: dict, after: dict, tested_root: Path | str,
                      commands: list[dict], environment: dict[str, str],
                      not_executed: list[dict] | None = None) -> Path:
    """Record actual caller-supplied results after verifying the tested source bytes.

    This validates evidence consistency; it does not execute commands or certify
    caller-supplied pass counts. The caller must retain its captured run output.
    """
    root = _root_path(root)
    _require_snapshot(after, before)
    _require_snapshot(capture_snapshot(root), before)
    tested_root = Path(tested_root).absolute()
    try:
        relative = tested_root.relative_to(root)
    except ValueError as exc:
        raise ReleaseError("tested snapshot must stay under root/dist") from exc
    _dist_path(root, relative)
    if _exact_manifest(tested_root) != before["files"]:
        raise ReleaseError("tested source bytes changed from the committed snapshot")
    _verify_command_evidence(root,commands)
    commands=public_commands(commands,root)
    for command in commands:
        command['skipped_browser']=sum('no_chromium_headless_shell' in reason for reason in command['skip_reasons'])
    report = {"schema_version": 1, "version": before["version"], "git_head": before["git_head"],
              "source_tree_sha256": before["source_tree_sha256"], "normalization": NORMALIZATION,
              "source_before": before["source_tree_sha256"], "source_after": after["source_tree_sha256"],
              "tested_source": before["source_tree_sha256"], "test_time": datetime.now(timezone.utc).isoformat(),
              "commands": commands, "environment": public_value(environment,root), "not_executed": public_value(not_executed or [],root)}
    report['skipped_browser'] = sum(sum('no_chromium_headless_shell' in reason for reason in command['skip_reasons']) for command in commands)
    report['verification'] = 'browser_tests_not_executed' if report['skipped_browser'] else 'all_required_suites_executed'
    _validate_report(report, before)
    _scan_public_text('test-report.json',_json_bytes(report))
    suffix='-no-browser' if report['skipped_browser'] else ''
    target = _dist_path(root, Path("dist") / ("test-report-" + before["git_head"][:8] + suffix + ".json"), create_parent=True)
    with target.open("xb") as stream:
        stream.write(_json_bytes(report))
    return target


def build_final_release(root: Path | str, *, test_report: Path | str, supplemental_report: Path | str | None = None) -> Path:
    """Build only committed sources whose final offline test evidence matches."""
    root = _root_path(root)
    snapshot = capture_snapshot(root)
    report_path = Path(test_report).absolute()
    try:
        relative = report_path.relative_to(root)
    except ValueError as exc:
        raise ReleaseError("test report must stay under root/dist") from exc
    _dist_path(root, relative)
    if report_path.suffix != ".json":
        raise ReleaseError("test report must be a JSON evidence file")
    try:
        report = json.loads(report_path.read_bytes())
    except (ValueError, UnicodeError) as exc:
        raise ReleaseError("invalid final test report JSON") from exc
    _validate_report(report, snapshot, require_browser=True)
    _verify_command_evidence(root,report['commands'])
    reports=[report]
    if supplemental_report is not None:
        supplement=Path(supplemental_report).absolute()
        try:relative=supplement.relative_to(root)
        except ValueError as error:raise ReleaseError('supplemental report must stay under root/dist') from error
        _dist_path(root,relative)
        extra=json.loads(supplement.read_bytes())
        _validate_report(extra,snapshot)
        _verify_command_evidence(root,extra['commands'])
        reports.append(extra)
    name = "douyin-streak-" + snapshot["version"] + "-" + snapshot["git_head"][:8]
    output = _dist_path(root, Path("dist") / name, create_parent=True)
    if output.exists():
        raise ReleaseError("release output already exists: " + name)
    output.mkdir()
    staging = output / "staging"
    staging.mkdir()
    _, contents = _head_sources(root)
    _write_sources(staging, contents)
    if _exact_manifest(staging) != snapshot["files"]:
        raise ReleaseError("build bytes do not match tested source manifest")
    metadata = {key: value for key, value in snapshot.items() if key != "files"}
    metadata["build_time"] = datetime.now(timezone.utc).isoformat()
    (staging / "SOURCE_VERSION.json").write_bytes(_json_bytes(metadata))
    (staging / "source-manifest.json").write_bytes(_json_bytes({"normalization": NORMALIZATION, "source_tree_sha256": snapshot["source_tree_sha256"], "files": snapshot["files"]}))
    report_bytes = _json_bytes(report)
    (staging / "test-report.json").write_bytes(report_bytes)
    (output / "test-report.json").write_bytes(report_bytes)
    summary=evidence_summary(reports)
    (staging/'evidence').mkdir()
    (staging/'evidence/SUMMARY.md').write_bytes(summary)
    (output/'evidence').mkdir()
    (output/'evidence/SUMMARY.md').write_bytes(summary)
    generated=['SOURCE_VERSION.json','source-manifest.json','test-report.json','evidence/SUMMARY.md']
    if len(reports)>1:
        (staging/'test-report-no-browser.json').write_bytes(_json_bytes(reports[1]))
        (output/'test-report-no-browser.json').write_bytes(_json_bytes(reports[1]))
        generated.append('test-report-no-browser.json')
    validate_staging(staging)
    archive = output / (name + ".zip")
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED) as package:
        for relative in sorted([*snapshot["files"], *generated]):
            package.write(_safe_path(staging, Path(relative)), relative)
    validate_archive(archive)
    with zipfile.ZipFile(archive) as package:
        for relative, digest in snapshot["files"].items():
            if hashlib.sha256(package.read(relative)).hexdigest() != digest:
                raise ReleaseError("archived source does not match manifest: " + relative)
    _require_snapshot(capture_snapshot(root), snapshot)
    # Publish the checksum marker last. Failed builds never have this marker.
    checksum_files=[archive,output/'test-report.json']
    if len(reports)>1:checksum_files.append(output/'test-report-no-browser.json')
    checksum_lines = [hashlib.sha256(path.read_bytes()).hexdigest() + "  " + path.name for path in checksum_files]
    (output / "SHA256SUMS.txt").write_bytes(("\n".join(checksum_lines) + "\n").encode("utf-8"))
    return output


def _validate_entry(name: str, *, is_dir: bool = False) -> PurePosixPath:
    relative = _parse_entry(name)
    if _forbidden(relative, is_dir=is_dir):
        raise ReleaseError("forbidden release entry: " + name)
    if is_dir:
        if relative.parts[0] not in SOURCE_DIRECTORIES | {"data","evidence"} or any(part.startswith(".") for part in relative.parts):
            raise ReleaseError("unlisted release directory: " + name)
    elif not _allowed_file(relative, generated=True):
        raise ReleaseError("unlisted release file: " + name)
    return relative


def validate_staging(staging: Path | str) -> None:
    """Check names and filesystem metadata, without opening file contents."""
    staging = _root_path(staging)

    def visit(relative: Path) -> None:
        folder = staging if relative == Path(".") else _safe_path(staging, relative)
        with os.scandir(folder) as entries:
            for entry in entries:
                rel = relative / entry.name
                info = entry.stat(follow_symlinks=False)
                if is_link_or_reparse(info):
                    raise ReleaseError("link/reparse point is forbidden: " + rel.as_posix())
                is_dir = stat.S_ISDIR(info.st_mode)
                _validate_entry(rel.as_posix(), is_dir=is_dir)
                if is_dir:
                    visit(rel)
                elif not stat.S_ISREG(info.st_mode):
                    raise ReleaseError("special file is forbidden: " + rel.as_posix())
                elif rel.suffix.lower() not in BINARY_SUFFIXES and rel.as_posix() != 'data/.gitkeep':
                    _scan_public_text(rel.as_posix(),_safe_path(staging,rel).read_bytes())

    visit(Path("."))


def validate_archive(archive: Path | str) -> None:
    """Inspect ZIP entries, not the ZIP filename or archived private contents."""
    archive = Path(archive)
    if is_link_or_reparse(archive.lstat()):
        raise ReleaseError("archive link/reparse point is forbidden: " + archive.name)
    seen: set[str] = set()
    with zipfile.ZipFile(archive) as package:
        for entry in package.infolist():
            # ZipInfo normalizes backslashes on Windows and truncates NULs;
            # validate its original central-directory name before that normalization.
            _validate_entry(entry.orig_filename, is_dir=entry.is_dir())
            normalized = entry.filename.rstrip("/").casefold()
            if normalized in seen:
                raise ReleaseError("duplicate release entry: " + entry.filename)
            seen.add(normalized)
            mode = entry.external_attr >> 16
            if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR}):
                raise ReleaseError("special archive entry: " + entry.filename)
            if not entry.is_dir() and PurePosixPath(entry.filename).suffix.lower() not in BINARY_SUFFIXES and entry.filename != 'data/.gitkeep':
                _scan_public_text(entry.filename,package.read(entry))


def validate(path: Path | str) -> None:
    """Public convenience interface used by build/report checks."""
    target = Path(path)
    if target.is_dir():
        validate_staging(target)
    else:
        validate_archive(target)


def build_release(root: Path | str, *, name: str = "source-preview") -> Path:
    """Create a new intermediate source ZIP in dist; never reuse old staging."""
    root = _root_path(root)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}", name):
        raise ReleaseError("unsafe release name: " + name)
    files = collect_source_files(root)
    dist = root / "dist"
    if dist.exists() or dist.is_symlink():
        _safe_path(root, Path("dist"))
    dist.mkdir(exist_ok=True)
    staging = dist / (name + "-staging")
    archive = dist / (name + ".zip")
    if staging.exists() or staging.is_symlink() or archive.exists() or archive.is_symlink():
        raise ReleaseError("release output already exists: " + name)
    staging.mkdir()
    for relative in files:
        source = _safe_path(root, relative)
        destination = staging / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Source selection and links have already been checked before the open.
        with destination.open("xb") as outgoing:
            outgoing.write(_canonical_bytes(relative, source.read_bytes()))
    validate_staging(staging)
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED) as package:
        for relative in files:
            source = _safe_path(staging, relative)
            package.write(source, relative.as_posix())
    validate_archive(archive)
    return archive


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--name", default="source-preview", help="Intermediate source package name, under root/dist")
    parser.add_argument("--validate", type=Path, help="Validate existing staging directory or ZIP")
    parser.add_argument("--snapshot", action="store_true", help="Print the clean committed source identity as JSON")
    parser.add_argument("--prepare-tests", action="store_true", help="Create normalized committed sources in dist for final tests")
    parser.add_argument("--final", action="store_true", help="Build a final release bound to a matching test report")
    parser.add_argument("--test-report", type=Path, help="Final JSON test evidence under root/dist")
    parser.add_argument('--supplemental-report',type=Path,help='Optional matching no-browser evidence report')
    args = parser.parse_args()
    try:
        if sum((args.validate is not None, args.snapshot, args.prepare_tests, args.final)) > 1:
            raise ReleaseError("choose only one validation/snapshot/test preparation/final operation")
        if args.final:
            if args.test_report is None:
                raise ReleaseError("--final requires --test-report")
            print("Final source release: " + str(build_final_release(args.root, test_report=args.test_report, supplemental_report=args.supplemental_report)))
        elif args.snapshot:
            print(_json_bytes(capture_snapshot(args.root)).decode("utf-8"), end="")
        elif args.prepare_tests:
            print("Final test snapshot: " + str(prepare_test_snapshot(args.root)))
        elif args.validate is not None:
            validate(args.validate)
            print("Release entries validated: " + str(args.validate))
        else:
            print("Intermediate source package: " + str(build_release(args.root, name=args.name)))
    except (ReleaseError, OSError, zipfile.BadZipFile) as exc:
        parser.exit(1, str(exc) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

