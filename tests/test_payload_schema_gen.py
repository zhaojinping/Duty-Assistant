"""§10.6 Schema 生成器自身测试：生成产物对全部黄金样本输入通过手写校验器交叉验证。

生成器由 registry 声明产出 payload JSON Schema（§7.1，**不作权威**）；
本文件同时验证「生成 Schema 拒绝的输入」与「引擎拒绝的输入」口径一致。
"""

from __future__ import annotations

import json

import pytest

import records_kit
from records_kit.protocol import check_instance
from records_kit.protocol.payload_schema import payload_schema, schema_bundle
from records_kit.registry import default_registry

from conftest import golden_files


def test_generated_schema_shape(declaration):
    schema = payload_schema(declaration)
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert schema["x-record-type"] == "battery_voltage_test"
    assert schema["x-schema-version"] == "1.5"
    assert schema["x-layout"] == "item_list"
    assert schema["x-dedupe-key"] == ["station", "occurred_day", "test_kind"]
    assert schema["required"] == ["dc_system_id", "float_voltage", "test_kind", "items"]
    voltage = schema["properties"]["items"]["items"]["properties"]["voltage"]
    assert voltage == {"type": "number", "minimum": 0.0, "maximum": 15.0, "x-unit": "V", "title": "单体电压"}
    assert schema["properties"]["test_kind"]["enum"] == ["定期", "核对性放电"]
    assert schema["properties"]["items"]["items"]["properties"]["lagging"]["enum"] == ["是", "否", "不适用"]
    assert schema["properties"]["items"]["minItems"] == 1


def test_bundle_covers_every_record_type():
    bundle = schema_bundle(default_registry())
    assert set(bundle) == set(default_registry().record_types)


@pytest.mark.parametrize("path", golden_files(), ids=lambda path: path.stem)
def test_golden_inputs_pass_generated_schema(path):
    sample = json.loads(path.read_text(encoding="utf-8"))
    payload = sample["envelope"].get("payload")
    if not payload or sample["envelope"]["operation"] != "create":
        pytest.skip("仅校验 create 样本的 payload")
    if sample["expect"]["status"] != "ok":
        pytest.skip("负样本（期望 rejected）：其 payload 本就应被生成 Schema 拒绝")
    declaration = default_registry()[sample["envelope"]["record_type"]]   # 按样本类型取声明（10 类时代）
    schema = payload_schema(declaration)
    problems = check_instance(payload, schema)
    assert problems == [], problems


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.pop("test_kind"),
        lambda payload: payload.update(float_voltage=400.0),
        lambda payload: payload.update(test_kind="月度"),
        lambda payload: payload.update(unexpected=1),
        lambda payload: payload.update(items=[{"cell_no": 1}]),
        lambda payload: payload.update(items=[{"cell_no": 1, "voltage": 2.3, "extra": 1}]),
        lambda payload: payload.update(items=[]),
        lambda payload: payload.update(dc_system_id=1),
    ],
)
def test_generated_schema_and_engine_agree_on_rejection(helpers, declaration, mutate):
    payload = helpers.battery_payload()
    mutate(payload)
    schema_problems = check_instance(payload, payload_schema(declaration))
    result = records_kit.process(helpers.envelope("create", payload=payload))
    assert schema_problems != [], payload
    assert result["status"] == "rejected", payload


def test_engine_rejects_nan_and_inf_that_json_schema_cannot_express(helpers):
    """手写校验器与 JSON Schema 都无法表达 NaN/±Inf，由引擎显式拒绝（§7.3）。"""
    for value in (float("nan"), float("inf"), float("-inf")):
        payload = helpers.battery_payload(float_voltage=value)
        result = records_kit.process(helpers.envelope("create", payload=payload))
        assert result["status"] == "rejected"
        assert ("E_TYPE", "payload.float_voltage") in {
            (error["code"], error["path"]) for error in result["validation"]["errors"]
        }
