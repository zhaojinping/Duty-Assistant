"""表内规则判定：T1（单字段）与 T2（表内派生）（design.md §7.3）。

- 周期规则（``kind="cycle"``）**不进本流水线**，唯一出口是探针（§7.3 豁免注）；
- ``when`` 当前仅支持字段等值匹配（三类扩展文法随 M2）；
- 结果条目形状：``{rule_id, kind, tier, verdict, threshold, level, detail}``；
  ``verdict`` = ``pass | violation | skipped``——``skipped`` 表示操作数缺省/不可算
  （如可选字段未填、除零），**不静默通过**。
"""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field

from records_kit.errors import E_ACTION_CODE, reject
from records_kit.registry.declaration import ITEMS_KEY, Declaration, RuleSpec, resolve_path, split_expr
from records_kit.util import TimeTextError, days_between, parse_rfc3339

MISSING = object()

# deviation 的基准口径（§7.3 样例 ``deviation:mean,5%``）：均值/最小/最大/末位
DEVIATION_BASES = ("mean", "min", "max", "last")

# 数值比较算子 → 判定函数
_COMPARATORS = {
    "gt": lambda value, bound: value > bound,
    "gte": lambda value, bound: value >= bound,
    "lt": lambda value, bound: value < bound,
    "lte": lambda value, bound: value <= bound,
}
_COMPARATOR_TEXT = {"gt": ">", "gte": "≥", "lt": "<", "lte": "≤"}


@dataclass
class RulesReport:
    """规则判定结果：协议条目 + 告警候选（供告警指纹）+ 建议动作。"""

    entries: list[dict] = dataclass_field(default_factory=list)
    alarm_candidates: list[tuple[str, str]] = dataclass_field(default_factory=list)
    actions: list[dict] = dataclass_field(default_factory=list)

    def extend(self, other: "RulesReport") -> None:
        self.entries.extend(other.entries)
        self.alarm_candidates.extend(other.alarm_candidates)
        self.actions.extend(other.actions)


def when_matches(declaration: Declaration, fields: dict, when: dict) -> bool:
    """``when`` 等值匹配：顶层字段取记录字段，条目字段取任一条目命中（§7.3）。"""
    if not when:
        return True
    for path, expected in when.items():
        resolved = resolve_path(declaration, path)
        if resolved is None:
            return False
        scope, spec = resolved
        if spec is None:
            return False
        if scope == "items":
            items = fields.get(ITEMS_KEY)
            if not isinstance(items, list):
                return False
            if not any(isinstance(item, dict) and item.get(spec.key) == expected for item in items):
                return False
        elif fields.get(spec.key) != expected:
            return False
    return True


def _operand(declaration: Declaration, fields: dict, item: dict | None, path: str):
    resolved = resolve_path(declaration, path)
    if resolved is None:
        return MISSING
    scope, spec = resolved
    if spec is None:
        return MISSING
    if scope == "items":
        if item is None:
            return MISSING
        return item.get(spec.key, MISSING)
    return fields.get(spec.key, MISSING)


def _number(value) -> float | None:
    if value is MISSING or value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _pct(text: str) -> float | None:
    body = text[:-1] if text.endswith("%") else text
    try:
        return float(body)
    except ValueError:
        return None


def _candidate_label(index: int | None, key_field: str | None, key_value) -> str:
    if index is None:
        return "记录"
    return f"{ITEMS_KEY}[{index}].{key_field}={key_value}"


def _entry(rule: RuleSpec, verdict: str, level: str, detail: str) -> dict:
    return {
        "rule_id": rule.id,
        "kind": rule.kind,
        "tier": rule.tier,
        "verdict": verdict,
        "threshold": rule.threshold,
        "level": level,
        "detail": detail,
    }


def evaluate_rules(declaration: Declaration, fields: dict) -> RulesReport:
    """按声明顺序判定全部 T1/T2 规则。"""
    report = RulesReport()
    for rule in declaration.pipeline_rules:
        if not when_matches(declaration, fields, rule.when):
            continue
        _evaluate_rule(declaration, rule, fields, report)
    return report


def _targets(declaration: Declaration, rule: RuleSpec, fields: dict):
    """把 target 展开成待判定候选：``[(label, value, item, values_of_field)]``。"""
    resolved = resolve_path(declaration, rule.target or "")
    if resolved is None:
        return []
    scope, spec = resolved
    if spec is None:
        return []
    if scope == "items":
        items = fields.get(ITEMS_KEY)
        if not isinstance(items, list) or not items:
            return []
        key_field = declaration.items.key_field if declaration.items else None
        series = [item.get(spec.key) for item in items if isinstance(item, dict)]
        return [
            (_candidate_label(index, key_field, item.get(key_field)), item.get(spec.key, MISSING), item, series)
            for index, item in enumerate(items)
            if isinstance(item, dict)
        ]
    return [("记录", fields.get(spec.key, MISSING), None, [fields.get(spec.key)])]


