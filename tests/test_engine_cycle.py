"""§10.3 周期探针 ``cycle_status``：三态与 now 临界、voided 过滤、correct 并存窗口不误报。

「做过」口径（§8.3）：记录非 voided 且存在 confirmed/archived 版本。
"""

from __future__ import annotations

import pytest

import records_kit

BATTERY_UID = "ST001-battery_voltage_test-20260901-1000-1"


def _done_row(helpers, occurred_at: str, *, lifecycle: str = "confirmed", test_kind: str = "定期") -> dict:
    return helpers.ledger_row(
        f"ST001-battery_voltage_test-{occurred_at[0:4]}{occurred_at[5:7]}{occurred_at[8:10]}-1000-1",
        lifecycle=lifecycle,
        occurred_at=occurred_at,
        fields=helpers.battery_payload(test_kind=test_kind),
    )


def _cycle(result):
    assert result["status"] == "ok", result["validation"]
    assert len(result["cycle"]) == 1
    return result["cycle"][0]


def test_due_when_within_the_period(helpers):
    rows = [_done_row(helpers, "2026-09-10T10:00:00+08:00")]
    entry = _cycle(records_kit.process(helpers.envelope("cycle_probe", ledger_view=helpers.ledger_view(rows))))
    assert entry["verdict"] == "due"
    assert entry["due_at"] == "2026-10-10T10:00:00+08:00"
    assert entry["overdue_since"] is None
    assert entry["record_type"] == "battery_voltage_test"


def test_overdue_past_the_due_date(helpers):
    rows = [_done_row(helpers, "2026-08-01T10:00:00+08:00")]
    entry = _cycle(records_kit.process(helpers.envelope("cycle_probe", ledger_view=helpers.ledger_view(rows))))
    assert entry["verdict"] == "overdue"
    assert entry["due_at"] == "2026-08-31T10:00:00+08:00"
    assert entry["overdue_since"] == entry["due_at"]


def test_now_equal_to_due_date_is_still_due(helpers):
    rows = [_done_row(helpers, "2026-08-18T15:00:00+08:00")]  # +30d = 2026-09-17T15:00:00+08:00 == now
    entry = _cycle(records_kit.process(helpers.envelope("cycle_probe", ledger_view=helpers.ledger_view(rows))))
    assert entry["verdict"] == "due"
    assert entry["overdue_since"] is None


def test_missing_without_any_completed_record(helpers):
    entry = _cycle(records_kit.process(helpers.envelope("cycle_probe", ledger_view=helpers.ledger_view())))
    assert entry["verdict"] == "missing"
    assert entry["due_at"] is None
    assert "漏做" in entry["detail"]
    assert "未配置 cycle_baseline" in entry["detail"]


def test_latest_completion_wins(helpers):
    rows = [
        _done_row(helpers, "2026-07-01T10:00:00+08:00"),
        _done_row(helpers, "2026-09-10T10:00:00+08:00"),
    ]
    entry = _cycle(records_kit.process(helpers.envelope("cycle_probe", ledger_view=helpers.ledger_view(rows))))
    assert entry["verdict"] == "due"
    assert "最后完成 2026-09-10T10:00:00+08:00" in entry["detail"]


@pytest.mark.parametrize("lifecycle", ["draft", "voided"])
def test_unfinished_and_voided_records_do_not_count_as_done(helpers, lifecycle):
    rows = [_done_row(helpers, "2026-09-10T10:00:00+08:00", lifecycle=lifecycle)]
    entry = _cycle(records_kit.process(helpers.envelope("cycle_probe", ledger_view=helpers.ledger_view(rows))))
    assert entry["verdict"] == "missing"


def test_correct_window_does_not_fake_a_missing(helpers):
    """并存窗口内行 lifecycle=confirmed（最高完成态）→ 计入做过，不误报漏做。"""
    row = helpers.ledger_row(
        BATTERY_UID,
        rev=2,
        lifecycle="confirmed",
        occurred_at="2026-09-10T10:00:00+08:00",
        fields=helpers.battery_payload(),
        confirmed_fields=helpers.battery_payload(),
    )
    entry = _cycle(records_kit.process(helpers.envelope("cycle_probe", ledger_view=helpers.ledger_view([row]))))
    assert entry["verdict"] == "due"
    assert "最后完成 2026-09-10T10:00:00+08:00" in entry["detail"]


def test_when_filter_excludes_other_kinds(helpers):
    rows = [_done_row(helpers, "2026-09-10T10:00:00+08:00", test_kind="核对性放电")]
    entry = _cycle(records_kit.process(helpers.envelope("cycle_probe", ledger_view=helpers.ledger_view(rows))))
    assert entry["verdict"] == "missing"


