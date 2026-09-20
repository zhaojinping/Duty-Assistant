"""趋势判定（design.md §7.2 trend 段 / §5.1 历史契约）。

**M3 口径收敛（已定）**：`docs/trend-caliber-proposal.md` §3（拍板见 #28 评论）——

- 取值：``trend.source`` 的 ``agg(field_path)`` 对**每条历史行**取值，加当前记录一点；
- 分组：声明 ``group`` 时只消费**同键值**的历史行 + 当前记录（序列隔离，防跨设备假趋势）；
  缺省不分组（保持 M1 行为，向后兼容）；
- 窗口：取末尾 ``window`` 点；不足**不静默通过** → ``insufficient_history``（level=info，evidence 注明 n/window）；
- 基准：窗口内**首值**；``dropping ⇔ drop ≥ 阈值``（``≥`` 含等于）；
- 阈值二选一（声明层互斥）：``drop_warn`` = 相对降幅 ``(首值 − 末值) / |首值|``；
  ``drop_abs`` = 绝对差值 ``首值 − 末值``（零基准口径）。声明 ``drop_warn`` 而首值为 0 → 不可算 → ``insufficient_history``；
  声明 ``drop_abs`` 时该 trend 一律按绝对差值判定（互斥前提下无未定义分支）；
- 历史行取不到所需值时该行跳过并计入 evidence（§5.1 旧数据适配口径）。
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


def _group_key(declaration: Declaration, spec: TrendSpec, fields: dict) -> tuple[str | None, object]:
    """分组键与实际键值；未声明 ``group`` → ``(None, None)``（不分组，M1 行为）。"""
    if spec.group is None:
        return None, None
    resolved = resolve_path(declaration, spec.group)
    key = resolved[1].key if resolved is not None and resolved[1] is not None else spec.group
    return key, fields.get(key)


def _insufficient(spec: TrendSpec, evidence: str) -> dict:
    return {"metric": spec.metric, "verdict": "insufficient_history", "level": LEVEL_INFO, "evidence": evidence}


def _conclusion(spec: TrendSpec, dropping: bool, evidence: str) -> dict:
    return {
        "metric": spec.metric,
        "verdict": "dropping" if dropping else "stable",
        "level": LEVEL_DROPPING if dropping else LEVEL_INFO,
        "evidence": evidence,
    }


def evaluate_trend(declaration: Declaration, fields: dict, history_rows: list[dict]) -> list[dict]:
    """对声明内每个 ``[[trend]]`` 给一条结论。"""
    entries: list[dict] = []
    for spec in declaration.trends:
        entries.append(_evaluate_one(declaration, spec, fields, history_rows))
    return entries


def _evaluate_one(declaration: Declaration, spec: TrendSpec, fields: dict, history_rows: list[dict]) -> dict:
    group_key, group_value = _group_key(declaration, spec, fields)
    if group_key is not None and group_value is None:
        return _insufficient(
            spec, f"当前记录缺分组键 {spec.group}，无法划定序列，不输出趋势结论"
        )

    series: list[float] = []
    skipped = 0
    for row in history_rows:
        row_fields = row.get("fields") or {}
        if group_key is not None and row_fields.get(group_key) != group_value:
            continue  # 序列隔离（提案 §3.4）：不同键值行不参与，也不计入「跳过」（跳过的语义是取不到值）
        value = _extract(declaration, spec, row_fields)
        if value is None:
            skipped += 1
        else:
            series.append(value)
    current = _extract(declaration, spec, fields)
    notes = f"；跳过 {skipped} 行（source 取不到值）" if skipped else ""

    if current is None:
        return _insufficient(spec, f"当前记录 {spec.source} 取不到值，不输出趋势结论{notes}")
    series.append(current)

    if len(series) < spec.window:
        return _insufficient(spec, f"{len(series)}/{spec.window}（窗口不足，按 §5.1 不输出结论）{notes}")

    window_values = series[-spec.window :]
    first, last = window_values[0], window_values[-1]

    if spec.drop_abs is not None:
        drop = first - last
        dropping = drop >= spec.drop_abs
        return _conclusion(
            spec,
            dropping,
            f"窗口 {len(window_values)}/{spec.window} 点，{first} → {last}，"
            f"绝对差值 {round(drop, 4)}（阈值 {spec.drop_abs}，零基准口径）{notes}",
        )

    if first == 0:
        return _insufficient(
            spec,
            f"窗口 {len(window_values)} 点但首值为 0，相对降幅不可算（未声明 drop_abs）{notes}",
        )
    drop = (first - last) / abs(first)
    dropping = drop >= spec.drop_warn
    return _conclusion(
        spec,
        dropping,
        f"窗口 {len(window_values)}/{spec.window} 点，{first} → {last}，"
        f"相对降幅 {round(drop, 4)}（阈值 {spec.drop_warn}）{notes}",
    )
