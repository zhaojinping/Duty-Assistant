"""§10.3 T3 台账/跨记录规则单测（design.md §7.3 + M0 定稿 §13/§14）。

覆盖算子：
- continuity（跳号/分组/复位窗口/跨组不干扰）
- pairing（重复占用/正常释放/悬空释放/复合键/voided 排除）
- external_baseline（E_BASELINE_MISSING/单键缺失/越限/通过）
- recovery_within（超时/按时/无配对退出/非恢复行不判）
- aggregate（计数 + 基线对照/缺 ref/单键缺失）
- date_diff 跨记录（linked. 取数 + occurred_at 伪字段）
- cycle 探针悬空配对 detail（probe_cycle）

全部为合成数据（synthetic），站名/人名沿用 conftest 占位约定；
声明经 meta 校验真实加载，坏声明拒绝用例见 test_registry_meta.py。
"""

from __future__ import annotations

import pytest

from records_kit.engine.cycle import probe_cycle
from records_kit.engine.rules import evaluate_rules
from records_kit.errors import E_BASELINE_MISSING, Rejected

OCCURRED = "2026-09-17T10:00:00+08:00"
NOW = "2026-09-17T15:00:00+08:00"


def _flat_declaration(synthetic_registry, helpers, fields, rules, record_type="synthetic_flat"):
    text = helpers.build_toml(
        meta={"record_type": record_type, "layout": "flat", "dedupe_key": ["station", "occurred_day"]},
        fields=fields,
        items=False,
        rules=rules,
        trend=[],
    )
    registry = synthetic_registry(toml_text=text, filename=f"{record_type}.toml")
    return registry[record_type]


def _judge(declaration, fields, *, rows=(), linked=(), baselines=None, occurred_at=OCCURRED):
    """直接跑 T3 判定，返回规则结论条目列表（合成账本）。"""
    report = evaluate_rules(
        declaration,
        fields,
        ledger_view={
            "confirmed_digests": [],
            "same_type_records": list(rows),
            "linked_records": list(linked),
        },
        baselines=baselines,
        occurred_at=occurred_at,
    )
    return report.entries


def _row(record_uid, fields, *, lifecycle="confirmed", occurred_at=OCCURRED, confirmed_fields=None):
    row = {
        "record_uid": record_uid,
        "lifecycle": lifecycle,
        "rev": 1,
        "occurred_at": occurred_at,
        "digest": "sha256:" + "0" * 64,
        "fields": dict(fields),
    }
    if confirmed_fields is not None:
        row["confirmed_fields"] = dict(confirmed_fields)
    return row


# ---------------------------------------------------------------- continuity


TICKET_FIELDS = [
    {"key": "ticket_kind", "name": "票种", "type": "enum", "options": ["工作票", "操作票"], "required": True},
    {"key": "ticket_no", "name": "编号", "type": "text", "required": True},
]
CONTINUITY_RULE = {
    "id": "no_gap",
    "kind": "limit",
    "tier": 3,
    "expr": "continuity:ticket_no,group=ticket_kind,reset=monthly",
    "level": "warn",
}


@pytest.fixture
def ticket_declaration(synthetic_registry, helpers):
    return _flat_declaration(synthetic_registry, helpers, TICKET_FIELDS, [CONTINUITY_RULE], "ticket_decl")


def test_continuity_no_gap_passes(ticket_declaration):
    rows = [
        _row("u1", {"ticket_kind": "工作票", "ticket_no": "2026-09-001"}),
        _row("u2", {"ticket_kind": "工作票", "ticket_no": "2026-09-002"}),
    ]
    entries = _judge(ticket_declaration, {"ticket_kind": "工作票", "ticket_no": "2026-09-003"}, rows=rows)
    assert entries[0]["verdict"] == "pass"
    assert "无跳号" in entries[0]["detail"]


