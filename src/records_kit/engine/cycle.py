"""周期台账探针 ``cycle_status``（design.md §7.3 豁免注 / §8.3）。

纯函数：``cycle_status(ledger_view, now, registry)``。周期规则不进 create/confirm
流水线，**唯一出口是本探针**——``rules`` 里永不含周期结论。

「做过」口径（§8.3）：记录**非 voided 且存在 confirmed/archived 版本**；
correct 并存窗口不产生假「漏做」，confirmed→voided 的记录也不遮掩。
"""

from __future__ import annotations

from records_kit.engine.rules import when_matches
from records_kit.engine.views import DONE_LIFECYCLES
from records_kit.util import TimeTextError, add_period, format_rfc3339, parse_rfc3339


def probe_cycle(registry, ledger_view: dict, now_text: str, record_types=None) -> list[dict]:
    """对（指定或全部）记录类型的周期规则逐一给出 ``due / overdue / missing``。"""
    now = parse_rfc3339(now_text)
    rows = ledger_view.get("same_type_records", []) if isinstance(ledger_view, dict) else []
    names = list(record_types) if record_types else list(registry.record_types)
    entries: list[dict] = []
    for name in names:
        declaration = registry[name]
        for rule in declaration.cycle_rules:
            entries.append(_probe_one(declaration, rule, rows, now))
    return entries


def _probe_one(declaration, rule, rows: list[dict], now) -> dict:
    completed: list[tuple] = []
    unparsable = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        # 版本行语义：voided 为吸收态（行 lifecycle 已按「先判 voided」归并，§8.3）
        if row.get("lifecycle") not in DONE_LIFECYCLES:
            continue
        fields = row.get("fields") or {}
        if not when_matches(declaration, fields, rule.when):
            continue
        occurred = row.get("occurred_at")
        if not isinstance(occurred, str):
            unparsable += 1
            continue
        try:
            completed.append((parse_rfc3339(occurred), occurred))
        except TimeTextError:
            unparsable += 1
    notes = f"；{unparsable} 行缺可解析时间，未计入" if unparsable else ""

    if completed:
        moment, text = max(completed, key=lambda pair: pair[0])
        due = add_period(moment, rule.expr)
        overdue = now > due
        return {
            "record_type": declaration.record_type,
            "verdict": "overdue" if overdue else "due",
            "due_at": format_rfc3339(due),
            "overdue_since": format_rfc3339(due) if overdue else None,
            "detail": f"周期 {rule.expr}；最后完成 {text}；下次应做 {format_rfc3339(due)}{notes}",
        }

    baseline = rule.cycle_baseline
    baseline_moment = None
    if isinstance(baseline, str):
        try:
            baseline_moment = parse_rfc3339(baseline)
        except TimeTextError:
            baseline_moment = None
    if baseline_moment is not None:
        due = add_period(baseline_moment, rule.expr)
        overdue = now > due
        return {
            "record_type": declaration.record_type,
            "verdict": "missing",
            "due_at": format_rfc3339(due),
            "overdue_since": format_rfc3339(due) if overdue else None,
            "detail": (
                f"无已完成记录（漏做）；周期 {rule.expr}，起算 {baseline}，应做日 {format_rfc3339(due)}{notes}"
            ),
        }
    return {
        "record_type": declaration.record_type,
        "verdict": "missing",
        "due_at": None,
        "overdue_since": None,
        "detail": f"无已完成记录（漏做）；未配置 cycle_baseline（起算日待现场），不判到期日{notes}",
    }
