"""声明视图的读取与结构校验（design.md §5.1 / §8.3 / §6.2）。

核心没有自己的存储（铁律 3）：history / ledger_view / alarm_history / baselines
全部由调用方取出传入，这里只做**结构合规**与取值口径，不做任何 I/O。
"""

from __future__ import annotations

from records_kit.errors import (
    Collector,
    E_HISTORY,
    E_REQUIRED,
    E_TYPE,
    reject,
)
from records_kit.util import TimeTextError, parse_rfc3339, wall_day

LIFECYCLES = ("draft", "confirmed", "archived", "voided")
# 「已做过」口径：记录非 voided 且存在 confirmed/archived 版本（§8.3）
DONE_LIFECYCLES = ("confirmed", "archived")


def _as_list(envelope: dict, name: str) -> list:
    value = envelope.get(name)
    if value is None:
        return []
    if not isinstance(value, list):
        raise reject(name, E_TYPE, f"{name} 必须是数组")
    return value


def ledger(envelope: dict, declaration=None, *, required: bool = False) -> dict:
    """取 ledger_view；``required`` 时缺失即拒（操作/探针/T3 需要，§5）。"""
    view = envelope.get("ledger_view")
    if view is None:
        if required:
            raise reject("ledger_view", E_REQUIRED, "该操作需要账本视图（§5/§8.3）")
        return {"confirmed_digests": [], "same_type_records": [], "linked_records": []}
    if not isinstance(view, dict):
        raise reject("ledger_view", E_TYPE, "ledger_view 必须是对象")
    errors = Collector()
    for key in view:
        if key not in ("confirmed_digests", "same_type_records", "linked_records"):
            errors.add(f"ledger_view.{key}", E_TYPE, "账本视图含协议外字段")
    digests = view.get("confirmed_digests", [])
    if not isinstance(digests, list) or not all(isinstance(item, str) for item in digests):
        errors.add("ledger_view.confirmed_digests", E_TYPE, "confirmed_digests 必须是字符串数组")
    for name in ("same_type_records", "linked_records"):
        rows = view.get(name, [])
        if not isinstance(rows, list):
            errors.add(f"ledger_view.{name}", E_TYPE, f"{name} 必须是数组")
            continue
        for index, row in enumerate(rows):
            _check_row(row, f"ledger_view.{name}[{index}]", errors)
    errors.raise_if_any()
    normalized = {
        "confirmed_digests": list(digests),
        "same_type_records": list(view.get("same_type_records", [])),
        "linked_records": list(view.get("linked_records", [])),
    }
    return normalized


LEDGER_ROW_KEYS = (
    "record_uid",
    "lifecycle",
    "rev",
    "occurred_at",
    "digest",
    "fields",
    "confirmed_fields",
    "confirmed_rev",
    "dedupe_key_values",
    "links",
)


def _check_row(row: object, path: str, errors: Collector) -> None:
    if not isinstance(row, dict):
        errors.add(path, E_TYPE, "账本行必须是对象")
        return
    for name in row:
        if name not in LEDGER_ROW_KEYS:
            errors.add(f"{path}.{name}", E_TYPE, "账本行含协议外字段")
    if not isinstance(row.get("record_uid"), str) or not row.get("record_uid"):
        errors.add(f"{path}.record_uid", E_TYPE, "账本行缺 record_uid")
    if row.get("lifecycle") not in LIFECYCLES:
        errors.add(f"{path}.lifecycle", E_TYPE, "账本行 lifecycle 取值非法")
    rev = row.get("rev")
    if not isinstance(rev, int) or isinstance(rev, bool) or rev < 1:
        errors.add(f"{path}.rev", E_TYPE, "账本行 rev 必须是从 1 起的整数")
    if not isinstance(row.get("fields"), dict):
        errors.add(f"{path}.fields", E_TYPE, "账本行必须带 fields 全量")
    for optional in ("occurred_at", "digest"):
        if row.get(optional) is not None and not isinstance(row[optional], str):
            errors.add(f"{path}.{optional}", E_TYPE, f"{path}.{optional} 必须是字符串")
    for optional_obj in ("confirmed_fields", "dedupe_key_values"):
        if row.get(optional_obj) is not None and not isinstance(row[optional_obj], dict):
            errors.add(f"{path}.{optional_obj}", E_TYPE, f"{path}.{optional_obj} 必须是对象")
    confirmed_rev = row.get("confirmed_rev")
    if confirmed_rev is not None and (
        not isinstance(confirmed_rev, int) or isinstance(confirmed_rev, bool) or confirmed_rev < 1
    ):
        errors.add(f"{path}.confirmed_rev", E_TYPE, "confirmed_rev 必须是从 1 起的整数")


