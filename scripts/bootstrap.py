"""在仓库里建立 .venv 并做可编辑安装。这一步不依赖 da_core 已被安装。

用法（仓库根目录，使用 Python 3.12 及以上）::

    python scripts/bootstrap.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def venv_python(repo: Path) -> Path:
    if sys.platform == "win32":
        return repo / ".venv" / "Scripts" / "python.exe"
    return repo / ".venv" / "bin" / "python"


def main() -> int:
    if sys.version_info < (3, 12):
        print("需要 Python 3.12 及以上。当前是 "
              f"{sys.version_info.major}.{sys.version_info.minor}。", file=sys.stderr)
        return 1
    root = repo_root()
    target = venv_python(root)
    if not target.is_file():
        subprocess.run([sys.executable, "-m", "venv", str(root / ".venv")], check=True)
    subprocess.run([str(target), "-m", "pip", "install", "-e", str(root)], check=True)
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
