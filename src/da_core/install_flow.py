"""首次安装：写用户配置、空账本、定时任务计划。确认前不落盘。"""

from __future__ import annotations

import datetime as _dt
import json
import platform
import sys
from pathlib import Path

from da_core.ledger import Ledger
from da_core.paths import config_path, ledger_path, on_sync_disk, writer_path
from da_core.user_config import (
    DEVELOPER_BASE_ID,
    TABLE_FIELDS,
    ConfigError,
    group_kinds_from,
    load_config,
    save_config,
    settings_from_config,
    validate_install,
)

BASE_NAME = "蓄电池电压测量记录"
TABLE_NAME = "电压测量记录"


def next_anchor_date(today: _dt.date, anchor_day: int) -> str:
    """今天之后最近的锚日；今天正好是锚日则用今天。"""
    day = min(anchor_day, _month_last(today.year, today.month))
    candidate = _dt.date(today.year, today.month, day)
    if candidate < today:
        year, month = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
        candidate = _dt.date(year, month, min(anchor_day, _month_last(year, month)))
    return candidate.isoformat()


def _month_last(year: int, month: int) -> int:
    if month == 12:
        nxt = _dt.date(year + 1, 1, 1)
    else:
        nxt = _dt.date(year, month + 1, 1)
    return (nxt - _dt.timedelta(days=1)).day


def this_hostname() -> str:
    return platform.node() or "unknown-host"


def plan_dependencies(*, python_ok: bool, dws_found: bool, npm_found: bool,
                      brew_found: bool, os_name: str, dws_version_ok: bool = True) -> list[dict]:
    """确认后允许自动安装的步骤。只含 Python，以及安装或升级 dws 所必需的 Node。"""
    steps: list[dict] = []
    if not python_ok:
        if os_name == "win32":
            steps.append({
                "id": "python",
                "command": ["winget", "install", "-e", "--id", "Python.Python.3.12",
                            "--scope", "user", "--accept-package-agreements",
                            "--accept-source-agreements"],
                "message": "安装 Python 3.12",
            })
        elif os_name == "darwin":
            if not brew_found:
                steps.append({
                    "id": "brew",
                    "command": [],
                    "message": "这台 Mac 还没有 Homebrew。先装 Homebrew，再装 Python。这一步会要求用户输入开机密码。",
                })
            else:
                steps.append({
                    "id": "python",
                    "command": ["brew", "install", "python@3.12"],
                    "message": "用 Homebrew 安装 Python 3.12",
                })
        else:
            steps.append({"id": "python", "command": [], "message": "请安装 Python 3.12 及以上"})
    if not dws_found or not dws_version_ok:
        if not npm_found:
            if os_name == "win32":
                steps.append({
                    "id": "node",
                    "command": ["winget", "install", "-e", "--id", "OpenJS.NodeJS.LTS",
                                "--scope", "user", "--accept-package-agreements",
                                "--accept-source-agreements"],
                    "message": "安装 dws 需要 Node.js，确认后一并安装",
                })
            elif os_name == "darwin" and brew_found:
                steps.append({
                    "id": "node",
                    "command": ["brew", "install", "node"],
                    "message": "安装 dws 需要 Node.js，确认后一并安装",
                })
            else:
                steps.append({
                    "id": "node", "command": [],
                    "message": "请先安装 Node.js，才能安装 dws",
                })
        steps.append({
            "id": "dws",
            "command": ["npm", "install", "-g", "dingtalk-workspace-cli"],
            "message": "安装或升级钉钉命令行 dws 到 1.0.62 及以上",
        })
    return steps


def apply_dependencies(steps: list[dict], *, runner) -> list[dict]:
    results = []
    for step in steps:
        command = step.get("command") or []
        if not command:
            results.append({**step, "ok": False, "detail": "这一步需要人处理，不能自动执行"})
            continue
        completed = runner(command)
        results.append({**step, "ok": completed == 0, "returncode": completed})
    return results


def table_field_specs() -> list[dict]:
    specs = []
    for name, kind in TABLE_FIELDS:
        item: dict = {"fieldName": name, "type": kind}
        if kind == "number":
            formatter = "INT" if name in {"电池序号", "账本Rev"} else "FLOAT_2"
            item["config"] = {"formatter": formatter}
        specs.append(item)
    return specs


