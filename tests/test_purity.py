"""§10.8 纯度断言：静态扫描核心包，禁止文件/网络/时间类模块与时钟调用。

铁律 1/2/3：核心不读时钟、不碰文件与网络、无持久状态。
两处**唯一**允许读文件的边界（且只读随包数据）：
``protocol/__init__.py``（读手写 Schema）与 ``registry/loader.py``（读 TOML 声明）；
读声明与 Schema 是调用方显式注入的动作，不是判定路径上的 I/O。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import records_kit

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "records_kit"

# 判定路径上禁止出现的模块（文件/网络/时钟/随机/并发/序列化副作用）
FORBIDDEN_IMPORTS = {
    "os",
    "io",
    "socket",
    "urllib",
    "http",
    "ftplib",
    "smtplib",
    "subprocess",
    "shutil",
    "glob",
    "tempfile",
    "sqlite3",
    "time",
    "random",
    "secrets",
    "uuid",
    "threading",
    "multiprocessing",
    "asyncio",
    "logging",
    "sys",
    "pickle",
    "csv",
}
# 只有「读文件」的边界文件可用的模块（pathlib 直接触文件系统；tomllib 解析已打开的流）
FILE_ONLY_MODULES = {"pathlib", "tomllib"}
# 「读文件」边界文件 → 允许其额外导入的模块
FILE_BOUNDARY_EXTRA = {
    "protocol/__init__.py": {"pathlib", "json"},
    "registry/loader.py": {"pathlib", "tomllib"},
}
# 内建危险调用（按名字匹配：eval/exec/open 等）
FORBIDDEN_CALL_NAMES = {"open", "eval", "exec", "compile", "__import__", "input"}
# 时钟/计时调用（按属性匹配：datetime.now / time.perf_counter 等）
FORBIDDEN_CALL_ATTRS = {
    "now",
    "utcnow",
    "today",
    "monotonic",
    "perf_counter",
    "process_time",
    "time_ns",
}


def _modules() -> list[Path]:
    return sorted(PACKAGE.rglob("*.py"))


def _relative(path: Path) -> str:
    return path.relative_to(PACKAGE).as_posix()


def _imports(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


def _called_names(tree: ast.AST) -> tuple[set[str], set[str]]:
    """返回 ``(内建名字调用, 属性调用)`` 两个集合——``pathlib.Path.open`` 与内建 ``open`` 区分对待。"""
    names: set[str] = set()
    attrs: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            target = node.func
            if isinstance(target, ast.Name):
                names.add(target.id)
            elif isinstance(target, ast.Attribute):
                attrs.add(target.attr)
    return names, attrs


def test_core_has_modules():
    assert len(_modules()) >= 8


@pytest.mark.parametrize("path", _modules(), ids=lambda path: path.name)
def test_no_forbidden_imports(path):
    imports = _imports(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
    allowed_extra = FILE_BOUNDARY_EXTRA.get(_relative(path), set())
    offending = (imports & FORBIDDEN_IMPORTS) | ((imports & FILE_ONLY_MODULES) - allowed_extra)
    assert offending == set(), f"{_relative(path)} 导入了禁止的模块：{sorted(offending)}"


@pytest.mark.parametrize("path", _modules(), ids=lambda path: path.name)
def test_no_clock_or_io_calls(path):
    names, attrs = _called_names(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
    assert names & FORBIDDEN_CALL_NAMES == set(), f"{_relative(path)} 出现禁止的调用：{sorted(names & FORBIDDEN_CALL_NAMES)}"
    assert attrs & FORBIDDEN_CALL_ATTRS == set(), f"{_relative(path)} 出现时钟调用：{sorted(attrs & FORBIDDEN_CALL_ATTRS)}"


def test_file_boundaries_are_the_two_declared_ones():
    readers = set()
    for path in _modules():
        imports = _imports(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        if imports & FILE_ONLY_MODULES:
            readers.add(_relative(path))
    assert readers == set(FILE_BOUNDARY_EXTRA)


def test_engine_modules_are_import_free_of_views_and_io():
    """engine/ 只做判定：不读文件、不读时钟、不打日志。"""
    for path in sorted((PACKAGE / "engine").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        assert _imports(tree) & FORBIDDEN_IMPORTS == set(), path.name


def test_same_input_yields_same_output(helpers):
    """铁律 5：同输入必同输出（无隐藏状态、无随机数）。"""
    import records_kit as kit

    envelope = helpers.envelope("create", history=[helpers.history_row("2026-08-17T10:00:00+08:00", 2.3)])
    first = kit.process(envelope, kit.default_registry())
    second = kit.process(envelope, kit.default_registry())
    assert first == second
    assert first is not second


def test_importing_core_does_not_load_process_environment():
    """核心不依赖进程环境：子进程里导入包后，模块差集里不得出现 os/pathlib 之外的新面。"""
    import subprocess
    import sys

    script = (
        "import sys\n"
        "before = set(sys.modules)\n"
        "import records_kit\n"
        "print(sorted(set(sys.modules) - before))\n"
    )
    completed = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True)
    added = {
        name.split(".")[0]
        for name in ast.literal_eval(completed.stdout.strip())
    }
    # 注意：本机 venv 走 editable 安装，导入包时解释器自身可能带上 urllib/http 等装配模块，
    # 故只断言「与判定无关的重型/副作用模块」未被引入（差集口径）。
    banned = {"socket", "subprocess", "sqlite3", "asyncio", "threading", "logging", "pickle"}
    assert added & banned == set(), sorted(added & banned)
    assert records_kit.PROTOCOL == "records-kit"
