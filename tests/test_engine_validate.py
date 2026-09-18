"""payload 校验与信封语义（design.md §5/§7.3/§7.6）。

用例全部走对外入口 ``process()``——校验结果以结构化错误码呈现，壳层无需 try/except。
"""

from __future__ import annotations

import pytest

import records_kit

BAD_PAYLOADS = [
    ({"dc_system_id": None}, "E_REQUIRED", "payload.dc_system_id"),
    ({"test_kind": None}, "E_REQUIRED", "payload.test_kind"),
    ({"dc_system_id": "DC-001", "test_kind": "月度"}, "E_ENUM", "payload.test_kind"),
    ({"float_voltage": "高"}, "E_TYPE", "payload.float_voltage"),
    ({"float_voltage": True}, "E_TYPE", "payload.float_voltage"),
    ({"float_voltage": float("nan")}, "E_TYPE", "payload.float_voltage"),
    ({"float_voltage": float("inf")}, "E_TYPE", "payload.float_voltage"),
    ({"float_voltage": 301.0}, "E_RANGE", "payload.float_voltage"),
    ({"float_voltage": -1.0}, "E_RANGE", "payload.float_voltage"),
    ({"env_temp": "25"}, "E_TYPE", "payload.env_temp"),
    ({"unknown_field": 1}, "E_UNKNOWN_FIELD", "payload.unknown_field"),
    ({"items": []}, "E_REQUIRED", "payload.items"),
    ({"items": "x"}, "E_TYPE", "payload.items"),
    ({"items": [{"cell_no": 1}]}, "E_REQUIRED", "payload.items[0].voltage"),
    ({"items": [{"cell_no": 1, "voltage": 16.0}]}, "E_RANGE", "payload.items[0].voltage"),
    ({"items": [{"cell_no": 1.5, "voltage": 2.0}]}, "E_TYPE", "payload.items[0].cell_no"),
    ({"items": [{"cell_no": 1, "voltage": 2.0, "extra": 1}]}, "E_UNKNOWN_FIELD", "payload.items[0].extra"),
    ({"items": [{"cell_no": 1, "voltage": 2.0}, {"cell_no": 1, "voltage": 2.1}]}, "E_DUP_KEY", "payload.items[1].cell_no"),
    ({"items": [{"cell_no": 1, "voltage": 2.0, "lagging": "大概"}]}, "E_ENUM", "payload.items[0].lagging"),
    ({"测试人": "李四"}, "E_SIGNATURE_VIOLATION", "payload.测试人"),
    ({"confirmations": []}, "E_SIGNATURE_VIOLATION", "payload.confirmations"),
    ({"items": [{"cell_no": 1, "voltage": 2.0, "测试人": "李四"}]}, "E_SIGNATURE_VIOLATION", "payload.items[0].测试人"),
]


@pytest.mark.parametrize("patch, code, path", BAD_PAYLOADS)
def test_bad_payloads_are_rejected(helpers, patch, code, path):
    envelope = helpers.envelope("create", payload=helpers.battery_payload(**patch))
    result = records_kit.process(envelope)
    assert result["status"] == "rejected"
    codes = {(error["code"], error["path"]) for error in result["validation"]["errors"]}
    assert (code, path) in codes, codes


def test_payload_optional_fields_may_be_absent(helpers):
    payload = helpers.battery_payload()
    payload.pop("float_current")
    payload.pop("env_temp")
    result = records_kit.process(helpers.envelope("create", payload=payload))
    assert result["status"] == "ok"
    assert "float_current" not in result["record"]["fields"]


def test_tri_bool_accepts_the_three_states(helpers):
    for state in ("是", "否", "不适用"):
        payload = helpers.battery_payload(items=[{"cell_no": 1, "voltage": 2.2, "lagging": state}])
        assert records_kit.process(helpers.envelope("create", payload=payload))["status"] == "ok"


def test_extra_allow_declaration_accepts_unknown_fields(synthetic_registry, helpers):
    registry = synthetic_registry(helpers.build_toml(meta={"extra": "allow"}))
    payload = helpers.battery_payload(extra_field="自由字段")
    result = records_kit.process(
        helpers.envelope("create", record_type="synthetic_record", payload=payload), registry
    )
    assert result["status"] == "ok"


