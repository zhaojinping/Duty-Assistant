"""§10.3 状态机全流转 + 并发/幂等（design.md §8）。

表驱动覆盖：create / confirm / return / correct（**双语义**）/ void（墓碑）/
archive（含 correct 并存窗口的归档目标版本）/ alarm_ack；乐观锁 rev 冲突、
uid 撞墓碑、作废后序号不可重用、correct 触及业务键重判重、digest 精确判重。
"""

from __future__ import annotations

import pytest

import records_kit
from records_kit.util import compute_digest

BATTERY = "battery_voltage_test"


# ---------------------------------------------------------------- 小工具


def run(helpers, operation, **overrides):
    return records_kit.process(helpers.envelope(operation, **overrides))


def create(helpers, **overrides):
    result = run(helpers, "create", **overrides)
    assert result["status"] == "ok", result["validation"]
    return result["record"]


def row_of(record, lifecycle=None, **extra):
    from conftest import ledger_row

    return ledger_row(
        record["record_uid"],
        rev=record["rev"],
        lifecycle=lifecycle or record["lifecycle"],
        fields=record["fields"],
        digest=record["digest"],
        **extra,
    )


def codes(result):
    return {(error["code"], error["path"]) for error in result["validation"]["errors"]}


# ---------------------------------------------------------------- create


def test_create_assembles_uid_rev_digest(helpers):
    record = create(helpers)
    assert record["record_uid"] == "ST001-battery_voltage_test-20260917-1000-1"
    assert record["rev"] == 1
    assert record["lifecycle"] == "draft"
    assert record["digest"].startswith("sha256:") and len(record["digest"]) == 71
    assert record["signature_slots"] == [{"slot": "测试人", "state": "pending"}]
    assert record["links"] == []
    expected = compute_digest("ST001", BATTERY, helpers.OCCURRED, "1.5", helpers.battery_payload())
    assert record["digest"] == expected


def test_create_uid_conflicts_with_tombstone(helpers):
    uid = "ST001-battery_voltage_test-20260917-1000-1"
    tomb = helpers.ledger_row(uid, lifecycle="voided")
    duplicate = run(helpers, "create", ledger_view=helpers.ledger_view([tomb]))
    assert duplicate["status"] == "rejected"
    assert ("E_DUP_UID", "create_seq") in codes(duplicate)


def test_create_business_dedupe_and_void_yields(helpers):
    existing = helpers.ledger_row("ST001-battery_voltage_test-20260817-1000-1", lifecycle="confirmed")
    hit = run(helpers, "create", ledger_view=helpers.ledger_view([existing]))
    assert ("E_DUP_KEY", "payload") in codes(hit)

    voided = helpers.ledger_row("ST001-battery_voltage_test-20260817-1000-1", lifecycle="voided")
    allowed = run(helpers, "create", ledger_view=helpers.ledger_view([voided]))
    assert allowed["status"] == "ok"


def test_create_digest_duplicate_is_rejected(helpers):
    payload = helpers.battery_payload()
    digest = compute_digest("ST001", BATTERY, helpers.OCCURRED, "1.5", payload)
    result = run(helpers, "create", ledger_view=helpers.ledger_view(digests=[digest]))
    assert ("E_DUP_DIGEST", "payload") in codes(result)


def test_create_rejects_subject_and_confirmations(helpers):
    result = run(helpers, "create", subject={"record_uid": "u", "lifecycle": "draft", "rev": 1})
    assert ("E_STATE_ILLEGAL", "subject") in codes(result)
    early = run(helpers, "create", confirmations=[{"slot": "测试人", "by": "李四", "at": helpers.NOW}])
    assert ("E_STATE_ILLEGAL", "confirmations") in codes(early)


# ---------------------------------------------------------------- confirm


def test_confirm_freezes_the_version(helpers):
    record = create(helpers)
    result = run(
        helpers,
        "confirm",
        subject=helpers.subject(record),
        confirmations=[helpers.confirmation()],
        ledger_view=helpers.ledger_view([row_of(record)]),
    )
    assert result["status"] == "ok", result["validation"]
    confirmed = result["record"]
    assert confirmed["lifecycle"] == "confirmed"
    assert confirmed["rev"] == record["rev"]
    assert confirmed["digest"] == record["digest"]
    assert confirmed["signature_slots"] == [
        {"slot": "测试人", "state": "signed", "by": helpers.SYNTHETIC_TESTER, "at": helpers.NOW}
    ]


