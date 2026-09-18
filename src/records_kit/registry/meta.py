"""registry 声明的 meta-test 自检（design.md §10.2）。

TOML 可被 ``tomllib`` 解析只是第一关；这里再校验引用完整性：
字段 / items.key_field / 规则 target / 趋势 source（agg 白名单 + 字段存在性）/
action_codes / link_types / 三个视图无关的声明内部一致性。
**坏声明一律拒绝加载**（引用不存在字段、动作码不在声明内等）。
"""

from __future__ import annotations

from records_kit.errors import CODES  # noqa: F401  (错误码表是同一权威来源)
from records_kit.registry.declaration import (
    ATTACHMENT_KINDS,
    EXTRA_MODES,
    FIELD_TYPES,
    LAYOUTS,
    LINK_TYPES,
    RULE_KINDS,
    RULE_LEVELS,
    SUPPORTED_TIERS,
    Declaration,
    DeclarationError,
    FieldSpec,
    ItemSpec,
    RuleSpec,
    TrendSpec,
    resolve_path,
    split_expr,
)
from records_kit.util import AGG_WHITELIST, PERIOD_KINDS, TimeTextError, period_spec

# dedupe_key 允许的信封派生伪键（§7.2 样例：station / occurred_day）
DERIVED_DEDUPE_KEYS = ("station", "occurred_day", "occurred_at")
_NUMERIC_OPS = ("band", "gt", "gte", "lt", "lte")
_DERIVED_OPS = ("ratio", "deviation", "diff", "date_diff")
_DEVIATION_BASES = ("mean", "min", "max", "last")
_COMPARATORS = ("ge", "le")


class _Problems:
    def __init__(self) -> None:
        self.items: list[str] = []

    def add(self, message: str) -> None:
        self.items.append(message)

    def check(self, condition: bool, message: str) -> bool:
        if not condition:
            self.add(message)
        return condition

    def raise_if_any(self, source: str) -> None:
        if self.items:
            raise DeclarationError(f"{source}: " + "；".join(self.items))


def _as_number(text: str) -> float | None:
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _field_specs(raw_fields: object, where: str, problems: _Problems) -> tuple[FieldSpec, ...]:
    if not isinstance(raw_fields, list):
        problems.add(f"{where} 必须是字段数组")
        return ()
    specs: list[FieldSpec] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_fields):
        label = f"{where}[{index}]"
        if not isinstance(raw, dict):
            problems.add(f"{label} 必须是表")
            continue
        key = raw.get("key")
        name = raw.get("name")
        kind = raw.get("type")
        if not isinstance(key, str) or not key:
            problems.add(f"{label}.key 缺失或非字符串")
            continue
        if key in seen:
            problems.add(f"{label}.key 重复：{key}")
        seen.add(key)
        if not isinstance(name, str) or not name:
            problems.add(f"{label}.name 缺失或非字符串")
        if kind not in FIELD_TYPES:
            problems.add(f"{label}.type 必须是 {'/'.join(FIELD_TYPES)} 之一，实为 {kind!r}")
            continue
        options = raw.get("options")
        if kind == "enum":
            if not isinstance(options, list) or not options or not all(isinstance(o, str) for o in options):
                problems.add(f"{label}.options 枚举必须是非空字符串数组")
                options = []
            elif len(set(options)) != len(options):
                problems.add(f"{label}.options 含重复取值")
        elif options not in (None, []):
            problems.add(f"{label}.options 只允许出现在 enum 字段")
            options = []
        else:
            options = []
        required = raw.get("required", False)
        if not isinstance(required, bool):
            problems.add(f"{label}.required 必须是布尔")
            required = bool(required)
        unit = raw.get("unit")
        if unit is not None and not isinstance(unit, str):
            problems.add(f"{label}.unit 必须是字符串")
        minimum = raw.get("min")
        maximum = raw.get("max")
        decimals = raw.get("decimals")
        if kind == "number":
            for name_, value in (("min", minimum), ("max", maximum)):
                if value is not None and not isinstance(value, (int, float)):
                    problems.add(f"{label}.{name_} 必须是数值")
            if isinstance(minimum, (int, float)) and isinstance(maximum, (int, float)) and minimum > maximum:
                problems.add(f"{label}：min 大于 max")
            if decimals is not None:
                if not isinstance(decimals, int) or isinstance(decimals, bool) or decimals < 0:
                    problems.add(f"{label}.decimals 必须是非负整数")
                    decimals = None
        else:
            for name_, value in (("min", minimum), ("max", maximum), ("decimals", decimals)):
                if value is not None:
                    problems.add(f"{label}.{name_} 只允许出现在 number 字段")
            minimum = maximum = decimals = None
        attachment = raw.get("require_attachment")
        if attachment is not None and attachment not in ATTACHMENT_KINDS:
            problems.add(f"{label}.require_attachment 必须是 {'/'.join(ATTACHMENT_KINDS)} 之一")
            attachment = None
        specs.append(
            FieldSpec(
                key=key,
                name=name if isinstance(name, str) else key,
                kind=kind,
                required=required,
                options=tuple(options or ()),
                unit=unit,
                minimum=float(minimum) if isinstance(minimum, (int, float)) else None,
                maximum=float(maximum) if isinstance(maximum, (int, float)) else None,
                decimals=decimals if isinstance(decimals, int) and not isinstance(decimals, bool) else None,
                require_attachment=attachment,
            )
        )
    return tuple(specs)


