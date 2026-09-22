"""周期扫描器：任务到期判定 + tasks 台账对账（催办引擎的“大脑”）。

口径（改造方案 §5；2026-09-22 起支持「月锚」周期）：
- **滚动锚点**：``cycle_mode=rolling_days``（缺省）时，下一到期 = 该组最近一次完成记录
  （confirmed/archived）的 occurred_at + cycle_days；
- **月锚**：``cycle_mode=monthly_day`` 时，下一到期 = 完成月**次月的 anchor_day**
  （缺省 15 日，即“每月 15 日一期”）——避免 30 天滚动逐月漂移，自然对齐月节拍；
- **从未完成**：用 cycle_baseline 起算（待现场规程核对后配置）；未配置则标 awaiting_baseline，不催办；
- **判定“做过”看测量时间**（迟录不冤枉）：迟到的完成同样结案（done_late）并滚动锚点；
- **任务粒度**：每站每组一条“当前周期承诺”；task_id 约定 ``station|group|due``；
- **滚期结案**：周期配置变更导致 due 变化、且该任务期内无「新完成」时，旧任务结案为
  rebased（判定：最近一次完成 occurred_at 不晚于任务 opened_at），不冒充 done；
- 本模块只算账/落台账，不发送任何消息（触达见 outbox，升级链见 escalation）。
"""

from __future__ import annotations

import calendar
import datetime as _dt

from records_kit.util import parse_rfc3339

from da_core.clock import iso_now
from da_core.settings import BATTERY_TYPE


def _wall_date(text: str) -> _dt.date:
    return parse_rfc3339(text).date()


def _now_date(now: str | None = None) -> _dt.date:
    return parse_rfc3339(now or iso_now()).date()


def _task_id(station_id: str, group_label: str, due: _dt.date) -> str:
    return f"{station_id}|{group_label}|{due.isoformat()}"


def _next_monthly_due(completed: _dt.date, anchor_day: int) -> _dt.date:
    """月锚规则：完成月次月的 anchor_day（短月自动回退到月末）。"""
    year, month = completed.year, completed.month
    year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    day = min(anchor_day, calendar.monthrange(year, month)[1])
    return _dt.date(year, month, day)


def group_completions(ledger, station_id: str, group_label: str) -> list[dict]:
    """该组完成历史（confirmed/archived，含迟到完成），按 occurred_at 升序。"""
    return [
        {"record_uid": row["record_uid"], "occurred_at": row["occurred_at"],
         "lifecycle": row["lifecycle"]}
        for row in ledger.conn.execute(
            "SELECT * FROM records WHERE station_id=? AND record_type=? AND group_label=? "
            "AND lifecycle IN ('confirmed','archived') ORDER BY occurred_at, record_uid",
            (station_id, BATTERY_TYPE, group_label),
        )
    ]