@pytest.mark.parametrize(
    "confirmations, code, path",
    [
        ([], "E_REQUIRED", "confirmations"),
        ([{"slot": "记录人", "by": "李四", "at": "2026-09-17T15:00:00+08:00"}], "E_STATE_ILLEGAL", "confirmations[0].slot"),
        (
            [
                {"slot": "测试人", "by": "李四", "at": "2026-09-17T15:00:00+08:00"},
                {"slot": "测试人", "by": "王五", "at": "2026-09-17T15:00:00+08:00"},
            ],
            "E_STATE_ILLEGAL",
            "confirmations[1].slot",
        ),
        ([{"slot": "测试人", "by": "", "at": "2026-09-17T15:00:00+08:00"}], "E_REQUIRED", "confirmations[0].by"),
        ([{"slot": "测试人", "by": "李四", "at": "2026-09-17"}], "E_TIME_INVALID", "confirmations[0].at"),
    ],
)
def test_bad_confirmations_are_rejected(helpers, confirmations, code, path):
    record = create(helpers)
    result = run(
        helpers,
        "confirm",
        subject=helpers.subject(record),
        confirmations=confirmations,
        ledger_view=helpers.ledger_view([row_of(record)]),
    )
    assert result["status"] == "rejected"
    assert (code, path) in codes(result), codes(result)


def test_confirm_requires_the_draft_state(helpers):
    record = create(helpers)
    confirmed = run(
        helpers,
        "confirm",
        subject=helpers.subject(record),
        confirmations=[helpers.confirmation()],
        ledger_view=helpers.ledger_view([row_of(record)]),
    )["record"]
    again = run(
        helpers,
        "confirm",
        subject=helpers.subject(confirmed),
        confirmations=[helpers.confirmation()],
        ledger_view=helpers.ledger_view([row_of(confirmed)]),
    )
    assert ("E_STATE_ILLEGAL", "subject.lifecycle") in codes(again)


def test_rev_conflict_on_every_mutating_operation(helpers):
    record = create(helpers)
    stale = {"record_uid": record["record_uid"], "lifecycle": "draft", "rev": 9}
    view = helpers.ledger_view([row_of(record)])
    operations = [
        ("confirm", {"confirmations": [helpers.confirmation()]}),
        ("return", {"return_reason": "数据存疑"}),
        ("correct", {"payload": helpers.battery_payload(), "occurred_at": helpers.OCCURRED}),
        ("void", {"void_reason": "误录", "voided_by": "张三"}),
    ]
    for operation, extra in operations:
        result = run(helpers, operation, subject=stale, ledger_view=view, **extra)
        assert result["status"] == "rejected", operation
        assert ("E_REV_CONFLICT", "subject.rev") in codes(result), operation


# ---------------------------------------------------------------- return / correct


def test_return_keeps_the_version(helpers):
    record = create(helpers)
    result = run(
        helpers,
        "return",
        subject=helpers.subject(record),
        return_reason="条目缺失，退回补充",
        ledger_view=helpers.ledger_view([row_of(record)]),
    )
    assert result["status"] == "ok"
    returned = result["record"]
    assert returned["lifecycle"] == "draft"
    assert returned["rev"] == record["rev"]
    assert returned["digest"] == record["digest"]
    assert returned["signature_slots"] == [{"slot": "测试人", "state": "pending"}]


def test_return_only_on_draft(helpers):
    record = create(helpers)
    confirmed = run(
        helpers,
        "confirm",
        subject=helpers.subject(record),
        confirmations=[helpers.confirmation()],
        ledger_view=helpers.ledger_view([row_of(record)]),
    )["record"]
    result = run(
        helpers,
        "return",
        subject=helpers.subject(confirmed),
        return_reason="存疑",
        ledger_view=helpers.ledger_view([row_of(confirmed)]),
    )
    assert ("E_STATE_ILLEGAL", "subject.lifecycle") in codes(result)