def test_continuity_gap_violates(ticket_declaration):
    rows = [
        _row("u1", {"ticket_kind": "工作票", "ticket_no": "2026-09-001"}),
        _row("u2", {"ticket_kind": "工作票", "ticket_no": "2026-09-003"}),
    ]
    entries = _judge(ticket_declaration, {"ticket_kind": "工作票", "ticket_no": "2026-09-004"}, rows=rows)
    assert entries[0]["verdict"] == "violation"
    assert "1→3" in entries[0]["detail"]


def test_continuity_other_group_does_not_interfere(ticket_declaration):
    # 操作票组有跳号，当前是工作票组 → 不判
    rows = [
        _row("u1", {"ticket_kind": "操作票", "ticket_no": "2026-001"}),
        _row("u2", {"ticket_kind": "操作票", "ticket_no": "2026-003"}),
        _row("u3", {"ticket_kind": "工作票", "ticket_no": "2026-09-001"}),
    ]
    entries = _judge(ticket_declaration, {"ticket_kind": "工作票", "ticket_no": "2026-09-002"}, rows=rows)
    assert entries[0]["verdict"] == "pass"


def test_continuity_other_month_does_not_interfere(ticket_declaration):
    # 上月有跳号（月复位窗口不同），本月无 → 不判
    rows = [
        _row("u1", {"ticket_kind": "工作票", "ticket_no": "2026-08-001"}, occurred_at="2026-08-01T10:00:00+08:00"),
        _row("u2", {"ticket_kind": "工作票", "ticket_no": "2026-08-003"}, occurred_at="2026-08-02T10:00:00+08:00"),
        _row("u3", {"ticket_kind": "工作票", "ticket_no": "2026-09-001"}, occurred_at="2026-09-01T10:00:00+08:00"),
    ]
    entries = _judge(
        ticket_declaration,
        {"ticket_kind": "工作票", "ticket_no": "2026-09-002"},
        rows=rows,
        occurred_at="2026-09-02T10:00:00+08:00",
    )
    assert entries[0]["verdict"] == "pass"


# ---------------------------------------------------------------- pairing


WIRE_FIELDS = [
    {"key": "action", "name": "类别", "type": "enum", "options": ["装设", "拆除"], "required": True},
    {"key": "wire_id", "name": "接地线编号", "type": "text", "required": True},
]
PAIRING_RULE = {
    "id": "wire_pairing",
    "kind": "limit",
    "tier": 3,
    "expr": "pairing:grounding,key=wire_id",
    "level": "alarm",
    "action": "FLAG_ITEM",
}


@pytest.fixture
def wire_declaration(synthetic_registry, helpers):
    return _flat_declaration(synthetic_registry, helpers, WIRE_FIELDS, [PAIRING_RULE], "wire_decl")


def test_pairing_fresh_occupy_passes(wire_declaration):
    entries = _judge(wire_declaration, {"action": "装设", "wire_id": "W-01"}, rows=[])
    assert entries[0]["verdict"] == "pass"


def test_pairing_reoccupy_violates(wire_declaration):
    rows = [_row("u1", {"action": "装设", "wire_id": "W-01"})]
    entries = _judge(wire_declaration, {"action": "装设", "wire_id": "W-01"}, rows=rows)
    assert entries[0]["verdict"] == "violation"
    assert "重复占用" in entries[0]["detail"]
    # alarm 级：进告警候选 + 动作提示
    report = evaluate_rules(
        wire_declaration,
        {"action": "装设", "wire_id": "W-01"},
        ledger_view={"confirmed_digests": [], "same_type_records": rows, "linked_records": []},
        baselines=None,
        occurred_at=OCCURRED,
    )
    assert ("wire_pairing", "记录") in report.alarm_candidates
    assert any(action["code"] == "FLAG_ITEM" for action in report.actions)


def test_pairing_normal_release_passes(wire_declaration):
    rows = [_row("u1", {"action": "装设", "wire_id": "W-01"})]
    entries = _judge(wire_declaration, {"action": "拆除", "wire_id": "W-01"}, rows=rows)
    assert entries[0]["verdict"] == "pass"