def _items_spec(raw_items: object, layout: str, problems: _Problems) -> ItemSpec | None:
    if raw_items is None:
        if layout == "item_list":
            problems.add("layout=item_list 必须声明 [items] 条目容器")
        return None
    if not isinstance(raw_items, dict):
        problems.add("[items] 必须是表")
        return None
    if layout != "item_list":
        problems.add("只有 layout=item_list 才能声明 [items]")
    fields = _field_specs(raw_items.get("fields"), "items.fields", problems)
    key_field = raw_items.get("key_field")
    if not isinstance(key_field, str) or not key_field:
        problems.add("items.key_field 缺失或非字符串")
    elif not any(spec.key == key_field for spec in fields):
        problems.add(f"items.key_field={key_field} 未在 items.fields 中声明")
    min_items = raw_items.get("min_items", 1)
    if not isinstance(min_items, int) or isinstance(min_items, bool) or min_items < 1:
        problems.add("items.min_items 必须是 ≥1 的整数")
        min_items = 1
    required = raw_items.get("required", True)
    if not isinstance(required, bool):
        problems.add("items.required 必须是布尔")
        required = True
    return ItemSpec(key_field=key_field if isinstance(key_field, str) else "", fields=fields, min_items=min_items, required=required)


def _check_when(declaration: Declaration, rule: RuleSpec, when: object, problems: _Problems, label: str) -> dict:
    if when is None:
        return {}
    if not isinstance(when, dict) or not when:
        problems.add(f"{label}.when 必须是非空表")
        return {}
    for path, expected in when.items():
        resolved = resolve_path(declaration, path)
        if resolved is None:
            problems.add(f"{label}.when 引用不存在的字段：{path}")
            continue
        scope, spec = resolved
        if scope == "items_list" or spec is None:
            problems.add(f"{label}.when 不允许指向条目容器：{path}")
            continue
        if spec.kind == "enum" and expected not in spec.options:
            problems.add(f"{label}.when 的取值 {expected!r} 不在字段 {path} 的枚举内")
        if spec.kind == "tri_bool" and expected not in ("是", "否", "不适用"):
            problems.add(f"{label}.when 的取值 {expected!r} 不是三态布尔取值")
    return dict(when)