def history(envelope: dict, declaration) -> list[dict]:
    """历史行：只有归档记录可入历史、时间严格递增、去重键不重复（§5.1）。"""
    rows = _as_list(envelope, "history")
    previous: str | None = None
    seen: set[tuple] = set()
    for index, row in enumerate(rows):
        path = f"history[{index}]"
        if not isinstance(row, dict):
            raise reject(path, E_HISTORY, "历史行必须是对象")
        if row.get("lifecycle") != "archived":
            raise reject(f"{path}.lifecycle", E_HISTORY, "只有归档记录可入历史")
        occurred = row.get("occurred_at")
        if not isinstance(occurred, str):
            raise reject(f"{path}.occurred_at", E_HISTORY, "历史行缺 occurred_at")
        try:
            parse_rfc3339(occurred)
        except TimeTextError as exc:
            raise reject(f"{path}.occurred_at", E_HISTORY, str(exc)) from exc
        if previous is not None and parse_rfc3339(occurred) <= parse_rfc3339(previous):
            raise reject(f"{path}.occurred_at", E_HISTORY, "历史行时间必须严格递增（排序由壳层负责）")
        previous = occurred
        fields = row.get("fields")
        if not isinstance(fields, dict):
            raise reject(f"{path}.fields", E_HISTORY, "历史行必须带 fields")
        if not isinstance(row.get("digest"), str):
            raise reject(f"{path}.digest", E_HISTORY, "历史行缺 digest")
        marker = (occurred, dedupe_marker(declaration, row))
        if marker in seen:
            raise reject(path, E_HISTORY, "历史行去重键重复（重复行由壳层去除后传入）")
        seen.add(marker)
    return list(rows)


def dedupe_marker(declaration, row: dict) -> tuple:
    """历史去重键成员：``occurred_at`` + dedupe_key 各字段值（§5.1 宽松口径）。"""
    fields = row.get("fields") or {}
    values = []
    for name in declaration.dedupe_key:
        if name == "station":
            values.append(None)
        elif name in ("occurred_day", "occurred_at"):
            values.append(row.get("occurred_at"))
        else:
            values.append(fields.get(name))
    return tuple(values)


def alarm_history(envelope: dict) -> list[dict]:
    """告警台账视图：``[{fingerprint, first_seen_at, occur_count, last_status, escalated_count}]``。"""
    rows = _as_list(envelope, "alarm_history")
    for index, row in enumerate(rows):
        path = f"alarm_history[{index}]"
        if not isinstance(row, dict):
            raise reject(path, E_TYPE, "告警台账行必须是对象")
        if not isinstance(row.get("fingerprint"), str) or not row.get("fingerprint"):
            raise reject(f"{path}.fingerprint", E_TYPE, "告警台账行缺指纹")
        count = row.get("occur_count", 0)
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise reject(f"{path}.occur_count", E_TYPE, "occur_count 必须是非负整数")
    return list(rows)


def alarm_row_for(envelope: dict, fingerprint: str) -> dict | None:
    for row in alarm_history(envelope):
        if row["fingerprint"] == fingerprint:
            return row
    return None


def baselines(envelope: dict) -> list[dict]:
    rows = _as_list(envelope, "baselines")
    for index, row in enumerate(rows):
        path = f"baselines[{index}]"
        if not isinstance(row, dict) or not isinstance(row.get("ref"), str):
            raise reject(path, E_TYPE, "baseline 行必须带 ref")
        if not isinstance(row.get("data"), dict):
            raise reject(f"{path}.data", E_TYPE, "baseline.data 必须是对象（键值表）")
    return list(rows)


def row_by_uid(envelope: dict, record_uid: str) -> dict | None:
    for row in ledger(envelope)["same_type_records"]:
        if row.get("record_uid") == record_uid:
            return row
    return None


def dedupe_values(declaration, station_id: str, occurred_at: str, fields: dict) -> tuple:
    """业务判重键取值：信封派生伪键 + 顶层 payload 字段（§7.2 dedupe_key）。"""
    values = []
    for name in declaration.dedupe_key:
        if name == "station":
            values.append(station_id)
        elif name == "occurred_day":
            values.append(wall_day(occurred_at))
        elif name == "occurred_at":
            values.append(occurred_at)
        else:
            values.append(fields.get(name))
    return tuple(values)


def row_dedupe_values(declaration, row: dict, station_id: str) -> tuple:
    """账本行的判重键取值：优先用行内 ``dedupe_key_values``，缺口按行内容推导。"""
    given = row.get("dedupe_key_values") or {}
    fields = row.get("fields") or {}
    occurred = row.get("occurred_at")
    values = []
    for name in declaration.dedupe_key:
        if name in given:
            values.append(given[name])
        elif name == "station":
            values.append(station_id)
        elif name == "occurred_day":
            values.append(wall_day(occurred) if isinstance(occurred, str) else None)
        elif name == "occurred_at":
            values.append(occurred)
        else:
            values.append(fields.get(name))
    return tuple(values)