def test_require_attachment_is_enforced(synthetic_registry, helpers):
    fields = [
        {"key": "test_kind", "name": "测试性质", "type": "text", "required": True},
        {"key": "photo_ref", "name": "红外图", "type": "text", "require_attachment": "photo"},
    ]
    text = helpers.build_toml(meta={"layout": "flat"}, fields=fields, items=False, rules=[], trend=[])
    registry = synthetic_registry(text)
    payload = {"test_kind": "例行", "photo_ref": "synthetic-ref-1"}
    missing = records_kit.process(
        helpers.envelope("create", record_type="synthetic_record", payload=payload), registry
    )
    assert missing["status"] == "rejected"
    assert ("E_REQUIRED", "attachments_ref") in {
        (error["code"], error["path"]) for error in missing["validation"]["errors"]
    }
    attached = records_kit.process(
        helpers.envelope(
            "create",
            record_type="synthetic_record",
            payload=payload,
            attachments_ref=[{"kind": "photo", "ref": "synthetic-ref-1"}],
        ),
        registry,
    )
    assert attached["status"] == "ok"


# ---------------------------------------------------------------- 信封语义


@pytest.mark.parametrize(
    "patch, code, path",
    [
        ({"protocol": "other-kit"}, "E_PROTOCOL", "protocol"),
        ({"protocol_version": "2.0"}, "E_PROTOCOL", "protocol_version"),
        ({"now": None}, "E_NOW_MISSING", "now"),
        ({"now": "2026-09-17 15:00:00"}, "E_TIME_INVALID", "now"),
        ({"extra_field": 1}, "E_PROTOCOL", "extra_field"),
        ({"station": {"station_id": "ST001"}}, "E_REQUIRED", "station.station_name"),
        ({"occurred_at": "2026-09-18T00:00:00+08:00"}, "E_TIME_INVALID", "occurred_at"),
        ({"occurred_at": None}, "E_REQUIRED", "occurred_at"),
        ({"occurred_at": "2026-09-17T10:00:00"}, "E_TIME_INVALID", "occurred_at"),
        ({"create_seq": None}, "E_REQUIRED", "create_seq"),
        ({"create_seq": 0}, "E_TYPE", "create_seq"),
        ({"create_seq": "1"}, "E_TYPE", "create_seq"),
        ({"submitted_by": None}, "E_REQUIRED", "submitted_by"),
        ({"subject": {"record_uid": "u", "lifecycle": "draft", "rev": 1}}, "E_STATE_ILLEGAL", "subject"),
    ],
)
def test_bad_create_envelopes_are_rejected(helpers, patch, code, path):
    envelope = helpers.envelope("create")
    for name, value in patch.items():
        envelope[name] = value
    result = records_kit.process(envelope)
    assert result["status"] == "rejected"
    assert (code, path) in {(error["code"], error["path"]) for error in result["validation"]["errors"]}


def test_missing_record_type_and_unknown_type(helpers):
    envelope = helpers.envelope("create")
    envelope.pop("record_type")
    result = records_kit.process(envelope)
    assert ("E_REQUIRED", "record_type") in {
        (error["code"], error["path"]) for error in result["validation"]["errors"]
    }
    unknown = records_kit.process(helpers.envelope("create", record_type="no_such_type"))
    assert unknown["validation"]["errors"][0]["code"] == "E_RECORD_TYPE"


def test_subject_operations_require_subject_and_ledger(helpers):
    missing_subject = records_kit.process(helpers.envelope("void", void_reason="r", voided_by="张三"))
    assert ("E_REQUIRED", "subject") in {
        (error["code"], error["path"]) for error in missing_subject["validation"]["errors"]
    }
    missing_ledger = records_kit.process(
        helpers.envelope("void", subject={"record_uid": "u", "lifecycle": "draft", "rev": 1}, void_reason="r", voided_by="张三")
    )
    assert ("E_REQUIRED", "ledger_view") in {
        (error["code"], error["path"]) for error in missing_ledger["validation"]["errors"]
    }


def test_return_requires_reason_and_confirm_requires_confirmations(helpers):
    record = helpers.created_record()
    view = helpers.ledger_view([helpers.ledger_row(record["record_uid"], digest=record["digest"])])
    no_reason = records_kit.process(helpers.envelope("return", subject=helpers.subject(record), ledger_view=view))
    assert ("E_REQUIRED", "return_reason") in {
        (error["code"], error["path"]) for error in no_reason["validation"]["errors"]
    }
    no_confirmations = records_kit.process(helpers.envelope("confirm", subject=helpers.subject(record), ledger_view=view))
    assert ("E_REQUIRED", "confirmations") in {
        (error["code"], error["path"]) for error in no_confirmations["validation"]["errors"]
    }


def test_invalid_ledger_and_history_rows_are_rejected(helpers):
    bad_rev = helpers.ledger_view([helpers.ledger_row("u", rev=0)])
    result = records_kit.process(
        helpers.envelope("void", subject={"record_uid": "u", "lifecycle": "draft", "rev": 1}, ledger_view=bad_rev, void_reason="r", voided_by="张三")
    )
    assert ("E_TYPE", "ledger_view.same_type_records[0].rev") in {
        (error["code"], error["path"]) for error in result["validation"]["errors"]
    }
    stray_key = helpers.ledger_view([helpers.ledger_row("u", stray="x")])
    result = records_kit.process(
        helpers.envelope("void", subject={"record_uid": "u", "lifecycle": "draft", "rev": 1}, ledger_view=stray_key, void_reason="r", voided_by="张三")
    )
    assert ("E_TYPE", "ledger_view.same_type_records[0].stray") in {
        (error["code"], error["path"]) for error in result["validation"]["errors"]
    }