def test_pairing_release_without_occupy_violates(wire_declaration):
    entries = _judge(wire_declaration, {"action": "拆除", "wire_id": "W-01"}, rows=[])
    assert entries[0]["verdict"] == "violation"
    assert "悬空释放" in entries[0]["detail"]


def test_pairing_after_release_reoccupy_passes(wire_declaration):
    rows = [
        _row("u1", {"action": "装设", "wire_id": "W-01"}, occurred_at="2026-09-01T10:00:00+08:00"),
        _row("u2", {"action": "拆除", "wire_id": "W-01"}, occurred_at="2026-09-02T10:00:00+08:00"),
    ]
    entries = _judge(wire_declaration, {"action": "装设", "wire_id": "W-01"}, rows=rows, occurred_at="2026-09-03T10:00:00+08:00")
    assert entries[0]["verdict"] == "pass"


def test_pairing_voided_row_does_not_count(wire_declaration):
    rows = [_row("u1", {"action": "装设", "wire_id": "W-01"}, lifecycle="voided")]
    entries = _judge(wire_declaration, {"action": "装设", "wire_id": "W-01"}, rows=rows)
    assert entries[0]["verdict"] == "pass"


SWITCH_FIELDS = [
    {"key": "device_id", "name": "装置", "type": "text", "required": True},
    {"key": "function", "name": "功能", "type": "text", "required": True},
    {"key": "action", "name": "投/退", "type": "enum", "options": ["投", "退"], "required": True},
]
COMPOSITE_PAIRING_RULE = {
    "id": "switch_pairing",
    "kind": "limit",
    "tier": 3,
    "expr": "pairing:protection,key=device_id+function",
    "level": "warn",
}


@pytest.fixture
def switch_declaration(synthetic_registry, helpers):
    return _flat_declaration(synthetic_registry, helpers, SWITCH_FIELDS, [COMPOSITE_PAIRING_RULE], "switch_decl")


def test_pairing_composite_key_reoccupy_violates(switch_declaration):
    rows = [_row("u1", {"device_id": "D-01", "function": "重合闸", "action": "投"})]
    entries = _judge(switch_declaration, {"device_id": "D-01", "function": "重合闸", "action": "投"}, rows=rows)
    assert entries[0]["verdict"] == "violation"
    assert "D-01+重合闸" in entries[0]["detail"]


def test_pairing_composite_key_partial_differs_is_independent(switch_declaration):
    # 装置相同、功能不同 → 不同配对键，互不干扰
    rows = [_row("u1", {"device_id": "D-01", "function": "重合闸", "action": "投"})]
    entries = _judge(switch_declaration, {"device_id": "D-01", "function": "主保护", "action": "投"}, rows=rows)
    assert entries[0]["verdict"] == "pass"


# ---------------------------------------------------------------- aggregate + external_baseline


BREAKER_FIELDS = [
    {"key": "breaker_id", "name": "开关编号", "type": "text", "required": True},
    {"key": "trip_count", "name": "累计跳闸次数", "type": "number", "decimals": 0, "min": 1.0, "required": True},
]
AGGREGATE_RULE = {
    "id": "trip_approaching",
    "kind": "limit",
    "tier": 3,
    "expr": "aggregate:count_over,key=breaker_id,ref=allow_trip_count",
    "level": "alarm",
}
ALLOW_BASELINES = [{"ref": "allow_trip_count", "kind": "count_table", "name": "允许事故开闸次数表", "data": {"BRK-01": 5}}]


@pytest.fixture
def breaker_declaration(synthetic_registry, helpers):
    return _flat_declaration(synthetic_registry, helpers, BREAKER_FIELDS, [AGGREGATE_RULE], "breaker_decl")


