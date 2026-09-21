"""无人值守日常跑：snapshot → scan --sync → escalate → reconcile（打印 JSON 摘要）。

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

from da_core.clock import iso_now
from da_core.escalation import run_escalation
from da_core.ledger import Ledger
from da_core.reconcile import reconcile
from da_core.scheduler import sync_tasks
from da_core.settings import Settings

_SNAPSHOT_KEEP = 14


def snapshot_ledger(ledger: Ledger, snapshot_dir: Path | None, *,
                    keep: int = _SNAPSHOT_KEEP) -> dict:
    """每日快照（VACUUM INTO）+ 滚动保留；同日重复执行跳过。"""
    if snapshot_dir is None:
        return {"skipped": True, "reason": "disabled"}
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    target = snapshot_dir / f"ledger-{iso_now()[:10]}.sqlite"
    if target.exists():
        return {"skipped": True, "reason": "exists", "file": target.name}
    ledger.conn.execute("VACUUM INTO ?", (str(target),))
    snapshots = sorted(snapshot_dir.glob("ledger-*.sqlite"))
    pruned = []
    for old in snapshots[:-keep]:
        old.unlink()
        pruned.append(old.name)
    return {"file": target.name, "pruned": pruned}


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
    ledger = Ledger(settings.db_path)
    ledger.seed_config(settings)

    if args.snapshot_dir == "none":
        snapshot_dir = None
    elif args.snapshot_dir:
        snapshot_dir = Path(args.snapshot_dir)
    else:
        snapshot_dir = settings.db_path.parent / "backups"

    summary = {
        "snapshot": snapshot_ledger(ledger, snapshot_dir),
        "synced": sync_tasks(ledger, settings),
        "escalation": run_escalation(ledger, settings, dry_run=args.dry_run),
    }
    if not args.skip_reconcile:
        summary["reconcile"] = reconcile(ledger, settings)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
