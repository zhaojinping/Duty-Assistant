"""§10.3 表驱动单测：T1/T2 全部算子边界（design.md §7.3）。

- T1：``band`` / ``gt`` / ``gte`` / ``lt`` / ``lte``（含边界值、整型与浮点）；
- T2：``ratio``（含除零）/ ``deviation``（四类基准 + 条目不足 + 基准为零）/ ``diff`` / ``date_diff``（含时区）；
- ``when`` 等值匹配、可选字段缺省记 skipped、动作码回传与告警候选。
"""

from __future__ import annotations

import pytest

from records_kit.engine.rules import evaluate_rules
from records_kit.errors import Rejected
from records_kit.registry.declaration import Declaration, FieldSpec, RuleSpec


@pytest.fixture
def make_declaration(synthetic_registry, helpers):
    def _make(**kwargs):
        return synthetic_registry(helpers.build_toml(**kwargs))["synthetic_record"]

    return _make


ITEM_NUMBER_RULE = [
    {"id": "r", "kind": "limit", "tier": 1, "target": "items.voltage", "expr": "band:2.00,2.25", "level": "warn"}
]


def _items(*voltages) -> list[dict]:
    return [{"cell_no": index + 1, "voltage": value} for index, value in enumerate(voltages)]


@pytest.mark.parametrize(
    "expr, value, verdict",
    [
        ("band:2.00,2.25", 2.00, "pass"),
        ("band:2.00,2.25", 2.25, "pass"),
        ("band:2.00,2.25", 1.999, "violation"),
        ("band:2.00,2.25", 2.251, "violation"),
        ("gt:2.00", 2.001, "pass"),
        ("gt:2.00", 2.00, "violation"),
        ("gte:2.00", 2.00, "pass"),
        ("gte:2.00", 1.999, "violation"),
        ("lt:2.25", 2.249, "pass"),
        ("lt:2.25", 2.25, "violation"),
        ("lte:2.25", 2.25, "pass"),
        ("lte:2.25", 2.251, "violation"),
    ],
)
def test_t1_numeric_operators(make_declaration, expr, value, verdict):
    declaration = make_declaration(rules=[dict(ITEM_NUMBER_RULE[0], expr=expr)], trend=[])
    report = evaluate_rules(declaration, {"items": _items(value)})
    assert [entry["verdict"] for entry in report.entries] == [verdict]
    assert report.entries[0]["threshold"] == expr
    assert report.entries[0]["tier"] == 1


def test_t1_on_top_level_field(make_declaration):
    rules = [{"id": "v", "kind": "limit", "tier": 1, "target": "float_voltage", "expr": "lte:250.0", "level": "warn"}]
    declaration = make_declaration(meta={"layout": "flat"}, items=False, rules=rules, trend=[])
    report = evaluate_rules(declaration, {"test_kind": "定期", "float_voltage": 260.0})
    assert report.entries[0]["verdict"] == "violation"
    assert report.entries[0]["detail"].startswith("记录 取值 260.0")


def test_when_equality_gates_the_rule(make_declaration):
    rules = [dict(ITEM_NUMBER_RULE[0], when={"test_kind": "核对性放电"})]
    declaration = make_declaration(rules=rules, trend=[])
    assert evaluate_rules(declaration, {"test_kind": "定期", "items": _items(1.0)}).entries == []
    hit = evaluate_rules(declaration, {"test_kind": "核对性放电", "items": _items(1.0)})
    assert [entry["verdict"] for entry in hit.entries] == ["violation"]


def test_optional_target_missing_is_skipped_not_passed(make_declaration):
    rules = [{"id": "g", "kind": "limit", "tier": 1, "target": "items.lagging_number", "expr": "lte:5.0", "level": "warn"}]
    declaration = make_declaration(
        items={
            "key_field": "cell_no",
            "fields": [
                {"key": "cell_no", "name": "序号", "type": "number", "required": True},
                {"key": "lagging_number", "name": "派生值", "type": "number"},
            ],
        },
        rules=rules,
        trend=[],
    )
    report = evaluate_rules(declaration, {"items": [{"cell_no": 1}]})
    assert [entry["verdict"] for entry in report.entries] == ["skipped"]
    assert "操作数缺省" in report.entries[0]["detail"]


