"""da_core CLI：联调与运维入口。

示例::

    python -m da_core.cli submit --file sample.json --db data/ledger.sqlite \
        --station-id ST001 --station-name XX风电场 [--dispatch --dry-run]

    python -m da_core.cli inspect --db data/ledger.sqlite
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from da_core.ledger import Ledger
from da_core.service import submit_submission
from da_core.settings import Settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="da_core", description="Duty-Assistant 核心系统 CLI")
    commands = parser.add_subparsers(dest="command", required=True)

    submit = commands.add_parser("submit", help="提交一批测量数据（完整核心链路）")
    submit.add_argument("--file", required=True, help="提交 JSON 文件")
    submit.add_argument("--db", default=None,
                        help="账本路径（缺省：$DA_DATA_DIR/ledger.sqlite 或 ./data/ledger.sqlite）")
    submit.add_argument("--station-id", default=None)
    submit.add_argument("--station-name", default=None)
    submit.add_argument("--dispatch", action="store_true", help="写输出投影（钉钉表格）")
    submit.add_argument("--dry-run", action="store_true", help="投递演练：只组装行、不写表")
    submit.add_argument("--remark-tag", default=None, help="联调标记：写入行备注（便于清理）")

    inspect = commands.add_parser("inspect", help="账本概览")
    inspect.add_argument("--db", required=True)

    args = parser.parse_args(argv)

    if args.command == "submit":
        station = None
        if args.station_id or args.station_name:
            if not (args.station_id and args.station_name):
                print("需要同时提供 --station-id 与 --station-name", file=sys.stderr)
                return 2
            station = {"station_id": args.station_id, "station_name": args.station_name}
        settings = Settings.default(db_path=args.db, station=station)
        data = json.loads(Path(args.file).read_text(encoding="utf-8"))
        summary = submit_submission(data, settings=settings, dispatch=args.dispatch,
                                    dry_run_dispatch=args.dry_run, remark_tag=args.remark_tag)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
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
