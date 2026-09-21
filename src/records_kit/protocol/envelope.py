"""信封（输入协议）的结构与条件必填校验（design.md §5）。

只做协议层判定：字段存在性、类型、协议版本、时间合法性——
业务语义（record_type 成员、payload 形状、links 引用）分别由
``engine.validate`` 与 ``registry`` 实施。
"""

from __future__ import annotations

from records_kit.errors import (
    Collector,
    E_NOW_MISSING,
    E_PROTOCOL,
    E_REQUIRED,
    E_TIME_INVALID,
    E_TYPE,
)
from records_kit.protocol import VALID_OPERATIONS, envelope_schema
from records_kit.util import TimeTextError, parse_rfc3339

# §5：需要 subject 的操作
SUBJECT_OPERATIONS = ("confirm", "return", "correct", "void", "archive", "alarm_ack")
# §5：需要 occurred_at 的操作
OCCURRED_OPERATIONS = ("create", "correct")
# §5：需要 payload 的操作
PAYLOAD_OPERATIONS = ("create", "correct", "alarm_ack")
# §5：create 必填 create_seq
ENVELOPE_SCHEMA_FIELDS = tuple(envelope_schema()["properties"])


def check_envelope(envelope: object) -> None:
    """结构、协议标识、now、station、时间窗口；任一不合规抛 ``Rejected``。"""
    from records_kit.errors import reject

    if not isinstance(envelope, dict):
        raise reject("$", E_PROTOCOL, "信封必须是对象")

    errors = Collector()
    for name in envelope:
        if name not in ENVELOPE_SCHEMA_FIELDS:
            errors.add(name, E_PROTOCOL, "信封含协议外字段")

    if envelope.get("protocol") != "records-kit":
        errors.add("protocol", E_PROTOCOL, "protocol 必须为 records-kit")
    version = envelope.get("protocol_version")
    if not isinstance(version, str) or "." not in version:
        errors.add("protocol_version", E_PROTOCOL, "protocol_version 必须是 主.次 形式")
    operation = envelope.get("operation")
    if operation not in VALID_OPERATIONS:
        errors.add("operation", E_PROTOCOL, f"未知操作：{operation!r}")
    errors.raise_if_any()

    station = envelope.get("station")
    if not isinstance(station, dict):
        raise reject("station", E_REQUIRED, "station 必填")
    for key in ("station_id", "station_name"):
        value = station.get(key)
        if not isinstance(value, str) or not value:
            raise reject(f"station.{key}", E_REQUIRED, "station_id/station_name 必填且非空")
    for key in station:
        if key not in ("station_id", "station_name"):
            raise reject(f"station.{key}", E_PROTOCOL, "station 含协议外字段")

    now_text = envelope.get("now")
    if now_text is None:
        raise reject("now", E_NOW_MISSING, "now 必填（核心不读时钟，铁律 1）")
    if not isinstance(now_text, str):
        raise reject("now", E_TIME_INVALID, "now 需 RFC3339 文本")
    try:
        now = parse_rfc3339(now_text)
    except TimeTextError as exc:
        raise reject("now", E_TIME_INVALID, str(exc)) from exc

    if operation in OCCURRED_OPERATIONS:
        occurred = envelope.get("occurred_at")
        if occurred is None:
            raise reject("occurred_at", E_REQUIRED, "create/correct 必须带业务发生时间")
        try:
            occurred_at = parse_rfc3339(occurred)
        except TimeTextError as exc:
            raise reject("occurred_at", E_TIME_INVALID, str(exc)) from exc
        if (occurred_at - now).total_seconds() > 300:
            raise reject("occurred_at", E_TIME_INVALID, "业务时间晚于 now+容差(5min)")
    elif envelope.get("occurred_at") is not None:
        try:
            parse_rfc3339(envelope["occurred_at"])
        except TimeTextError as exc:
            raise reject("occurred_at", E_TIME_INVALID, str(exc)) from exc

    if operation == "create":
        seq = envelope.get("create_seq")
        if seq is None:
            raise reject("create_seq", E_REQUIRED, "create 必须带 create_seq")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
            raise reject("create_seq", E_TYPE, "create_seq 必须是从 1 起的整数")

    if operation in SUBJECT_OPERATIONS:
        if operation in ("confirm",):
            # 签认列表必带；可为空（无签认槽位类型零签认，槽位完整性由生命周期层按声明判定）
            if not isinstance(envelope.get("confirmations"), list):
                raise reject("confirmations", E_REQUIRED, "confirm 必须带签认列表")
        if operation == "return" and not isinstance(envelope.get("return_reason"), str):
            raise reject("return_reason", E_REQUIRED, "return 必须带退回原因")
        subject = envelope.get("subject")
        if not isinstance(subject, dict):
            raise reject("subject", E_REQUIRED, f"{operation} 必须带 subject")
        if not isinstance(subject.get("record_uid"), str) or not subject.get("record_uid"):
            raise reject("subject.record_uid", E_REQUIRED, "subject.record_uid 必填")
        if subject.get("lifecycle") not in ("draft", "confirmed", "archived", "voided"):
            raise reject("subject.lifecycle", E_TYPE, "subject.lifecycle 取值非法")
        rev = subject.get("rev")
        if not isinstance(rev, int) or isinstance(rev, bool) or rev < 1:
            raise reject("subject.rev", E_TYPE, "subject.rev 必须是从 1 起的整数")

    if operation in PAYLOAD_OPERATIONS and not isinstance(envelope.get("payload"), dict):
        raise reject("payload", E_REQUIRED, f"{operation} 必须带 payload 对象")

    if operation in OCCURRED_OPERATIONS and not isinstance(envelope.get("submitted_by"), str):
        raise reject("submitted_by", E_REQUIRED, "create/correct 必须带提交人（≠签字人）")

    for name in ("history", "alarm_history", "baselines", "links", "attachments_ref", "confirmations"):
        value = envelope.get(name)
        if value is not None and not isinstance(value, list):
            raise reject(name, E_TYPE, f"{name} 必须是数组")
    if envelope.get("ledger_view") is not None and not isinstance(envelope["ledger_view"], dict):
        raise reject("ledger_view", E_TYPE, "ledger_view 必须是对象")
