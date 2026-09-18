"""M0 证据文件一致性测试。

`scripts/m0/excel_struct.json`（结构快照，前 12 行）与 `scripts/m0/excel_extra.json`
（脚注/下拉/批注/隐藏/序号预填）是《DSL 缺口清单》定稿的可复核证据，二者必须自洽：
同一批记录、同一 footer 行、未覆盖区无异常内容。模板本身不入库，故 CI 只能校验
提交的证据文件是否被改坏（真模板比对由 `scripts/m0_verify_snapshot.py` 在有模板时执行）。
"""

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STRUCT = ROOT / "scripts" / "m0" / "excel_struct.json"
EXTRA = ROOT / "scripts" / "m0" / "excel_extra.json"

SNAPSHOT_ROWS = 12
FOOTER_SCAN_FROM = 13

# 10 份在范围内的记录类型（停用的消防器材、保护定值压板不在此列）
RECORD_TYPES = (
    "breaker_trip_record",
    "surge_arrester_action_record",
    "grounding_wire_record",
    "two_ticket_ledger",
    "infrared_thermography_record",
    "insulation_test_record",
    "battery_voltage_test",
    "transformer_core_clamp_current_record",
    "protection_switch_record",
    "rodent_proof_check_record",
)

BATTERY = "battery_voltage_test"


@pytest.fixture(scope="module")
def evidence():
    return (
        json.loads(STRUCT.read_text(encoding="utf-8")),
        json.loads(EXTRA.read_text(encoding="utf-8")),
    )


def test_record_sets_match(evidence):
    struct, extra = evidence
    assert set(struct) == set(RECORD_TYPES)
    assert set(extra["records"]) == set(RECORD_TYPES)
    assert extra["generated_by"] == "scripts/m0_verify_snapshot.py"
    assert (ROOT / extra["generated_by"]).is_file()


def test_snapshot_covers_first_rows_only(evidence):
    struct, _ = evidence
    for record_type, entry in struct.items():
        sheet = entry["sheets"][0]
        assert len(sheet["rows"]) == SNAPSHOT_ROWS, record_type
        assert [row["r"] for row in sheet["rows"]] == list(range(1, SNAPSHOT_ROWS + 1)), record_type


def test_extra_matches_structure(evidence):
    struct, extra = evidence
    for record_type, entry in extra["records"].items():
        sheet = struct[record_type]["sheets"][0]
        # 脚注行即末行，且必须是一条整行合并
        assert entry["footer_row"] == sheet["max_row"], record_type
        assert entry["footer_range"] in sheet["merged"], record_type
        assert entry["sheet"] == sheet["sheet"], record_type
        assert entry["max_col"] == sheet["max_col"], record_type


def test_uncovered_region_is_clean(evidence):
    """快照未覆盖区（第 13 行起至脚注前）只允许出现脚注原文与序号预填。"""
    _, extra = evidence
    for record_type, entry in extra["records"].items():
        region = entry["uncovered_region"]
        assert region["from"] == FOOTER_SCAN_FROM, record_type
        assert region["to"] == entry["footer_row"] - 1, record_type
        assert region["unexpected_cells"] == [], record_type


def test_templates_are_blank(evidence):
    """空白模板证据：零数据有效性下拉、零批注、零隐藏行列，且每份都有脚注原文。"""
    _, extra = evidence
    for record_type, entry in extra["records"].items():
        assert entry["data_validations"] == [], record_type
        assert entry["comments"] == [], record_type
        assert entry["hidden_rows"] == [], record_type
        assert entry["hidden_cols"] == [], record_type
        assert entry["footer_lines"], record_type
        assert "填写说明" in entry["footer_lines"][0], record_type


def test_battery_prefill_is_the_only_prefill(evidence):
    _, extra = evidence
    prefill = extra["records"][BATTERY]["prefill"]
    assert prefill["column"] == "A"
    assert prefill["rows"] == "5-24"
    assert prefill["values"] == [str(n) for n in range(1, 21)]
    for record_type, entry in extra["records"].items():
        if record_type != BATTERY:
            assert "prefill" not in entry, record_type