def _trip_rows(count):
    return [
        _row(f"u{i}", {"breaker_id": "BRK-01", "trip_count": i}, occurred_at=f"2026-09-{i:02d}T10:00:00+08:00")
        for i in range(1, count + 1)
    ]


def test_aggregate_under_limit_passes(breaker_declaration):
    rows = _trip_rows(3)  # 历史 3 + 当前 1 = 4 < 5
    entries = _judge(breaker_declaration, {"breaker_id": "BRK-01", "trip_count": 4}, rows=rows, baselines=ALLOW_BASELINES)
    assert entries[0]["verdict"] == "pass"
    assert "4" in entries[0]["detail"]


def test_aggregate_reaches_limit_violates(breaker_declaration):
    rows = _trip_rows(4)  # 历史 4 + 当前 1 = 5 ≥ 5
    entries = _judge(breaker_declaration, {"breaker_id": "BRK-01", "trip_count": 5}, rows=rows, baselines=ALLOW_BASELINES)
    assert entries[0]["verdict"] == "violation"
    assert "5" in entries[0]["detail"]


def test_aggregate_missing_ref_rejects(breaker_declaration):
    rows = _trip_rows(1)
    with pytest.raises(Rejected) as excinfo:
        _judge(breaker_declaration, {"breaker_id": "BRK-01", "trip_count": 2}, rows=rows, baselines=[{"ref": "other", "data": {"X": 1}}])
    assert any(issue.code == E_BASELINE_MISSING for issue in excinfo.value.issues)


def test_aggregate_missing_device_key_skips(breaker_declaration):
    rows = _trip_rows(1)
    entries = _judge(breaker_declaration, {"breaker_id": "BRK-99", "trip_count": 1}, rows=rows, baselines=ALLOW_BASELINES)
    assert entries[0]["verdict"] == "skipped"
    assert "BRK-99" in entries[0]["detail"]


def test_aggregate_other_device_independent(breaker_declaration):
    rows = _trip_rows(4)  # BRK-01 已 4 条，当前是 BRK-02 → 只计 BRK-02 的 1 条
    entries = _judge(breaker_declaration, {"breaker_id": "BRK-02", "trip_count": 1}, rows=rows, baselines=ALLOW_BASELINES)
    assert entries[0]["verdict"] == "skipped"  # BRK-02 不在基线表 → 单键缺失


MANUAL_RULE = {
    "id": "manual_count_check",
    "kind": "limit",
    "tier": 3,
    "expr": "external_baseline:allow_trip_count,field=trip_count,key=breaker_id,op=gte",
    "level": "warn",
}


@pytest.fixture
def manual_baseline_declaration(synthetic_registry, helpers):
    return _flat_declaration(synthetic_registry, helpers, BREAKER_FIELDS, [MANUAL_RULE], "manual_decl")


def test_external_baseline_field_under_limit_passes(manual_baseline_declaration):
    entries = _judge(manual_baseline_declaration, {"breaker_id": "BRK-01", "trip_count": 3}, baselines=ALLOW_BASELINES)
    assert entries[0]["verdict"] == "pass"


def test_external_baseline_field_over_limit_violates(manual_baseline_declaration):
    entries = _judge(manual_baseline_declaration, {"breaker_id": "BRK-01", "trip_count": 6}, baselines=ALLOW_BASELINES)
    assert entries[0]["verdict"] == "violation"


def test_external_baseline_missing_ref_rejects(manual_baseline_declaration):
    with pytest.raises(Rejected) as excinfo:
        _judge(manual_baseline_declaration, {"breaker_id": "BRK-01", "trip_count": 3}, baselines=[{"ref": "other", "data": {"X": 1}}])
    assert any(issue.code == E_BASELINE_MISSING for issue in excinfo.value.issues)


# ---------------------------------------------------------------- recovery_within


RECOVER_RULE = {
    "id": "recover_in_time",
    "kind": "limit",
    "tier": 3,
    "expr": "recovery_within:30d,key=device_id+function",
    "level": "warn",
}