def scan(ledger, settings, *, now: str | None = None) -> dict:
    """按组计算周期状态（只读）：最后完成 / 下一到期 / 距到期 / 逾期天数。"""
    station_id = (settings.station or {}).get("station_id")
    if not station_id:
        raise ValueError("部署未配置 station（settings.station）")
    cycle = ledger.get_cycle_config()
    cycle_days = int(cycle.get("cycle_days") or 30)
    cycle_mode = cycle.get("cycle_mode") or "rolling_days"
    anchor_day = int(cycle.get("anchor_day") or 15)
    baseline = cycle.get("baseline")
    today = _now_date(now)

    groups: list[dict] = []
    for label in ledger.get_group_kinds():
        done = group_completions(ledger, station_id, label)
        last = done[-1] if done else None
        if last is not None:
            if cycle_mode == "monthly_day":
                due: _dt.date | None = _next_monthly_due(
                    _wall_date(last["occurred_at"]), anchor_day)
            else:
                due = (_wall_date(last["occurred_at"])
                       + _dt.timedelta(days=cycle_days))
        elif baseline:
            due = _dt.date.fromisoformat(baseline)
        else:
            due = None
        deferred_until = None
        due_effective = due
        if due is not None:
            deferred_until = ledger.latest_deferral(station_id, label, due.isoformat())
            if deferred_until and deferred_until > due.isoformat():
                due_effective = _dt.date.fromisoformat(deferred_until)
        overdue_days = ((today - due_effective).days
                        if due_effective and today > due_effective else 0)
        if due is None:
            status = "awaiting_baseline"
        elif overdue_days > 0:
            status = "overdue"
        elif deferred_until and deferred_until > due.isoformat():
            status = "deferred"
        else:
            status = "ok"
        groups.append({
            "group": label,
            "last_done_at": last["occurred_at"] if last else None,
            "last_done_uid": last["record_uid"] if last else None,
            "next_due": due.isoformat() if due else None,
            "due_effective": due_effective.isoformat() if due_effective else None,
            "deferred_until": deferred_until,
            "days_to_due": (due_effective - today).days if due_effective else None,
            "overdue_days": overdue_days,
            "status": status,
        })
    return {"station_id": station_id, "today": today.isoformat(),
            "cycle_days": cycle_days, "cycle_mode": cycle_mode,
            "anchor_day": anchor_day, "baseline": baseline, "groups": groups}


def sync_tasks(ledger, settings, *, now: str | None = None) -> dict:
    """扫描结果 ↔ tasks 台账对账：补建 / 滚动 / 结案 / 状态推进。"""
    report = scan(ledger, settings, now=now)
    station_id = report["station_id"]
    actions: dict = {"created": [], "closed": [], "updated": [],
                     "awaiting_baseline": []}

    for item in report["groups"]:
        if item["next_due"] is None:
            actions["awaiting_baseline"].append(item["group"])
            continue
        desired_due = _dt.date.fromisoformat(item["next_due"])
        desired_state = "overdue" if item["overdue_days"] > 0 else "open"
        overdue_since = ((desired_due + _dt.timedelta(days=1)).isoformat()
                         if desired_state == "overdue" else None)
        current = ledger.current_task(station_id, BATTERY_TYPE, item["group"])

        if current is not None and current["due_at"] == item["next_due"]:
            if current["state"] != desired_state:
                ledger.update_task_state(current["task_id"], desired_state,
                                         overdue_since=overdue_since)
                actions["updated"].append({"task_id": current["task_id"],
                                           "state": desired_state})
            continue

        if current is not None:
            last_done = item["last_done_at"]
            is_new_completion = False
            if last_done is not None:
                opened_at = current.get("opened_at")
                if opened_at:
                    # 「本期新完成」判定：完成时间不早于任务开启（相等=同一笔提交创建，
                    # 视为新完成）；仅当完成早于开启（周期配置变更滚期）才结案 rebased。
                    is_new_completion = (parse_rfc3339(last_done)
                                         >= parse_rfc3339(opened_at))
                else:
                    is_new_completion = True
            if not is_new_completion:
                # 周期配置变更滚期（本任务期内无新完成）：结案 rebased，不冒充 done
                final_state = "rebased"
            else:
                late = bool(_wall_date(last_done)
                            > _dt.date.fromisoformat(current["due_at"]))
                final_state = "done_late" if late else "done"
            ledger.close_task(current["task_id"], state=final_state, closed_at=iso_now())
            actions["closed"].append({"task_id": current["task_id"], "state": final_state})

        task_id = _task_id(station_id, item["group"], desired_due)
        created = ledger.upsert_task(
            task_id=task_id, station_id=station_id, record_type=BATTERY_TYPE,
            period_key=desired_due.isoformat(), due_at=desired_due.isoformat(),
            state=desired_state, overdue_since=overdue_since,
            opened_at=(now or iso_now()))
        (actions["created"] if created else actions["updated"]).append(
            {"task_id": task_id, "due": desired_due.isoformat(),
             "state": desired_state})
    return actions
