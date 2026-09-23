"""表内规则判定：T1（单字段）与 T2（表内派生）（design.md §7.3）。

- 周期规则（``kind="cycle"``）**不进本流水线**，唯一出口是探针（§7.3 豁免注）；
- ``when`` 当前仅支持字段等值匹配（三类扩展文法随 M2）；
- 结果条目形状：``{rule_id, kind, tier, verdict, threshold, level, detail}``；
  ``verdict`` = ``pass | violation | skipped``——``skipped`` 表示操作数缺省/不可算
  （如可选字段未填、除零），**不静默通过**。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field as dataclass_field

from records_kit.errors import E_ACTION_CODE, E_BASELINE_MISSING, reject
from records_kit.registry.declaration import (
    ITEMS_KEY,
    PAIRING_TYPES,
    Declaration,
    RuleSpec,
    parse_kv,
    resolve_path,
    split_expr,
    split_key,
    split_when_value,
)
from records_kit.util import TimeTextError, days_between, parse_rfc3339

MISSING = object()

# deviation 的基准口径（§7.3 样例 ``deviation:mean,5%``）：均值/最小/最大/末位
DEVIATION_BASES = ("mean", "min", "max", "last")

# pairing 类型 → (动作字段, 占用值, 释放值)（§7.3：接地线装设/拆除、保护投/退）
PAIRING_ACTIONS = {
    "grounding": ("action", "装设", "拆除"),
    "protection": ("action", "投", "退"),
}

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


def _when_holds(value, op: str, operands: tuple) -> bool:
    """单条 ``when`` 条件的成立判定（文法扩展提案 §3.3/§3.4）。

    空值口径：字段未填（缺键或 None）→ **条件不成立**——否定算子不改这条，
    不把「未填」当成「非雷雨」，避免旧数据缺列直接触发误报。
    """
    if value is MISSING or value is None:
        return False
    if op == "eq":
        return value == operands[0]
    if op == "ne":
        return value != operands[0]
    if op == "in":
        return value in operands
    if op == "not_in":
        return value not in operands
    return False


def when_matches(declaration: Declaration, fields: dict, when: dict) -> bool:
    """``when`` 命中判定（§7.3 + 提案 §3）。

    顶层字段取记录字段；条目字段取**任一条目**命中（存在量词，与现状一致）；
    多条件为 AND；空表恒命中。
    """
    if not when:
        return True
    for path, expected in when.items():
        resolved = resolve_path(declaration, path)
        if resolved is None:
            return False
        scope, spec = resolved
        if spec is None:
            return False
        op, operands = split_when_value(expected)
        if scope == "items":
            items = fields.get(ITEMS_KEY)
            if not isinstance(items, list):
                return False
            if not any(
                isinstance(item, dict) and _when_holds(item.get(spec.key, MISSING), op, operands)
                for item in items
            ):
                return False
        elif not _when_holds(fields.get(spec.key, MISSING), op, operands):
            return False
    return True


def _when_actual(declaration: Declaration, fields: dict, when: dict) -> str:
    """``when`` 引用字段的实际取值（条件型规则的 detail 用，供审计回看）。"""
    shown: list[str] = []
    for path in when:
        resolved = resolve_path(declaration, path)
        if resolved is None or resolved[1] is None:
            continue
        scope, spec = resolved
        if scope == "items":
            items = fields.get(ITEMS_KEY)
            values = (
                [item.get(spec.key) for item in items if isinstance(item, dict)]
                if isinstance(items, list)
                else []
            )
            shown.append(f"{path}={values}")
        else:
            shown.append(f"{path}={fields.get(spec.key)}")
    return "、".join(shown)


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


def evaluate_rules(
    declaration: Declaration,
    fields: dict,
    *,
    ledger_view: dict | None = None,
    baselines: list | None = None,
    occurred_at: str | None = None,
    current_record_uid: str | None = None,
) -> RulesReport:
    """按声明顺序判定全部 T1/T2/T3 与条件型规则。

    T3（台账/跨记录）需要额外事实（§7.3）：``ledger_view``（同类型台账与关联
    记录）、``baselines``（外部基线）、当前记录的 ``occurred_at``（排序与时间判定）；
    ``current_record_uid`` 为本次操作的记录 uid（可选，供 monotonic 排除自身 correct
    链，提案 §5.3-1）——create 的新记录不在视图内，缺省即可。
    """
    report = RulesReport()
    for rule in declaration.pipeline_rules:
        if rule.kind == "condition":
            _evaluate_condition(declaration, rule, fields, report)  # 条件型：命中与否都出条目
            continue
        if not when_matches(declaration, fields, rule.when):
            continue
        if rule.tier == 3:
            _evaluate_t3(
                declaration, rule, fields, report, ledger_view, baselines, occurred_at, current_record_uid
            )
        elif split_expr(rule.expr)[0] == "thermal_grade":
            _evaluate_thermal_grade(declaration, rule, fields, report)  # 分组算子：每组一条，不逐条目
        else:
            _evaluate_rule(declaration, rule, fields, report)
    return report


def _append_violation_effects(declaration: Declaration, rule: RuleSpec, level: str, label: str, detail: str, report: RulesReport) -> None:
    """violation 的附带效应：alarm 级追加告警候选；声明了 action 则追加建议动作（去重）。"""
    if level == "alarm":
        report.alarm_candidates.append((rule.id, label))
    if rule.action:
        if rule.action not in declaration.action_codes:
            raise reject(rule.id, E_ACTION_CODE, f"建议动作码不在声明的 action_codes 内：{rule.action}")
        if all(action["code"] != rule.action for action in report.actions):
            report.actions.append({"code": rule.action, "text": f"{rule.id}：{detail}"})


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


def _evaluate_condition(declaration: Declaration, rule: RuleSpec, fields: dict, report: RulesReport) -> None:
    """条件型规则判定（提案 §4.2）：``when`` 命中 → violation；未命中 → **pass**。

    与 ``limit`` 不同，条件型规则**始终出条目**——审计要看得见「查过且通过」。
    ``threshold`` 取 ``when`` 的规范化文本（``RuleSpec.threshold``），命中时 detail 附实际取值。
    """
    matched = when_matches(declaration, fields, rule.when)
    actual = _when_actual(declaration, fields, rule.when)
    if not matched:
        report.entries.append(_entry(rule, "pass", "info", f"条件未命中（{actual}）：查过且通过"))
        return
    detail = f"条件命中（{actual}）：{rule.threshold}"
    report.entries.append(_entry(rule, "violation", rule.level, detail))
    if rule.level == "alarm":
        report.alarm_candidates.append((rule.id, "记录"))
    if rule.action:
        if rule.action not in declaration.action_codes:
            raise reject(rule.id, E_ACTION_CODE, f"建议动作码不在声明的 action_codes 内：{rule.action}")
        if all(action["code"] != rule.action for action in report.actions):
            report.actions.append({"code": rule.action, "text": f"{rule.id}：{detail}"})


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
        operator = "≤" if ok else ">"
        return (
            "pass" if ok else "violation",
            f"{_label_text(declaration, item)} 取值 {number}，基准 {base_spec}={round(base, 4)}，"
            f"偏差 {round(deviation, 4)}% {operator} {limit}%：{'通过' if ok else '越界'}",
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


# ---------------------------------------------------------------- T2 分组算子：thermal_grade（测温分级）

# 缺陷等级顺序（低 → 高）：人工判定与引擎判定的比较基准
THERMAL_GRADES = ("正常", "一般缺陷", "严重缺陷", "危急缺陷")
# 电流致热型通用判据适用的致热类型：空 / 电流致热 才分级，其余类型输出 skipped
THERMAL_CURRENT_HEAT = ("电流致热",)
# 条目上的可选字段名：致热类型 / 人工缺陷判定（红外测温声明约定）
THERMAL_HEAT_TYPE_FIELD = "heat_type"
THERMAL_MANUAL_GRADE_FIELD = "defect_grade"


def _thermal_grade_of(hot: float, diff_k: float | None, delta: float | None, kv: dict) -> str:
    """按顺序命中：危急 → 严重 → 一般 → 正常；δt 不可算时只按温度阈值判。"""
    critical_temp = float(kv["critical_temp"])
    critical_delta = float(kv["critical_delta"])
    severe_temp = float(kv["severe_temp"])
    severe_delta = float(kv["severe_delta"])
    general_diff = float(kv["general_diff"])
    if hot > critical_temp or (delta is not None and delta >= critical_delta):
        return THERMAL_GRADES[3]
    if hot > severe_temp or (delta is not None and delta >= severe_delta):
        return THERMAL_GRADES[2]
    if diff_k is not None and diff_k > general_diff:
        return THERMAL_GRADES[1]
    return THERMAL_GRADES[0]


def _evaluate_thermal_grade(declaration: Declaration, rule: RuleSpec, fields: dict, report: RulesReport) -> None:
    """测温分级（T2）：按 ``group=`` 字段分组，组内最高温为热点、最低温为基准相。

    每组只对热点条目出一条 entry；``δt = (hot − normal) / (hot − env) × 100``，
    组内单条目或 ``hot − env <= 0`` 时 δt 不可算，仅按温度阈值判。
    致热类型非空且非「电流致热」→ ``skipped``（设备类别判据本版不判）。
    entry.level 按等级覆盖 rule.level：危急/严重 → alarm，一般 → warn，正常 → pass/info。
    """
    _, args = split_expr(rule.expr)
    _, kv = parse_kv(args)
    resolved = resolve_path(declaration, rule.target or "")
    if resolved is None or resolved[0] != "items" or resolved[1] is None or declaration.items is None:
        report.entries.append(_entry(rule, "skipped", "info", f"{rule.expr}：target 不是条目字段"))
        return
    measured_field = resolved[1].key
    group_field = kv["group"].split(".", 1)[1] if kv["group"].startswith(ITEMS_KEY + ".") else kv["group"]
    items = fields.get(ITEMS_KEY)
    if not isinstance(items, list) or not items:
        report.entries.append(_entry(rule, "skipped", "info", f"{rule.expr}：无可用条目/字段"))
        return

    groups: dict = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        temperature = _number(item.get(measured_field, MISSING))
        if temperature is None:
            continue
        groups.setdefault(item.get(group_field), []).append((temperature, item))
    if not groups:
        report.entries.append(
            _entry(rule, "skipped", "info", f"{rule.expr}：操作数缺省（记录未提供所需字段）")
        )
        return

    env = _number(fields.get(kv["env"], MISSING))
    for group_value, members in groups.items():
        hot_temp, hot = max(members, key=lambda pair: pair[0])
        label = _label_text(declaration, hot)
        heat_type = hot.get(THERMAL_HEAT_TYPE_FIELD)
        if heat_type not in (None, "") and heat_type not in THERMAL_CURRENT_HEAT:
            report.entries.append(
                _entry(
                    rule,
                    "skipped",
                    "info",
                    f"设备 {group_value}：热点 {label} 实测 {hot_temp}℃；"
                    f"{heat_type} 分级依设备类别判据，本版不判，以人工判定为准",
                )
            )
            continue
        normal_temp = min(members, key=lambda pair: pair[0])[0] if len(members) >= 2 else None
        diff_k = hot_temp - normal_temp if normal_temp is not None else None
        delta = None
        if diff_k is not None and env is not None and hot_temp - env > 0:
            delta = diff_k / (hot_temp - env) * 100
        grade = _thermal_grade_of(hot_temp, diff_k, delta, kv)

        parts = [f"设备 {group_value}：热点 {label} 实测 {hot_temp}℃"]
        if normal_temp is not None:
            parts.append(f"基准相 {normal_temp}℃")
            parts.append(f"温差 {round(diff_k, 2)}K")
        if delta is not None:
            parts.append(f"δt {round(delta, 2)}%")
        else:
            parts.append("无基准相/温升≤0，δt 未算")
        parts.append(f"等级={grade}")
        detail = "，".join(parts)
        manual = hot.get(THERMAL_MANUAL_GRADE_FIELD)
        if manual in THERMAL_GRADES and THERMAL_GRADES.index(manual) < THERMAL_GRADES.index(grade):
            detail += f"；人工判定「{manual}」低于引擎判定「{grade}」"

        if grade == THERMAL_GRADES[0]:
            report.entries.append(_entry(rule, "pass", "info", detail))
            continue
        level = "alarm" if grade in THERMAL_GRADES[2:] else "warn"
        report.entries.append(_entry(rule, "violation", level, detail))
        _append_violation_effects(declaration, rule, level, label, detail, report)


# ---------------------------------------------------------------- T3（台账/跨记录）


def _row_fields(row: dict) -> dict:
    """T3 取值源：``confirmed_fields`` 优先（G1 拍板：T3 默认取最后已确认版）。"""
    confirmed = row.get("confirmed_fields")
    if isinstance(confirmed, dict) and confirmed:
        return confirmed
    return row.get("fields") or {}


def _active_rows(ledger_view: dict | None) -> list[dict]:
    """同类型台账的非 voided 行（voided 是吸收态，不参与 T3 判定）。"""
    if not isinstance(ledger_view, dict):
        return []
    rows = ledger_view.get("same_type_records")
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict) and row.get("lifecycle") != "voided"]


def _find_baseline(baselines: list | None, ref: str) -> dict | None:
    if not isinstance(baselines, list):
        return None
    for item in baselines:
        if isinstance(item, dict) and item.get("ref") == ref:
            return item
    return None


def _safe_moment(text: str | None):
    """时间文本 → datetime；缺省/不可解析返回 ``None``（调用方按不可判处理）。"""
    try:
        return parse_rfc3339(text) if isinstance(text, str) else None
    except TimeTextError:
        return None


_SEQ_RE = re.compile(r"(\d+)\s*$")


def _extract_seq(text) -> int | None:
    """编号序号：取编号文本末尾的数字段（编号段解析格式待现场，GAP-E4）。"""
    if not isinstance(text, str):
        return None
    match = _SEQ_RE.search(text)
    return int(match.group(1)) if match else None


def _reset_window(occurred_at: str | None, reset: str | None) -> str | None:
    """continuity 复位窗口：``monthly``=同月、``yearly``=同年、缺省=全局不复位。"""
    if reset == "monthly" and isinstance(occurred_at, str) and len(occurred_at) >= 7:
        return occurred_at[:7]
    if reset == "yearly" and isinstance(occurred_at, str) and len(occurred_at) >= 4:
        return occurred_at[:4]
    return None


def _evaluate_t3(declaration, rule, fields, report, ledger_view, baselines, occurred_at, current_record_uid=None) -> None:
    """T3 规则判定：单一算子、单一结论条目（§7.3 归层原则）。"""
    op, _ = split_expr(rule.expr)
    if op == "monotonic":
        # 唯一需要「当前记录 uid」的 T3 算子（排除自身 correct 链，提案 §5.3-1），单独分派
        outcome = _judge_monotonic(
            declaration, rule, fields, ledger_view, baselines, occurred_at, current_record_uid
        )
    else:
        judge = _T3_JUDGES.get(op)
        if judge is None:
            report.entries.append(_entry(rule, "skipped", "info", f"{rule.expr}：未知 T3 算子"))
            return
        outcome = judge(declaration, rule, fields, ledger_view, baselines, occurred_at)
    if outcome is None:
        report.entries.append(
            _entry(rule, "skipped", "info", f"{rule.expr}：操作数缺省（T3 需要台账/基线/时间事实）")
        )
        return
    verdict, detail = outcome
    level = rule.level if verdict == "violation" else "info"
    report.entries.append(_entry(rule, verdict, level, detail))
    if verdict == "violation":
        if rule.level == "alarm":
            report.alarm_candidates.append((rule.id, "记录"))
        if rule.action:
            if rule.action not in declaration.action_codes:
                raise reject(rule.id, E_ACTION_CODE, f"建议动作码不在声明的 action_codes 内：{rule.action}")
            if all(action["code"] != rule.action for action in report.actions):
                report.actions.append({"code": rule.action, "text": f"{rule.id}：{detail}"})


def _judge_continuity(declaration, rule, fields, ledger_view, baselines, occurred_at):
    """两票编号连续性：仅判当前记录所在组+复位窗口内的序号无跳号（§7.3 / GAP-E4）。"""
    _, args = split_expr(rule.expr)
    positional, kv = parse_kv(args)
    no_field = positional[0]
    group_field = kv.get("group")
    reset = kv.get("reset")

    current_group = fields.get(group_field) if group_field else None
    window = _reset_window(occurred_at, reset)
    if reset is not None and window is None:
        return None

    seqs: list[int] = []
    for row in _active_rows(ledger_view):
        row_fields = _row_fields(row)
        row_group = row_fields.get(group_field) if group_field else None
        if row_group != current_group:
            continue
        if _reset_window(row.get("occurred_at"), reset) != window:
            continue
        seq = _extract_seq(row_fields.get(no_field))
        if seq is not None:
            seqs.append(seq)
    current_seq = _extract_seq(fields.get(no_field))
    if current_seq is not None:
        seqs.append(current_seq)

    ordered = sorted(set(seqs))
    gaps = [f"{low}→{high}" for low, high in zip(ordered, ordered[1:]) if high - low > 1]
    if gaps:
        return "violation", f"编号连续性：出现跳号 {', '.join(gaps)}"
    return "pass", "编号连续性：无跳号"


def _judge_pairing(declaration, rule, fields, ledger_view, baselines, occurred_at):
    """配对：重复占用告警 + 悬空释放告警（§7.3；悬空占用半边归探针 detail）。"""
    _, args = split_expr(rule.expr)
    positional, kv = parse_kv(args)
    pairing_type = positional[0]
    if pairing_type not in PAIRING_TYPES:
        return None
    action_field, occupy_value, release_value = PAIRING_ACTIONS[pairing_type]
    key_fields = split_key(kv["key"])

    current_key = tuple(fields.get(name) for name in key_fields)
    if any(value is None for value in current_key):
        return None
    current_action = fields.get(action_field)
    if current_action not in (occupy_value, release_value):
        return None

    # 同键历史（非 voided）按时间序扫描，统计未释放占用数
    events: list[tuple] = []
    for row in _active_rows(ledger_view):
        row_fields = _row_fields(row)
        if tuple(row_fields.get(name) for name in key_fields) != current_key:
            continue
        action = row_fields.get(action_field)
        if action not in (occupy_value, release_value):
            continue
        events.append((_safe_moment(row.get("occurred_at")), action))
    events.append((_safe_moment(occurred_at), current_action))
    events.sort(key=lambda pair: (0, pair[0]) if pair[0] is not None else (1, None))

    pending = 0
    for _, action in events[:-1]:  # 历史部分
        if action == occupy_value:
            pending += 1
        elif pending > 0:
            pending -= 1

    key_text = "+".join(str(value) for value in current_key)
    if current_action == occupy_value:
        if pending > 0:
            return "violation", f"配对键 {key_text} 重复占用（已有未释放的占用）"
        return "pass", f"配对键 {key_text} 无重复占用"
    if pending == 0:
        return "violation", f"配对键 {key_text} 悬空释放（无对应占用）"
    return "pass", f"配对键 {key_text} 正常释放"


def _judge_external_baseline(declaration, rule, fields, ledger_view, baselines, occurred_at):
    """外部基线对照：字段值 vs 基线允许值（§7.3 / B1 拍板）。"""
    _, args = split_expr(rule.expr)
    positional, kv = parse_kv(args)
    ref = positional[0]
    value_field = kv.get("field")
    key_field = kv.get("key")
    comparator = kv.get("op", "gte")

    baseline = _find_baseline(baselines, ref)
    if baseline is None:
        # 整表缺失 = 记录级错误（B1 拍板）
        raise reject("baselines", E_BASELINE_MISSING, f"external_baseline 引用的 ref 不在 baselines：{ref}")
    data = baseline.get("data")
    if not isinstance(data, dict):
        return None

    key_value = fields.get(key_field) if key_field else None
    if key_value is None:
        return None
    allowed = data.get(key_value)
    if allowed is None:
        # 单设备键缺失 = 规则级提示（B1 拍板：不拒绝）
        return "skipped", f"基线 {ref} 缺设备键 {key_value}（单键缺失）"
    allowed_num = _number(allowed)
    value_num = _number(fields.get(value_field)) if value_field else None
    if value_num is None or allowed_num is None:
        return None
    ok = value_num < allowed_num if comparator == "gte" else value_num > allowed_num
    symbol = "<" if comparator == "gte" else ">"
    return (
        "pass" if ok else "violation",
        f"字段值 {value_num} {symbol} 允许值 {allowed_num}：{'通过' if ok else '越限'}",
    )


def _judge_recovery_within(declaration, rule, fields, ledger_view, baselines, occurred_at):
    """恢复时限：保护「投」（恢复）行与其配对「退」行的间隔 ≤ Nd（§7.3）。"""
    _, args = split_expr(rule.expr)
    positional, kv = parse_kv(args)
    days = float(positional[0][:-1])
    action_field, occupy_value, release_value = PAIRING_ACTIONS["protection"]
    if fields.get(action_field) != occupy_value:  # 只有恢复行判定
        return None
    if "key" not in kv:
        return None
    key_fields = split_key(kv["key"])
    current_key = tuple(fields.get(name) for name in key_fields)
    if any(value is None for value in current_key):
        return None
    current_moment = _safe_moment(occurred_at)
    if current_moment is None:
        return None

    latest_release = None
    for row in _active_rows(ledger_view):
        row_fields = _row_fields(row)
        if tuple(row_fields.get(name) for name in key_fields) != current_key:
            continue
        if row_fields.get(action_field) != release_value:
            continue
        moment = _safe_moment(row.get("occurred_at"))
        if moment is None:
            continue
        if latest_release is None or moment > latest_release:
            latest_release = moment

    if latest_release is None:
        return "skipped", "无配对的退出记录（恢复时限不判）"
    span = (current_moment - latest_release).total_seconds() / 86400.0
    ok = span <= days
    return (
        "pass" if ok else "violation",
        f"恢复间隔 {round(span, 4)} 天 {'≤' if ok else '>'} 时限 {days} 天：{'通过' if ok else '超时'}",
    )


def _judge_aggregate(declaration, rule, fields, ledger_view, baselines, occurred_at):
    """聚合计数 + 可选基线对照：跳闸次数逼近允许事故开闸次数（§13 拍板 A2/B1）。"""
    _, args = split_expr(rule.expr)
    positional, kv = parse_kv(args)
    group_field = kv.get("key")
    baseline_ref = kv.get("ref")

    current_group = fields.get(group_field) if group_field else None
    count = 1  # 当前记录
    for row in _active_rows(ledger_view):
        row_fields = _row_fields(row)
        row_group = row_fields.get(group_field) if group_field else None
        if row_group == current_group:
            count += 1

    if not baseline_ref:
        return "pass", f"累计 {count} 次（无基线对照）"
    baseline = _find_baseline(baselines, baseline_ref)
    if baseline is None:
        raise reject("baselines", E_BASELINE_MISSING, f"aggregate 引用的 ref 不在 baselines：{baseline_ref}")
    data = baseline.get("data")
    if not isinstance(data, dict):
        return None
    allowed = data.get(current_group)
    if allowed is None:
        return "skipped", f"基线 {baseline_ref} 缺设备键 {current_group}（单键缺失）"
    allowed_num = _number(allowed)
    if allowed_num is None:
        return None
    ok = count < allowed_num
    return (
        "pass" if ok else "violation",
        f"累计 {count} 次 {'<' if ok else '≥'} 允许 {allowed_num} 次：{'未逼近' if ok else '逼近/达到允许事故开闸次数'}",
    )


def _judge_cross_record_date_diff(declaration, rule, fields, ledger_view, baselines, occurred_at):
    """跨记录 date_diff：``linked.<field>`` 从关联记录取数（§7.3 / GAP-E5）。"""
    _, args = split_expr(rule.expr)
    positional, kv = parse_kv(args)
    start_path, end_path = positional[0], positional[1]
    comparator, _, bound_text = positional[2].partition(":")
    bound = float(bound_text[:-1])

    start = _resolve_t3_value(fields, ledger_view, start_path, occurred_at)
    end = _resolve_t3_value(fields, ledger_view, end_path, occurred_at)
    if not isinstance(start, str) or not isinstance(end, str):
        return None
    try:
        span = days_between(parse_rfc3339(start), parse_rfc3339(end))
    except TimeTextError:
        return None
    ok = span >= bound if comparator == "ge" else span <= bound
    return (
        "pass" if ok else "violation",
        f"{start_path}→{end_path} 间隔 {round(span, 4)} 天 {'≥' if comparator == 'ge' else '≤'} {bound}：{'通过' if ok else '越限'}",
    )


def _resolve_t3_value(fields: dict, ledger_view: dict | None, path: str, occurred_at: str | None = None):
    """T3 取数：``linked.<field>`` 从 linked_records 取字段；``occurred_at`` 取信封伪字段；
    其余从当前 fields 取。"""
    if path.startswith("linked."):
        name = path[len("linked.") :]
        linked = ledger_view.get("linked_records") if isinstance(ledger_view, dict) else None
        if isinstance(linked, list):
            for row in linked:
                if isinstance(row, dict) and isinstance(row.get("fields"), dict) and name in row["fields"]:
                    return row["fields"][name]
        return None
    if path == "occurred_at":
        return occurred_at
    return fields.get(path)


def _judge_monotonic(declaration, rule, fields, ledger_view, baselines, occurred_at, current_record_uid=None):
    """跨记录单调算子（提案 §5.3）：与**前一记录自身值**比较（不是聚合）。

    前值选取六步：非 voided 同键候选 → 排除当前记录自身及其 correct 链（同 ``record_uid``）
    → 取 ``confirmed_fields`` 优先 → 按 ``occurred_at`` 最大、``rev`` 降序、``record_uid``
    字典序定序 → ``op`` 判定 → 不可判定（首次记录 / 前值非数值 / 超出 ``window``）出 ``skipped``。
    账本行字段已冻结、无 ``create_seq``，同刻多版本以 ``rev`` 降序作序以保证确定性。
    """
    _, args = split_expr(rule.expr)
    positional, kv = parse_kv(args)
    resolved = resolve_path(declaration, positional[0])
    if resolved is None or resolved[1] is None:
        return None
    spec = resolved[1]

    current = _number(fields.get(spec.key, MISSING))
    if current is None:
        return None  # 本次读数缺省或非数值：不可判定

    key_fields = split_key(kv["key"]) if "key" in kv else []
    current_key = tuple(fields.get(name) for name in key_fields)
    if key_fields and any(value is None for value in current_key):
        return None

    candidates: list[tuple] = []
    for row in _active_rows(ledger_view):
        if current_record_uid is not None and row.get("record_uid") == current_record_uid:
            continue  # 排除当前记录自身及其 correct 链（§5.3-1），避免自比
        row_fields = _row_fields(row)
        if key_fields and tuple(row_fields.get(name) for name in key_fields) != current_key:
            continue  # 分组隔离：不同键行互不比较（§5.5）
        previous = _number(row_fields.get(spec.key, MISSING))
        if previous is None:
            continue  # 前值字段缺省或非数值：不构成候选（§5.3-5）
        rev = row.get("rev")
        candidates.append(
            (
                _safe_moment(row.get("occurred_at")),
                rev if isinstance(rev, int) and not isinstance(rev, bool) else 0,
                str(row.get("record_uid") or ""),
                previous,
                row.get("occurred_at"),
            )
        )
    if not candidates:
        return "skipped", "无有效前值行（首次记录）；单调性不可判，不静默通过"

    # 定序（升序）→ 取末尾 = 优先级最高：有时间者优先 → occurred_at 最新 → rev 最大（最后版本）
    # → record_uid 最大。账本行字段已冻结、无 create_seq，同刻多版本以 rev 同向替代。
    candidates.sort(key=lambda item: (item[0] is not None, item[0], item[1], item[2]))
    moment, _, previous_uid, previous, previous_at = candidates[-1]

    window = kv.get("window")
    if window is not None:
        days = float(window[:-1])
        current_moment = _safe_moment(occurred_at)
        if current_moment is None or moment is None:
            return "skipped", f"时间不可解析（window={window}）：单调性不可判"
        if (current_moment - moment).total_seconds() / 86400.0 > days:
            return "skipped", f"前值 {previous_at} 超出窗口 {days} 天，视为无前值"

    op = kv.get("op", "ge")
    ok = current >= previous if op == "ge" else current > previous
    symbol = "≥" if op == "ge" else ">"
    return (
        "pass" if ok else "violation",
        f"单调核对：本次 {current} {symbol} 前值 {previous}（{previous_at}，{previous_uid}）"
        f"：{'通过' if ok else '读数倒退'}",
    )


# monotonic 不在表内：它额外需要「当前记录 uid」（提案 §5.3-1），由 _evaluate_t3 单独分派
_T3_JUDGES = {
    "continuity": _judge_continuity,
    "pairing": _judge_pairing,
    "external_baseline": _judge_external_baseline,
    "recovery_within": _judge_recovery_within,
    "aggregate": _judge_aggregate,
    "date_diff": _judge_cross_record_date_diff,
}