def test_ratio_operator(make_declaration):
    rules = [{"id": "r", "kind": "limit", "tier": 2, "target": "float_voltage", "expr": "ratio:float_voltage,env_temp,1.0,2.0", "level": "warn"}]
    declaration = make_declaration(meta={"layout": "flat"}, items=False, rules=rules, trend=[])
    ok = evaluate_rules(declaration, {"float_voltage": 240.0, "env_temp": 200.0})
    assert [entry["verdict"] for entry in ok.entries] == ["pass"]
    violated = evaluate_rules(declaration, {"float_voltage": 240.0, "env_temp": 100.0})
    assert [entry["verdict"] for entry in violated.entries] == ["violation"]
    zero = evaluate_rules(declaration, {"float_voltage": 240.0, "env_temp": 0.0})
    assert [entry["verdict"] for entry in zero.entries] == ["skipped"]
    assert "分母为 0" in zero.entries[0]["detail"]
    absent = evaluate_rules(declaration, {"float_voltage": 240.0})
    assert [entry["verdict"] for entry in absent.entries] == ["skipped"]


@pytest.mark.parametrize(
    "baseline, violating",
    [
        ("mean", [1]),
        ("min", [0, 2]),
        ("max", [1]),
        ("last", [1]),
    ],
)
def test_deviation_baselines(make_declaration, baseline, violating):
    rules = [{"id": "d", "kind": "limit", "tier": 2, "target": "items.voltage", "expr": f"deviation:{baseline},5%", "level": "alarm"}]
    declaration = make_declaration(rules=rules, trend=[])
    report = evaluate_rules(declaration, {"items": _items(2.21, 1.95, 2.20)})
    verdicts = [entry["verdict"] for entry in report.entries]
    assert [index for index, verdict in enumerate(verdicts) if verdict == "violation"] == violating
    assert f"基准 {baseline}=" in report.entries[violating[0]]["detail"]
    assert report.entries[violating[0]]["level"] == "alarm"


def test_deviation_boundary_and_skips(make_declaration):
    rules = [{"id": "d", "kind": "limit", "tier": 2, "target": "items.voltage", "expr": "deviation:mean,5%", "level": "alarm"}]
    declaration = make_declaration(rules=rules, trend=[])
    # 2.0 与 2.0 → 无偏差
    assert {entry["verdict"] for entry in evaluate_rules(declaration, {"items": _items(2.0, 2.0)}).entries} == {"pass"}
    # 恰好 5%：2.00 与 2.10 → 均值 2.05，偏差 4.878% ≤ 5% → pass
    assert {entry["verdict"] for entry in evaluate_rules(declaration, {"items": _items(2.00, 2.10)}).entries} == {"pass"}
    # 超过 5%：2.00 与 2.30 → 均值 2.15，偏差 13.95% → violation
    assert "violation" in {entry["verdict"] for entry in evaluate_rules(declaration, {"items": _items(2.00, 2.30)}).entries}
    # 单条目：均值基准需 ≥2 条
    single = evaluate_rules(declaration, {"items": _items(2.0)})
    assert [entry["verdict"] for entry in single.entries] == ["skipped"]
    assert "≥2 个条目" in single.entries[0]["detail"]
    # 基准为零：0 与 0 的均值为 0，相对偏差不可算（不静默通过）
    zeros = evaluate_rules(declaration, {"items": _items(0.0, 0.0)})
    assert {entry["verdict"] for entry in zeros.entries} == {"skipped"}
    assert "基准为 0" in zeros.entries[0]["detail"]


def test_deviation_against_named_field(make_declaration):
    rules = [{"id": "d", "kind": "limit", "tier": 2, "target": "items.voltage", "expr": "deviation:float_voltage,1%", "level": "warn"}]
    declaration = make_declaration(rules=rules, trend=[])
    report = evaluate_rules(declaration, {"float_voltage": 2.10, "items": _items(2.21)})
    assert [entry["verdict"] for entry in report.entries] == ["violation"]
    assert "基准 float_voltage=2.1" in report.entries[0]["detail"]


def test_diff_operator(make_declaration):
    ok_rule = [{"id": "d", "kind": "limit", "tier": 2, "target": "float_voltage", "expr": "diff:float_voltage,env_temp,ge:5", "level": "warn"}]
    declaration = make_declaration(meta={"layout": "flat"}, items=False, rules=ok_rule, trend=[])
    assert [entry["verdict"] for entry in evaluate_rules(declaration, {"float_voltage": 240.0, "env_temp": 235.0}).entries] == ["pass"]
    assert [entry["verdict"] for entry in evaluate_rules(declaration, {"float_voltage": 240.0, "env_temp": 238.0}).entries] == ["violation"]
    le_rule = [dict(ok_rule[0], expr="diff:float_voltage,env_temp,le:1")]
    declaration_le = make_declaration(meta={"layout": "flat"}, items=False, rules=le_rule, trend=[])
    assert [entry["verdict"] for entry in evaluate_rules(declaration_le, {"float_voltage": 240.0, "env_temp": 239.5}).entries] == ["pass"]