def test_rows_without_parseable_time_are_noted(helpers):
    row = helpers.ledger_row(BATTERY_UID, lifecycle="confirmed", occurred_at=None)
    entry = _cycle(records_kit.process(helpers.envelope("cycle_probe", ledger_view=helpers.ledger_view([row]))))
    assert entry["verdict"] == "missing"
    assert "缺可解析时间" in entry["detail"]


def test_confirmed_then_voided_record_is_not_counted(helpers):
    """confirmed→voided 的记录行 lifecycle=voided（吸收态），不遮掩也不虚报（§8.3）。"""
    rows = [_done_row(helpers, "2026-09-10T10:00:00+08:00", lifecycle="voided")]
    entry = _cycle(records_kit.process(helpers.envelope("cycle_probe", ledger_view=helpers.ledger_view(rows))))
    assert entry["verdict"] == "missing"


def test_month_end_period_arithmetic(helpers, synthetic_registry):
    """自然月月末收敛：1-31 的下一次应做日是 2-28（§10.3 自然月月末）。"""
    registry = synthetic_registry(
        helpers.build_toml(rules=[{"id": "monthly_check", "kind": "cycle", "tier": 1, "expr": "monthly"}], trend=[])
    )
    row = helpers.ledger_row("ST001-synthetic_record-20260131-1000-1", lifecycle="confirmed", occurred_at="2026-01-31T10:00:00+08:00")
    result = records_kit.process(
        helpers.envelope("cycle_probe", record_type="synthetic_record", ledger_view=helpers.ledger_view([row])),
        registry,
    )
    assert result["status"] == "ok", result["validation"]
    assert result["cycle"][0]["due_at"] == "2026-02-28T10:00:00+08:00"


def test_cycle_baseline_gives_a_due_date_when_never_done(helpers, synthetic_registry):
    registry = synthetic_registry(
        helpers.build_toml(
            rules=[{"id": "cycle_30", "kind": "cycle", "tier": 1, "expr": "periodic:30d", "cycle_baseline": "2026-08-01T00:00:00+08:00"}],
            trend=[],
        )
    )
    result = records_kit.process(
        helpers.envelope("cycle_probe", record_type="synthetic_record", ledger_view=helpers.ledger_view()), registry
    )
    assert result["status"] == "ok", result["validation"]
    entry = result["cycle"][0]
    assert entry["verdict"] == "missing"
    assert entry["due_at"] == "2026-08-31T00:00:00+08:00"
    assert entry["overdue_since"] == entry["due_at"]


def test_probe_all_types_and_single_type(helpers, synthetic_registry):
    registry = synthetic_registry(
        helpers.build_toml(rules=[{"id": "cycle_30", "kind": "cycle", "tier": 1, "expr": "periodic:30d"}], trend=[])
    )
    every_type = helpers.envelope("cycle_probe", ledger_view=helpers.ledger_view())
    every_type.pop("record_type")  # 省略 record_type = 对 registry 全部类型逐一探测
    all_types = records_kit.process(every_type, registry)
    assert {entry["record_type"] for entry in all_types["cycle"]} == {"synthetic_record"}
    one = records_kit.process(
        helpers.envelope("cycle_probe", record_type="synthetic_record", ledger_view=helpers.ledger_view()), registry
    )
    assert len(one["cycle"]) == 1
    assert one["record"] is None and one["digest"] is None and one["rules"] == []


def test_probe_requires_ledger_and_known_type(helpers):
    missing_ledger = records_kit.process(helpers.envelope("cycle_probe"))
    assert missing_ledger["status"] == "rejected"
    assert ("E_REQUIRED", "ledger_view") in {
        (error["code"], error["path"]) for error in missing_ledger["validation"]["errors"]
    }
    unknown = records_kit.process(helpers.envelope("cycle_probe", record_type="no_such_type", ledger_view=helpers.ledger_view()))
    assert unknown["validation"]["errors"][0]["code"] == "E_RECORD_TYPE"


def test_declaration_without_cycle_rule_probes_nothing(helpers, synthetic_registry):
    registry = synthetic_registry(helpers.build_toml(rules=[], trend=[]))
    probe = helpers.envelope("cycle_probe", ledger_view=helpers.ledger_view())
    probe.pop("record_type")
    result = records_kit.process(probe, registry)
    assert result["status"] == "ok"
    assert result["cycle"] == []