@pytest.fixture
def recover_declaration(synthetic_registry, helpers):
    return _flat_declaration(synthetic_registry, helpers, SWITCH_FIELDS, [RECOVER_RULE], "recover_decl")


def test_recovery_within_in_time_passes(recover_declaration):
    rows = [
        _row("u1", {"device_id": "D-01", "function": "重合闸", "action": "退"}, occurred_at="2026-09-01T10:00:00+08:00")
    ]
    entries = _judge(
        recover_declaration,
        {"device_id": "D-01", "function": "重合闸", "action": "投"},
        rows=rows,
        occurred_at="2026-09-15T10:00:00+08:00",
    )
    assert entries[0]["verdict"] == "pass"


def test_recovery_within_overdue_violates(recover_declaration):
    rows = [
        _row("u1", {"device_id": "D-01", "function": "重合闸", "action": "退"}, occurred_at="2026-09-01T10:00:00+08:00")
    ]
    entries = _judge(
        recover_declaration,
        {"device_id": "D-01", "function": "重合闸", "action": "投"},
        rows=rows,
        occurred_at="2026-10-15T10:00:00+08:00",
    )
    assert entries[0]["verdict"] == "violation"
    assert "超时" in entries[0]["detail"]


def test_recovery_without_release_skips(recover_declaration):
    entries = _judge(
        recover_declaration,
        {"device_id": "D-01", "function": "重合闸", "action": "投"},
        rows=[],
        occurred_at="2026-10-15T10:00:00+08:00",
    )
    assert entries[0]["verdict"] == "skipped"


def test_recovery_non_recover_row_skips(recover_declaration):
    # 当前是"退"行：恢复时限只在"投"（恢复）行判定
    entries = _judge(
        recover_declaration,
        {"device_id": "D-01", "function": "重合闸", "action": "退"},
        rows=[],
        occurred_at="2026-10-15T10:00:00+08:00",
    )
    assert entries[0]["verdict"] == "skipped"


# ---------------------------------------------------------------- 跨记录 date_diff


REMOVE_OVERDUE_RULE = {
    "id": "remove_overdue",
    "kind": "limit",
    "tier": 3,
    "expr": "date_diff:linked.end_at,occurred_at,le:2d",
    "level": "warn",
}


@pytest.fixture
def remove_declaration(synthetic_registry, helpers):
    return _flat_declaration(synthetic_registry, helpers, WIRE_FIELDS, [REMOVE_OVERDUE_RULE], "remove_decl")


def test_cross_record_date_diff_within_limit_passes(remove_declaration):
    linked = [{"record_uid": "t1", "fields": {"end_at": "2026-09-16T10:00:00+08:00"}}]
    entries = _judge(
        remove_declaration,
        {"action": "拆除", "wire_id": "W-01"},
        linked=linked,
        occurred_at="2026-09-17T10:00:00+08:00",
    )
    assert entries[0]["verdict"] == "pass"


def test_cross_record_date_diff_over_limit_violates(remove_declaration):
    linked = [{"record_uid": "t1", "fields": {"end_at": "2026-09-10T10:00:00+08:00"}}]
    entries = _judge(
        remove_declaration,
        {"action": "拆除", "wire_id": "W-01"},
        linked=linked,
        occurred_at="2026-09-17T10:00:00+08:00",
    )
    assert entries[0]["verdict"] == "violation"
    assert "7.0" in entries[0]["detail"]


def test_cross_record_date_diff_missing_linked_skips(remove_declaration):
    entries = _judge(
        remove_declaration,
        {"action": "拆除", "wire_id": "W-01"},
        linked=[],
        occurred_at="2026-09-17T10:00:00+08:00",
    )
    assert entries[0]["verdict"] == "skipped"


# ---------------------------------------------------------------- cycle 探针悬空配对


