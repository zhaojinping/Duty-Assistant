"""升级链：按到期/逾期阶段触达（幂等，每级每渠道只发一次）。

级别（与 task_events.level 对应；0 保留给联调/手工）：
  1 前3天提醒 / 2 当天 / 3 逾期+1 / 4 逾期+3（升级班长） /
  5 逾期+7（升级班长）/ 6 月底（跨月节点，升级班长）
渠道：群 + 待办（双轨）；级别 ≥4 追加对升级对象的 DM。
"""

from __future__ import annotations

import datetime as _dt

from da_core import outbox
from da_core.scheduler import scan
from da_core.settings import BATTERY_TYPE


def compute_stage(item: dict, today: _dt.date) -> int | None:
    """按扫描项计算当前应处级别；未到触达窗口返回 None。"""
    if item.get("next_due") is None:
        return None
    overdue = int(item.get("overdue_days") or 0)
    if overdue > 0:
        if (today + _dt.timedelta(days=1)).month != today.month:
            return 6  # 月底节点（明天跨月）
        if overdue >= 7:
            return 5
        if overdue >= 3:
            return 4
        return 3
    days = item.get("days_to_due")
    if days == 0:
        return 2
    if days is not None and 1 <= days <= 3:
        return 1
    return None


def run_escalation(ledger, settings, *, now: str | None = None, runner=None,
                   dry_run: bool = False) -> list[dict]:
    """按升级链执行触达（幂等）；返回各任务的动作清单。"""
    report = scan(ledger, settings, now=now)
    today = _dt.date.fromisoformat(report["today"])
    station_id = report["station_id"]
    contacts = ledger.get_contacts(station_id)

    outputs: list[dict] = []
    for item in report["groups"]:
        level = compute_stage(item, today)
        if level is None:
            continue
        task = ledger.current_task(station_id, BATTERY_TYPE, item["group"])
        if task is None:
            continue
        channels = outbox.deliver_cycle(ledger, task, item, contacts=contacts,
                                        level=level, runner=runner, dry_run=dry_run)
        if level >= 4:
            escalate_to = contacts.get(outbox.ROLE_ESCALATE)
            if escalate_to:
                due = item.get("due_effective") or item.get("next_due")
                dm_text = (f"【升级】{item['group']} 蓄电池测量已超期 "
                           f"{item['overdue_days']} 天（应于 {due} 前完成），请跟进。\n——AI助手")
                channels.append({"channel": "dm", **outbox.deliver(ledger, {
                    "task_id": task["task_id"], "level": level, "channel": "dm",
                    "target": escalate_to, "text": dm_text},
                    runner=runner, dry_run=dry_run)})
        outputs.append({"task_id": task["task_id"], "level": level,
                        "channels": channels})
    return outputs
