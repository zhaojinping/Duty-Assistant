"""纯函数工具：规范序列化、摘要、时间解析与周期算术。

铁律 1/5：本模块只把**输入文本**解析成时间并做算术，不读时钟、不取随机数，
同输入必同输出。时间来源永远是信封里的 ``now`` / ``occurred_at``。
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta

from records_kit.errors import reject, E_TIME_INVALID

DIGEST_PREFIX = "sha256:"

# RFC3339 带时区（§5：业务时间必须带时区）
RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})$"
)
_PERIOD_RE = re.compile(r"^(?:periodic:(\d+)d|monthly|quarterly)$")
_AGG_RE = re.compile(r"^(min|max|avg|count|last)\((.+)\)$")
PERIOD_KINDS = ("periodic", "monthly", "quarterly")
AGG_WHITELIST = ("min", "max", "avg", "count", "last")


class TimeTextError(ValueError):
    """时间文本不合法（调用方决定映射成哪个错误码）。"""


def parse_rfc3339(text: str) -> datetime:
    """解析带时区的 RFC3339 文本；无时区或格式不符抛 ``TimeTextError``。"""
    if not isinstance(text, str) or not RFC3339_RE.match(text):
        raise TimeTextError("需 RFC3339 带时区文本，如 2026-09-17T10:00:00+08:00")
    normalized = text.replace("t", "T")
    if normalized[-1] in "Zz":
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:  # 形似但日期不成立（如 2026-02-30）
        raise TimeTextError(f"时间文本不成立：{exc}") from exc
    if parsed.tzinfo is None:  # 理论上被正则挡住，双保险
        raise TimeTextError("必须带时区偏移")
    return parsed


def parse_occurred_at(text: str, path: str = "occurred_at") -> datetime:
    try:
        return parse_rfc3339(text)
    except TimeTextError as exc:
        raise reject(path, E_TIME_INVALID, str(exc)) from exc


def format_rfc3339(moment: datetime) -> str:
    """按原偏移输出 RFC3339 文本（秒精度，保留微秒若存在）。"""
    text = moment.isoformat()
    return text


def wall_day(occurred_at_text: str) -> str:
    """业务日期（按输入文本自身偏移的墙上日期，不做 UTC 换算）。"""
    return parse_rfc3339(occurred_at_text).strftime("%Y-%m-%d")


def wall_stamp(occurred_at_text: str) -> tuple[str, str]:
    """uid 用到的 ``yyyyMMdd`` 与 ``HHmm``（同样取输入偏移的墙上时间）。"""
    moment = parse_rfc3339(occurred_at_text)
    return moment.strftime("%Y%m%d"), moment.strftime("%H%M")


def canonical_json(value: object) -> str:
    """规范序列化：键字典序、无空白分隔、UTF-8 原样（§6.3）。"""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_digest(
    station_id: str,
    record_type: str,
    occurred_at: str,
    schema_version: str,
    payload: dict,
) -> str:
    """``sha256:<hex>``：摘要含站/类型/发生时间，不同日期的合法复测不误判（§6.3）。"""
    body = canonical_json(
        {
            "station_id": station_id,
            "record_type": record_type,
            "occurred_at": occurred_at,
            "schema_version": schema_version,
            "payload": payload,
        }
    )
    return DIGEST_PREFIX + hashlib.sha256(body.encode("utf-8")).hexdigest()


def period_spec(expr: str) -> tuple[str, int]:
    """``periodic:30d`` / ``monthly`` / ``quarterly`` → ``(kind, days)``；days 仅 periodic 有效。"""
    match = _PERIOD_RE.match(expr)
    if not match:
        raise TimeTextError(f"周期表达式不合法：{expr}")
    if match.group(1):
        return "periodic", int(match.group(1))
    return expr, 0


def _month_length(year: int, month: int) -> int:
    if month == 12:
        return 31
    return (datetime(year, month + 1, 1) - datetime(year, month, 1)).days


def add_months(moment: datetime, months: int) -> datetime:
    """自然月加法：月末按目标月最后一天收敛（如 1-31 +1M → 2-28）。"""
    total = (moment.year * 12 + moment.month - 1) + months
    year, month = divmod(total, 12)
    month += 1
    day = min(moment.day, _month_length(year, month))
    return moment.replace(year=year, month=month, day=day)


def add_period(moment: datetime, expr: str) -> datetime:
    """按声明周期表达式推进一个周期。"""
    kind, days = period_spec(expr)
    if kind == "periodic":
        return moment + timedelta(days=days)
    if kind == "monthly":
        return add_months(moment, 1)
    if kind == "quarterly":
        return add_months(moment, 3)
    raise TimeTextError(f"周期表达式不合法：{expr}")


def days_between(earlier: datetime, later: datetime) -> float:
    """两个时刻之间的小时差换算成天数（含时区偏移差异）。"""
    return (later - earlier).total_seconds() / 86400.0


def aggregate(agg: str, values: list[float]) -> float | None:
    """trend.source 的 agg 白名单实现（§7.3）；取值不可得返回 None。"""
    if not values:
        return None
    if agg == "min":
        return min(values)
    if agg == "max":
        return max(values)
    if agg == "avg":
        return sum(values) / len(values)
    if agg == "count":
        return float(len(values))
    if agg == "last":
        return values[-1]
    raise TimeTextError(f"聚合算子不在白名单：{agg}")