def test_probe_pairing_reports_dangling_occupy(wire_declaration, helpers):
    rows = [
        _row("u1", {"action": "装设", "wire_id": "W-01"}, occurred_at="2026-09-01T10:00:00+08:00"),
        _row("u2", {"action": "装设", "wire_id": "W-02"}, occurred_at="2026-09-02T10:00:00+08:00"),
    ]
    entries = probe_cycle(
        {"wire_decl": wire_declaration},
        helpers.ledger_view(rows=rows),
        NOW,
        ["wire_decl"],
    )
    assert len(entries) == 1
    assert entries[0]["verdict"] == "due"
    assert "W-01" in entries[0]["detail"]
    assert "W-02" in entries[0]["detail"]
    assert "未拆除" in entries[0]["detail"]


def test_probe_pairing_no_dangling_no_entry(wire_declaration, helpers):
    rows = [
        _row("u1", {"action": "装设", "wire_id": "W-01"}, occurred_at="2026-09-01T10:00:00+08:00"),
        _row("u2", {"action": "拆除", "wire_id": "W-01"}, occurred_at="2026-09-02T10:00:00+08:00"),
    ]
    entries = probe_cycle(
        {"wire_decl": wire_declaration},
        helpers.ledger_view(rows=rows),
        NOW,
        ["wire_decl"],
    )
    assert entries == []


def test_probe_pairing_voided_occupy_not_dangling(wire_declaration, helpers):
    rows = [_row("u1", {"action": "装设", "wire_id": "W-01"}, lifecycle="voided")]
    entries = probe_cycle(
        {"wire_decl": wire_declaration},
        helpers.ledger_view(rows=rows),
        NOW,
        ["wire_decl"],
    )
    assert entries == []


# ---------------------------------------------------------------- process 集成（create 走 T3 判定）


def _create_envelope(record_type, payload, *, create_seq=1, ledger_rows=(), baselines=None):
    envelope = {
        "protocol": "records-kit",
        "protocol_version": "1.5",
        "operation": "create",
        "record_type": record_type,
        "station": {"station_id": "ST001", "station_name": "XX风电场"},
        "now": NOW,
        "occurred_at": OCCURRED,
        "submitted_by": "张三",
        "create_seq": create_seq,
        "payload": dict(payload),
        "ledger_view": {
            "confirmed_digests": [],
            "same_type_records": list(ledger_rows),
            "linked_records": [],
        },
    }
    if baselines is not None:
        envelope["baselines"] = baselines
    return envelope


def test_process_create_runs_t3_pairing(wire_declaration):
    import records_kit

    result = records_kit.process(_create_envelope("wire_decl", {"action": "装设", "wire_id": "W-01"}), {"wire_decl": wire_declaration})
    assert result["status"] == "ok", result["validation"]
    pairing = [entry for entry in result["rules"] if entry["rule_id"] == "wire_pairing"]
    assert pairing and pairing[0]["verdict"] == "pass"


def test_process_create_detects_reoccupy_via_ledger(wire_declaration):
    import records_kit

    rows = [_row("u1", {"action": "装设", "wire_id": "W-01"}, occurred_at="2026-09-01T10:00:00+08:00")]
    result = records_kit.process(
        _create_envelope("wire_decl", {"action": "装设", "wire_id": "W-01"}, ledger_rows=rows),
        {"wire_decl": wire_declaration},
    )
    assert result["status"] == "ok", result["validation"]
    pairing = [entry for entry in result["rules"] if entry["rule_id"] == "wire_pairing"]
    assert pairing and pairing[0]["verdict"] == "violation"
    assert any(action["code"] == "FLAG_ITEM" for action in result["actions_hint"])