def test_correct_on_draft_is_an_edit_without_supersedes(helpers):
    record = create(helpers)
    payload = helpers.battery_payload(items=[{"cell_no": 1, "voltage": 2.24}])
    result = run(
        helpers,
        "correct",
        subject=helpers.subject(record),
        payload=payload,
        occurred_at=helpers.OCCURRED,
        ledger_view=helpers.ledger_view([row_of(record)]),
    )
    assert result["status"] == "ok", result["validation"]
    corrected = result["record"]
    assert corrected["rev"] == 2
    assert corrected["lifecycle"] == "draft"
    assert corrected["links"] == []
    assert corrected["fields"] == payload
    assert corrected["digest"] != record["digest"]


def test_correct_on_confirmed_opens_a_new_draft_with_supersedes(helpers):
    record = create(helpers)
    confirmed = run(
        helpers,
        "confirm",
        subject=helpers.subject(record),
        confirmations=[helpers.confirmation()],
        ledger_view=helpers.ledger_view([row_of(record)]),
    )["record"]
    payload = helpers.battery_payload(items=[{"cell_no": 1, "voltage": 2.19}])
    result = run(
        helpers,
        "correct",
        subject=helpers.subject(confirmed),
        payload=payload,
        occurred_at=helpers.OCCURRED,
        ledger_view=helpers.ledger_view(
            [helpers.ledger_row(record["record_uid"], lifecycle="confirmed", rev=1, fields=record["fields"], digest=record["digest"])]
        ),
    )
    assert result["status"] == "ok", result["validation"]
    corrected = result["record"]
    assert corrected["rev"] == 2
    assert corrected["lifecycle"] == "draft"
    assert corrected["links"] == [{"type": "supersedes", "record_uid": record["record_uid"]}]
    assert corrected["signature_slots"] == [{"slot": "测试人", "state": "pending"}]


@pytest.mark.parametrize("lifecycle", ["voided", "archived"])
def test_correct_refuses_closed_states(helpers, lifecycle):
    record = create(helpers)
    result = run(
        helpers,
        "correct",
        subject={"record_uid": record["record_uid"], "lifecycle": lifecycle, "rev": 1},
        payload=helpers.battery_payload(),
        occurred_at=helpers.OCCURRED,
        ledger_view=helpers.ledger_view([row_of(record, lifecycle=lifecycle)]),
    )
    assert ("E_STATE_ILLEGAL", "subject.lifecycle") in codes(result)


def test_correct_recheks_business_key_only_when_touched(helpers):
    record = create(helpers)
    other = helpers.ledger_row(
        "ST001-battery_voltage_test-20260916-1000-1",
        lifecycle="confirmed",
        fields=helpers.battery_payload(test_kind="核对性放电"),
    )
    view = helpers.ledger_view([row_of(record), other])

    untouched = run(
        helpers,
        "correct",
        subject=helpers.subject(record),
        payload=helpers.battery_payload(items=[{"cell_no": 1, "voltage": 2.22}]),
        occurred_at=helpers.OCCURRED,
        ledger_view=view,
    )
    assert untouched["status"] == "ok", untouched["validation"]

    touched = run(
        helpers,
        "correct",
        subject=helpers.subject(record),
        payload=helpers.battery_payload(test_kind="核对性放电"),
        occurred_at=helpers.OCCURRED,
        ledger_view=view,
    )
    assert ("E_DUP_KEY", "payload") in codes(touched)


def test_correct_ignores_voided_rows_for_business_key(helpers):
    record = create(helpers)
    voided = helpers.ledger_row(
        "ST001-battery_voltage_test-20260916-1000-1",
        lifecycle="voided",
        fields=helpers.battery_payload(test_kind="核对性放电"),
    )
    result = run(
        helpers,
        "correct",
        subject=helpers.subject(record),
        payload=helpers.battery_payload(test_kind="核对性放电"),
        occurred_at=helpers.OCCURRED,
        ledger_view=helpers.ledger_view([row_of(record), voided]),
    )
    assert result["status"] == "ok", result["validation"]