def _evaluate_rule(declaration: Declaration, rule: RuleSpec, fields: dict, report: RulesReport) -> None:
    op, args = split_expr(rule.expr)
    targets = _targets(declaration, rule, fields)
    if not targets:
        report.entries.append(_entry(rule, "skipped", "info", f"{rule.expr}：无可用条目/字段"))
        return
    handled = False
    for label, value, item, series in targets:
        outcome = _judge(declaration, rule, op, args, fields, item, value, series)
        if outcome is None:
            continue
        handled = True
        verdict, detail = outcome
        level = rule.level if verdict == "violation" else "info"
        report.entries.append(_entry(rule, verdict, level, detail))
        if verdict == "violation":
            if rule.level == "alarm":
                report.alarm_candidates.append((rule.id, label))
            if rule.action:
                if rule.action not in declaration.action_codes:
                    raise reject(rule.id, E_ACTION_CODE, f"建议动作码不在声明的 action_codes 内：{rule.action}")
                if all(action["code"] != rule.action for action in report.actions):
                    report.actions.append({"code": rule.action, "text": f"{rule.id}：{detail}"})
    if not handled:
        report.entries.append(
            _entry(rule, "skipped", "info", f"{rule.expr}：操作数缺省（记录未提供所需字段）")
        )


def _judge(declaration, rule, op, args, fields, item, value, series):
    """返回 ``(verdict, detail)``；操作数不可得返回 ``None``（由调用方记 skipped）。"""
    if op in _COMPARATORS:
        number = _number(value)
        if number is None:
            return None
        bound = float(args[0])
        ok = _COMPARATORS[op](number, bound)
        return (
            "pass" if ok else "violation",
            f"{_label_text(declaration, item)} 取值 {number} {_COMPARATOR_TEXT[op]} {bound}：{'通过' if ok else '越限'}",
        )
    if op == "band":
        number = _number(value)
        if number is None:
            return None
        low, high = float(args[0]), float(args[1])
        ok = low <= number <= high
        return (
            "pass" if ok else "violation",
            f"{_label_text(declaration, item)} 取值 {number}，带 [{low}, {high}]：{'通过' if ok else '越界'}",
        )
    if op == "ratio":
        numerator = _number(_operand(declaration, fields, item, args[0]))
        denominator = _number(_operand(declaration, fields, item, args[1]))
        if numerator is None or denominator is None:
            return None
        if denominator == 0:
            return "skipped", f"ratio 分母为 0（{args[1]}），不可计算"
        low, high = float(args[2]), float(args[3])
        ratio = numerator / denominator
        ok = low <= ratio <= high
        return (
            "pass" if ok else "violation",
            f"{_label_text(declaration, item)} 比值 {ratio:.4f}，带 [{low}, {high}]：{'通过' if ok else '越界'}",
        )
    if op == "deviation":
        number = _number(value)
        if number is None:
            return None
        base_spec, pct_text = args[0], args[1]
        limit = _pct(pct_text)
        if limit is None:
            return None
        if base_spec in DEVIATION_BASES:
            numbers = [num for num in (_number(raw) for raw in series) if num is not None]
            if len(numbers) < 2:
                return "skipped", f"deviation 基准 {base_spec} 需要 ≥2 个条目取值"
            base = {
                "mean": sum(numbers) / len(numbers),
                "min": min(numbers),
                "max": max(numbers),
                "last": numbers[-1],
            }[base_spec]
        else:
            base = _number(_operand(declaration, fields, item, base_spec))
            if base is None:
                return None
        if base == 0:
            return "skipped", f"deviation 基准为 0（{base_spec}），不可计算相对偏差"
        deviation = abs(number - base) / abs(base) * 100
        ok = deviation <= limit
        return (
            "pass" if ok else "violation",
            f"{_label_text(declaration, item)} 取值 {number}，基准 {base_spec}={round(base, 4)}，"
            f"偏差 {round(deviation, 4)}% ≤ {limit}%：{'通过' if ok else '越界'}",
        )
    if op == "diff":
        left = _number(_operand(declaration, fields, item, args[0]))
        right = _number(_operand(declaration, fields, item, args[1]))
        if left is None or right is None:
            return None
        comparator, _, bound_text = args[2].partition(":")
        bound = float(bound_text)
        difference = left - right
        ok = difference >= bound if comparator == "ge" else difference <= bound
        return (
            "pass" if ok else "violation",
            f"{_label_text(declaration, item)} {args[0]}−{args[1]}={round(difference, 4)} "
            f"{'≥' if comparator == 'ge' else '≤'} {bound}：{'通过' if ok else '越限'}",
        )
    if op == "date_diff":
        start = _operand(declaration, fields, item, args[0])
        end = _operand(declaration, fields, item, args[1])
        if not isinstance(start, str) or not isinstance(end, str):
            return None
        try:
            span = days_between(parse_rfc3339(start), parse_rfc3339(end))
        except TimeTextError:
            return None
        comparator, _, bound_text = args[2].partition(":")
        bound = float(bound_text[:-1])
        ok = span >= bound if comparator == "ge" else span <= bound
        return (
            "pass" if ok else "violation",
            f"{_label_text(declaration, item)} {args[0]}→{args[1]} 间隔 {round(span, 4)} 天 "
            f"{'≥' if comparator == 'ge' else '≤'} {bound}：{'通过' if ok else '越限'}",
        )
    return None


def _label_text(declaration: Declaration, item: dict | None) -> str:
    if item is None or declaration.items is None:
        return "记录"
    key_field = declaration.items.key_field
    return f"条目 {key_field}={item.get(key_field)}"
