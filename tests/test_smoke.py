"""M1 冒烟测试：包可导入、默认注册表可加载蓄电池声明、入口对空信封给出结构化拒绝。"""

from __future__ import annotations

import pytest

import records_kit
from records_kit.registry import default_registry, loader


def test_package_identity_and_entry():
    assert records_kit.PROTOCOL == "records-kit"
    assert records_kit.PROTOCOL_VERSION == "1.5"
    result = records_kit.process({})
    assert result["status"] == "rejected"
    assert result["validation"]["ok"] is False
    assert {error["code"] for error in result["validation"]["errors"]} == {"E_PROTOCOL"}


def test_default_registry_carries_battery_declaration():
    registry = default_registry()
    declaration = registry["battery_voltage_test"]
    assert declaration.layout == "item_list"
    assert declaration.signature_slots == ("测试人",)
    assert registry.titles["battery_voltage_test"] == "蓄电池电压测试记录簿"


def test_registry_loader_reads_bundled_declaration():
    path = loader.declarations_dir() / "battery_voltage_test.toml"
    raw = loader.load(path)
    assert raw["meta"]["record_type"] == "battery_voltage_test"
    declaration = loader.load_declaration(path)
    assert declaration.record_type == "battery_voltage_test"


def test_load_rejects_missing_file():
    from records_kit.registry import DeclarationError

    with pytest.raises(DeclarationError):
        loader.load("registry/不存在的声明.toml")