def _check_rule(
    declaration: Declaration,
    rule: RuleSpec,
    raw: dict,
    problems: _Problems,
    label: str,
) -> None:
    op, args = split_expr(rule.expr)
    if rule.kind == "cycle":
        if rule.tier != 1:
            problems.add(f"{label}：周期规则 tier 固定 1（§7.3 豁免注）")
        if rule.target is not None:
            problems.add(f"{label}：周期规则不设 target（唯一出口是探针）")
        if op not in PERIOD_KINDS:
            problems.add(f"{label}.expr 周期表达式必须是 periodic:Nd / monthly / quarterly")
        else:
            try:
                period_spec(rule.expr)
            except TimeTextError as exc:
                problems.add(f"{label}.expr {exc}")
        baseline = raw.get("cycle_baseline")
        if baseline is not None and not isinstance(baseline, str):
            problems.add(f"{label}.cycle_baseline 必须是日期文本")
        return

    if rule.tier not in SUPPORTED_TIERS:
        problems.add(f"{label}：tier={rule.tier} 属 M2（T3 台账/跨记录）范围，M1 引擎拒绝加载")
        return
    if op not in _NUMERIC_OPS + _DERIVED_OPS:
        problems.add(f"{label}.expr 未知算子：{op!r}")
        return
    if not rule.target:
        problems.add(f"{label}：limit 规则必须声明 target")
        return
    resolved = resolve_path(declaration, rule.target)
    if resolved is None:
        problems.add(f"{label}.target 引用不存在的字段：{rule.target}")
        return
    scope, spec = resolved
    if spec is None:
        problems.add(f"{label}.target 不允许指向条目容器")
        return

    def _number_field(path: str, what: str) -> bool:
        target = resolve_path(declaration, path)
        if target is None:
            problems.add(f"{label}.{what} 引用不存在的字段：{path}")
            return False
        if target[1] is None or target[1].kind != "number":
            problems.add(f"{label}.{what} 必须是 number 字段：{path}")
            return False
        return True

    if op in _NUMERIC_OPS:
        if spec.kind != "number":
            problems.add(f"{label}：{op} 只作用于 number 字段，实为 {spec.kind}")
        expected = 2 if op == "band" else 1
        if len(args) != expected or any(_as_number(arg) is None for arg in args):
            problems.add(f"{label}.expr 参数个数/取值不合法：{rule.expr}")
        elif op == "band" and _as_number(args[0]) > _as_number(args[1]):
            problems.add(f"{label}.expr 区间下限大于上限：{rule.expr}")
    elif op == "ratio":
        if len(args) != 4:
            problems.add(f"{label}.expr ratio 需要 num,den,lo,hi 四个参数")
            return
        for path in args[:2]:
            _number_field(path, "expr")
        for bound in args[2:]:
            if _as_number(bound) is None:
                problems.add(f"{label}.expr 区界必须是数值：{bound!r}")
    elif op == "deviation":
        if len(args) != 2:
            problems.add(f"{label}.expr deviation 需要 baseline,pct 两个参数")
            return
        base, pct = args
        if base not in _DEVIATION_BASES:
            _number_field(base, "expr")
        if not (pct.endswith("%") and _as_number(pct[:-1]) is not None) and _as_number(pct) is None:
            problems.add(f"{label}.expr 偏差百分比不合法：{pct!r}")
    elif op == "diff":
        if len(args) != 3 or not args[2].startswith(_COMPARATORS):
            problems.add(f"{label}.expr diff 需要 a,b,ge|le:N 三个参数")
            return
        for path in args[:2]:
            _number_field(path, "expr")
        comparator, _, bound = args[2].partition(":")
        if comparator not in _COMPARATORS or _as_number(bound) is None:
            problems.add(f"{label}.expr diff 比较项不合法：{args[2]!r}")
    elif op == "date_diff":
        if len(args) != 3 or not args[2].startswith(_COMPARATORS):
            problems.add(f"{label}.expr date_diff 需要 from,to,ge|le:Nd 三个参数")
            return
        for path in args[:2]:
            target = resolve_path(declaration, path)
            if target is None or target[1] is None or target[1].kind != "datetime":
                problems.add(f"{label}.expr date_diff 只能作用于 datetime 字段：{path}")
        comparator, _, bound = args[2].partition(":")
        if comparator not in _COMPARATORS or not bound.endswith("d") or _as_number(bound[:-1]) is None:
            problems.add(f"{label}.expr date_diff 比较项不合法：{args[2]!r}")

    action = rule.action
    if action is not None and action not in declaration.action_codes:
        problems.add(f"{label}.action 不在声明的 action_codes 内：{action}")