def test_correct_digest_dedupe_excludes_own_versions(helpers):
    record = create(helpers)
    payload = helpers.battery_payload(items=[{"cell_no": 1, "voltage": 2.24}])
    digest = compute_digest("ST001", BATTERY, helpers.OCCURRED, "1.5", payload)

    other = helpers.ledger_row("ST001-battery_voltage_test-20260916-1000-1", lifecycle="confirmed", digest=digest)
    clash = run(
        helpers,
        "correct",
        subject=helpers.subject(record),
        payload=payload,
        occurred_at=helpers.OCCURRED,
        ledger_view=helpers.ledger_view([row_of(record), other]),
    )
    assert ("E_DUP_DIGEST", "payload") in codes(clash)

    # 改回与自身旧版完全一致的内容：不误判（§8.2）
    own_history = helpers.ledger_row(
        record["record_uid"], lifecycle="confirmed", digest=digest, fields=payload
    )
    same = run(
        helpers,
        "correct",
        subject=helpers.subject(record),
        payload=payload,
        occurred_at=helpers.OCCURRED,
        ledger_view=helpers.ledger_view([row_of(record), own_history]),
    )
    assert same["status"] == "ok", same["validation"]


# ---------------------------------------------------------------- void / archive


@pytest.mark.parametrize("lifecycle", ["draft", "confirmed"])
def test_void_keeps_the_version_and_marks_tombstone(helpers, lifecycle):
    record = create(helpers)
    row = row_of(record, lifecycle=lifecycle)
    result = run(
        helpers,
        "void",
        subject={"record_uid": record["record_uid"], "lifecycle": lifecycle, "rev": 1},
        void_reason="录入错误",
        voided_by="张三",
        ledger_view=helpers.ledger_view([row]),
    )
    assert result["status"] == "ok", result["validation"]
    voided = result["record"]
    assert voided["lifecycle"] == "voided"
    assert voided["rev"] == 1
    assert voided["digest"] == record["digest"]
    assert voided["fields"] == record["fields"]


@pytest.mark.parametrize("lifecycle", ["archived", "voided"])
def test_void_refuses_closed_states(helpers, lifecycle):
    record = create(helpers)
    result = run(
        helpers,
        "void",
        subject={"record_uid": record["record_uid"], "lifecycle": lifecycle, "rev": 1},
        void_reason="录入错误",
        voided_by="张三",
        ledger_view=helpers.ledger_view([row_of(record, lifecycle=lifecycle)]),
    )
    assert ("E_STATE_ILLEGAL", "subject.lifecycle") in codes(result)


@pytest.mark.parametrize(
    "extra",
    [
        {"void_reason": "", "voided_by": "张三"},
        {"void_reason": "误录", "voided_by": ""},
        {"voided_by": "张三"},
    ],
)
def test_void_requires_reason_and_actor(helpers, extra):
    record = create(helpers)
    result = run(helpers, "void", subject=helpers.subject(record), ledger_view=helpers.ledger_view([row_of(record)]), **extra)
    assert ("E_VOID_REASON", "void_reason") in codes(result)


def test_archive_marks_archived(helpers):
    record = create(helpers)
    row = row_of(record, lifecycle="confirmed")
    result = run(
        helpers,
        "archive",
        subject={"record_uid": record["record_uid"], "lifecycle": "confirmed", "rev": 1},
        archive_ref="ARCHIVE-0001",
        archived_by="张三",
        ledger_view=helpers.ledger_view([row]),
    )
    assert result["status"] == "ok", result["validation"]
    archived = result["record"]
    assert archived["lifecycle"] == "archived"
    assert archived["rev"] == 1
    assert archived["digest"] == record["digest"]
    assert archived["signature_slots"] == [{"slot": "测试人", "state": "signed"}]


def test_archive_requires_confirmed_and_reference(helpers):
    record = create(helpers)
    draft = run(
        helpers,
        "archive",
        subject=helpers.subject(record),
        archive_ref="ARCHIVE-0001",
        archived_by="张三",
        ledger_view=helpers.ledger_view([row_of(record)]),
    )
    assert ("E_STATE_ILLEGAL", "subject.lifecycle") in codes(draft)

    missing_ref = run(
        helpers,
        "archive",
        subject={"record_uid": record["record_uid"], "lifecycle": "confirmed", "rev": 1},
        archived_by="张三",
        ledger_view=helpers.ledger_view([row_of(record, lifecycle="confirmed")]),
    )
    assert ("E_ARCHIVE_REF", "archive_ref") in codes(missing_ref)


