"""卡 #24 ②：``cycle_status`` 对 registry 全量记录类型的覆盖自查。

探针口径（主设 §7.3 豁免注）：周期结论唯一出口是 ``cycle`` 字段，``rules`` 里永不含周期项。
本文件对**真实 registry 的全部记录类型**逐一探测，把「哪几类有周期语义、哪几类没有」
钉成回归——断言写成**声明驱动**（条目数 == 该类型声明的 cycle 规则数），不硬编码类型名；
末尾的覆盖矩阵快照是**有意**的变更探测器：给待现场那几类补周期规则时它会失败，
提示同步 `docs/dsl-gap-list.md` §周期 与卡面口径。
"""

from __future__ import annotations

import pytest

import records_kit
from records_kit.registry import default_registry

#: 空台账下，全量探测里「周期条目」与「配对悬空条目」的区分前缀
PAIRING_DETAIL_PREFIX = "配对悬空"

ALL_TYPES = sorted(default_registry().record_types)

#: 覆盖矩阵（卡 #24 ② 自查结论）：值 = 该类型声明的 cycle 规则数。
#: 0 的三类待现场数值（设备测温 / 绝缘测试 / 防小动物，制度汇编无周期数值条款）；
#: 其余 0 的四类为事件触发型（断路器跳闸 / 接地线 / 保护投退 / 两票），明确无周期。
EXPECTED_CYCLE_COVERAGE = {
    "battery_voltage_test": 1,
    "breaker_trip_record": 0,
    "grounding_wire_record": 0,
    "infrared_thermography_record": 0,
    "insulation_test_record": 0,
    "protection_switch_record": 0,
    "rodent_proof_check_record": 0,
    "surge_arrester_action_record": 1,
    "transformer_core_clamp_current_record": 1,
    "two_ticket_ledger": 0,
}


def _probe(record_type: str | None, helpers) -> dict:
    probe = helpers.envelope("cycle_probe", ledger_view=helpers.ledger_view())
    probe.pop("record_type")
    if record_type is not None:
        probe["record_type"] = record_type
    result = records_kit.process(probe)
    assert result["status"] == "ok", result["validation"]
    return result


def _period_entries(entries: list[dict]) -> list[dict]:
    return [entry for entry in entries if not entry["detail"].startswith(PAIRING_DETAIL_PREFIX)]


def test_registry_covers_ten_record_types(registry):
    """10 类记录全部在 registry 内（本次自查的主体范围）。"""
    assert len(registry.record_types) == 10
    assert set(registry.record_types) == set(EXPECTED_CYCLE_COVERAGE)


@pytest.mark.parametrize("record_type", ALL_TYPES)
def test_each_type_probes_without_error_and_matches_its_declaration(record_type, registry, helpers):
    """逐类探测：不报错，且周期条目数恰等于该类型声明的 cycle 规则数（空台账 → 全部 missing）。"""
    result = _probe(record_type, helpers)
    cycle_rules = [rule for rule in registry[record_type].rules if rule.kind == "cycle"]
    period_entries = _period_entries(result["cycle"])
    assert len(period_entries) == len(cycle_rules), (
        f"{record_type}：声明 {len(cycle_rules)} 条 cycle 规则，探针出了 {len(period_entries)} 条周期条目"
    )
    for entry in period_entries:
        assert entry["record_type"] == record_type
        assert entry["verdict"] == "missing"  # 空台账：无已完成记录 → 漏做
        assert "漏做" in entry["detail"]


def test_full_probe_reports_only_known_types(registry, helpers):
    """省略 ``record_type`` 的全量探测：条目只涉及 registry 内的类型，结构完整。"""
    result = _probe(None, helpers)
    assert result["cycle"], "全量探测应有条目（至少含周期或配对规则的类）"
    for entry in result["cycle"]:
        assert entry["record_type"] in registry.record_types
        assert entry["verdict"] in ("due", "overdue", "missing")
        assert set(entry) == {"record_type", "verdict", "due_at", "overdue_since", "detail"}


def test_periodic_types_report_due_with_a_completed_record(registry, helpers):
    """有周期规则的类：存在已完成记录 → ``due`` 而不是 ``missing``（三类逐个验证）。"""
    periodic_types = [name for name in ALL_TYPES if EXPECTED_CYCLE_COVERAGE[name] > 0]
    assert periodic_types == [
        "battery_voltage_test",
        "surge_arrester_action_record",
        "transformer_core_clamp_current_record",
    ]
    for record_type in periodic_types:
        declaration = registry[record_type]
        rule = next(r for r in declaration.rules if r.kind == "cycle")
        fields = {}
        for spec in declaration.fields:
            if spec.required and spec.kind == "enum":
                fields[spec.key] = spec.options[0]
            elif spec.required and spec.kind == "number":
                fields[spec.key] = 1.0
            elif spec.required and spec.kind == "text":
                fields[spec.key] = "SYNTH"
        row = helpers.ledger_row(
            f"ST001-{record_type}-20260901-1000-1",
            lifecycle="confirmed",
            occurred_at="2026-09-01T10:00:00+08:00",
            fields=fields,
        )
        probe = helpers.envelope("cycle_probe", record_type=record_type, ledger_view=helpers.ledger_view([row]))
        result = records_kit.process(probe)
        assert result["status"] == "ok", result["validation"]
        period_entries = _period_entries(result["cycle"])
        assert len(period_entries) == 1, record_type
        assert period_entries[0]["verdict"] == "due", (record_type, period_entries[0])
        assert period_entries[0]["due_at"] is not None
        # 周期规则是唯一出口：rules 里不含周期项（§7.3 豁免注）
        assert all(entry["kind"] != "cycle" for entry in result["rules"])


def test_cycle_coverage_matrix_snapshot(registry):
    """覆盖矩阵快照：3 类有周期规则、7 类无（详见 ``EXPECTED_CYCLE_COVERAGE`` 注）。

    变更探测器（有意）：给「周期数值待现场」的三类补规则后，本断言会失败——
    请同步 `docs/dsl-gap-list.md` §周期 与卡面口径，而不是直接改期望值。
    """
    actual = {
        name: len([rule for rule in registry[name].rules if rule.kind == "cycle"])
        for name in registry.record_types
    }
    assert actual == EXPECTED_CYCLE_COVERAGE
