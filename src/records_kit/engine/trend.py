"""趋势判定（design.md §7.2 trend 段 / §5.1 历史契约）。

- 取值：``trend.source`` 的 ``agg(field_path)`` 对**每条历史行**取值，加当前记录一点；
- 窗口不足**不静默通过**：``verdict="insufficient_history"``（level=info，evidence 注明 n/window）；
- 历史行取不到所需值时该行跳过并计入 evidence（§5.1 旧数据适配口径）。

**M1 默认口径（待拍板，主设 §12 列为待定）**：窗口 = 历史点数 + 当前点，
``drop_warn`` 为**相对降幅**、比较基准为窗口内**最早值**：
``(首值 − 末值) / |首值| ≥ drop_warn`` → ``dropping``。定稿后只改本模块与声明。
"""

from __future__ import annotations

from records_kit.registry.declaration import ITEMS_KEY, Declaration, TrendSpec, resolve_path
from records_kit.util import aggregate

LEVEL_DROPPING = "warn"
LEVEL_INFO = "info"


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _extract(declaration: Declaration, spec: TrendSpec, source_fields: dict) -> float | None:
    """按 ``agg(field_path)`` 从一份 fields 取一个数值；取不到返回 None。"""
    resolved = resolve_path(declaration, spec.field_path)
    if resolved is None:
        return None
    scope, field = resolved
    if field is None:
        return None
    if scope == "items":
        items = source_fields.get(ITEMS_KEY)
        if not isinstance(items, list):
            return None
        values = [
            number
            for number in (_number(item.get(field.key)) for item in items if isinstance(item, dict))
            if number is not None
        ]
    else:
        single = _number(source_fields.get(field.key))
        values = [] if single is None else [single]
    if not values:
        return None
    if spec.agg == "count":
        values = [float(len(values))]
    return aggregate(spec.agg, values)


def evaluate_trend(declaration: Declaration, fields: dict, history_rows: list[dict]) -> list[dict]:
    """对声明内每个 ``[[trend]]`` 给一条结论。"""
    entries: list[dict] = []
    for spec in declaration.trends:
        entries.append(_evaluate_one(declaration, spec, fields, history_rows))
    return entries


def _evaluate_one(declaration: Declaration, spec: TrendSpec, fields: dict, history_rows: list[dict]) -> dict:
    series: list[float] = []
    skipped = 0
    for row in history_rows:
        value = _extract(declaration, spec, row.get("fields") or {})
        if value is None:
            skipped += 1
        else:
            series.append(value)
    current = _extract(declaration, spec, fields)
    notes = f"；跳过 {skipped} 行（source 取不到值）" if skipped else ""

    if current is None:
        return {
            "metric": spec.metric,
            "verdict": "insufficient_history",
            "level": LEVEL_INFO,
            "evidence": f"当前记录 {spec.source} 取不到值，不输出趋势结论{notes}",
        }
    series.append(current)

    if len(series) < spec.window:
        return {
            "metric": spec.metric,
            "verdict": "insufficient_history",
            "level": LEVEL_INFO,
            "evidence": f"{len(series)}/{spec.window}（窗口不足，按 §5.1 不输出结论）{notes}",
        }

    window_values = series[-spec.window :]
    first, last = window_values[0], window_values[-1]
    if first == 0:
        return {
            "metric": spec.metric,
            "verdict": "insufficient_history",
            "level": LEVEL_INFO,
            "evidence": f"窗口 {len(window_values)} 点但首值为 0，相对降幅不可算{notes}",
        }
    drop = (first - last) / abs(first)
    dropping = drop >= spec.drop_warn
    return {
        "metric": spec.metric,
        "verdict": "dropping" if dropping else "stable",
        "level": LEVEL_DROPPING if dropping else LEVEL_INFO,
        "evidence": (
            f"窗口 {len(window_values)}/{spec.window} 点，{first} → {last}，"
            f"相对降幅 {round(drop, 4)}（阈值 {spec.drop_warn}）{notes}"
        ),
    }