def test_archive_in_correct_window_targets_last_confirmed_version(helpers):
    """correct 并存窗口：archive 目标=最后确认版（修订A 拍板，§8.3）。"""
    record = create(helpers)
    confirmed_fields = record["fields"]
    new_draft_fields = helpers.battery_payload(items=[{"cell_no": 1, "voltage": 2.19}])
    row = helpers.ledger_row(
        record["record_uid"],
        rev=2,
        lifecycle="confirmed",
        fields=new_draft_fields,
        digest=compute_digest("ST001", BATTERY, helpers.OCCURRED, "1.5", new_draft_fields),
        confirmed_fields=confirmed_fields,
    )
    result = run(
        helpers,
        "archive",
        subject={"record_uid": record["record_uid"], "lifecycle": "confirmed", "rev": 2},
        archive_ref="ARCHIVE-0002",
        archived_by="张三",
        ledger_view=helpers.ledger_view([row]),
    )
    assert result["status"] == "ok", result["validation"]
    archived = result["record"]
    assert archived["rev"] == 1
    assert archived["lifecycle"] == "archived"
    assert archived["fields"] == confirmed_fields
    assert archived["digest"] == compute_digest("ST001", BATTERY, helpers.OCCURRED, "1.5", confirmed_fields)

    # 只接受最后确认版 rev（或窗口内最新 rev）
    stale = run(
        helpers,
        "archive",
        subject={"record_uid": record["record_uid"], "lifecycle": "confirmed", "rev": 3},
        archive_ref="ARCHIVE-0002",
        archived_by="张三",
        ledger_view=helpers.ledger_view([row]),
    )
    assert ("E_REV_CONFLICT", "subject.rev") in codes(stale)


def test_archive_targets_the_requested_rev_in_the_window(helpers):
    """窗口内显式指向最后确认版 rev（row.rev-1）亦可归档。"""
    record = create(helpers)
    confirmed_fields = record["fields"]
    new_draft_fields = helpers.battery_payload(items=[{"cell_no": 1, "voltage": 2.19}])
    row = helpers.ledger_row(record["record_uid"], rev=2, lifecycle="confirmed", fields=new_draft_fields, confirmed_fields=confirmed_fields)
    result = run(
        helpers,
        "archive",
        subject={"record_uid": record["record_uid"], "lifecycle": "confirmed", "rev": 1},
        archive_ref="ARCHIVE-0003",
        archived_by="张三",
        ledger_view=helpers.ledger_view([row]),
    )
    assert result["status"] == "ok", result["validation"]
    assert result["record"]["rev"] == 1
    assert result["record"]["fields"] == confirmed_fields


def test_archive_after_multiple_corrects_uses_confirmed_rev(helpers):
    """多轮 correct（rev=3、confirmed_rev=1）：归档版本号须取行内 confirmed_rev，而非位置推断（方案A）。"""
    record = create(helpers)
    confirmed_fields = record["fields"]
    draft_fields = helpers.battery_payload(items=[{"cell_no": 1, "voltage": 2.19}])
    row = helpers.ledger_row(
        record["record_uid"],
        rev=3,
        lifecycle="confirmed",
        fields=draft_fields,
        confirmed_fields=confirmed_fields,
        confirmed_rev=1,
    )
    ok = run(
        helpers,
        "archive",
        subject={"record_uid": record["record_uid"], "lifecycle": "confirmed", "rev": 3},
        archive_ref="ARCHIVE-0005",
        archived_by="张三",
        ledger_view=helpers.ledger_view([row]),
    )
    assert ok["status"] == "ok", ok["validation"]
    assert ok["record"]["rev"] == 1
    assert ok["record"]["fields"] == confirmed_fields
    assert ok["record"]["digest"] == compute_digest("ST001", BATTERY, helpers.OCCURRED, "1.5", confirmed_fields)

    # 显式指向 confirmed_rev 亦可
    ok2 = run(
        helpers,
        "archive",
        subject={"record_uid": record["record_uid"], "lifecycle": "confirmed", "rev": 1},
        archive_ref="ARCHIVE-0006",
        archived_by="张三",
        ledger_view=helpers.ledger_view([row]),
    )
    assert ok2["status"] == "ok", ok2["validation"]
    assert ok2["record"]["rev"] == 1

    # 判别性关键用例：位置推断旧值（row_rev-1=2）不再放行
    stale = run(
        helpers,
        "archive",
        subject={"record_uid": record["record_uid"], "lifecycle": "confirmed", "rev": 2},
        archive_ref="ARCHIVE-0007",
        archived_by="张三",
        ledger_view=helpers.ledger_view([row]),
    )
    assert ("E_REV_CONFLICT", "subject.rev") in codes(stale)


