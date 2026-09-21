"""无人值守日常跑：scan --sync → escalate → reconcile（打印 JSON 摘要）。

用法（仓库环境）::

    uv run python scripts/da_daily.py --db <ledger.sqlite> \\
        --station-id ST001 --station-name XX风电场 [--dry-run] [--skip-reconcile]

建议调度：每日 08:30（cron / Windows 任务计划）。
"""

from __future__ import annotations

import argparse
import json

from da_core.escalation import run_escalation
from da_core.ledger import Ledger
from da_core.reconcile import reconcile
from da_core.scheduler import sync_tasks
from da_core.settings import Settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Duty-Assistant 日常跑（无人值守）")
    parser.add_argument("--db", required=True)
    parser.add_argument("--station-id", required=True)
    parser.add_argument("--station-name", required=True)
    parser.add_argument("--dry-run", action="store_true", help="触达演练（不真发）")
    parser.add_argument("--skip-reconcile", action="store_true")
    args = parser.parse_args(argv)

    settings = Settings.default(
        db_path=args.db,
        station={"station_id": args.station_id, "station_name": args.station_name})
    ledger = Ledger(settings.db_path)
    ledger.seed_config(settings)

    summary = {
        "synced": sync_tasks(ledger, settings),
        "escalation": run_escalation(ledger, settings, dry_run=args.dry_run),
    }
    if not args.skip_reconcile:
        summary["reconcile"] = reconcile(ledger, settings)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
