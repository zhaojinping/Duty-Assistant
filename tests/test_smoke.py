"""M1 冒烟测试：包可导入、入口占位按约定抛 NotImplementedError。"""

import pytest

import records_kit
from records_kit.registry import loader


def test_package_import_and_entry_placeholder():
    assert records_kit.PROTOCOL == "records-kit"
    assert records_kit.PROTOCOL_VERSION == "1.5"
    with pytest.raises(NotImplementedError):
        records_kit.process({})


def test_registry_loader_placeholder():
    with pytest.raises(NotImplementedError):
        loader.load("registry/battery_voltage_test.toml")