def test_date_diff_operator_handles_timezone(make_declaration):
    fields = [
        {"key": "start_at", "name": "开始", "type": "datetime", "required": True},
        {"key": "end_at", "name": "结束", "type": "datetime", "required": True},
    ]
    rules = [{"id": "dd", "kind": "limit", "tier": 2, "target": "start_at", "expr": "date_diff:start_at,end_at,le:2d", "level": "warn"}]
    declaration = make_declaration(
        meta={"layout": "flat", "dedupe_key": ["station", "occurred_day"]},
        fields=fields,
        items=False,
        rules=rules,
        trend=[],
    )
    within = {"start_at": "2026-09-17T10:00:00+08:00", "end_at": "2026-09-19T09:00:00+08:00"}
    assert [entry["verdict"] for entry in evaluate_rules(declaration, within).entries] == ["pass"]
    beyond = {"start_at": "2026-09-17T10:00:00+08:00", "end_at": "2026-09-20T10:00:00+08:00"}
    assert [entry["verdict"] for entry in evaluate_rules(declaration, beyond).entries] == ["violation"]
    # 同一瞬间、不同时区表示 → 间隔 0 天
    same = {"start_at": "2026-09-17T10:00:00+08:00", "end_at": "2026-09-17T02:00:00+00:00"}
    assert [entry["verdict"] for entry in evaluate_rules(declaration, same).entries] == ["pass"]


def test_alarm_candidates_and_actions(make_declaration):
    rules = [
        {"id": "band", "kind": "limit", "tier": 1, "target": "items.voltage", "expr": "band:2.00,2.25", "level": "warn"},
        {
            "id": "dev",
            "kind": "limit",
            "tier": 2,
            "target": "items.voltage",
            "expr": "deviation:mean,5%",
            "level": "alarm",
            "action": "FLAG_ITEM",
        },
    ]
    declaration = make_declaration(rules=rules, trend=[])
    report = evaluate_rules(declaration, {"items": _items(2.21, 1.95, 2.20)})
    assert report.alarm_candidates == [("dev", "items[1].cell_no=2")]
    assert [action["code"] for action in report.actions] == ["FLAG_ITEM"]
    assert report.actions[0]["text"].startswith("dev：")
    assert all(entry["level"] == "warn" for entry in report.entries if entry["verdict"] == "violation" and entry["rule_id"] == "band")


def test_actions_are_deduplicated_by_code(make_declaration):
    rules = [
        {"id": "band", "kind": "limit", "tier": 1, "target": "items.voltage", "expr": "band:2.00,2.25", "level": "alarm", "action": "FLAG_ITEM"}
    ]
    declaration = make_declaration(rules=rules, trend=[])
    report = evaluate_rules(declaration, {"items": _items(1.0, 1.1)})
    assert len(report.actions) == 1
    assert len([entry for entry in report.entries if entry["verdict"] == "violation"]) == 2


def test_action_code_outside_declaration_is_refused_at_runtime():
    """绕过 meta-test 直接构造声明时，运行期仍拦截未声明的动作码（§10.3）。"""
    declaration = Declaration(
        record_type="synthetic_direct",
        title="合成直建声明（测试夹具）",
        schema_version="1.5",
        layout="flat",
        dedupe_key=("station",),
        link_types=(),
        extra="reject",
        signature_slots=("记录人",),
        action_codes=(),
        escalate_after=2,
        fields=(FieldSpec(key="current", name="电流", kind="number", required=True),),
        items=None,
        rules=(
            RuleSpec(
                id="limit_rule",
                kind="limit",
                tier=1,
                expr="lte:5.0",
                target="current",
                level="warn",
                action="NOT_DECLARED",
            ),
        ),
        trends=(),
    )
    with pytest.raises(Rejected) as excinfo:
        evaluate_rules(declaration, {"current": 9.0})
    assert excinfo.value.issues[0].code == "E_ACTION_CODE"
