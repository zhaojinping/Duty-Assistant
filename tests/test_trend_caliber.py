"""M3 trend 口径收敛：``docs/trend-caliber-proposal.md`` §4 九例边界例逐例落地。

用例名标注例号（``ex1``…``ex9``）便于与提案逐条对单。第 7 例为**判别性用例**：
反向变异（关掉 ``group`` 过滤）后必须失败——本文件末尾以注记说明该自查。

合成声明（synthetic）经 meta 真实加载；字段名沿用提案示例（``transformer_id`` / ``core_current``）。
"""

from __future__ import annotations

import pytest

from records_kit.engine.trend import evaluate_trend
from records_kit.registry.declaration import DeclarationError

FIELDS = [
    {"key": "transformer_id", "name": "主变编号", "type": "text", "required": True},
    {"key": "core_current", "name": "铁芯夹件电流", "type": "number", "required": False},
]


def _declaration(synthetic_registry, helpers, *trend, fields=None):
    registry = synthetic_registry(
        helpers.build_toml(
            meta={
                "record_type": "synthetic_trend",
                "layout": "flat",
                "dedupe_key": ["station", "occurred_day"],
                "action_codes": [],
            },
            fields=fields if fields is not None else FIELDS,
            items=False,
            rules=[],
            trend=list(trend),
        )
    )
    return registry["synthetic_trend"]


def _rows(*pairs) -> list[dict]:
    """``(transformer_id, 读数)`` → 历史行；顺序即壳层给定的时间序。"""
    return [{"fields": {"transformer_id": name, "core_current": value}} for name, value in pairs]


def _row_of(*pairs) -> dict:
    return _rows(*pairs)


def _only(declaration, fields, rows) -> dict:
    entries = evaluate_trend(declaration, fields, rows)
    assert len(entries) == 1
    return entries[0]


# --------------------------------------------------------------- §4-1 窗口不足

def test_ex1_partial_window_reports_n_over_window(synthetic_registry, helpers):
    """§4-1：窗口不足（3/6）→ ``insufficient_history``（level=info，evidence ``3/6``）。"""
    declaration = _declaration(
        synthetic_registry, helpers,
        {"metric": "core_current", "source": "min(core_current)", "window": 6, "drop_warn": 0.10},
    )
    entry = _only(declaration, {"transformer_id": "A", "core_current": 1.90}, _rows(("A", 2.00), ("A", 2.00)))
    assert entry["verdict"] == "insufficient_history"
    assert entry["level"] == "info"
    assert "3/6" in entry["evidence"]


# --------------------------------------------------- §4-2 当前记录取不到值

def test_ex2_current_record_without_source_value(synthetic_registry, helpers):
    """§4-2：当前记录 source 取不到值 → ``insufficient_history``，不输出趋势结论。"""
    declaration = _declaration(
        synthetic_registry, helpers,
        {"metric": "core_current", "source": "min(core_current)", "window": 2, "drop_warn": 0.10},
    )
    entry = _only(declaration, {"transformer_id": "A"}, _rows(("A", 2.00)))
    assert entry["verdict"] == "insufficient_history"
    assert "取不到值" in entry["evidence"]


# ------------------------------------------- §4-3 首值 0 且未声明 drop_abs

def test_ex3_zero_baseline_without_drop_abs(synthetic_registry, helpers):
    """§4-3：首值 = 0 且未声明 ``drop_abs`` → ``insufficient_history``（不可算）。"""
    declaration = _declaration(
        synthetic_registry, helpers,
        {"metric": "core_current", "source": "min(core_current)", "window": 3, "drop_warn": 0.10},
    )
    entry = _only(declaration, {"transformer_id": "A", "core_current": 0.0}, _rows(("A", 0.0), ("A", 0.0)))
    assert entry["verdict"] == "insufficient_history"
    assert "首值为 0" in entry["evidence"]


# --------------------------------------------- §4-4 首值 0 且声明 drop_abs

def test_ex4_zero_baseline_with_drop_abs_uses_absolute_difference(synthetic_registry, helpers):
    """§4-4：首值 = 0 且声明 ``drop_abs`` → 按绝对差值判定（``≥`` 为 dropping）。"""
    declaration = _declaration(
        synthetic_registry, helpers,
        {"metric": "core_current", "source": "min(core_current)", "window": 3, "drop_abs": 1.0},
    )
    dropping = _only(declaration, {"transformer_id": "A", "core_current": -1.5}, _rows(("A", 0.0), ("A", 0.0)))
    assert dropping["verdict"] == "dropping"
    assert dropping["level"] == "warn"
    assert "绝对差值" in dropping["evidence"]

    stable = _only(declaration, {"transformer_id": "A", "core_current": -0.5}, _rows(("A", 0.0), ("A", 0.0)))
    assert stable["verdict"] == "stable"


# ------------------------------------------ §4-5 降幅恰等于 drop_warn

def test_ex5_drop_equal_to_threshold_is_dropping(synthetic_registry, helpers):
    """§4-5：降幅恰等于 ``drop_warn`` → ``dropping``（``≥`` 含等于）。"""
    declaration = _declaration(
        synthetic_registry, helpers,
        {"metric": "core_current", "source": "min(core_current)", "window": 3, "drop_warn": 0.2},
    )
    entry = _only(declaration, {"transformer_id": "A", "core_current": 2.0}, _rows(("A", 2.5), ("A", 2.5)))
    assert entry["verdict"] == "dropping"
    assert entry["level"] == "warn"


# ------------------------------------- §4-6 历史行取不到值计入 evidence

