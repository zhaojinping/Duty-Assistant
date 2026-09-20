"""周期台账探针 ``cycle_status``（design.md §7.3 豁免注 / §8.3）。

纯函数：``cycle_status(ledger_view, now, registry)``。周期规则不进 create/confirm
流水线，**唯一出口是本探针**——``rules`` 里永不含周期结论。

「做过」口径（§8.3）：记录**非 voided 且存在 confirmed/archived 版本**；
correct 并存窗口不产生假「漏做」，confirmed→voided 的记录也不遮掩。

配对悬空半边（§7.3/§6）：占用后未释放的配对在本探针输出 ``verdict="due"``
的条目，悬空键在 ``detail`` 列出（规则流水线只判重复占用/悬空释放）。
"""

from __future__ import annotations

from records_kit.engine.rules import PAIRING_ACTIONS, _row_fields, _safe_moment, when_matches
from records_kit.engine.views import DONE_LIFECYCLES
from records_kit.registry.declaration import PAIRING_TYPES, parse_kv, split_expr, split_key
from records_kit.util import TimeTextError, add_period, format_rfc3339, parse_rfc3339


def probe_cycle(registry, ledger_view: dict, now_text: str, record_types=None) -> list[dict]:
    """对（指定或全部）记录类型的周期规则逐一给出 ``due / overdue / missing``；
    含 pairing 规则的类型追加悬空配对条目（§7.3 探针 detail 输出）。"""
    now = parse_rfc3339(now_text)
    rows = ledger_view.get("same_type_records", []) if isinstance(ledger_view, dict) else []
    names = list(record_types) if record_types else list(registry.record_types)
    entries: list[dict] = []
    for name in names:
        declaration = registry[name]
        for rule in declaration.cycle_rules:
            entries.append(_probe_one(declaration, rule, rows, now))
        entries.extend(_probe_pairing(declaration, rows))
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


def _probe_pairing(declaration, rows: list[dict]) -> list[dict]:
    """配对悬空半边：占用后未释放的键 → ``verdict="due"`` 条目，detail 列出（§7.3）。"""
    entries: list[dict] = []
    for rule in declaration.pipeline_rules:
        op, args = split_expr(rule.expr)
        if rule.tier != 3 or op != "pairing":
            continue
        positional, kv = parse_kv(args)
        pairing_type = positional[0] if positional else ""
        if pairing_type not in PAIRING_TYPES:
            continue
        action_field, occupy_value, release_value = PAIRING_ACTIONS[pairing_type]
        key_fields = split_key(kv.get("key", ""))

        events: dict[tuple, list[tuple]] = {}
        for row in rows:
            if not isinstance(row, dict) or row.get("lifecycle") == "voided":
                continue
            row_fields = _row_fields(row)
            key = tuple(row_fields.get(name) for name in key_fields)
            if any(value is None for value in key):
                continue
            action = row_fields.get(action_field)
            if action not in (occupy_value, release_value):
                continue
            occurred = row.get("occurred_at")
            events.setdefault(key, []).append((_safe_moment(occurred), action, occurred))

        dangling: list[str] = []
        for key, key_events in events.items():
            key_events.sort(key=lambda item: (0, item[0]) if item[0] is not None else (1, None))
            pending = 0
            last_occupy_at = None
            for moment, action, occurred_text in key_events:
                if action == occupy_value:
                    pending += 1
                    last_occupy_at = occurred_text
                elif pending > 0:
                    pending -= 1
            if pending > 0:
                undone = "未拆除" if pairing_type == "grounding" else "未投回"
                since = f"（{last_occupy_at} 起）" if isinstance(last_occupy_at, str) else ""
                dangling.append(f"{'+'.join(str(value) for value in key)}{since}{undone}")
        if dangling:
            entries.append(
                {
                    "record_type": declaration.record_type,
                    "verdict": "due",
                    "due_at": None,
                    "overdue_since": None,
                    "detail": f"配对悬空（{pairing_type}）：{'；'.join(dangling)}",
                }
            )
    return entries
