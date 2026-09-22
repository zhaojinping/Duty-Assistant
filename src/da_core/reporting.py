"""月报与按时率：只读汇总自权威账本（任务达成 / 测量 / 更正作废 / 触达）。

另含**周期收口推送**（``push_monthly_report``）：每月固定窗口把当月月报正文
（群消息）+ 入口卡（X-APM 机器人）发到提醒群；按自然月幂等。
"""

from __future__ import annotations

import datetime as _dt

from da_core import entry_card, outbox
from da_core.clock import iso_now
from da_core.settings import Settings

REPORT_PUSH_DAY = 16     # 收口推送起始日（每月 16 日）
REPORT_PUSH_WINDOW = 3   # 推送窗口 16–18 日（失败次日自动重试；窗口外不触发）


def _month_bounds(month: str) -> tuple[str, str]:
    year, mon = int(month[:4]), int(month[5:7])
    nxt = f"{year + 1}-01" if mon == 12 else f"{year}-{mon + 1:02d}"
    return f"{month}-01", f"{nxt}-01"


def _group_of(task_id: str) -> str:
    parts = (task_id or "").split("|")
    return parts[1] if len(parts) > 1 else (task_id or "?")


def _rate(part: int, whole: int) -> str:
    return "—" if not whole else f"{part / whole * 100:.0f}%"


def monthly_report(ledger, settings: Settings, month: str | None = None,
                   now: str | None = None) -> dict:
    """月度汇总。``month`` 形如 ``2026-09``，缺省=当前月；``now`` 可注入。"""
    now = now or iso_now()
    month = month or now[:7]
    conn = ledger.conn

    task_rows = conn.execute(
        "SELECT task_id, state FROM tasks WHERE due_at LIKE ?",
        (month + "%",)).fetchall()
    record_rows = conn.execute(
        "SELECT group_label, lifecycle FROM records WHERE occurred_at LIKE ?",
        (month + "%",)).fetchall()
    corrections = conn.execute(
        "SELECT COUNT(*) FROM record_versions WHERE op = 'correct' AND created_at LIKE ?",
        (month + "%",)).fetchone()[0]
    voids = conn.execute(
        "SELECT COUNT(*) FROM records WHERE voided_at LIKE ?",
        (month + "%",)).fetchone()[0]
    alerts = conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE sent_at LIKE ?",
        (month + "%",)).fetchone()[0]
    deferrals = conn.execute(
        "SELECT COUNT(*) FROM deferrals WHERE created_at LIKE ?",
        (month + "%",)).fetchone()[0]

    groups: dict[str, dict] = {}

    def _bucket(name: str) -> dict:
        return groups.setdefault(name, {
            "group": name, "due_total": 0, "done": 0, "done_late": 0,
            "overdue": 0, "open": 0, "records": 0, "voided": 0})

    for task_id, state in task_rows:
        if state == "rebased":  # 配置滚期产物：不计入达成统计
            continue
        bucket = _bucket(_group_of(task_id))
        bucket["due_total"] += 1
        if state in ("done", "done_late", "overdue", "open"):
            bucket[state] += 1

    for group_label, lifecycle in record_rows:
        bucket = _bucket(group_label or "（未知组）")
        bucket["records"] += 1
        if lifecycle == "voided":
            bucket["voided"] += 1

    for bucket in groups.values():
        bucket["on_time_rate"] = _rate(bucket["done"], bucket["due_total"])

    ordered = sorted(groups.values(), key=lambda item: str(item["group"]))
    totals = {
        "due_total": sum(b["due_total"] for b in ordered),
        "done": sum(b["done"] for b in ordered),
        "done_late": sum(b["done_late"] for b in ordered),
        "overdue": sum(b["overdue"] for b in ordered),
        "open": sum(b["open"] for b in ordered),
        "records": sum(b["records"] for b in ordered),
        "voided": sum(b["voided"] for b in ordered),
        "corrections": corrections,
        "voids": voids,
        "alerts": alerts,
        "deferrals": deferrals,
    }
    totals["on_time_rate"] = _rate(totals["done"], totals["due_total"])

    lines = [f"# 蓄电池电压测量月报 · {month}",
             "统计口径：任务按 due 归月；测量按 occurred_at 归月；只读汇总。",
             "",
             "| 组别 | 应做 | 按时 | 迟到 | 逾期未做 | 按时率 |",
             "| --- | ---: | ---: | ---: | ---: | ---: |"]
    for bucket in ordered:
        lines.append("| {group} | {due_total} | {done} | {done_late} | {overdue} "
                     "| {on_time_rate} |".format(**bucket))
    lines += [
        "",
        (f"**合计**：应做 {totals['due_total']} · 按时 {totals['done']} · "
         f"迟到 {totals['done_late']} · 逾期未做 {totals['overdue']} · "
         f"按时率 {totals['on_time_rate']}"),
        "",
        (f"本月测量记录 {totals['records']} 条 · 更正 {totals['corrections']} 次 · "
         f"作废 {totals['voids']} 条 · 催办触达 {totals['alerts']} 次 · "
         f"延期登记 {totals['deferrals']} 次"),
        "",
        "——AI助手",
    ]

    return {"month": month, "generated_at": now, "groups": ordered,
            "totals": totals, "text": "\n".join(lines)}


def push_monthly_report(ledger, settings: Settings, *, now: str | None = None,
                        runner=None, poster=None, dry_run: bool = False) -> dict:
    """周期收口推送：每月 16–18 日把当月月报 + 入口卡发到提醒群。

    - 触发窗口：``[16, 18]`` 日（失败次日自动重试；窗口外不触发）。
    - 发件对象：触点配置 ``reminder_group``；月报正文走群消息，入口卡走 X-APM 机器人。
    - 幂等标记：config_params.report_last_pushed = 'YYYY-MM'（月报正文发送成功后落）。
    """
    now = now or iso_now()
    today = _dt.date.fromisoformat(now[:10])
    if not (REPORT_PUSH_DAY <= today.day < REPORT_PUSH_DAY + REPORT_PUSH_WINDOW):
        return {"pushed": False, "reason": "not-in-window", "day": today.day}
    month = now[:7]
    if ledger.get_config_param("report_last_pushed") == month:
        return {"pushed": False, "reason": "already-pushed", "month": month}

    station_id = (settings.station or {}).get("station_id")
    contacts = ledger.get_contacts(station_id) if station_id else {}
    target = contacts.get(outbox.ROLE_GROUP)
    if not target:
        return {"pushed": False, "reason": "no-target", "month": month}

    report = monthly_report(ledger, settings, month=month, now=now)
    if dry_run:
        return {"pushed": False, "reason": "dry-run", "month": month,
                "text_preview": report["text"][:200]}

    group_result = outbox.send_group(target, report["text"], runner=runner)
    try:
        card_result = entry_card.send_entry_card(target="group", runner=runner,
                                                 poster=poster)
    except Exception as exc:  # noqa: BLE001 — 卡片失败不阻断月报正文
        card_result = {"sent": False, "detail": f"error: {exc}"}
    if group_result["ok"]:
        ledger.set_config_param("report_last_pushed", month, updated_by="da_daily")
    return {"pushed": group_result["ok"], "month": month, "group": group_result,
            "card": card_result}