def ledger_doc_url(base_id: str, table_id: str) -> str:
    from urllib.parse import quote

    return ("https://alidocs.dingtalk.com/i/nodes/"
            f"{base_id}?iframeQuery=sheetId%3D{quote(table_id)}")


def _find_str(obj, keys: tuple[str, ...]) -> str:
    if isinstance(obj, dict):
        for key in keys:
            value = obj.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for value in obj.values():
            found = _find_str(value, keys)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_str(item, keys)
            if found:
                return found
    return ""


def _field_map(obj) -> dict[str, str]:
    wanted = {item[0] for item in TABLE_FIELDS}
    found: dict[str, str] = {}

    def walk(node) -> None:
        if isinstance(node, dict):
            name = node.get("fieldName") or node.get("name")
            fid = node.get("fieldId") or node.get("id")
            if isinstance(name, str) and isinstance(fid, str) and name in wanted and fid:
                found[name] = fid
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(obj)
    return found


def create_user_table(*, runner) -> dict:
    """用 dws 新建用户自己的表。runner(args) -> (rc, stdout, stderr)。"""
    rc, out, err = runner([
        "aitable", "base", "create", "--name", BASE_NAME, "--yes", "--format", "json"])
    if rc != 0:
        raise ConfigError(f"新建钉钉表失败：{(err or out).strip()[:300]}")
    base_id = _find_str(json.loads(out), ("baseId", "base_id"))
    if not base_id or base_id == DEVELOPER_BASE_ID:
        raise ConfigError("没有得到用户自己的表格编号")
    fields = json.dumps(table_field_specs(), ensure_ascii=False)
    rc, out, err = runner([
        "aitable", "table", "create", "--base-id", base_id, "--name", TABLE_NAME,
        "--fields", fields, "--yes", "--format", "json"])
    if rc != 0:
        raise ConfigError(f"新建电压表失败：{(err or out).strip()[:300]}")
    created = json.loads(out)
    table_id = _find_str(created, ("tableId", "table_id"))
    field_ids = _field_map(created)
    if len(field_ids) < len(TABLE_FIELDS) and table_id:
        rc, got, err = runner([
            "aitable", "field", "get", "--base-id", base_id, "--table-id", table_id,
            "--format", "json"])
        if rc == 0 and got.strip():
            field_ids.update(_field_map(json.loads(got)))
    missing = [name for name, _kind in TABLE_FIELDS if name not in field_ids]
    if not table_id or missing:
        raise ConfigError("表已建立，但有列没有编号：" + "、".join(missing or ["表编号"]))
    return {
        "base_id": base_id,
        "table_id": table_id,
        "field_ids": field_ids,
        "ledger_url": ledger_doc_url(base_id, table_id),
    }


def repo_root() -> Path:
    """从源码位置向上找到含 pyproject.toml 的仓库根。"""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").is_file() and (parent / "src" / "da_core").is_dir():
            return parent
    return Path.cwd()


def venv_python(repo: Path | None = None) -> Path:
    root = repo or repo_root()
    if sys.platform == "win32":
        return root / ".venv" / "Scripts" / "python.exe"
    return root / ".venv" / "bin" / "python"


def resolve_task_python(explicit: str | None = None) -> str:
    """定时任务用已安装本包的解释器。未指定时优先仓库 .venv。"""
    if explicit:
        return explicit
    candidate = venv_python()
    if candidate.is_file():
        return str(candidate)
    return sys.executable


def task_commands(python_exe: str) -> dict[str, list[str]]:
    return {
        "watch": [python_exe, "-m", "da_core.cli", "watch"],
        "daily": [python_exe, "-m", "da_core.cli", "daily"],
    }


def quote_command(args: list[str]) -> str:
    def one(part: str) -> str:
        if any(ch in part for ch in ' "'):
            return '"' + part.replace('"', '\\"') + '"'
        return part

    return " ".join(one(part) for part in args)


def windows_task_commands(python_exe: str) -> list[list[str]]:
    commands = task_commands(python_exe)
    quoted = {name: quote_command(args) for name, args in commands.items()}
    return [
        ["schtasks", "/Create", "/F", "/TN", "DutyAssistant-watch",
         "/SC", "MINUTE", "/MO", "5", "/TR", quoted["watch"]],
        ["schtasks", "/Create", "/F", "/TN", "DutyAssistant-daily",
         "/SC", "DAILY", "/ST", "08:30", "/TR", quoted["daily"]],
    ]


