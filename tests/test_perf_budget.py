"""§10.7 性能基准（对应《DSL 缺口清单》§11 ⑧ P1/P2，预算值待 M1 实测）。

- **P1 台账全量传入**：单类型 5,000 行账本 + 10,000 行历史全量传入，create + 全规则
  P95 ≤ 200ms（Python 参考机）；
- **P2 item_list 大数组**：items ≤ 200/记录、trend 窗口 ≤ 12 时不降级。

基准在本机（开发机/CI runner）实测；数值随机器浮动，故断言用预算上限并对结果留有
说明性输出。测得数值与机器口径记入 PR「实测命令与结果」。
"""

from __future__ import annotations

import time

import pytest

import records_kit

LEDGER_ROWS = 5_000
HISTORY_ROWS = 10_000
BUDGET_MS = 200.0
ROUNDS = 25


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(fraction * len(ordered))) - 1))
    return ordered[index]


def _big_ledger(helpers, count: int) -> dict:
    rows = []
    for index in range(count):
        day = index % 28 + 1
        rows.append(
            helpers.ledger_row(
                f"ST001-battery_voltage_test-202601{day:02d}-{index % 24:02d}00-{index + 1}",
                rev=1,
                lifecycle="confirmed" if index % 3 else "voided",
                occurred_at=f"2026-01-{day:02d}T{index % 24:02d}:00:00+08:00",
                digest="sha256:" + f"{index:064d}",
                dedupe_key_values={"station": "ST001", "occurred_day": f"2026-01-{day:02d}", "test_kind": "定期"},
            )
        )
    return helpers.ledger_view(rows)


def _big_history(helpers, count: int) -> list[dict]:
    rows = []
    for index in range(count):
        minute = index % 60
        hour = (index // 60) % 24
        day = (index // (60 * 24)) % 28 + 1
        rows.append(
            helpers.history_row(
                f"2026-01-{day:02d}T{hour:02d}:{minute:02d}:00+08:00",
                2.2 + (index % 10) / 100,
                digest="sha256:" + f"{index:064d}",
            )
        )
    return rows


def _timed(callable_, rounds=ROUNDS) -> tuple[float, float]:
    callable_()  # 预热
    samples = []
    for _ in range(rounds):
        started = time.perf_counter()
        callable_()
        samples.append((time.perf_counter() - started) * 1000)
    return _percentile(samples, 0.95), max(samples)


@pytest.fixture(scope="module")
def big_inputs():
    import conftest as helpers

    ledger = _big_ledger(helpers, LEDGER_ROWS)
    history = _big_history(helpers, HISTORY_ROWS)
    envelope = helpers.envelope("create", ledger_view=ledger, history=history)
    return envelope


def test_p1_full_ledger_and_history_within_budget(big_inputs):
    registry = records_kit.default_registry()

    def run_once():
        result = records_kit.process(dict(big_inputs), registry)
        assert result["status"] == "ok", result["validation"]
        return result

    result = run_once()
    assert len(result["rules"]) >= 3
    assert result["trend"][0]["verdict"] in ("stable", "dropping")

    p95, worst = _timed(run_once)
    print(f"P1 p95={p95:.2f}ms max={worst:.2f}ms（预算 {BUDGET_MS}ms，账本 {LEDGER_ROWS} 行 + 历史 {HISTORY_ROWS} 行）")
    assert p95 <= BUDGET_MS, f"P1 超预算：p95={p95:.2f}ms > {BUDGET_MS}ms"


def test_p2_large_item_list_within_budget(helpers):
    registry = records_kit.default_registry()
    items = [{"cell_no": index + 1, "voltage": 2.2} for index in range(200)]
    history = _big_history(helpers, 11)
    envelope = helpers.envelope("create", payload=helpers.battery_payload(items=items), history=history)

    def run_once():
        result = records_kit.process(dict(envelope), registry)
        assert result["status"] == "ok", result["validation"]
        return result

    result = run_once()
    assert len(result["rules"]) == 400  # 200 条 × 2 条规则
    assert result["trend"][0]["verdict"] == "stable"

    p95, worst = _timed(run_once)
    print(f"P2 p95={p95:.2f}ms max={worst:.2f}ms（预算 {BUDGET_MS}ms，items=200 + 窗口 12）")
    assert p95 <= BUDGET_MS, f"P2 超预算：p95={p95:.2f}ms > {BUDGET_MS}ms"


def test_p2_large_item_list_is_not_degraded_by_window_growth(helpers):
    """窗口从 2 涨到 12 时结论与耗时都不应退化（不降级）。"""
    registry = records_kit.default_registry()
    items = [{"cell_no": index + 1, "voltage": 2.2} for index in range(200)]
    short = helpers.envelope("create", payload=helpers.battery_payload(items=items), history=_big_history(helpers, 1))
    long = helpers.envelope("create", payload=helpers.battery_payload(items=items), history=_big_history(helpers, 11))
    short_result = records_kit.process(short, registry)
    long_result = records_kit.process(long, registry)
    assert short_result["trend"][0]["verdict"] == "insufficient_history"
    assert long_result["trend"][0]["verdict"] == "stable"
    assert len(long_result["rules"]) == len(short_result["rules"]) == 400
