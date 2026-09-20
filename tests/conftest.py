"""M1 测试夹具。

**全部为合成数据（synthetic）**：站名/人名沿用《DSL 缺口清单》§0 约定的占位
（``ST001`` / ``XX风电场`` / ``张三`` / ``李四``），不含真实场站、人员或表 ID；
合成声明统一以 ``synthetic`` 标注 record_type 或注释，避免与真实声明混淆。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from records_kit.registry import Registry, default_registry

ROOT = Path(__file__).resolve().parents[1]
GOLDEN_DIR = ROOT / "tests" / "golden"
SYNTHETIC_STATION = {"station_id": "ST001", "station_name": "XX风电场"}
SYNTHETIC_SUBMITTER = "张三"
SYNTHETIC_TESTER = "李四"
NOW = "2026-09-17T15:00:00+08:00"
OCCURRED = "2026-09-17T10:00:00+08:00"
BATTERY = "battery_voltage_test"


# ---------------------------------------------------------------- 信封 / payload


def battery_payload(**overrides) -> dict:
    payload = {
        "dc_system_id": "DC-001",
        "float_voltage": 241.5,
        "float_current": 12.0,
        "env_temp": 25.0,
        "test_kind": "定期",
        "items": [
            {"cell_no": 1, "voltage": 2.21},
            {"cell_no": 2, "voltage": 1.95},
            {"cell_no": 3, "voltage": 2.20},
        ],
    }
    payload.update(overrides)
    return payload


def envelope(operation: str, **overrides) -> dict:
    """合成信封：默认带 ST001 / 2026-09-17 的蓄电池 create 素材。"""
    base = {
        "protocol": "records-kit",
        "protocol_version": "1.5",
        "operation": operation,
        "record_type": BATTERY,
        "station": dict(SYNTHETIC_STATION),
        "now": NOW,
    }
    if operation in ("create", "correct"):
        base["occurred_at"] = OCCURRED
        base["submitted_by"] = SYNTHETIC_SUBMITTER
    if operation == "create":
        base["create_seq"] = 1
        base["payload"] = battery_payload()
    base.update(overrides)
    return base


def created_record(envelope_overrides: dict | None = None) -> dict:
    """跑一次 create，返回 ``record`` 块（合成数据）。"""
    import records_kit

    result = records_kit.process(envelope("create", **(envelope_overrides or {})))
    assert result["status"] == "ok", result["validation"]
    return result["record"]


# ---------------------------------------------------------------- 账本 / 历史行


def ledger_row(
    record_uid: str,
    *,
    rev: int = 1,
    lifecycle: str = "draft",
    fields: dict | None = None,
    digest: str = "sha256:" + "0" * 64,
    occurred_at: str | None = OCCURRED,
    **extra,
) -> dict:
    row = {
        "record_uid": record_uid,
        "lifecycle": lifecycle,
        "rev": rev,
        "occurred_at": occurred_at,
        "digest": digest,
        "fields": battery_payload() if fields is None else fields,
    }
    row.update(extra)
    return row


def ledger_view(rows: list[dict] | None = None, *, digests: list[str] | None = None, linked: list[dict] | None = None) -> dict:
    return {
        "confirmed_digests": list(digests or []),
        "same_type_records": list(rows or []),
        "linked_records": list(linked or []),
    }


def history_row(occurred_at: str, min_voltage: float, *, digest: str | None = None) -> dict:
    return {
        "occurred_at": occurred_at,
        "digest": digest or ("sha256:" + "1" * 64),
        "lifecycle": "archived",
        "fields": battery_payload(items=[{"cell_no": 1, "voltage": min_voltage}]),
    }


def subject(record: dict) -> dict:
    return {"record_uid": record["record_uid"], "lifecycle": record["lifecycle"], "rev": record["rev"]}


def confirmation(slot: str = "测试人", by: str = SYNTHETIC_TESTER) -> dict:
    return {"slot": slot, "by": by, "at": NOW}


# ---------------------------------------------------------------- 合成声明


def toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{name} = {toml_value(item)}" for name, item in value.items()) + " }"
    raise TypeError(f"无法序列化：{value!r}")


DEFAULT_META = {
    "record_type": "synthetic_record",
    "title": "合成记录（测试夹具，非真实记录类型）",
    "schema_version": "1.5",
    "layout": "item_list",
    "dedupe_key": ["station", "occurred_day", "test_kind"],
    "link_types": [],
    "extra": "reject",
    "signature_slots": ["测试人"],
    "action_codes": ["FLAG_ITEM"],
    "escalate_after": 3,
}
DEFAULT_FIELDS = [
    {"key": "test_kind", "name": "测试性质", "type": "enum", "options": ["定期", "核对性放电"], "required": True},
    {"key": "float_voltage", "name": "浮充电压", "type": "number", "unit": "V", "min": 0.0, "max": 300.0, "required": True},
    {"key": "env_temp", "name": "环境温度", "type": "number", "unit": "℃"},
]
DEFAULT_ITEMS = {
    "key_field": "cell_no",
    "fields": [
        {"key": "cell_no", "name": "电池序号", "type": "number", "decimals": 0, "required": True},
        {"key": "voltage", "name": "单体电压", "type": "number", "unit": "V", "min": 0.0, "max": 15.0, "required": True},
    ],
}
DEFAULT_RULES = [
    {"id": "voltage_band", "kind": "limit", "tier": 1, "target": "items.voltage", "expr": "band:2.00,2.25", "level": "warn"},
]
DEFAULT_TRENDS = [
    {"metric": "cell_voltage_min", "source": "min(items.voltage)", "window": 3, "drop_warn": 0.10},
]


def build_toml(
    *,
    meta: dict | None = None,
    omit: tuple[str, ...] = (),
    fields: list[dict] | None = None,
    items: dict | None = None,
    rules: list[dict] | None = None,
    trend: list[dict] | None = None,
) -> str:
    """按声明格式生成 TOML 文本（合成声明夹具）。"""
    meta_values = dict(DEFAULT_META)
    meta_values.update(meta or {})
    for name in omit:
        meta_values.pop(name, None)
    lines = ["[meta]"]
    for name, value in meta_values.items():
        lines.append(f"{name} = {toml_value(value)}")
    if "fields" not in omit:
        for spec in DEFAULT_FIELDS if fields is None else fields:
            lines.append("")
            lines.append("[[fields]]")
            for name, value in spec.items():
                lines.append(f"{name} = {toml_value(value)}")
    if "items" not in omit and items is not False:
        items_spec = DEFAULT_ITEMS if items is None else items
        if items_spec is not None:
            lines.append("")
            lines.append("[items]")
            for name, value in items_spec.items():
                if name == "fields":
                    continue
                lines.append(f"{name} = {toml_value(value)}")
            for spec in items_spec.get("fields", []):
                lines.append("")
                lines.append("[[items.fields]]")
                for name, value in spec.items():
                    lines.append(f"{name} = {toml_value(value)}")
    if "rules" not in omit:
        for spec in DEFAULT_RULES if rules is None else rules:
            lines.append("")
            lines.append("[[rules]]")
            for name, value in spec.items():
                lines.append(f"{name} = {toml_value(value)}")
    if "trend" not in omit:
        for spec in (DEFAULT_TRENDS if trend is None else trend) or []:
            lines.append("")
            lines.append("[[trend]]")
            for name, value in spec.items():
                lines.append(f"{name} = {toml_value(value)}")
    return "\n".join(lines) + "\n"


def write_declaration(directory: Path, filename: str, text: str) -> Path:
    path = directory / filename
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------- fixtures


@pytest.fixture
def helpers():
    """把本模块的合成素材构造函数暴露给用例（``helpers.envelope(...)`` 等）。"""
    import conftest

    return conftest


@pytest.fixture
def battery(request) -> str:
    return BATTERY


@pytest.fixture
def registry() -> Registry:
    return default_registry()


@pytest.fixture
def declaration(registry):
    return registry[BATTERY]


@pytest.fixture
def synthetic_dir(tmp_path) -> Path:
    return tmp_path / "synthetic-declarations"


@pytest.fixture
def make_registry(synthetic_dir):
    """``make_registry({文件名: TOML 文本})`` → Registry（合成声明，临时目录）。"""

    def _make(mapping: dict[str, str]) -> Registry:
        synthetic_dir.mkdir(exist_ok=True)
        paths = [write_declaration(synthetic_dir, name, text) for name, text in mapping.items()]
        from records_kit.registry import loader

        return Registry([loader.load_declaration(path) for path in paths])

    return _make


@pytest.fixture
def synthetic_registry(synthetic_dir):
    """单份合成声明构成的注册表（默认与蓄电池同形）。"""

    def _make(toml_text: str | None = None, filename: str = "synthetic_record.toml") -> Registry:
        from records_kit.registry import loader

        synthetic_dir.mkdir(exist_ok=True)
        path = write_declaration(synthetic_dir, filename, toml_text if toml_text is not None else build_toml())
        return Registry([loader.load_declaration(path)])

    return _make


def read_golden(name: str) -> dict:
    return json.loads((GOLDEN_DIR / name).read_text(encoding="utf-8"))


def golden_files() -> list[Path]:
    return sorted(GOLDEN_DIR.glob("*.json")) if GOLDEN_DIR.is_dir() else []
