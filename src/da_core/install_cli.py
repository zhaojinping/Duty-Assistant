"""安装与定时任务的命令行。业务命令仍在 cli.py。"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from da_core.health import collect_health
from da_core.install_flow import (
    apply_dependencies,
    create_user_table,
    init_install,
    launchd_plist,
    load_runtime,
    plan_dependencies,
    resolve_task_python,
    task_commands,
    this_hostname,
    windows_task_commands,
)
from da_core.lock import AlreadyRunning, SingleWriterLock
from da_core.paths import config_path, lock_path, on_sync_disk, user_data_dir
from da_core.user_config import ConfigError, load_config


def add_install_parsers(commands) -> None:
    commands.add_parser("health", help="体检：缺什么、能不能装。只检查不安装。")

    init = commands.add_parser("init", help="首次安装：写入用户配置和空账本")
    init.add_argument("--data-dir", default=None)
    init.add_argument("--confirm", action="store_true", help="用户明确同意后才落盘")
    init.add_argument("--station-id", required=True)
    init.add_argument("--station-name", required=True)
    init.add_argument("--group-name", required=True)
    init.add_argument("--group-cid", default="")
    init.add_argument("--assignee-name", default="")
    init.add_argument("--assignee-id", default="")
    init.add_argument("--escalate-name", default="")
    init.add_argument("--escalate-id", default="")
    init.add_argument("--entry-url", required=True)
    init.add_argument("--pull-url", default="")
    init.add_argument("--pull-token", default="")
    init.add_argument("--groups-file", required=True, help="组别 JSON：[{name,kind,count}]")
    init.add_argument("--anchor-day", type=int, default=15)
    init.add_argument("--baseline", default=None, help="第一次应完成的日期 YYYY-MM-DD")

    deps = commands.add_parser("install-deps", help="确认后安装 Python 3.12 和 dws")
    deps.add_argument("--confirm", action="store_true")

    table = commands.add_parser("create-table", help="在用户钉钉里新建电压记录表")
    table.add_argument("--data-dir", default=None)
    table.add_argument("--confirm", action="store_true")

    tasks = commands.add_parser("register-tasks", help="注册每 5 分钟拉取和每天 08:30 催办")
    tasks.add_argument("--confirm", action="store_true")
    tasks.add_argument("--python", default=None,
                       help="定时任务使用的 Python。缺省用仓库 .venv")

    commands.add_parser("watch", help="拉一次录入接口和群消息")
    daily = commands.add_parser("daily", help="每天催办、月报和对账")
    daily.add_argument("--dry-run", action="store_true")


def handle_install(args) -> int | None:
    command = args.command
    if command == "health":
        return _health()
    if command == "init":
        return _init(args)
    if command == "install-deps":
        return _deps(args)
    if command == "create-table":
        return _table(args)
    if command == "register-tasks":
        return _tasks(args)
    if command == "watch":
        return _watch()
    if command == "daily":
        return _daily(dry_run=args.dry_run)
    return None


def _print(payload: dict, *, code: int = 0) -> int:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return code


def _dws_authenticated() -> bool | None:
    if not shutil.which("dws"):
        return None
    from da_core import dws_cli

    rc, out, _err = dws_cli.run_dws(["auth", "status"])
    if rc != 0 or not out.strip():
        return False
    try:
        return bool(json.loads(out).get("authenticated"))
    except (TypeError, ValueError):
        return False


def _health() -> int:
    folder = user_data_dir()
    report = collect_health(
        data_dir=folder,
        hostname=this_hostname(),
        python_ok=sys.version_info >= (3, 12),
        dws_found=bool(shutil.which("dws")),
        dws_authenticated=_dws_authenticated() if shutil.which("dws") else None,
        on_sync=on_sync_disk(folder),
    )
    return _print(report, code=0 if report["ok"] or report["ready_to_install"] else 1)


def _init(args) -> int:
    groups = json.loads(Path(args.groups_file).read_text(encoding="utf-8"))
    payload = {
        "station_id": args.station_id,
        "station_name": args.station_name,
        "group_name": args.group_name,
        "group_cid": args.group_cid,
        "assignee_name": args.assignee_name,
        "assignee_id": args.assignee_id,
        "escalate_name": args.escalate_name,
        "escalate_id": args.escalate_id,
        "entry_url": args.entry_url,
        "pull_url": args.pull_url,
        "pull_token": args.pull_token,
        "groups": groups,
        "anchor_day": args.anchor_day,
        "baseline": args.baseline,
    }
    folder = Path(args.data_dir) if args.data_dir else user_data_dir()
    try:
        plan = init_install(payload, data_dir=folder, hostname=this_hostname(),
                            confirm=args.confirm)
    except ConfigError as exc:
        return _print({"ok": False, "message": str(exc)}, code=1)
    return _print(plan, code=2 if plan.get("needs_confirm") else 0)


def _deps(args) -> int:
    steps = plan_dependencies(
        python_ok=sys.version_info >= (3, 12),
        dws_found=bool(shutil.which("dws")),
        npm_found=bool(shutil.which("npm")),
        brew_found=bool(shutil.which("brew")),
        os_name=sys.platform,
    )
    if not steps:
        return _print({"ok": True, "message": "Python 和 dws 都已具备", "steps": []})
    if not args.confirm:
        return _print({
            "needs_confirm": True,
            "message": "把将要安装的项目读给用户。用户明确同意后再加 --confirm。钉钉扫码不能代点。",
            "steps": steps,
        }, code=2)

    def runner(command: list[str]) -> int:
        completed = subprocess.run(command, check=False)
        return completed.returncode

    results = apply_dependencies(steps, runner=runner)
    ok = all(item.get("ok") for item in results)
    return _print({"ok": ok, "steps": results}, code=0 if ok else 1)


def _table(args) -> int:
    folder = Path(args.data_dir) if args.data_dir else user_data_dir()
    payload = load_config(folder)
    if not payload:
        return _print({"ok": False, "message": "先完成 init，再新建钉钉表"}, code=1)
    if not args.confirm:
        return _print({
            "needs_confirm": True,
            "message": "将在当前登录的钉钉账号下新建《蓄电池电压测量记录》，共 13 列。用户同意后再加 --confirm。",
            "base_name": "蓄电池电压测量记录",
        }, code=2)
    from da_core import dws_cli

    try:
        created = create_user_table(runner=lambda cmd: dws_cli.run_dws(cmd))
    except (ConfigError, json.JSONDecodeError) as exc:
        return _print({"ok": False, "message": str(exc)}, code=1)
    payload.update(created)
    config_path(folder).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return _print({"ok": True, **created})


def _tasks(args) -> int:
    python_exe = resolve_task_python(args.python)
    probe = subprocess.run([python_exe, "-c", "import da_core"], capture_output=True, text=True)
    if probe.returncode != 0:
        return _print({
            "ok": False,
            "message": "这个 Python 里没有安装本包。先在仓库根目录运行 python scripts/bootstrap.py，再用它注册定时任务。",
            "python": python_exe,
        }, code=1)
    commands = task_commands(python_exe)
    if sys.platform == "win32":
        planned = windows_task_commands(python_exe)
        if not args.confirm:
            return _print({
                "needs_confirm": True,
                "message": "将注册两个计划任务：每 5 分钟拉取，每天 08:30 催办。这台电脑到点要开着。",
                "commands": planned,
            }, code=2)
        for command in planned:
            subprocess.run(command, check=False)
        return _print({"ok": True, "registered": [item[4] for item in planned]})
    if sys.platform == "darwin":
        agents = Path.home() / "Library" / "LaunchAgents"
        specs = [
            ("host.dutyassistant.watch", commands["watch"], 300, False),
            ("host.dutyassistant.daily", commands["daily"], None, True),
        ]
        if not args.confirm:
            return _print({
                "needs_confirm": True,
                "message": "将注册两个登录项：每 5 分钟拉取，每天 08:30 催办。这台电脑到点要开着。",
                "labels": [item[0] for item in specs],
            }, code=2)
        agents.mkdir(parents=True, exist_ok=True)
        for label, argv, interval, calendar in specs:
            path = agents / f"{label}.plist"
            path.write_text(launchd_plist(label, argv, interval=interval, calendar=calendar),
                            encoding="utf-8")
            subprocess.run(["launchctl", "bootstrap", f"gui/{_uid()}", str(path)], check=False)
        return _print({"ok": True, "registered": [item[0] for item in specs]})
    return _print({"ok": False, "message": "只注册 Windows 任务计划或 Mac 登录项"}, code=1)


def _uid() -> str:
    import os
    return str(os.getuid())


def _watch() -> int:
    try:
        settings, payload, folder = load_runtime()
    except ConfigError as exc:
        return _print({"ok": False, "message": str(exc)}, code=1)
    try:
        with SingleWriterLock(lock_path(folder)):
            return _watch_body(settings, payload)
    except AlreadyRunning:
        return _print({"skipped": True, "reason": "已有任务在跑"})


def _watch_body(settings, payload) -> int:
    from da_core.group_intake import poll_group
    from da_core.ledger import Ledger
    from da_core.pull_intake import pull_remote

    ledger = Ledger(settings.db_path)
    ledger.seed_config(settings)
    summary: dict = {"group": None, "pull": None}
    pull_url = payload.get("pull_url")
    if pull_url:
        try:
            summary["pull"] = pull_remote(
                ledger, settings, base_url=pull_url, token=payload.get("pull_token") or None,
                notify_group=False)
        except Exception as exc:  # noqa: BLE001 — 主路失败不能停掉群兜底
            summary["pull"] = {"ok": False, "error": str(exc)[:300]}
    if payload.get("group_name"):
        summary["group"] = poll_group(ledger, settings)
    ledger.close()
    return _print(summary)


def _daily(*, dry_run: bool) -> int:
    from da_core.daily_job import run_once

    try:
        settings, _payload, folder = load_runtime()
    except ConfigError as exc:
        return _print({"ok": False, "message": str(exc)}, code=1)
    try:
        with SingleWriterLock(lock_path(folder)):
            summary = run_once(settings, dry_run=dry_run)
    except AlreadyRunning:
        return _print({"skipped": True, "reason": "已有任务在跑"})
    return _print(summary)
