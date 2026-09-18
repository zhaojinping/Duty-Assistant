"""§10.4 黄金样本：「输入 → 期望输出」快照，逐字段断言。

- 样本文件在 ``tests/golden/*.json``，全部为**合成**样本（占位站/人名，见各文件 note）；
- 指纹占位 ``__FINGERPRINT_FROM_FIRST_CANDIDATE__`` 表示该字段由用例先跑一遍取首个告警
  候选指纹后回填（指纹算法本身另有专门用例）；
- digest 不快照字面值，而是按 §6.3 规范序列化**独立重算**（hashlib + 键序 JSON）后比对；
- §10.4 要求 10 类记录各存快照：M1 只有蓄电池一类有声明，故覆盖率用例按 registry
  逐一校验——M2 新增声明时必须同步补快照。
"""

from __future__ import annotations

import hashlib
import json

import pytest

import records_kit
from records_kit.protocol import check_instance, result_schema
from records_kit.registry import default_registry

from conftest import GOLDEN_DIR, golden_files

FINGERPRINT_MARKER = "__FINGERPRINT_FROM_FIRST_CANDIDATE__"


def _load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _independent_digest(envelope: dict, payload: dict) -> str:
    """按 §6.3 独立重算指纹：键字典序、无空白、UTF-8 原样、时间保持原文。"""
    body = json.dumps(
        {
            "station_id": envelope["station"]["station_id"],
            "record_type": envelope["record_type"],
            "occurred_at": envelope["occurred_at"],
            "schema_version": envelope["protocol_version"],
            "payload": payload,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


def _resolve_fingerprint(envelope: dict) -> dict:
    marker = json.dumps(envelope, ensure_ascii=False)
    if FINGERPRINT_MARKER not in marker:
        return envelope
    probe = json.loads(marker)
    probe.pop("alarm_history")
    fingerprint = records_kit.process(probe)["alarm_state"]["fingerprint"]
    assert fingerprint, "样本要求回填指纹，但首轮没有产生告警候选"
    for row in envelope["alarm_history"]:
        if row["fingerprint"] == FINGERPRINT_MARKER:
            row["fingerprint"] = fingerprint
    return envelope


@pytest.mark.parametrize("path", golden_files(), ids=lambda path: path.stem)
def test_golden_samples(path):
    sample = _load(path)
    envelope = _resolve_fingerprint(sample["envelope"])
    expect = sample["expect"]
    result = records_kit.process(envelope)

    assert result["status"] == expect["status"], result["validation"]
    assert check_instance(result, result_schema()) == []

    if expect.get("errors"):
        actual = {(error["code"], error["path"]) for error in result["validation"]["errors"]}
        for wanted in expect["errors"]:
            assert (wanted["code"], wanted["path"]) in actual, actual
        return

    for name, value in (expect.get("record") or {}).items():
        assert result["record"][name] == value, name
    if result["record"] is not None and envelope["operation"] in ("create", "correct"):
        assert result["record"]["digest"] == _independent_digest(envelope, result["record"]["fields"])

    for wanted in expect.get("rules") or []:
        matches = [entry for entry in result["rules"] if entry["rule_id"] == wanted["rule_id"]]
        assert matches, wanted
        for key, value in wanted.items():
            if key == "detail_contains":
                assert any(value in entry["detail"] for entry in matches), matches
            else:
                assert any(entry[key] == value for entry in matches), (key, matches)

    for wanted in expect.get("trend") or []:
        matches = [entry for entry in result["trend"] if entry["metric"] == wanted["metric"]]
        assert matches, wanted
        for key, value in wanted.items():
            if key == "evidence_contains":
                assert any(value in entry["evidence"] for entry in matches), matches
            else:
                assert any(entry[key] == value for entry in matches), (key, matches)

    for wanted in expect.get("cycle") or []:
        matches = [entry for entry in result["cycle"] if entry["record_type"] == wanted["record_type"]]
        assert matches, wanted
        for key, value in wanted.items():
            if key == "detail_contains":
                assert any(value in entry["detail"] for entry in matches), matches
            else:
                assert any(entry[key] == value for entry in matches), (key, matches)

    for name in ("alarm_state", "record"):
        if name in expect and expect[name] is None:
            assert result[name] is None, name

    if expect.get("alarm_state") is not None:
        for key, value in expect["alarm_state"].items():
            if key == "evidence_contains":
                assert value in result["alarm_state"]["evidence"]
            else:
                assert result["alarm_state"][key] == value, key

    if "actions_hint" in expect:
        assert [action["code"] for action in result["actions_hint"]] == expect["actions_hint"]

    if "rules" not in expect:
        assert result["rules"] in ([], None)


def test_every_declared_record_type_has_a_golden_sample():
    """§10.4：10 类记录各存快照；M1 只有蓄电池有声明，其余随 M2 声明一并补。"""
    declared = set(default_registry().record_types)
    covered = {_load(path)["record_type"] for path in golden_files()}
    assert declared <= covered, f"缺黄金样本：{sorted(declared - covered)}"


def test_golden_samples_are_synthetic():
    """样本必须显式标注合成（不得夹带真实数据）。"""
    for path in golden_files():
        sample = _load(path)
        assert "synthetic" in sample["note"], path.name
        assert "测试夹具" in sample["note"] or "合成" in sample["note"]
        blob = json.dumps(sample, ensure_ascii=False)
        assert "ST001" in blob or "XX风电场" in blob


def test_golden_dir_is_tracked():
    assert GOLDEN_DIR.is_dir()
