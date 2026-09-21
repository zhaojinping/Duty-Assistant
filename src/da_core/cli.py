"""da_core CLI：联调与运维入口。

示例::

    python -m da_core.cli submit --file sample.json --db data/ledger.sqlite \
        --station-id ST001 --station-name XX风电场 [--dispatch --dry-run]

    python -m da_core.cli correct --uid <UID> --file new_payload.json --actor 张三 \
        --db data/ledger.sqlite --station-id ST001 --station-name XX风电场

    python -m da_core.cli void --uid <UID> --reason "录入错误" --actor 张三 \
        --db data/ledger.sqlite --station-id ST001 --station-name XX风电场

    python -m da_core.cli inspect --db data/ledger.sqlite
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from da_core.ledger import Ledger
from da_core.service import correct_submission, submit_submission, void_record
from da_core.settings import Settings


def _station_from_args(args) -> dict | None:
    if args.station_id or args.station_name:
        if not (args.station_id and args.station_name):
            raise SystemExit("需要同时提供 --station-id 与 --station-name")
        return {"station_id": args.station_id, "station_name": args.station_name}
    return None


def _settings_from_args(args) -> Settings:
    return Settings.default(db_path=args.db, station=_station_from_args(args))


def _add_station_args(parser) -> None:
    parser.add_argument("--db", default=None,
                        help="账本路径（缺省：$DA_DATA_DIR/ledger.sqlite 或 ./data/ledger.sqlite）")
    parser.add_argument("--station-id", default=None)
    parser.add_argument("--station-name", default=None)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="da_core", description="Duty-Assistant 核心系统 CLI")
    commands = parser.add_subparsers(dest="command", required=True)

    submit = commands.add_parser("submit", help="提交一批测量数据（完整核心链路）")
    submit.add_argument("--file", required=True, help="提交 JSON 文件")
    _add_station_args(submit)
    submit.add_argument("--dispatch", action="store_true", help="写输出投影（钉钉表格）")
    submit.add_argument("--dry-run", action="store_true", help="投递演练：只组装行、不写表")
    submit.add_argument("--remark-tag", default=None, help="联调标记：写入行备注（便于清理）")

    correct = commands.add_parser("correct", help="定稿更正（全量替换 payload，免签自动重新定稿）")
    correct.add_argument("--uid", required=True, help="账本记录 UID")
    correct.add_argument("--file", required=True, help="新 payload JSON（与提交 groups[] 同形）")
    correct.add_argument("--actor", required=True, help="操作人")
    _add_station_args(correct)

    void = commands.add_parser("void", help="作废记录（墓碑占号，判重键释放）")
    void.add_argument("--uid", required=True, help="账本记录 UID")
    void.add_argument("--reason", required=True, help="作废原因")
    void.add_argument("--actor", required=True, help="操作人")
    _add_station_args(void)

    scan_cmd = commands.add_parser("scan", help="周期扫描（只读视图；--sync 落台账）")
    _add_station_args(scan_cmd)
    scan_cmd.add_argument("--sync", action="store_true", help="同步 tasks 台账（写）")
    scan_cmd.add_argument("--now", default=None, help="模拟时刻（RFC3339，联调用）")

    notify_cmd = commands.add_parser("notify", help="按任务当前状态触达（联调/运维入口）")
    notify_cmd.add_argument("--task-id", default=None, help="指定任务；缺省=全部在办任务")
    _add_station_args(notify_cmd)
    notify_cmd.add_argument("--level", type=int, default=0, help="触达级别（幂等键组成）")
    notify_cmd.add_argument("--dry-run", action="store_true", help="只组装不发送")

    contacts_cmd = commands.add_parser("contacts", help="触达目标（联系人）配置")
    contacts_cmd.add_argument("--db", required=True)
    contacts_cmd.add_argument("--station-id", required=True)
    contacts_cmd.add_argument("--set", default=None, metavar="ROLE=TARGET",
                              help="设置角色目标：reminder_group / reminder_assignee / reminder_escalate")

    inspect = commands.add_parser("inspect", help="账本概览")
    inspect.add_argument("--db", required=True)

    args = parser.parse_args(argv)

    if args.command == "submit":
        settings = _settings_from_args(args)
        data = json.loads(Path(args.file).read_text(encoding="utf-8"))
        summary = submit_submission(data, settings=settings, dispatch=args.dispatch,
                                    dry_run_dispatch=args.dry_run, remark_tag=args.remark_tag)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    if args.command == "correct":
        settings = _settings_from_args(args)
        payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
        result = correct_submission(payload, record_uid=args.uid, actor=args.actor,
                                    settings=settings)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.command == "void":
        settings = _settings_from_args(args)
        result = void_record(args.uid, reason=args.reason, actor=args.actor, settings=settings)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.command == "scan":
        from da_core.scheduler import scan as scan_cycles, sync_tasks

        settings = _settings_from_args(args)
        ledger = Ledger(settings.db_path)
        ledger.seed_config(settings)
        if args.sync:
            result = sync_tasks(ledger, settings, now=args.now)
        else:
            result = scan_cycles(ledger, settings, now=args.now)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.command == "notify":
        from da_core import outbox
        from da_core.scheduler import scan as scan_cycles

        settings = _settings_from_args(args)
        ledger = Ledger(settings.db_path)
        ledger.seed_config(settings)
        contacts = ledger.get_contacts(settings.station["station_id"])
        report = scan_cycles(ledger, settings)
        items = {item["group"]: item for item in report["groups"]}
        tasks = ledger.list_tasks(settings.station["station_id"], states=("open", "overdue"))
        if args.task_id:
            tasks = [task for task in tasks if task["task_id"] == args.task_id]
        results = []
        for task in tasks:
            group_label = task["task_id"].split("|")[1]
            item = items.get(group_label) or {"group": group_label, "overdue_days": 0}
            results.append({
                "task_id": task["task_id"],
                "channels": outbox.deliver_cycle(ledger, task, item, contacts=contacts,
                                                 level=args.level, dry_run=args.dry_run),
            })
        print(json.dumps({"contacts": sorted(contacts), "sent": results},
                         ensure_ascii=False, indent=2))
        return 0

    if args.command == "contacts":
        ledger = Ledger(args.db)
        if args.set:
            role, _, target = args.set.partition("=")
            if not target.strip():
                raise SystemExit("格式：--set role=target（如 --set reminder_group=APM测试）")
            ledger.set_contact(args.station_id, role.strip(), target.strip(), updated_by="cli")
        print(json.dumps(ledger.get_contacts(args.station_id), ensure_ascii=False, indent=2))
        return 0

    if args.command == "inspect":
        ledger = Ledger(args.db)
        print(json.dumps({"db": str(args.db), "tables": ledger.counts()},
                         ensure_ascii=False, indent=2))
        ledger.close()
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