def _rules(declaration_probe: dict, problems: _Problems, declaration) -> tuple[RuleSpec, ...]:
    raw_rules = declaration_probe.get("rules", [])
    if not isinstance(raw_rules, list):
        problems.add("[[rules]] 必须是规则数组")
        return ()
    rules: list[RuleSpec] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_rules):
        label = f"rules[{index}]"
        if not isinstance(raw, dict):
            problems.add(f"{label} 必须是表")
            continue
        rule_id = raw.get("id")
        if not isinstance(rule_id, str) or not rule_id:
            problems.add(f"{label}.id 缺失或非字符串")
            continue
        if rule_id in seen:
            problems.add(f"{label}.id 重复：{rule_id}")
        seen.add(rule_id)
        kind = raw.get("kind")
        if kind not in RULE_KINDS:
            problems.add(f"{label}.kind 必须是 {'/'.join(RULE_KINDS)} 之一")
            continue
        tier = raw.get("tier")
        if not isinstance(tier, int) or isinstance(tier, bool) or tier < 1:
            problems.add(f"{label}.tier 必须是正整数")
            tier = 1
        expr = raw.get("expr")
        if not isinstance(expr, str) or not expr:
            problems.add(f"{label}.expr 缺失或非字符串")
            continue
        level = raw.get("level", "warn" if kind == "limit" else "info")
        if kind == "limit" and level not in RULE_LEVELS:
            problems.add(f"{label}.level 必须是 {'/'.join(RULE_LEVELS)} 之一")
            level = "warn"
        target = raw.get("target")
        if target is not None and not isinstance(target, str):
            problems.add(f"{label}.target 必须是字符串")
            target = None
        action = raw.get("action")
        if action is not None and not isinstance(action, str):
            problems.add(f"{label}.action 必须是字符串")
            action = None
        when = _check_when(declaration, None, raw.get("when"), problems, label)  # type: ignore[arg-type]
        rule = RuleSpec(
            id=rule_id,
            kind=kind,
            tier=tier,
            expr=expr,
            target=target,
            when=when,
            level=level,
            action=action,
            cycle_baseline=raw.get("cycle_baseline"),
        )
        _check_rule(declaration, rule, raw, problems, label)
        rules.append(rule)
    return tuple(rules)


def _trends(declaration: Declaration, raw_trends: object, problems: _Problems) -> tuple[TrendSpec, ...]:
    if raw_trends is None:
        return ()
    if not isinstance(raw_trends, list):
        problems.add("[[trend]] 必须是数组")
        return ()
    trends: list[TrendSpec] = []
    for index, raw in enumerate(raw_trends):
        label = f"trend[{index}]"
        if not isinstance(raw, dict):
            problems.add(f"{label} 必须是表")
            continue
        metric = raw.get("metric")
        source = raw.get("source")
        window = raw.get("window")
        drop_warn = raw.get("drop_warn")
        if not isinstance(metric, str) or not metric:
            problems.add(f"{label}.metric 缺失或非字符串")
            continue
        if not isinstance(source, str) or not source:
            problems.add(f"{label}.source 缺失或非字符串")
            continue
        agg_match = None
        if "(" in source and source.endswith(")"):
            head, _, tail = source.partition("(")
            if head in AGG_WHITELIST:
                agg_match = (head, tail[:-1])
        if agg_match is None:
            problems.add(f"{label}.source 必须是 agg(field_path)，agg ∈ {'/'.join(AGG_WHITELIST)}：{source}")
            continue
        agg, path = agg_match
        resolved = resolve_path(declaration, path)
        if resolved is None:
            problems.add(f"{label}.source 引用不存在的字段：{path}")
            continue
        if resolved[1] is None or resolved[1].kind not in ("number", "bool", "tri_bool"):
            # count 允许任意条目字段，其余聚合需要可数值化的字段
            if agg != "count":
                problems.add(f"{label}.source 聚合需要可数值化字段：{path}")
                continue
        if not isinstance(window, int) or isinstance(window, bool) or window < 2:
            problems.add(f"{label}.window 必须是 ≥2 的整数")
            continue
        if not isinstance(drop_warn, (int, float)) or isinstance(drop_warn, bool) or not (0 < drop_warn <= 1):
            problems.add(f"{label}.drop_warn 必须是 (0,1] 内的数值")
            continue
        trends.append(
            TrendSpec(metric=metric, source=source, window=window, drop_warn=float(drop_warn), agg=agg, field_path=path)
        )
    return tuple(trends)