def test_process_create_aggregate_reaches_limit(breaker_declaration):
    import records_kit

    rows = _trip_rows(4)  # 历史 4 + 当前 1 = 5 ≥ 5
    result = records_kit.process(
        _create_envelope("breaker_decl", {"breaker_id": "BRK-01", "trip_count": 5}, ledger_rows=rows, baselines=ALLOW_BASELINES),
        {"breaker_decl": breaker_declaration},
    )
    assert result["status"] == "ok", result["validation"]
    trip = [entry for entry in result["rules"] if entry["rule_id"] == "trip_approaching"]
    assert trip and trip[0]["verdict"] == "violation"


def test_process_create_aggregate_missing_ref_rejected(breaker_declaration):
    import records_kit

    rows = _trip_rows(1)
    result = records_kit.process(
        _create_envelope(
            "breaker_decl",
            {"breaker_id": "BRK-01", "trip_count": 2},
            ledger_rows=rows,
            baselines=[{"ref": "other", "data": {"X": 1}}],
        ),
        {"breaker_decl": breaker_declaration},
    )
    assert result["status"] == "rejected"
    assert any(error["code"] == E_BASELINE_MISSING for error in result["validation"]["errors"])


# ---------------------------------------------------------------- meta 校验（T3 坏声明）


@pytest.mark.parametrize(
    ("rule", "fragment"),
    [
        ({"id": "r", "kind": "limit", "tier": 3, "expr": "pairing:grounding,key=nope"}, "引用不存在的字段"),
        ({"id": "r", "kind": "limit", "tier": 3, "expr": "pairing:nope,key=wire_id"}, "pairing 类型必须"),
        ({"id": "r", "kind": "limit", "tier": 3, "expr": "pairing:grounding"}, "必须声明 key"),
        ({"id": "r", "kind": "limit", "tier": 3, "expr": "continuity:ticket_no,reset=nope"}, "reset 只能是"),
        ({"id": "r", "kind": "limit", "tier": 3, "expr": "recovery_within:x"}, "Nd 形式"),
        ({"id": "r", "kind": "limit", "tier": 3, "expr": "recovery_within:30d"}, "必须声明 key"),
        ({"id": "r", "kind": "limit", "tier": 3, "expr": "aggregate:sum"}, "仅支持 count_over"),
        ({"id": "r", "kind": "limit", "tier": 3, "expr": "external_baseline"}, "需要 ref"),
        ({"id": "r", "kind": "limit", "tier": 3, "expr": "external_baseline:x,op=gt"}, "op 只能是"),
        ({"id": "r", "kind": "limit", "tier": 3, "expr": "date_diff:occurred_at,linked.end_at,le:2d"}, None),  # 合法形态，见下
    ],
)
def test_t3_bad_declarations_are_refused(synthetic_registry, helpers, rule, fragment):
    from records_kit.registry.declaration import DeclarationError

    fields = WIRE_FIELDS + TICKET_FIELDS
    text = helpers.build_toml(
        meta={"record_type": "synthetic_bad", "layout": "flat", "dedupe_key": ["station", "occurred_day"]},
        fields=fields,
        items=False,
        rules=[rule],
        trend=[],
    )
    if fragment is None:
        synthetic_registry(toml_text=text, filename="synthetic_bad.toml")  # 合法：不应抛
        return
    with pytest.raises(DeclarationError) as excinfo:
        synthetic_registry(toml_text=text, filename="synthetic_bad.toml")
    assert fragment in str(excinfo.value), str(excinfo.value)


def test_t3_tier4_rejected(synthetic_registry, helpers):
    from records_kit.registry.declaration import DeclarationError

    text = helpers.build_toml(
        meta={"record_type": "synthetic_t4", "layout": "flat", "dedupe_key": ["station", "occurred_day"]},
        fields=WIRE_FIELDS,
        items=False,
        rules=[{"id": "r", "kind": "limit", "tier": 4, "expr": "pairing:grounding,key=wire_id"}],
        trend=[],
    )
    with pytest.raises(DeclarationError) as excinfo:
        synthetic_registry(toml_text=text, filename="synthetic_t4.toml")
    assert "超出支持层" in str(excinfo.value)
