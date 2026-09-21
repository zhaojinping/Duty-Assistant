"""§10.2 声明自检（meta-test）：好声明可加载，坏声明一律拒绝加载。

覆盖：TOML 解析、引用完整性（字段 / items.key_field / 规则 target / trend source /
action_codes / link_types / dedupe_key）、DSL 表达式文法、层的边界（T1/T2/T3 算子白名单）。
"""

from __future__ import annotations

import pytest

from records_kit.engine.rules import evaluate_rules
from records_kit.registry import DeclarationError, Registry, loader
from records_kit.registry.declaration import resolve_path, split_expr
from records_kit.registry.meta import validate_declaration

# 合成声明的局部替换件（仅供本文件使用）
BAD_FIELDS_TWO = [
    {"key": "test_kind", "name": "测试性质", "type": "enum", "options": ["定期"], "required": True},
    {"key": "float_voltage", "name": "浮充电压", "type": "number", "min": 0.0, "max": 300.0},
]
BAD_ITEM_FIELDS = [
    {"key": "cell_no", "name": "电池序号", "type": "number", "decimals": 0, "required": True},
    {"key": "voltage", "name": "单体电压", "type": "number", "min": 0.0, "max": 15.0, "required": True},
]
BAD_RULE = {
    "id": "voltage_band",
    "kind": "limit",
    "tier": 1,
    "target": "items.voltage",
    "expr": "band:2.0,2.25",
    "level": "warn",
}


@pytest.fixture
def toml_builder(helpers):
    """合成声明的 TOML 文本构造器（``toml_builder(meta=..., omit=..., ...)``）。"""
    return helpers.build_toml


def _flat_toml() -> str:
    """flat 声明（无 [items]）：一个数值字段 + 一条 T1 规则 + 一条趋势。"""
    return "\n".join(
        [
            "[meta]",
            'record_type = "synthetic_record"',
            'title = "合成记录（测试夹具）"',
            'schema_version = "1.5"',
            'layout = "flat"',
            'dedupe_key = ["station", "occurred_day"]',
            "link_types = []",
            'extra = "reject"',
            'signature_slots = ["记录人"]',
            'action_codes = ["FLAG"]',
            "escalate_after = 2",
            "",
            "[[fields]]",
            'key = "current"',
            'name = "电流"',
            'type = "number"',
            "min = 0.0",
            "",
            "[[rules]]",
            'id = "current_limit"',
            'kind = "limit"',
            "tier = 1",
            'target = "current"',
            'expr = "lte:5.0"',
            'level = "warn"',
            'action = "FLAG"',
            "",
            "[[trend]]",
            'metric = "current_max"',
            'source = "max(current)"',
            "window = 2",
            "drop_warn = 0.5",
            "",
        ]
    )


# ---------------------------------------------------------------- 好声明


def test_bundled_battery_declaration_shape(declaration):
    assert declaration.record_type == "battery_voltage_test"
    assert declaration.layout == "item_list"
    assert declaration.schema_version == "1.5"
    assert declaration.dedupe_key == ("station", "occurred_day", "test_kind", "dc_system_id")
    assert declaration.link_types == ("retest_of",)
    assert declaration.extra == "reject"
    assert declaration.signature_slots == ("测试人",)
    assert declaration.action_codes == ("MARK_LAGGING_CELL", "RETEST_CELL")
    assert declaration.escalate_after == 3
    assert [spec.key for spec in declaration.fields] == [
        "dc_system_id",
        "float_voltage",
        "float_current",
        "env_temp",
        "test_kind",
    ]
    assert declaration.items.key_field == "cell_no"
    assert declaration.items.min_items == 1
    assert [spec.key for spec in declaration.items.fields] == [
        "cell_no",
        "voltage",
        "lagging",
        "remark",
        "retest_voltage",
    ]
    assert [rule.id for rule in declaration.pipeline_rules] == ["voltage_band", "lagging_detect"]
    assert [rule.id for rule in declaration.cycle_rules] == ["cycle_regular"]
    assert declaration.cycle_rules[0].tier == 1
    assert declaration.cycle_rules[0].when == {"test_kind": "定期"}
    assert declaration.trends[0].source == "min(items.voltage)"
    assert declaration.trends[0].window == 6
    assert declaration.trends[0].drop_warn == 0.10


def test_battery_field_types_and_boundaries(declaration):
    voltage = declaration.items.field_by_key("voltage")
    assert (voltage.minimum, voltage.maximum, voltage.unit) == (0.0, 15.0, "V")
    assert declaration.items.field_by_key("lagging").kind == "tri_bool"
    assert declaration.field_by_key("test_kind").options == ("定期", "核对性放电")
    assert declaration.items.field_by_key("cell_no").decimals == 0
    assert declaration.field_by_key("dc_system_id").required is True
    assert declaration.field_by_key("float_current").required is False


