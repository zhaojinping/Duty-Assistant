"""引擎门面：按口径构建「阈值配置注入版」注册表。

- 声明源：包内 ``battery_voltage_test.toml``（含免签 ``signature_slots=[]``）；
  仅覆盖 ``voltage_band`` 规则的 expr，其余原样。
- 覆盖顺序：注册表注入优先（``process(envelope, registry=...)``），核心零改动。
- 进程内按 (record_type, lo, hi) 缓存注册表。
"""

from __future__ import annotations

from copy import deepcopy

from records_kit.registry import Registry, default_registry, loader
from records_kit.registry.meta import validate_declaration

from da_core.settings import BATTERY_TYPE, THERMO_TYPE

_BAND_RULE_ID = "voltage_band"
_THERMO_RULE_ID = "thermal_grade"
_cache: dict = {}


def _fmt(value: float) -> str:
    return "%g" % float(value)


def registry_for_band(lo: float, hi: float, *, record_type: str = BATTERY_TYPE) -> Registry:
    """构建电压带被配置覆盖的注册表（其余声明照常）。"""
    key = (record_type, float(lo), float(hi))
    if key in _cache:
        return _cache[key]

    path = loader.declarations_dir() / f"{record_type}.toml"
    raw = deepcopy(loader.load(path))
    matched = False
    for rule in raw.get("rules", []):
        if rule.get("id") == _BAND_RULE_ID and str(rule.get("expr", "")).startswith("band:"):
            rule["expr"] = f"band:{_fmt(lo)},{_fmt(hi)}"
            matched = True
    if not matched:
        raise ValueError(f"声明 {record_type!r} 无 voltage_band 规则，无法应用阈值覆盖")

    declaration = validate_declaration(raw, source=f"config-overlay:{record_type}")
    others = [d for d in default_registry().values() if d.record_type != record_type]
    registry = Registry(others + [declaration])
    _cache[key] = registry
    return registry


def registry_with_overrides(record_type: str, replacements: dict[str, str]) -> Registry:
    """按规则 id 覆盖 expr，其余声明照常。``replacements`` 为 ``{rule_id: expr}``。"""
    if not replacements:
        raise ValueError("replacements 不能为空")
    key = (record_type, tuple(sorted(replacements.items())))
    if key in _cache:
        return _cache[key]

    path = loader.declarations_dir() / f"{record_type}.toml"
    raw = deepcopy(loader.load(path))
    seen: set[str] = set()
    for rule in raw.get("rules", []):
        rule_id = rule.get("id")
        if rule_id in replacements:
            rule["expr"] = replacements[rule_id]
            seen.add(rule_id)
    missing = sorted(set(replacements) - seen)
    if missing:
        raise ValueError(f"声明 {record_type!r} 缺少规则：{'、'.join(missing)}")

    declaration = validate_declaration(raw, source=f"config-overlay:{record_type}")
    others = [item for item in default_registry().values() if item.record_type != record_type]
    registry = Registry(others + [declaration])
    _cache[key] = registry
    return registry


def registry_for_thermo(thresholds: dict) -> Registry:
    """用部署阈值覆盖测温分级表达式（参数名与声明固定一致）。"""
    expr = (
        "thermal_grade:group=device_name,env=env_temp,"
        f"general_diff={_fmt(thresholds['general_diff'])},"
        f"severe_temp={_fmt(thresholds['severe_temp'])},"
        f"severe_delta={_fmt(thresholds['severe_delta'])},"
        f"critical_temp={_fmt(thresholds['critical_temp'])},"
        f"critical_delta={_fmt(thresholds['critical_delta'])}"
    )
    return registry_with_overrides(THERMO_TYPE, {_THERMO_RULE_ID: expr})


def clear_cache() -> None:
    _cache.clear()
