"""§10.1 协议合规：输入/输出各一组 JSON 实例过手写权威 Schema。

Schema 是手写权威文件（``src/records_kit/protocol/schemas``），校验器同为手写实现
（不引入 jsonschema）；本文件既验「好实例通过」，也验「坏实例被拦」。
"""

from __future__ import annotations

import json

import pytest

import records_kit
from records_kit.protocol import (
    SCHEMA_DIR,
    VALID_OPERATIONS,
    check_instance,
    envelope_schema,
    result_schema,
)


def test_schema_files_are_valid_json():
    for name in ("record_envelope.schema.json", "record_result.schema.json"):
        parsed = json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8"))
        assert parsed["$schema"].endswith("2020-12/schema")
        assert parsed["additionalProperties"] is False


def test_both_schemas_enumerate_the_same_operations():
    for schema in (envelope_schema(), result_schema()):
        assert set(schema["properties"]["operation"]["enum"]) >= set(VALID_OPERATIONS)
    assert set(VALID_OPERATIONS) == {
        "create",
        "confirm",
        "return",
        "correct",
        "void",
        "archive",
        "alarm_ack",
        "cycle_probe",
    }


def test_create_envelope_passes_envelope_schema(helpers):
    assert check_instance(helpers.envelope("create"), envelope_schema()) == []


def test_full_envelope_with_all_views_passes_schema(helpers):
    envelope = helpers.envelope(
        "create",
        history=[helpers.history_row("2026-08-17T10:00:00+08:00", 2.22)],
        ledger_view=helpers.ledger_view([helpers.ledger_row("ST001-battery_voltage_test-20260817-1000-1")]),
        alarm_history=[{"fingerprint": "fp:x", "occur_count": 1, "first_seen_at": helpers.NOW}],
        baselines=[{"ref": "BL-001", "kind": "count_table", "name": "合成基线", "data": {"BRK": 3}}],
        attachments_ref=[{"kind": "photo", "ref": "synthetic-ref-1", "note": "合成附件"}],
    )
    assert check_instance(envelope, envelope_schema()) == []


@pytest.mark.parametrize(
    "mutation, fragment",
    [
        ({"now": None}, "$.now"),
        ({"protocol": "other-kit"}, "$.protocol"),
        ({"operation": "frobnicate"}, "$.operation"),
        ({"station": {"station_id": "ST001"}}, "$.station.station_name"),
        ({"station": {"station_id": "", "station_name": "X"}}, "$.station.station_id"),
        ({"create_seq": 0}, "$.create_seq"),
        ({"extra_field": 1}, "$.extra_field"),
    ],
)
def test_bad_envelopes_are_rejected_by_schema(helpers, mutation, fragment):
    envelope = helpers.envelope("create")
    for name, value in mutation.items():
        if value is None and name != "now":
            envelope.pop(name, None)
        else:
            envelope[name] = value
    if mutation.get("now", "keep") is None:
        envelope["now"] = None
    problems = check_instance(envelope, envelope_schema())
    assert problems, mutation
    assert any(fragment in problem for problem in problems), problems


@pytest.mark.parametrize(
    "field_patch",
    [
        {"subject": {"record_uid": "u", "lifecycle": "draft", "rev": 0}},
        {"subject": {"record_uid": "u", "lifecycle": "gone", "rev": 1}},
        {"links": [{"type": "whatever", "record_uid": "u"}]},
        {"history": [{"occurred_at": "2026-09-17T10:00:00+08:00", "digest": "d", "lifecycle": "draft", "fields": {}}]},
        {"ledger_view": {"same_type_records": [{"record_uid": "u", "lifecycle": "draft", "rev": 1}]}},
        {"confirmations": [{"slot": "测试人", "by": "李四"}]},
    ],
)
def test_bad_nested_views_are_rejected_by_schema(helpers, field_patch):
    envelope = helpers.envelope("confirm", subject={"record_uid": "u", "lifecycle": "draft", "rev": 1})
    envelope.update(field_patch)
    assert check_instance(envelope, envelope_schema()) != []


def test_ok_results_pass_result_schema(helpers):
    registry = records_kit.default_registry()
    create = records_kit.process(helpers.envelope("create"), registry)
    assert check_instance(create, result_schema()) == []
    record = create["record"]
    confirm = records_kit.process(
        helpers.envelope(
            "confirm",
            subject=helpers.subject(record),
            confirmations=[],
            ledger_view=helpers.ledger_view([helpers.ledger_row(record["record_uid"], digest=record["digest"])]),
        ),
        registry,
    )
    assert confirm["status"] == "ok"
    assert check_instance(confirm, result_schema()) == []
    probe = records_kit.process(helpers.envelope("cycle_probe", ledger_view=helpers.ledger_view()), registry)
    assert check_instance(probe, result_schema()) == []


@pytest.mark.parametrize(
    "mutation",
    [
        {"status": "maybe"},
        {"digest": "deadbeef"},
        {"operation": "frobnicate"},
        {"validation": {"ok": True}},
        {"record": {"record_uid": "u", "rev": 0, "lifecycle": "draft", "fields": {}, "digest": "x", "signature_slots": []}},
        {"rules": [{"rule_id": "r", "kind": "limit", "tier": 1, "verdict": "maybe", "threshold": "x", "level": "warn", "detail": ""}]},
        {"cycle": [{"record_type": "r", "verdict": "later", "due_at": None, "overdue_since": None, "detail": ""}]},
    ],
)
def test_bad_results_are_rejected_by_schema(helpers, mutation):
    result = records_kit.process(helpers.envelope("create"))
    result.update(mutation)
    assert check_instance(result, result_schema()) != []


def test_rejected_result_passes_schema_with_null_operation():
    result = records_kit.process({"protocol": "other"})
    assert check_instance(result, result_schema()) == []
    assert result["operation"] is None
    assert result["status"] == "rejected"
