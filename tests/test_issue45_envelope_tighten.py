"""#45 协议收紧落地：信封 additionalProperties:false 反例 + 三补足项回归断言。

依据 #26 影响评估（已合入）：
- 反例：信封（含 station 子对象）出现协议外字段 → ``E_PROTOCOL`` 拒绝；
- 三补足固化：``record_type`` 回显 / ``record.links`` / ``alarm_state.evidence``
  作为 ``process()`` 输出协议的回归契约——删任一项本文件即红。
"""

from __future__ import annotations

import pytest

import records_kit
from records_kit.protocol import envelope_schema

from conftest import envelope


# ---- 反例：信封协议外字段（additionalProperties:false 的运行时行为） ----


def test_envelope_rejects_unknown_top_level_field():
    bad = envelope("create", hacker_note="合成反例：协议外字段")
    result = records_kit.process(bad)
    assert result["status"] == "rejected", result
    codes = [(e["code"], e["path"]) for e in result["validation"]["errors"]]
    assert ("E_PROTOCOL", "hacker_note") in codes
    assert any("协议外字段" in e["message"] for e in result["validation"]["errors"])


def test_envelope_rejects_unknown_station_field():
    bad = envelope(
        "create",
        station={"station_id": "ST001", "station_name": "XX风电场", "region": "北方"},
    )
    result = records_kit.process(bad)
    assert result["status"] == "rejected", result
    codes = [(e["code"], e["path"]) for e in result["validation"]["errors"]]
    assert ("E_PROTOCOL", "station.region") in codes


def test_envelope_schema_runtime_whitelist_is_closed():
    """运行时白名单来自 envelope_schema()；其自身必须 additionalProperties:false。"""
    schema = envelope_schema()
    assert schema["additionalProperties"] is False
    # 白名单必须覆盖 create 反例中的合法字段（防白名单与权威文件漂移由文件断言兜底）
    for name in ("protocol", "protocol_version", "operation", "record_type", "station", "now"):
        assert name in schema["properties"]


# ---- 三补足项回归断言（#26：record_type 回显 / record.links / alarm_state.evidence） ----


def test_three_supplements_are_part_of_output_contract():
    result = records_kit.process(envelope("create"))
    assert result["status"] == "ok", result["validation"]

    # ① record_type 回显（壳层无需从 envelope 二次解析）
    assert result["record_type"] == "battery_voltage_test"

    # ② record.links 恒在（无成对关系时空数组，壳层可直读）
    assert result["record"]["links"] == []

    # ③ alarm_state.evidence 恒在（默认素材含 alarm 级违规 → 注明命中规则与判定口径）
    assert result["alarm_state"]["evidence"]
    assert "命中规则" in result["alarm_state"]["evidence"]


def test_output_protocol_echo_fields_are_stable():
    """输出协议回显字段（protocol/protocol_version/operation）与输入一致。"""
    env = envelope("create")
    result = records_kit.process(env)
    assert result["protocol"] == env["protocol"]
    assert result["protocol_version"] == env["protocol_version"]
    assert result["operation"] == env["operation"]
