"""每天一次：快照、催办、月报、对账。"""

from __future__ import annotations

from pathlib import Path

from da_core.clock import iso_now
from da_core.escalation import run_escalation
from da_core.ledger import Ledger
from da_core.reconcile import reconcile
from da_core.reporting import push_monthly_report
from da_core.scheduler import sync_tasks

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


def run_once(settings, *, dry_run: bool = False, skip_reconcile: bool = False,
             snapshot_dir: Path | None = None, skip_snapshot: bool = False) -> dict:
    ledger = Ledger(settings.db_path)
    ledger.seed_config(settings)
    if skip_snapshot:
        snap_dir = None
    elif snapshot_dir is None:
        snap_dir = settings.db_path.parent / "backups"
    else:
        snap_dir = snapshot_dir
    from da_core.scheduler import ensure_thermo_baseline
    summary = {
        "snapshot": snapshot_ledger(ledger, snap_dir),
        "thermo_baseline": ensure_thermo_baseline(ledger, settings),
        "synced": sync_tasks(ledger, settings),
        "escalation": run_escalation(ledger, settings, dry_run=dry_run,
                                     with_entry_card=True),
        "report_push": push_monthly_report(ledger, settings, dry_run=dry_run),
    }
    if not skip_reconcile:
        if not (settings.table or {}).get("base_id"):
            summary["reconcile"] = {"skipped": True, "reason": "还没有钉钉表"}
        else:
            summary["reconcile"] = reconcile(ledger, settings)
    ledger.close()
    return summary