def launchd_plist(label: str, args: list[str], *, interval: int | None,
                  calendar: bool) -> str:
    program = "\n".join(f"    <string>{_xml(part)}</string>" for part in args)
    if calendar:
        start = """    <key>StartCalendarInterval</key>
    <dict>
      <key>Hour</key><integer>8</integer>
      <key>Minute</key><integer>30</integer>
    </dict>"""
    else:
        start = f"    <key>StartInterval</key>\n    <integer>{interval}</integer>"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>{_xml(label)}</string>
    <key>ProgramArguments</key>
    <array>
{program}
    </array>
{start}
    <key>RunAtLoad</key><false/>
</dict>
</plist>
"""


def _xml(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def assert_same_host(data_dir: Path, hostname: str) -> None:
    path = writer_path(data_dir)
    if not path.is_file():
        return
    writer = json.loads(path.read_text(encoding="utf-8"))
    owner = writer.get("hostname") or ""
    if owner and owner != hostname:
        raise ConfigError(
            f"这个场站的账本已经在电脑「{owner}」上运行。这台电脑不能再装一套。")


def init_install(payload: dict, *, data_dir: Path, hostname: str, confirm: bool) -> dict:
    """确认前只返回将要写入的内容。确认后写空账本和配置。"""
    if on_sync_disk(data_dir):
        raise ConfigError("数据目录在同步盘里。请改用系统默认的本机目录。")
    assert_same_host(data_dir, hostname)
    normalized = validate_install(payload)
    if not normalized["baseline"]:
        normalized["baseline"] = next_anchor_date(_dt.date.today(), normalized["anchor_day"])
    plan = {
        "data_dir": str(data_dir),
        "ledger": str(ledger_path(data_dir)),
        "station": normalized["station_name"],
        "group": normalized["group_name"],
        "assignee": normalized["assignee_name"] or normalized["assignee_id"],
        "escalate": normalized["escalate_name"] or normalized["escalate_id"] or "不填，不发私聊",
        "entry_url": normalized["entry_url"],
        "pull_url": normalized["pull_url"],
        "groups": normalized["groups"],
        "cycle": f"每月 {normalized['anchor_day']} 日，起算 {normalized['baseline']}",
        "thresholds": "2V 为 1.85–2.35，12V 为 11.85–13.80",
        "hostname": hostname,
    }
    if not confirm:
        plan["needs_confirm"] = True
        plan["message"] = "把以上内容读给用户。用户明确同意后再加 --confirm。"
        return plan

    data_dir.mkdir(parents=True, exist_ok=True)
    writer = {"hostname": hostname}
    save_config(data_dir, normalized, writer=writer)
    writer_path(data_dir).write_text(
        json.dumps(writer, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    settings = settings_from_config(data_dir, normalized)
    ledger = Ledger(settings.db_path)
    ledger.seed_config(settings)
    ledger.set_cycle_config(
        cycle_mode="monthly_day",
        anchor_day=normalized["anchor_day"],
        baseline=normalized["baseline"],
        updated_by="install")
    station_id = normalized["station_id"]
    ledger.set_contact(station_id, "reminder_group", normalized["group_name"], updated_by="install")
    assignee = normalized["assignee_id"] or normalized["assignee_name"]
    ledger.set_contact(station_id, "reminder_assignee", assignee, updated_by="install")
    if normalized["escalate_id"] or normalized["escalate_name"]:
        ledger.set_contact(
            station_id, "reminder_escalate",
            normalized["escalate_id"] or normalized["escalate_name"],
            updated_by="install")
    ledger.set_param("group_intake", {"group": normalized["group_name"]}, updated_by="install")
    ledger.close()
    plan["needs_confirm"] = False
    plan["config"] = str(config_path(data_dir))
    plan["group_kinds"] = group_kinds_from(normalized["groups"])
    return plan


def load_runtime(data_dir: Path | None = None):
    """定时任务用的配置。没有用户配置就拒绝，不用开发者默认值。"""
    from da_core.paths import user_data_dir

    folder = data_dir or user_data_dir()
    payload = load_config(folder)
    if not payload:
        raise ConfigError("还没有完成本机安装。先运行 health，再按安装说明 init。")
    assert_same_host(folder, this_hostname())
    return settings_from_config(folder, payload), payload, folder
