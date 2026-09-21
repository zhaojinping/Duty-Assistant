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

    if args.command == "inspect":
        ledger = Ledger(args.db)
        print(json.dumps({"db": str(args.db), "tables": ledger.counts()},
                         ensure_ascii=False, indent=2))
        ledger.close()
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
