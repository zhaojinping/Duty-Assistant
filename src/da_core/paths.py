"""用户本机数据目录。账本和配置不放仓库、不放同步盘。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_DIR_NAME = "DutyAssistant"

# 这些目录经常被云同步，SQLite 放进去会损坏。
_SYNC_MARKERS = (
    f"{os.sep}OneDrive{os.sep}",
    f"{os.sep}iCloud{os.sep}",
    f"{os.sep}Dropbox{os.sep}",
    f"{os.sep}Nutstore{os.sep}",
    f"{os.sep}坚果云{os.sep}",
    f"{os.sep}Desktop{os.sep}",
    f"{os.sep}桌面{os.sep}",
    f"{os.sep}Documents{os.sep}",
    f"{os.sep}文稿{os.sep}",
)


def user_data_dir() -> Path:
    """未设置 DA_DATA_DIR 时，用当前系统用户的本地目录。"""
    override = os.environ.get("DA_DATA_DIR")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / APP_DIR_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_DIR_NAME
    return Path.home() / ".local" / "share" / APP_DIR_NAME


def config_path(data_dir: Path | None = None) -> Path:
    return (data_dir or user_data_dir()) / "config.json"


def ledger_path(data_dir: Path | None = None) -> Path:
    return (data_dir or user_data_dir()) / "ledger.sqlite"


def writer_path(data_dir: Path | None = None) -> Path:
    return (data_dir or user_data_dir()) / "writer.json"


def lock_path(data_dir: Path | None = None) -> Path:
    return (data_dir or user_data_dir()) / "run.lock"


def on_sync_disk(path: Path) -> bool:
    """路径是否落在常见同步目录里。"""
    text = str(path.resolve()) + os.sep
    return any(marker in text for marker in _SYNC_MARKERS)
