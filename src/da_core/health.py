"""安装体检。只检查，不安装。"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from da_core.paths import config_path, ledger_path, on_sync_disk, user_data_dir, writer_path


def _item(ok: bool, code: str, message: str, *, action: str = "") -> dict:
    return {"ok": ok, "code": code, "message": message, "action": action}


def check_sqlite(data_dir: Path) -> dict:
    """Python 自带的 sqlite3 能否在用户目录建成空库。不安装独立的 SQLite 软件。"""
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        probe = data_dir / ".sqlite-probe"
        conn = sqlite3.connect(probe)
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS probe(id INTEGER)")
            conn.commit()
        finally:
            conn.close()
        probe.unlink(missing_ok=True)
    except OSError as exc:
        return _item(False, "sqlite", f"当前目录写不了账本：{exc}",
                     action="换一个本机目录，不要放在桌面、文稿或网盘同步文件夹里")
    return _item(True, "sqlite", "Python 自带的账本功能可用，不必另装 SQLite")


def check_writer(data_dir: Path, hostname: str) -> dict:
    path = writer_path(data_dir)
    if not path.is_file():
        return _item(True, "writer", "这台电脑还没有装过，可以做第一次安装")
    import json

    writer = json.loads(path.read_text(encoding="utf-8"))
    owner = writer.get("hostname") or "另一台电脑"
    if owner != hostname:
        return _item(
            False, "writer",
            f"这个场站的账本已经在电脑「{owner}」上运行。这台电脑不能再装一套。",
            action="继续使用那一台电脑。这台只看钉钉，不要再安装。")
    return _item(True, "writer", f"写账电脑就是这一台（{hostname}）")


def collect_health(*, data_dir: Path | None = None, hostname: str,
                   python_ok: bool, dws_found: bool, dws_authenticated: bool | None,
                   on_sync: bool, os_name: str | None = None) -> dict:
    """汇总体检项。外部探测（dws、主机名）由调用方注入，便于测试。"""
    folder = data_dir or user_data_dir()
    system = os_name or sys.platform
    checks = [
        _item(system in {"win32", "darwin"}, "os",
              "这台是 Windows 或 Mac" if system in {"win32", "darwin"}
              else f"当前系统 {system} 不在本次交付范围"),
        _item(python_ok, "python",
              "Python 版本符合要求（3.12 及以上）" if python_ok
              else "需要 Python 3.12 及以上",
              action="" if python_ok else "确认后可自动安装 Python 3.12"),
        check_sqlite(folder),
        _item(not on_sync, "sync-disk",
              "数据目录不在同步盘里" if not on_sync
              else "数据目录在桌面、文稿或网盘同步文件夹里，账本会损坏",
              action="" if not on_sync else "改用系统默认的本机目录"),
        _item(dws_found, "dws",
              "已找到钉钉命令行 dws" if dws_found else "还没有钉钉命令行 dws",
              action="" if dws_found else "确认后可自动安装 dws。安装 dws 时如果没有 Node.js，会一并安装。"),
    ]
    if dws_found:
        if dws_authenticated is None:
            checks.append(_item(False, "dws-auth", "还没有检查钉钉登录状态",
                                action="运行 dws auth status"))
        else:
            checks.append(_item(
                dws_authenticated, "dws-auth",
                "钉钉已登录" if dws_authenticated else "钉钉还没登录",
                action="" if dws_authenticated else "把扫码链接交给用户，必须本人用手机点。这一步不能代点。"))
    configured = config_path(folder).is_file()
    checks.append(_item(configured, "config",
                        "已经有用户配置" if configured else "还没有完成本机配置",
                        action="" if configured else "问清场站、生产群、责任人、电池组别和录入网址后再 init"))
    checks.append(check_writer(folder, hostname))
    ready = all(item["ok"] for item in checks if item["code"] not in {"config"})
    # 第一次安装时允许还没有 config。装完后的日常运行则要求 config 也通过。
    installed = configured and all(item["ok"] for item in checks)
    return {
        "ok": installed,
        "ready_to_install": ready and not configured,
        "data_dir": str(folder),
        "ledger": str(ledger_path(folder)),
        "checks": checks,
    }