def test_cycle_rules_stay_out_of_the_pipeline(declaration, helpers):
    """周期规则不进 create/confirm 流水线，rules 里永不含周期结论（§7.3 豁免注）。"""
    report = evaluate_rules(declaration, helpers.battery_payload())
    assert report.entries
    assert all(entry["rule_id"] != "cycle_regular" for entry in report.entries)
    assert all(entry["kind"] != "cycle" for entry in report.entries)


def test_split_expr_and_resolve_path(declaration):
    assert split_expr("band:2.00,2.25") == ("band", ("2.00", "2.25"))
    assert split_expr("monthly") == ("monthly", ())
    assert resolve_path(declaration, "items.voltage")[0] == "items"
    assert resolve_path(declaration, "float_voltage")[0] == "top"
    assert resolve_path(declaration, "items")[0] == "items_list"
    assert resolve_path(declaration, "items.nope") is None
    assert resolve_path(declaration, "nope") is None


def test_good_synthetic_declaration_loads(synthetic_registry):
    registry = synthetic_registry()
    declaration = registry["synthetic_record"]
    assert declaration.title.startswith("合成记录")
    assert declaration.items.key_field == "cell_no"


def test_flat_declaration_without_items_loads(synthetic_registry):
    registry = synthetic_registry(toml_text=_flat_toml())
    declaration = registry["synthetic_record"]
    assert declaration.layout == "flat"
    assert declaration.items is None
    assert declaration.pipeline_rules[0].expr == "lte:5.0"
    assert declaration.trends[0].agg == "max"


# ---------------------------------------------------------------- 坏声明


@pytest.mark.parametrize(
    "patch, fragment",
    [
        ({"omit": ("record_type",)}, "meta.record_type"),
        ({"omit": ("schema_version",)}, "meta.schema_version"),
        ({"meta": {"layout": "grid"}}, "meta.layout"),
        ({"meta": {"extra": "purge"}}, "meta.extra"),
        ({"meta": {"dedupe_key": []}}, "meta.dedupe_key"),
        ({"meta": {"dedupe_key": ["station", "nope"]}}, "meta.dedupe_key 引用不存在的字段"),
        ({"meta": {"link_types": ["whatever"]}}, "meta.link_types"),
        ({"meta": {"signature_slots": []}}, "meta.signature_slots"),
        ({"meta": {"action_codes": [1]}}, "meta.action_codes"),
        ({"meta": {"escalate_after": 1}}, "meta.escalate_after"),
        ({"fields": [{"key": "x", "name": "X", "type": "matrix"}]}, "type 必须是"),
        ({"fields": [{"key": "x", "name": "X", "type": "enum"}]}, "options"),
        ({"fields": [{"key": "x", "name": "X", "type": "number", "min": 5.0, "max": 1.0}]}, "min 大于 max"),
        ({"fields": [{"key": "x", "name": "X", "type": "text", "min": 1}]}, "只允许出现在 number"),
        ({"fields": [{"key": "x", "name": "X", "type": "number", "decimals": -1}]}, "decimals"),
        ({"fields": [{"key": "x", "name": "X", "type": "text", "require_attachment": "video"}]}, "require_attachment"),
        ({"fields": BAD_FIELDS_TWO + BAD_FIELDS_TWO[:1]}, "key 重复"),
        ({"items": {"key_field": "nope", "fields": BAD_ITEM_FIELDS}}, "items.key_field"),
        ({"items": {"key_field": "cell_no", "min_items": 0, "fields": BAD_ITEM_FIELDS}}, "min_items"),
        ({"omit": ("items",), "meta": {"layout": "item_list"}}, "必须声明 [items]"),
        ({"meta": {"layout": "flat"}}, "只有 layout=item_list"),
        ({"meta": {"signature_slots": ["env_temp"]}}, "与 payload 字段重名"),
        (
            {"rules": [{"id": "r", "kind": "limit", "tier": 1, "target": "items.nope", "expr": "band:1,2"}]},
            "target 引用不存在的字段",
        ),
        (
            {"rules": [{"id": "r", "kind": "limit", "tier": 1, "target": "items.voltage", "expr": "band:1,x"}]},
            "参数个数/取值不合法",
        ),
        (
            {"rules": [{"id": "r", "kind": "limit", "tier": 1, "target": "items.voltage", "expr": "band:3,2"}]},
            "下限大于上限",
        ),
        (
            {"rules": [{"id": "r", "kind": "limit", "tier": 1, "target": "items.voltage", "expr": "mystery:1"}]},
            "未知算子",
        ),
        (
            {
                "rules": [
                    {"id": "r", "kind": "limit", "tier": 1, "target": "items.voltage", "expr": "band:1,2", "action": "NOPE"}
                ]
            },
            "action 不在声明的 action_codes",
        ),
        (
            {"rules": [{"id": "r", "kind": "limit", "tier": 3, "target": "items.voltage", "expr": "band:1,2"}]},
            "未知 T3 算子",
        ),
        ({"rules": [{"id": "r", "kind": "cycle", "tier": 1, "expr": "every:30d"}]}, "周期表达式"),
        (
            {"rules": [{"id": "r", "kind": "cycle", "tier": 2, "expr": "periodic:30d"}]},
            "周期规则 tier 固定 1",
        ),
        (
            {"rules": [{"id": "r", "kind": "cycle", "tier": 1, "expr": "periodic:30d", "target": "items.voltage"}]},
            "不设 target",
        ),
        ({"rules": [{"id": "r", "kind": "cleanup", "tier": 1, "expr": "band:1,2"}]}, "kind 必须是"),
        ({"rules": [BAD_RULE, BAD_RULE]}, "id 重复"),
        (
            {"rules": [{"id": "r", "kind": "limit", "tier": 1, "target": "items.voltage", "expr": "band:1,2", "level": "nuke"}]},
            "level 必须是",
        ),
        (
            {
                "rules": [
                    {"id": "r", "kind": "limit", "tier": 1, "target": "items.voltage", "expr": "band:1,2", "when": {"nope": "x"}}
                ]
            },
            "when 引用不存在的字段",
        ),
        (
            {
                "rules": [
                    {"id": "r", "kind": "limit", "tier": 1, "target": "items.voltage", "expr": "band:1,2", "when": {"test_kind": "不存在"}}
                ]
            },
            "不在字段",
        ),
        (
            {"rules": [{"id": "r", "kind": "limit", "tier": 2, "target": "items.voltage", "expr": "ratio:voltage,cell_no,1,2,3"}]},
            "ratio 需要",
        ),
        (
            {"rules": [{"id": "r", "kind": "limit", "tier": 2, "target": "items.voltage", "expr": "date_diff:float_voltage,env_temp,le:3d"}]},
            "datetime 字段",
        ),
        (
            {"rules": [{"id": "r", "kind": "limit", "tier": 1, "target": "items.voltage", "expr": "diff:voltage,cell_no,gt:1"}]},
            "diff 需要",
        ),
        (
            {"trend": [{"metric": "m", "source": "median(items.voltage)", "window": 3, "drop_warn": 0.1}]},
            "agg(field_path)",
        ),
        (
            {"trend": [{"metric": "m", "source": "min(items.nope)", "window": 3, "drop_warn": 0.1}]},
            "source 引用不存在的字段",
        ),
        (
            {"trend": [{"metric": "m", "source": "min(items.voltage)", "window": 1, "drop_warn": 0.1}]},
            "window",
        ),
        (
            {"trend": [{"metric": "m", "source": "min(items.voltage)", "window": 3, "drop_warn": 2}]},
            "drop_warn",
        ),
    ],
)
def test_bad_declarations_are_refused(toml_builder, synthetic_registry, patch, fragment):
    text = toml_builder(**patch)
    with pytest.raises(DeclarationError) as excinfo:
        synthetic_registry(toml_text=text)
    assert fragment in str(excinfo.value), str(excinfo.value)