def validate_declaration(raw: object, source: str = "<declaration>") -> Declaration:
    """把 TOML 解析结果校验为 ``Declaration``；坏声明抛 ``DeclarationError``。"""
    problems = _Problems()
    if not isinstance(raw, dict):
        raise DeclarationError(f"{source}: 声明根必须是表")
    meta = raw.get("meta")
    if not isinstance(meta, dict):
        raise DeclarationError(f"{source}: 缺少 [meta] 段")

    def _text(name: str, required: bool = True, default: str = "") -> str:
        value = meta.get(name, default)
        if required and (not isinstance(value, str) or not value):
            problems.add(f"meta.{name} 缺失或非字符串")
            return default
        if value is not None and not isinstance(value, str):
            problems.add(f"meta.{name} 必须是字符串")
            return default
        return value if isinstance(value, str) else default

    record_type = _text("record_type")
    title = _text("title")
    schema_version = _text("schema_version")

    layout = meta.get("layout")
    if layout not in LAYOUTS:
        problems.add(f"meta.layout 必须是 {'/'.join(LAYOUTS)} 之一")
        layout = "flat"
    extra = meta.get("extra", "reject")
    if extra not in EXTRA_MODES:
        problems.add(f"meta.extra 必须是 {'/'.join(EXTRA_MODES)} 之一")
        extra = "reject"

    raw_dedupe = meta.get("dedupe_key")
    if not isinstance(raw_dedupe, list) or not raw_dedupe or not all(isinstance(k, str) for k in raw_dedupe):
        problems.add("meta.dedupe_key 必须是非空字符串数组")
        raw_dedupe = []

    raw_links = meta.get("link_types", [])
    if not isinstance(raw_links, list) or not all(isinstance(k, str) and k in LINK_TYPES for k in raw_links):
        problems.add(f"meta.link_types 必须是 {'/'.join(LINK_TYPES)} 的子集")
        raw_links = []

    raw_slots = meta.get("signature_slots")
    if not isinstance(raw_slots, list) or not raw_slots or not all(isinstance(s, str) and s for s in raw_slots):
        problems.add("meta.signature_slots 必须是非空字符串数组")
        raw_slots = []

    raw_actions = meta.get("action_codes")
    if not isinstance(raw_actions, list) or not all(isinstance(a, str) and a for a in raw_actions):
        problems.add("meta.action_codes 必须是字符串数组")
        raw_actions = []

    escalate_after = meta.get("escalate_after")
    if not isinstance(escalate_after, int) or isinstance(escalate_after, bool) or escalate_after < 2:
        problems.add("meta.escalate_after 必须是 ≥2 的整数")
        escalate_after = 2

    fields = _field_specs(raw.get("fields"), "fields", problems)
    items = _items_spec(raw.get("items"), layout, problems)

    top_keys = {spec.key for spec in fields}
    item_keys = {spec.key for spec in items.fields} if items else set()
    for slot in raw_slots:
        if slot in top_keys or slot in item_keys:
            problems.add(f"signature_slots 的 {slot!r} 与 payload 字段重名（签字栏只读留白，§铁律 7）")

    # dedupe_key 引用完整性：信封派生伪键或顶层字段
    for name in raw_dedupe:
        if name not in DERIVED_DEDUPE_KEYS and name not in top_keys:
            problems.add(f"meta.dedupe_key 引用不存在的字段：{name}")

    probe = Declaration(
        record_type=record_type,
        title=title,
        schema_version=schema_version,
        layout=layout,
        dedupe_key=tuple(raw_dedupe),
        link_types=tuple(raw_links),
        extra=extra,
        signature_slots=tuple(raw_slots),
        action_codes=tuple(raw_actions),
        escalate_after=escalate_after,
        fields=fields,
        items=items,
        rules=(),
        trends=(),
        source=source,
    )
    rules = _rules(raw, problems, probe)
    final = Declaration(
        record_type=record_type,
        title=title,
        schema_version=schema_version,
        layout=layout,
        dedupe_key=tuple(raw_dedupe),
        link_types=tuple(raw_links),
        extra=extra,
        signature_slots=tuple(raw_slots),
        action_codes=tuple(raw_actions),
        escalate_after=escalate_after,
        fields=fields,
        items=items,
        rules=rules,
        trends=(),
        source=source,
    )
    trends = _trends(final, raw.get("trend"), problems)
    problems.raise_if_any(source)
    return Declaration(
        record_type=final.record_type,
        title=final.title,
        schema_version=final.schema_version,
        layout=final.layout,
        dedupe_key=final.dedupe_key,
        link_types=final.link_types,
        extra=final.extra,
        signature_slots=final.signature_slots,
        action_codes=final.action_codes,
        escalate_after=final.escalate_after,
        fields=final.fields,
        items=final.items,
        rules=rules,
        trends=trends,
        source=source,
    )