def test_archive_window_without_confirmed_rev_keeps_legacy_fallback(helpers):
    """兼容路径：行未带 confirmed_rev 时维持 row_rev-1 兜底（单轮 correct 等价）。"""
    record = create(helpers)
    confirmed_fields = record["fields"]
    draft_fields = helpers.battery_payload(items=[{"cell_no": 1, "voltage": 2.19}])
    row = helpers.ledger_row(
        record["record_uid"], rev=2, lifecycle="confirmed", fields=draft_fields, confirmed_fields=confirmed_fields
    )
    result = run(
        helpers,
        "archive",
        subject={"record_uid": record["record_uid"], "lifecycle": "confirmed", "rev": 2},
        archive_ref="ARCHIVE-0008",
        archived_by="张三",
        ledger_view=helpers.ledger_view([row]),
    )
    assert result["status"] == "ok", result["validation"]
    assert result["record"]["rev"] == 1
    assert result["record"]["fields"] == confirmed_fields


def test_archive_in_window_without_occurred_at_is_refused(helpers):
    record = create(helpers)
    row = helpers.ledger_row(record["record_uid"], rev=2, lifecycle="confirmed", occurred_at=None, confirmed_fields=record["fields"])
    result = run(
        helpers,
        "archive",
        subject={"record_uid": record["record_uid"], "lifecycle": "confirmed", "rev": 2},
        archive_ref="ARCHIVE-0004",
        archived_by="张三",
        ledger_view=helpers.ledger_view([row]),
    )
    assert ("E_REQUIRED", "ledger_view.occurred_at") in codes(result)


def test_archive_rev_mismatch_without_window(helpers):
    record = create(helpers)
    result = run(
        helpers,
        "archive",
        subject={"record_uid": record["record_uid"], "lifecycle": "confirmed", "rev": 7},
        archive_ref="ARCHIVE-0005",
        archived_by="张三",
        ledger_view=helpers.ledger_view([row_of(record, lifecycle="confirmed")]),
    )
    assert ("E_REV_CONFLICT", "subject.rev") in codes(result)


# ---------------------------------------------------------------- alarm_ack


def test_alarm_ack_does_not_change_fields_or_rev(helpers):
    record = create(helpers)
    result = run(
        helpers,
        "alarm_ack",
        subject=helpers.subject(record),
        payload={"alarm_disposition": {"fingerprint": "fp:abc", "status": "acked", "by": "张三", "at": helpers.NOW}},
        ledger_view=helpers.ledger_view([row_of(record)]),
    )
    assert result["status"] == "ok", result["validation"]
    assert result["record"] == {
        **record,
        "signature_slots": [{"slot": "测试人", "state": "pending"}],
    }
    assert result["rules"] == [] and result["trend"] == [] and result["alarm_state"] is None
    assert result["digest"] == record["digest"]


def test_alarm_ack_on_voided_record_is_refused(helpers):
    record = create(helpers)
    result = run(
        helpers,
        "alarm_ack",
        subject={"record_uid": record["record_uid"], "lifecycle": "voided", "rev": 1},
        payload={"alarm_disposition": {"fingerprint": "fp:abc", "status": "acked", "by": "张三", "at": helpers.NOW}},
        ledger_view=helpers.ledger_view([row_of(record, lifecycle="voided")]),
    )
    assert ("E_STATE_ILLEGAL", "subject.lifecycle") in codes(result)