@pytest.mark.parametrize(
    "rows, path",
    [
        ([{"occurred_at": "2026-08-17T10:00:00+08:00", "digest": "d", "lifecycle": "draft", "fields": {}}], "history[0].lifecycle"),
        ([{"occurred_at": "2026-08-17T10:00:00", "digest": "d", "lifecycle": "archived", "fields": {}}], "history[0].occurred_at"),
        (
            [
                {"occurred_at": "2026-09-17T10:00:00+08:00", "digest": "d", "lifecycle": "archived", "fields": {}},
                {"occurred_at": "2026-08-17T10:00:00+08:00", "digest": "d", "lifecycle": "archived", "fields": {}},
            ],
            "history[1].occurred_at",
        ),
        ([{"occurred_at": "2026-08-17T10:00:00+08:00", "lifecycle": "archived", "fields": {}}], "history[0].digest"),
    ],
)
def test_bad_history_rows_are_rejected(helpers, rows, path):
    result = records_kit.process(helpers.envelope("create", history=rows))
    assert result["status"] == "rejected"
    assert any(error["path"] == path for error in result["validation"]["errors"])


def test_duplicate_history_rows_are_rejected(helpers):
    row = helpers.history_row("2026-08-17T10:00:00+08:00", 2.22)
    rows = [row, dict(row, occurred_at="2026-09-17T09:00:00+08:00")]
    duplicate_key = rows[:1] + rows[:1]
    result = records_kit.process(helpers.envelope("create", history=duplicate_key))
    assert result["status"] == "rejected"
    assert any(error["path"].startswith("history[1]") for error in result["validation"]["errors"])
    assert records_kit.process(helpers.envelope("create", history=rows))["status"] == "ok"


def test_bad_alarm_history_is_rejected(helpers):
    result = records_kit.process(helpers.envelope("create", alarm_history=[{"occur_count": 1}]))
    assert result["status"] == "rejected"
    assert ("E_TYPE", "alarm_history[0].fingerprint") in {
        (error["code"], error["path"]) for error in result["validation"]["errors"]
    }


def test_baselines_must_carry_ref_and_data(helpers):
    result = records_kit.process(helpers.envelope("create", baselines=[{"name": "x", "data": {}}]))
    assert result["status"] == "rejected"
    assert ("E_TYPE", "baselines[0]") in {
        (error["code"], error["path"]) for error in result["validation"]["errors"]
    }


# ---------------------------------------------------------------- links（§7.6）


def test_links_must_be_declared_and_resolvable(helpers):
    target_uid = "ST001-battery_voltage_test-20260817-1000-1"
    linked = [helpers.ledger_row(target_uid)]
    envelope = helpers.envelope(
        "create",
        links=[{"type": "retest_of", "record_uid": target_uid}],
        ledger_view=helpers.ledger_view(linked=linked),
    )
    result = records_kit.process(envelope)
    assert result["status"] == "ok", result["validation"]
    assert result["record"]["links"] == [{"type": "retest_of", "record_uid": target_uid}]

    undeclared = records_kit.process(
        helpers.envelope("create", links=[{"type": "references", "record_uid": "x"}])
    )
    assert ("E_LINK_INVALID", "links[0].type") in {
        (error["code"], error["path"]) for error in undeclared["validation"]["errors"]
    }

    missing_reference = records_kit.process(
        helpers.envelope("create", links=[{"type": "retest_of", "record_uid": "no-such-uid"}])
    )
    assert ("E_LINK_INVALID", "links[0].record_uid") in {
        (error["code"], error["path"]) for error in missing_reference["validation"]["errors"]
    }


def test_supersedes_cannot_be_supplied_by_caller(synthetic_registry, helpers):
    registry = synthetic_registry(helpers.build_toml(meta={"link_types": ["supersedes"]}))
    payload = {"test_kind": "定期", "float_voltage": 241.5, "items": [{"cell_no": 1, "voltage": 2.21}]}
    result = records_kit.process(
        helpers.envelope(
            "create",
            record_type="synthetic_record",
            payload=payload,
            links=[{"type": "supersedes", "record_uid": "u"}],
        ),
        registry,
    )
    assert result["status"] == "rejected"
    assert ("E_LINK_INVALID", "links[0]") in {
        (error["code"], error["path"]) for error in result["validation"]["errors"]
    }
