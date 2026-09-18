"""§10.5 告警闭环专项：``alarm_history`` 驱动的 new/recurred/escalated 与 suppressed。

判定顺序与计数语义（§6.2）：``occur_count`` 是**此前**出现次数（不含本次）；
``occur_count + 1 ≥ escalate_after`` → escalated；``occur_count ≥ 1`` → recurred；否则 new。
抑制：recurred → suppressed=true；new/escalated → suppressed=false。
"""

from __future__ import annotations

import pytest

import records_kit

ALARM_RULE = [
    {
        "id": "voltage_alarm",
        "kind": "limit",
        "tier": 1,
        "target": "items.voltage",
        "expr": "band:2.00,2.25",
        "level": "alarm",
        "action": "FLAG_ITEM",
    }
]
WARN_ONLY_RULE = [
    {"id": "voltage_warn", "kind": "limit", "tier": 1, "target": "items.voltage", "expr": "band:2.00,2.25", "level": "warn"}
]


@pytest.fixture
def alarm_registry(synthetic_registry, helpers):
    def _make(rules=ALARM_RULE, escalate_after=3):
        return synthetic_registry(helpers.build_toml(rules=rules, trend=[], meta={"escalate_after": escalate_after}))

    return _make


def _payload(helpers, *, cell_no: int = 1, voltage: float = 1.90) -> dict:
    return {
        "test_kind": "定期",
        "float_voltage": 241.5,
        "items": [{"cell_no": cell_no, "voltage": voltage}],
    }


def _run(helpers, registry, *, alarm_history=None, **payload_kwargs):
    envelope = helpers.envelope("create", record_type="synthetic_record", payload=_payload(helpers, **payload_kwargs))
    if alarm_history is not None:
        envelope["alarm_history"] = alarm_history
    return records_kit.process(envelope, registry)


def test_no_alarm_level_violation_produces_no_fingerprint(helpers, alarm_registry):
    result = _run(helpers, alarm_registry(rules=WARN_ONLY_RULE), voltage=1.90)
    assert result["status"] == "ok"
    assert result["alarm_state"]["fingerprint"] is None
    assert result["alarm_state"]["state"] is None
    assert result["alarm_state"]["suppressed"] is False
    assert "无 alarm 级违规" in result["alarm_state"]["evidence"]


def test_first_occurrence_is_new_and_not_suppressed(helpers, alarm_registry):
    result = _run(helpers, alarm_registry())
    state = result["alarm_state"]
    assert state["state"] == "new"
    assert state["suppressed"] is False
    assert state["fingerprint"].startswith("fp:")
    assert "occur_count=0" in state["evidence"]
    assert "未传 alarm_history" in state["evidence"]


def test_recurrence_is_suppressed(helpers, alarm_registry):
    first = _run(helpers, alarm_registry())
    mark = first["alarm_state"]["fingerprint"]
    again = _run(helpers, alarm_registry(), alarm_history=[{"fingerprint": mark, "occur_count": 1}])
    assert again["alarm_state"]["state"] == "recurred"
    assert again["alarm_state"]["suppressed"] is True
    assert again["alarm_state"]["fingerprint"] == mark


def test_escalation_after_threshold(helpers, alarm_registry):
    registry = alarm_registry(escalate_after=3)
    mark = _run(helpers, registry)["alarm_state"]["fingerprint"]
    escalated = _run(helpers, registry, alarm_history=[{"fingerprint": mark, "occur_count": 2}])
    assert escalated["alarm_state"]["state"] == "escalated"
    assert escalated["alarm_state"]["suppressed"] is False


def test_escalation_threshold_boundary(helpers, alarm_registry):
    registry = alarm_registry(escalate_after=2)
    mark = _run(helpers, registry)["alarm_state"]["fingerprint"]
    result = _run(helpers, registry, alarm_history=[{"fingerprint": mark, "occur_count": 1}])
    assert result["alarm_state"]["state"] == "escalated"


def test_history_rows_for_other_fingerprints_are_ignored(helpers, alarm_registry):
    registry = alarm_registry()
    result = _run(helpers, registry, alarm_history=[{"fingerprint": "fp:other", "occur_count": 5}])
    assert result["alarm_state"]["state"] == "new"


def test_fingerprint_is_stable_and_depends_on_station_rule_and_target(helpers, alarm_registry):
    registry = alarm_registry()
    first = _run(helpers, registry)["alarm_state"]["fingerprint"]
    again = _run(helpers, registry)["alarm_state"]["fingerprint"]
    assert first == again

    other_cell = _run(helpers, registry, cell_no=7)["alarm_state"]["fingerprint"]
    assert other_cell != first

    other_station = records_kit.process(
        helpers.envelope(
            "create",
            record_type="synthetic_record",
            station={"station_id": "ST009", "station_name": "另一站（合成）"},
            payload=_payload(helpers),
        ),
        registry,
    )["alarm_state"]["fingerprint"]
    assert other_station != first

    warn_registry = alarm_registry(
        rules=[dict(ALARM_RULE[0], id="other_alarm", level="alarm")],
    )
    assert _run(helpers, warn_registry)["alarm_state"]["fingerprint"] != first


def test_first_alarm_rule_in_declaration_order_wins(helpers, alarm_registry):
    rules = [
        dict(ALARM_RULE[0], id="first_alarm"),
        dict(ALARM_RULE[0], id="second_alarm"),
    ]
    registry = alarm_registry(rules=rules)
    result = _run(helpers, registry)
    assert "命中规则 first_alarm" in result["alarm_state"]["evidence"]
    assert len(result["actions_hint"]) == 1


def test_alarm_history_is_consumed_only_when_a_candidate_exists(helpers, alarm_registry):
    registry = alarm_registry(rules=WARN_ONLY_RULE)
    result = _run(helpers, registry, alarm_history=[{"fingerprint": "fp:x", "occur_count": 4}])
    assert result["alarm_state"]["state"] is None
    assert result["rules"]  # 规则照常判定，只是不产生告警指纹


@pytest.mark.parametrize("occur_count", [0, 1, 2, 3, 9])
def test_escalated_count_is_not_consumed(helpers, alarm_registry, occur_count):
    """``escalated_count`` 供壳层控制升级频率，核心判定不消费该字段（§6.2）。"""
    registry = alarm_registry(escalate_after=3)
    mark = _run(helpers, registry)["alarm_state"]["fingerprint"]
    baseline = _run(helpers, registry, alarm_history=[{"fingerprint": mark, "occur_count": occur_count}])
    with_count = _run(
        helpers,
        registry,
        alarm_history=[{"fingerprint": mark, "occur_count": occur_count, "escalated_count": 99}],
    )
    assert baseline["alarm_state"] == with_count["alarm_state"]
