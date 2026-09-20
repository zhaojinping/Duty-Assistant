"""registry 声明模型（design.md §7.2）。

声明是记录 payload 的唯一权威：字段、条目容器、规则、趋势、签字槽、动作码。
本模块只表达**结构与引用关系**，DSL 表达式语义在 ``engine`` 内实现。
"""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field

# §7.3 字段类型（item_list 仅顶层容器，经 [items] 声明，不出现在 [[fields]]）
FIELD_TYPES = ("text", "number", "enum", "tri_bool", "bool", "datetime")
TRI_BOOL_OPTIONS = ("是", "否", "不适用")
ATTACHMENT_KINDS = ("photo", "file", "csv", "pdf")
# §7.6 link_types 枚举
LINK_TYPES = ("retest_of", "pairs_with", "supersedes", "references")
LAYOUTS = ("flat", "item_list")
EXTRA_MODES = ("reject", "allow")
RULE_KINDS = ("cycle", "limit")
RULE_LEVELS = ("warn", "alarm")
# M2 起支持三层：T1（表内单字段）/ T2（表内派生）/ T3（台账/跨记录）
SUPPORTED_TIERS = (1, 2, 3)
# T3 专用算子（design.md §7.3）：date_diff 为 T2/T3 共用，不在此列
T3_OPS = ("continuity", "pairing", "external_baseline", "recovery_within", "aggregate")
# pairing 配对类型（§7.3）：引擎内置各类型的占用/释放动作判别
PAIRING_TYPES = ("grounding", "protection")
# items 容器的固定 payload 键（§7.2 声明的条目容器即 ``items``）
ITEMS_KEY = "items"


class DeclarationError(ValueError):
    """坏声明：引擎拒绝加载（design.md §10.2 反例要求）。"""


@dataclass(frozen=True)
class FieldSpec:
    key: str
    name: str
    kind: str
    required: bool = False
    options: tuple[str, ...] = ()
    unit: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    decimals: int | None = None
    require_attachment: str | None = None

    @property
    def path(self) -> str:  # 供规则 target / trend source 引用
        return self.key


@dataclass(frozen=True)
class ItemSpec:
    key_field: str
    fields: tuple[FieldSpec, ...]
    min_items: int = 1
    required: bool = True

    def field_by_key(self, key: str) -> FieldSpec | None:
        for spec in self.fields:
            if spec.key == key:
                return spec
        return None


@dataclass(frozen=True)
class RuleSpec:
    id: str
    kind: str
    tier: int
    expr: str
    target: str | None = None
    when: dict = dataclass_field(default_factory=dict)
    level: str = "info"
    action: str | None = None
    cycle_baseline: str | None = None

    @property
    def threshold(self) -> str:
        """结果协议里的 threshold：取声明表达式原文（可复核、无二次解释）。"""
        return self.expr


@dataclass(frozen=True)
class TrendSpec:
    metric: str
    source: str
    window: int
    drop_warn: float | None
    agg: str
    field_path: str
    group: str | None = None
    drop_abs: float | None = None


@dataclass(frozen=True)
class Declaration:
    record_type: str
    title: str
    schema_version: str
    layout: str
    dedupe_key: tuple[str, ...]
    link_types: tuple[str, ...]
    extra: str
    signature_slots: tuple[str, ...]
    action_codes: tuple[str, ...]
    escalate_after: int
    fields: tuple[FieldSpec, ...]
    items: ItemSpec | None
    rules: tuple[RuleSpec, ...]
    trends: tuple[TrendSpec, ...]
    source: str = ""

    @property
    def items_key(self) -> str:
        return ITEMS_KEY

    @property
    def cycle_rules(self) -> tuple[RuleSpec, ...]:
        """周期规则：不进 create/confirm 流水线，唯一出口是探针（§7.3 豁免注）。"""
        return tuple(rule for rule in self.rules if rule.kind == "cycle")

    @property
    def pipeline_rules(self) -> tuple[RuleSpec, ...]:
        return tuple(rule for rule in self.rules if rule.kind != "cycle")

    def field_by_key(self, key: str) -> FieldSpec | None:
        for spec in self.fields:
            if spec.key == key:
                return spec
        return None


def resolve_path(declaration: Declaration, path: str) -> tuple[str, FieldSpec | None] | None:
    """解析声明内的字段路径：``items.voltage`` / ``float_voltage`` / ``items``。

    返回 ``(scope, field)``，scope ∈ ``{"top", "items", "items_list"}``；
    路径不存在返回 ``None``（meta-test 据此拒绝坏声明）。
    """
    if path == declaration.items_key:
        return ("items_list", None) if declaration.items is not None else None
    if path.startswith(declaration.items_key + "."):
        if declaration.items is None:
            return None
        name = path[len(declaration.items_key) + 1 :]
        spec = declaration.items.field_by_key(name)
        return ("items", spec) if spec is not None else None
    spec = declaration.field_by_key(path)
    return ("top", spec) if spec is not None else None


def split_expr(expr: str) -> tuple[str, tuple[str, ...]]:
    """``band:2.00,2.25`` → ``("band", ("2.00", "2.25"))``；无参数的算子返回空元组。"""
    if ":" not in expr:
        return expr, ()
    op, rest = expr.split(":", 1)
    return op, tuple(part.strip() for part in rest.split(","))


def parse_kv(args: tuple[str, ...]) -> tuple[list[str], dict[str, str]]:
    """T3 expr 参数：``key=value`` 形式收进键值对，其余按位置参数顺序收。"""
    positional: list[str] = []
    kv: dict[str, str] = {}
    for arg in args:
        if "=" in arg:
            key, _, value = arg.partition("=")
            kv[key.strip()] = value.strip()
        else:
            positional.append(arg.strip())
    return positional, kv


def split_key(key_text: str) -> list[str]:
    """配对键：单字段或 ``+`` 连接的复合键（§13 拍板数组形式，字符串表达用 + 连接）。"""
    return [part.strip() for part in key_text.split("+") if part.strip()]
