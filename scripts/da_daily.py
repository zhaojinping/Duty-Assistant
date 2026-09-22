"""开发机上的日常跑入口。生产环境用 ``python -m da_core.cli daily``，读用户自己的配置。

用法（仓库环境）::

    uv run python scripts/da_daily.py --db <ledger.sqlite> \
        --station-id ST001 --station-name XX风电场 [--dry-run] [--skip-reconcile]

快照：每日一份 ``ledger-YYYY-MM-DD.sqlite``（VACUUM INTO），默认放账本同目录
``backups/``，滚动保留最近 14 份；``--snapshot-dir none`` 可关闭。
建议调度：每日 08:30（cron / Windows 任务计划）。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from da_core.daily_job import run_once, snapshot_ledger
from da_core.settings import Settings

__all__ = ["snapshot_ledger"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Duty-Assistant 日常跑（无人值守）")
    parser.add_argument("--db", required=True)
    parser.add_argument("--station-id", required=True)
    parser.add_argument("--station-name", required=True)
    parser.add_argument("--dry-run", action="store_true", help="触达演练（不真发）")
    parser.add_argument("--skip-reconcile", action="store_true")
    parser.add_argument("--snapshot-dir", default=None,
                        help="快照目录（缺省：账本同目录 backups/；传 none 关闭）")
    args = parser.parse_args(argv)

    settings = Settings.default(
        db_path=args.db,
        station={"station_id": args.station_id, "station_name": args.station_name})
    summary = run_once(
        settings,
        dry_run=args.dry_run,
        skip_reconcile=args.skip_reconcile,
        snapshot_dir=None if args.snapshot_dir in {None, "none"} else Path(args.snapshot_dir),
        skip_snapshot=args.snapshot_dir == "none",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