def test_ex6_unusable_history_row_is_counted(synthetic_registry, helpers):
    """§4-6：历史行 source 取不到值 → 该行跳过，evidence 计入「跳过 N 行」。"""
    declaration = _declaration(
        synthetic_registry, helpers,
        {"metric": "core_current", "source": "min(core_current)", "window": 3, "drop_warn": 0.10},
    )
    rows = _rows(("A", 2.00)) + [{"fields": {"transformer_id": "A"}}, {"fields": {}}] + _rows(("A", 2.00))
    entry = _only(declaration, {"transformer_id": "A", "core_current": 1.90}, rows)
    assert entry["verdict"] == "stable"
    assert "跳过 2 行" in entry["evidence"]


# ------------------------------------------------ §4-7 group 序列隔离（判别性）

def test_ex7_group_isolates_series(synthetic_registry, helpers):
    """§4-7：``group = "transformer_id"``，台账含两台主变 → A 台不消费 B 台历史行。

    判别性：只数到 A 台 2 行 + 当前 = ``3/4``（窗口不足）。若分组失效，B 台两行会并入序列
    （``4/4``，窗口凑满后按跨设备首值给出 stable）——verdict 与 evidence 同时变化。
    """
    declaration = _declaration(
        synthetic_registry, helpers,
        {"metric": "core_current", "source": "min(core_current)", "window": 4, "drop_warn": 0.10, "group": "transformer_id"},
    )
    rows = _rows(("A", 2.00), ("B", 0.50), ("A", 2.00), ("B", 0.50))
    entry = _only(declaration, {"transformer_id": "A", "core_current": 1.90}, rows)
    assert entry["verdict"] == "insufficient_history"
    assert "3/4" in entry["evidence"]

    other = _only(declaration, {"transformer_id": "B", "core_current": 0.45}, rows)
    assert "3/4" in other["evidence"]  # B 台同样只见自己的 2 行


# -------------------------------------------- §4-8 未声明 group 保持现行

def test_ex8_without_group_keeps_single_series(synthetic_registry, helpers):
    """§4-8：未声明 ``group`` → 全部历史行合一序列（行为与 M1 一致）。"""
    declaration = _declaration(
        synthetic_registry, helpers,
        {"metric": "core_current", "source": "min(core_current)", "window": 4, "drop_warn": 0.10},
    )
    rows = _rows(("A", 2.00), ("B", 0.50), ("A", 2.00), ("B", 0.50))
    entry = _only(declaration, {"transformer_id": "A", "core_current": 1.90}, rows)
    assert entry["verdict"] == "stable"
    assert "4/4" in entry["evidence"]  # 跨设备行全部计入（现行行为，未变）


# ------------------------------------ §4-9 drop_warn 与 drop_abs 互斥

def test_ex9_drop_warn_and_drop_abs_are_mutually_exclusive(synthetic_registry, helpers):
    """§4-9：同时声明 ``drop_warn`` 与 ``drop_abs`` → 声明加载报错（口径互斥）。"""
    with pytest.raises(DeclarationError, match="互斥"):
        _declaration(
            synthetic_registry, helpers,
            {"metric": "core_current", "source": "min(core_current)", "window": 2, "drop_warn": 0.10, "drop_abs": 1.0},
        )


# ------------------------------------------------------------- 配套边界例

def test_group_key_missing_on_current_record_is_insufficient(synthetic_registry, helpers):
    """配套：当前记录缺分组键 → 不判趋势（不静默按全量序列出结论）。"""
    declaration = _declaration(
        synthetic_registry, helpers,
        {"metric": "core_current", "source": "min(core_current)", "window": 2, "drop_warn": 0.10, "group": "transformer_id"},
    )
    entry = _only(declaration, {"core_current": 1.90}, _rows(("A", 2.00)))
    assert entry["verdict"] == "insufficient_history"
    assert "缺分组键" in entry["evidence"]


@pytest.mark.parametrize(
    "trend, fragment",
    [
        ({"metric": "m", "source": "min(core_current)", "window": 2, "drop_warn": 0.1, "group": "nope"},
         "引用不存在的字段"),
        ({"metric": "m", "source": "min(core_current)", "window": 2, "drop_warn": 0.1, "group": "items.core_current"},
         "不存在"),
        ({"metric": "m", "source": "min(core_current)", "window": 2, "drop_warn": 0.1, "drop_abs": 0},
         "drop_abs"),
        ({"metric": "m", "source": "min(core_current)", "window": 2}, "drop_warn 缺失"),
    ],
)
def test_bad_trend_declarations_are_refused(synthetic_registry, helpers, trend, fragment):
    """配套：``group`` 非法、``drop_abs`` 非正、两口径都缺 → 拒绝加载。"""
    with pytest.raises(DeclarationError, match=fragment):
        _declaration(synthetic_registry, helpers, trend)


def test_real_battery_declaration_still_loads_and_judges(helpers):
    """真声明回归：蓄电池 trend 段（无 ``group``、``drop_warn`` 口径）行为与 M1 一致。

    口径收敛不得改动存量声明的判定：5 行 2.30 + 当前 2.00 → 相对降幅 0.13 ≥ 0.10 → dropping。
    """
    import records_kit

    rows = [
        helpers.history_row(f"2026-08-{day:02d}T10:00:00+08:00", 2.30) for day in range(1, 6)
    ]
    payload = helpers.battery_payload(items=[{"cell_no": 1, "voltage": 2.00}])
    result = records_kit.process(helpers.envelope("create", payload=payload, history=rows))
    assert result["status"] == "ok", result["validation"]
    entry = result["trend"][0]
    assert entry["verdict"] == "dropping"
    assert entry["level"] == "warn"
    assert "相对降幅" in entry["evidence"]