@pytest.mark.parametrize(
    "disposition, code, path",
    [
        ({"fingerprint": "", "status": "acked", "by": "张三", "at": "2026-09-17T15:00:00+08:00"}, "E_STATE_ILLEGAL", "payload.alarm_disposition.fingerprint"),
        ({"fingerprint": "fp:a", "status": "maybe", "by": "张三", "at": "2026-09-17T15:00:00+08:00"}, "E_STATE_ILLEGAL", "payload.alarm_disposition.status"),
        ({"fingerprint": "fp:a", "status": "resolved", "by": "", "at": "2026-09-17T15:00:00+08:00"}, "E_STATE_ILLEGAL", "payload.alarm_disposition.by"),
        ({"fingerprint": "fp:a", "status": "resolved", "by": "张三", "at": "今晚"}, "E_TIME_INVALID", "payload.alarm_disposition.at"),
        ({"status": "acked"}, "E_STATE_ILLEGAL", "payload.alarm_disposition.fingerprint"),
    ],
)
def test_bad_dispositions_are_refused(helpers, disposition, code, path):
    record = create(helpers)
    result = run(
        helpers,
        "alarm_ack",
        subject=helpers.subject(record),
        payload={"alarm_disposition": disposition},
        ledger_view=helpers.ledger_view([row_of(record)]),
    )
    assert result["status"] == "rejected"
    assert (code, path) in codes(result), codes(result)


def test_alarm_ack_requires_disposition_payload(helpers):
    record = create(helpers)
    result = run(
        helpers,
        "alarm_ack",
        subject=helpers.subject(record),
        payload={},
        ledger_view=helpers.ledger_view([row_of(record)]),
    )
    assert ("E_REQUIRED", "payload.alarm_disposition") in codes(result)
    stray = run(
        helpers,
        "alarm_ack",
        subject=helpers.subject(record),
        payload={"alarm_disposition": {"fingerprint": "fp:a", "status": "acked", "by": "张三", "at": helpers.NOW}, "x": 1},
        ledger_view=helpers.ledger_view([row_of(record)]),
    )
    assert ("E_UNKNOWN_FIELD", "payload.x") in codes(stray)


# ---------------------------------------------------------------- 账本缺失 / 指纹来源


def test_missing_ledger_row_is_refused(helpers):
    record = create(helpers)
    result = run(
        helpers,
        "void",
        subject=helpers.subject(record),
        void_reason="误录",
        voided_by="张三",
        ledger_view=helpers.ledger_view([]),
    )
    assert ("E_STATE_ILLEGAL", "subject.record_uid") in codes(result)


def test_subject_lifecycle_must_match_the_ledger(helpers):
    """§5：subject 由调用方从账本取出传入，核心校验一致性；不符 → E_STATE_ILLEGAL。"""
    record = create(helpers)
    stale = {"record_uid": record["record_uid"], "lifecycle": "confirmed", "rev": 1}
    result = run(
        helpers,
        "void",
        subject=stale,
        void_reason="误录",
        voided_by="张三",
        ledger_view=helpers.ledger_view([row_of(record, lifecycle="draft")]),
    )
    assert ("E_STATE_ILLEGAL", "subject.lifecycle") in codes(result)


def test_subject_lifecycle_mismatch_on_confirm(helpers):
    record = create(helpers)
    result = run(
        helpers,
        "confirm",
        subject={"record_uid": record["record_uid"], "lifecycle": "archived", "rev": 1},
        confirmations=[helpers.confirmation()],
        ledger_view=helpers.ledger_view([row_of(record, lifecycle="draft")]),
    )
    assert ("E_STATE_ILLEGAL", "subject.lifecycle") in codes(result)


def test_version_digest_falls_back_to_row_digest(helpers):
    record = create(helpers)
    row = helpers.ledger_row(record["record_uid"], occurred_at=None, digest=record["digest"])
    result = run(
        helpers,
        "confirm",
        subject=helpers.subject(record),
        confirmations=[helpers.confirmation()],
        ledger_view=helpers.ledger_view([row]),
    )
    assert result["status"] == "ok"
    assert result["record"]["digest"] == record["digest"]