def test_toml_syntax_error_is_refused(synthetic_registry):
    with pytest.raises(DeclarationError) as excinfo:
        synthetic_registry(toml_text='[meta]\nrecord_type = "x\n')
    assert "TOML 解析失败" in str(excinfo.value)


def test_missing_meta_section_is_refused():
    with pytest.raises(DeclarationError) as excinfo:
        validate_declaration({"fields": []}, source="synthetic-inline")
    assert "缺少 [meta]" in str(excinfo.value)


def test_non_table_root_is_refused():
    with pytest.raises(DeclarationError):
        validate_declaration([], source="synthetic-inline")


def test_duplicate_record_type_in_directory_is_refused(make_registry, toml_builder):
    with pytest.raises(DeclarationError) as excinfo:
        make_registry({"a.toml": toml_builder(), "b.toml": toml_builder()})
    assert "record_type 重复" in str(excinfo.value)


def test_directory_without_declarations_is_refused(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(DeclarationError):
        loader.load_dir(empty)


def test_missing_declaration_file_is_refused(tmp_path):
    with pytest.raises(DeclarationError):
        loader.load(tmp_path / "synthetic-missing.toml")


def test_registry_from_files_and_mapping(helpers, tmp_path):
    path = helpers.write_declaration(tmp_path, "synthetic_record.toml", helpers.build_toml())
    registry = Registry.from_files([str(path)])
    assert registry.record_types == ("synthetic_record",)
    assert registry.get("missing") is None
    assert len(registry) == 1
    assert loader.load_declaration(path).record_type == "synthetic_record"


def test_registry_rejects_key_mismatch(declaration):
    with pytest.raises(DeclarationError):
        Registry({"other_type": declaration})
