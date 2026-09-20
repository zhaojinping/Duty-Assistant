"""§10.3 趋势判定：窗口不足不静默通过、drop_warn 口径、取不到值的行计入 evidence。

**M1 默认口径**（主设 §12 列为待定）：窗口 = 历史点 + 当前点；``drop_warn`` 为相对降幅、
基准为窗口内最早值。定稿后只需改 ``engine/trend.py`` 与声明。
"""

from __future__ import annotations

import records_kit


def _trend(result):
    assert result["status"] == "ok", result["validation"]
    assert len(result["trend"]) == 1
    return result["trend"][0]


def _history(helpers, count: int, voltage: float, *, start_day: int = 1) -> list[dict]:
    return [
        helpers.history_row(f"2026-08-{start_day + index:02d}T10:00:00+08:00", voltage)
        for index in range(count)
    ]


def test_no_history_reports_insufficient_history(helpers):
    result = records_kit.process(helpers.envelope("create"))
    entry = _trend(result)
    assert entry["verdict"] == "insufficient_history"
    assert entry["level"] == "info"
    assert "1/6" in entry["evidence"]
    assert entry["metric"] == "cell_voltage_min"


def test_partial_window_reports_n_over_window(helpers):
    rows = _history(helpers, 3, 2.30)
    payload = helpers.battery_payload(items=[{"cell_no": 1, "voltage": 2.30}])
    entry = _trend(records_kit.process(helpers.envelope("create", payload=payload, history=rows)))
    assert entry["verdict"] == "insufficient_history"
    assert "4/6" in entry["evidence"]


def test_dropping_when_relative_drop_reaches_threshold(helpers):
    rows = _history(helpers, 5, 2.30)
    payload = helpers.battery_payload(items=[{"cell_no": 1, "voltage": 2.00}])
    entry = _trend(records_kit.process(helpers.envelope("create", payload=payload, history=rows)))
    assert entry["verdict"] == "dropping"
    assert entry["level"] == "warn"
    assert "0.1" in entry["evidence"]


def test_stable_when_drop_is_below_threshold(helpers):
    rows = _history(helpers, 5, 2.30)
    payload = helpers.battery_payload(items=[{"cell_no": 1, "voltage": 2.28}])
    entry = _trend(records_kit.process(helpers.envelope("create", payload=payload, history=rows)))
    assert entry["verdict"] == "stable"
    assert entry["level"] == "info"


def test_only_the_last_window_is_used(helpers):
    rows = _history(helpers, 6, 2.30) + _history(helpers, 2, 2.28, start_day=7)
    payload = helpers.battery_payload(items=[{"cell_no": 1, "voltage": 2.00}])
    entry = _trend(records_kit.process(helpers.envelope("create", payload=payload, history=rows)))
    assert entry["verdict"] == "dropping"
    assert "窗口 6/6 点" in entry["evidence"]


def test_rows_without_the_source_value_are_counted_in_evidence(helpers):
    rows = _history(helpers, 5, 2.30)
    unusable = helpers.history_row("2026-09-01T10:00:00+08:00", 2.30, digest="sha256:" + "9" * 64)
    unusable["fields"] = {}  # 旧数据缺逐只明细 → source 取不到值（§5.1）
    rows.append(unusable)
    payload = helpers.battery_payload(items=[{"cell_no": 1, "voltage": 2.00}])
    entry = _trend(records_kit.process(helpers.envelope("create", payload=payload, history=rows)))
    assert entry["verdict"] == "dropping"
    assert "跳过 1 行" in entry["evidence"]


def test_current_record_without_the_metric_does_not_produce_a_conclusion(helpers, synthetic_registry):
    registry = synthetic_registry(
        helpers.build_toml(
            items={
                "key_field": "cell_no",
                "required": False,
                "fields": [
                    {"key": "cell_no", "name": "序号", "type": "number"},
                    {"key": "voltage", "name": "单体电压", "type": "number"},
                ],
            },
            rules=[],
            trend=[{"metric": "avg_v", "source": "avg(items.voltage)", "window": 2, "drop_warn": 0.1}],
        )
    )
    payload = {"test_kind": "定期", "float_voltage": 241.5}
    row = helpers.history_row("2026-08-17T10:00:00+08:00", 2.30)
    result = records_kit.process(
        helpers.envelope("create", record_type="synthetic_record", payload=payload, history=[row]), registry
    )
    assert result["status"] == "ok", result["validation"]
    entry = result["trend"][0]
    assert entry["verdict"] == "insufficient_history"
    assert "取不到值" in entry["evidence"]


def test_zero_baseline_is_not_comparable(helpers):
    rows = _history(helpers, 5, 0.0)
    payload = helpers.battery_payload(items=[{"cell_no": 1, "voltage": 2.00}])
    entry = _trend(records_kit.process(helpers.envelope("create", payload=payload, history=rows)))
    assert entry["verdict"] == "insufficient_history"
    assert "首值为 0" in entry["evidence"]


def test_trend_entry_shape(helpers):
    result = records_kit.process(helpers.envelope("create"))
    entry = _trend(result)
    assert set(entry) == {"metric", "verdict", "level", "evidence"}


def test_declaration_without_trends_yields_empty_list(helpers, synthetic_registry):
    registry = synthetic_registry(helpers.build_toml(trend=[]))
    payload = {"test_kind": "定期", "float_voltage": 241.5, "items": [{"cell_no": 1, "voltage": 2.21}]}
    result = records_kit.process(
        helpers.envelope("create", record_type="synthetic_record", payload=payload), registry
    )
    assert result["status"] == "ok"
    assert result["trend"] == []


def test_avg_and_count_aggregations(helpers, synthetic_registry):
    registry = synthetic_registry(
        helpers.build_toml(trend=[{"metric": "avg_v", "source": "avg(items.voltage)", "window": 2, "drop_warn": 0.05}])
    )
    payload = {"test_kind": "定期", "float_voltage": 241.5, "items": [{"cell_no": 1, "voltage": 2.00}, {"cell_no": 2, "voltage": 2.00}]}
    row = helpers.history_row("2026-08-17T10:00:00+08:00", 2.20)
    result = records_kit.process(
        helpers.envelope("create", record_type="synthetic_record", payload=payload, history=[row]), registry
    )
    assert result["status"] == "ok", result["validation"]
    assert result["trend"][0]["verdict"] == "dropping"
    assert "2.2 → 2.0" in result["trend"][0]["evidence"]
