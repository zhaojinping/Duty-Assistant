"""生命周期操作集与乐观锁（design.md §8）。

状态机（§8.1，correct 双语义）：

```
create ─► draft
draft  ──confirm(签齐)──► confirmed
draft  ──return(留痕, rev 不变)──► draft
draft  ──correct(编辑修订: rev+1, 仍 draft, 不加 supersedes)──► draft
draft/confirmed ──void(reason)──► voided
confirmed ──correct(定稿更正: rev+1 回 draft + supersedes 指向前版)──► draft(新版)
confirmed ──archive(ref)──► archived（archived 禁 void）
alarm_ack：任意非 voided，不改 fields、不加 rev
```

核心无持久状态（铁律 3）：被操作记录的现状从 ``ledger_view`` 取出，``subject.rev``
即乐观锁版本号；``return`` 的退回留痕、``alarm_ack`` 的处置记录由壳层记账。
"""

from __future__ import annotations

from dataclasses import dataclass

from records_kit.errors import (
    E_ARCHIVE_REF,
    E_DUP_DIGEST,
    E_DUP_KEY,
    E_DUP_UID,
    E_REQUIRED,
    E_REV_CONFLICT,
    E_STATE_ILLEGAL,
    E_TIME_INVALID,
    E_UNKNOWN_FIELD,
    E_VOID_REASON,
    reject,
)
from records_kit.engine.views import dedupe_values, row_dedupe_values
from records_kit.util import TimeTextError, compute_digest, parse_rfc3339, wall_stamp

TRANSITION_LIFECYCLES = ("draft", "confirmed", "archived", "voided")
DISPOSITION_STATUSES = ("acked", "resolved")


@dataclass
class LifecycleResult:
    """结果协议的 ``record`` 块 + 本轮判定的依据（供调用方组装其余字段）。"""

    record: dict
    fields: dict
    occurred_at: str | None = None
    source_lifecycle: str | None = None


def run(operation: str, envelope: dict, declaration, ledger_view: dict) -> LifecycleResult:
    """按操作推进生命周期；非法流转/版本冲突一律抛 ``Rejected``。"""
    handlers = {
        "create": _create,
        "confirm": _confirm,
        "return": _return_to_draft,
        "correct": _correct,
        "void": _void,
        "archive": _archive,
        "alarm_ack": _alarm_ack,
    }
    if operation not in handlers:  # pragma: no cover - 信封已收窄 operation
        raise reject("operation", E_STATE_ILLEGAL, f"不支持的流转操作：{operation}")
    return handlers[operation](envelope, declaration, ledger_view)


# ---------------------------------------------------------------- create


def _create(envelope: dict, declaration, ledger_view: dict) -> LifecycleResult:
    station_id = envelope["station"]["station_id"]
    payload = envelope["payload"]
    occurred_at = envelope["occurred_at"]
    rows = ledger_view.get("same_type_records", [])

    if envelope.get("subject") is not None:
        raise reject("subject", E_STATE_ILLEGAL, "create 不接受 subject（记录尚不存在）")
    if envelope.get("confirmations"):
        raise reject("confirmations", E_STATE_ILLEGAL, "create 阶段不签认，签认走 confirm")

    stamp_day, stamp_time = wall_stamp(occurred_at)
    uid = f"{station_id}-{declaration.record_type}-{stamp_day}-{stamp_time}-{envelope['create_seq']}"
    # uid/create_seq 查重对**含 voided 墓碑**的完整视图（墓碑占号，防审计链断裂）
    if any(row.get("record_uid") == uid for row in rows):
        raise reject("create_seq", E_DUP_UID, "uid 冲突：同分钟 create_seq 重复或撞上墓碑")

    values = dedupe_values(declaration, station_id, occurred_at, payload)
    for row in rows:
        if row.get("lifecycle") == "voided":  # 业务判重对非 voided 视图：作废让位
            continue
        if row_dedupe_values(declaration, row, station_id) == values:
            raise reject("payload", E_DUP_KEY, f"业务判重命中（{'+'.join(declaration.dedupe_key)}）")

    digest = compute_digest(
        station_id, declaration.record_type, occurred_at, declaration.schema_version, payload
    )
    if digest in ledger_view.get("confirmed_digests", []) or any(
        row.get("digest") == digest and row.get("lifecycle") != "voided" for row in rows
    ):
        raise reject("payload", E_DUP_DIGEST, "同版本重复提交（digest 命中）")

    return LifecycleResult(
        record={
            "record_uid": uid,
            "rev": 1,
            "lifecycle": "draft",
            "fields": payload,
            "digest": digest,
            "signature_slots": _slots(declaration, "draft"),
            "links": [],
        },
        fields=payload,
        occurred_at=occurred_at,
        source_lifecycle=None,
    )


# ---------------------------------------------------------------- confirm / return


def _confirm(envelope: dict, declaration, ledger_view: dict) -> LifecycleResult:
    row = _subject_row(envelope, ledger_view)
    _check_rev(row, envelope["subject"])
    if row.get("lifecycle") != "draft":
        raise reject("subject.lifecycle", E_STATE_ILLEGAL, "confirm 仅适用于 draft 记录")

    confirmations = envelope.get("confirmations") or []
    if not isinstance(confirmations, list) or not confirmations:
        raise reject("confirmations", E_REQUIRED, "confirm 必须带签认列表")
    signed: dict[str, dict] = {}
    for index, item in enumerate(confirmations):
        path = f"confirmations[{index}]"
        if not isinstance(item, dict):
            raise reject(path, E_STATE_ILLEGAL, "签认项必须是对象")
        slot = item.get("slot")
        if slot not in declaration.signature_slots:
            raise reject(f"{path}.slot", E_STATE_ILLEGAL, f"未知签字槽：{slot!r}")
        if slot in signed:
            raise reject(f"{path}.slot", E_STATE_ILLEGAL, f"签字槽重复签认：{slot}")
        by = item.get("by")
        if not isinstance(by, str) or not by:
            raise reject(f"{path}.by", E_REQUIRED, "签认必须带签认人")
        at = item.get("at")
        if not isinstance(at, str):
            raise reject(f"{path}.at", E_TIME_INVALID, "签认必须带 RFC3339 时间")
        try:
            parse_rfc3339(at)
        except TimeTextError as exc:
            raise reject(f"{path}.at", E_TIME_INVALID, str(exc)) from exc
        signed[slot] = {"slot": slot, "state": "signed", "by": by, "at": at}

    missing = [slot for slot in declaration.signature_slots if slot not in signed]
    if missing:
        raise reject("confirmations", E_STATE_ILLEGAL, f"签认不全，缺槽位：{'、'.join(missing)}")

    fields = row.get("fields") or {}
    digest = _version_digest(row, declaration, envelope, fields)
    return LifecycleResult(
        record=_record(row, fields, digest, "confirmed", signature_slots=[signed[slot] for slot in declaration.signature_slots]),
        fields=fields,
        occurred_at=row.get("occurred_at"),
        source_lifecycle="draft",
    )


def _return_to_draft(envelope: dict, declaration, ledger_view: dict) -> LifecycleResult:
    row = _subject_row(envelope, ledger_view)
    _check_rev(row, envelope["subject"])
    if row.get("lifecycle") != "draft":
        raise reject("subject.lifecycle", E_STATE_ILLEGAL, "return 仅适用于 draft 记录")
    reason = envelope.get("return_reason")
    if not isinstance(reason, str) or not reason:
        raise reject("return_reason", E_REQUIRED, "return 必须带退回原因（留痕）")
    fields = row.get("fields") or {}
    digest = row.get("digest") or _version_digest(row, declaration, envelope, fields)
    # rev 不变：退回只留痕，修改走 correct(draft)，改完再 confirm（§8.1）
    return LifecycleResult(
        record=_record(row, fields, digest, "draft", signature_slots=_slots(declaration, "draft")),
        fields=fields,
        occurred_at=row.get("occurred_at"),
        source_lifecycle="draft",
    )


# ---------------------------------------------------------------- correct / void


def _correct(envelope: dict, declaration, ledger_view: dict) -> LifecycleResult:
    station_id = envelope["station"]["station_id"]
    row = _subject_row(envelope, ledger_view)
    _check_rev(row, envelope["subject"])
    lifecycle = row.get("lifecycle")
    if lifecycle not in ("draft", "confirmed"):
        raise reject("subject.lifecycle", E_STATE_ILLEGAL, f"{lifecycle} 记录不可更正（archived 冲正走壳层）")

    payload = envelope["payload"]
    occurred_at = envelope["occurred_at"]
    previous_fields = row.get("fields") or {}
    new_rev = int(row.get("rev", 1)) + 1

    # 触及 dedupe_key 字段时对非 voided 视图重判业务判重（排除自身，§8.2）
    before = dedupe_values(declaration, station_id, row.get("occurred_at") or occurred_at, previous_fields)
    after = dedupe_values(declaration, station_id, occurred_at, payload)
    if before != after:
        for other in ledger_view.get("same_type_records", []):
            if other.get("record_uid") == row.get("record_uid") or other.get("lifecycle") == "voided":
                continue
            if row_dedupe_values(declaration, other, station_id) == after:
                raise reject("payload", E_DUP_KEY, "更正触及业务键，重判命中其它记录")

    digest = compute_digest(station_id, declaration.record_type, occurred_at, declaration.schema_version, payload)
    if _digest_taken(digest, ledger_view, row):
        raise reject("payload", E_DUP_DIGEST, "同版本重复提交（digest 命中其它记录）")

    links: list[dict] = []
    if lifecycle == "confirmed":
        # 定稿更正：原 confirmed 版并存，自动加 supersedes 指向前版（§8.1）
        links.append({"type": "supersedes", "record_uid": row.get("record_uid")})
    return LifecycleResult(
        record={
            "record_uid": row.get("record_uid"),
            "rev": new_rev,
            "lifecycle": "draft",
            "fields": payload,
            "digest": digest,
            "signature_slots": _slots(declaration, "draft"),
            "links": links,
        },
        fields=payload,
        occurred_at=occurred_at,
        source_lifecycle=lifecycle,
    )


def _void(envelope: dict, declaration, ledger_view: dict) -> LifecycleResult:
    row = _subject_row(envelope, ledger_view)
    _check_rev(row, envelope["subject"])
    lifecycle = row.get("lifecycle")
    if lifecycle not in ("draft", "confirmed"):
        raise reject("subject.lifecycle", E_STATE_ILLEGAL, f"{lifecycle} 记录不可作废（archived 冲正走壳层）")
    reason = envelope.get("void_reason")
    voided_by = envelope.get("voided_by")
    if not isinstance(reason, str) or not reason or not isinstance(voided_by, str) or not voided_by:
        raise reject("void_reason", E_VOID_REASON, "void 必须带作废原因与作废人（墓碑留痕）")
    fields = row.get("fields") or {}
    digest = row.get("digest") or _version_digest(row, declaration, envelope, fields)
    return LifecycleResult(
        record=_record(
            row,
            fields,
            digest,
            "voided",
            signature_slots=_slots(declaration, "confirmed" if lifecycle == "confirmed" else "draft"),
        ),
        fields=fields,
        occurred_at=row.get("occurred_at"),
        source_lifecycle=lifecycle,
    )


# ---------------------------------------------------------------- archive


def _archive(envelope: dict, declaration, ledger_view: dict) -> LifecycleResult:
    row = _subject_row(envelope, ledger_view)
    if row.get("lifecycle") != "confirmed":
        raise reject("subject.lifecycle", E_STATE_ILLEGAL, "archive 仅在 confirmed 上可用")
    reference = envelope.get("archive_ref")
    archived_by = envelope.get("archived_by")
    if not isinstance(reference, str) or not reference or not isinstance(archived_by, str) or not archived_by:
        raise reject("archive_ref", E_ARCHIVE_REF, "archive 必须带归档引用与归档人")

    subject_rev = envelope["subject"]["rev"]
    row_rev = int(row.get("rev", 1))
    confirmed_fields = row.get("confirmed_fields")
    if confirmed_fields is not None:
        # correct 并存窗口：archive 目标=**最后确认版**（修订A 拍板，§8.3）
        if subject_rev not in (row_rev, row_rev - 1):
            raise reject("subject.rev", E_REV_CONFLICT, "并存窗口内只能归档最后确认版")
        target_rev = row_rev - 1
        fields = confirmed_fields
        occurred_at = row.get("occurred_at")
        if not isinstance(occurred_at, str):
            raise reject("ledger_view.occurred_at", E_REQUIRED, "并存窗口归档需账本行给出 occurred_at 以重算该版指纹")
        digest = compute_digest(
            envelope["station"]["station_id"], declaration.record_type, occurred_at, declaration.schema_version, fields
        )
    else:
        if subject_rev != row_rev:
            raise reject("subject.rev", E_REV_CONFLICT, "归档目标版本与账本不符")
        target_rev = row_rev
        fields = row.get("fields") or {}
        digest = _version_digest(row, declaration, envelope, fields)

    return LifecycleResult(
        record=_record(
            row,
            fields,
            digest,
            "archived",
            rev=target_rev,
            signature_slots=_slots(declaration, "confirmed"),
        ),
        fields=fields,
        occurred_at=row.get("occurred_at"),
        source_lifecycle="confirmed",
    )


# ---------------------------------------------------------------- alarm_ack


def _alarm_ack(envelope: dict, declaration, ledger_view: dict) -> LifecycleResult:
    row = _subject_row(envelope, ledger_view)
    if row.get("lifecycle") == "voided":
        raise reject("subject.lifecycle", E_STATE_ILLEGAL, "voided 记录不接受告警处置")
    payload = envelope["payload"]
    for name in payload:
        if name != "alarm_disposition":
            raise reject(f"payload.{name}", E_UNKNOWN_FIELD, "alarm_ack 的 payload 仅含 alarm_disposition")
    disposition = payload.get("alarm_disposition")
    if not isinstance(disposition, dict):
        raise reject("payload.alarm_disposition", E_REQUIRED, "alarm_ack 必须带处置对象")
    mark = disposition.get("fingerprint")
    if not isinstance(mark, str) or not mark:
        raise reject("payload.alarm_disposition.fingerprint", E_STATE_ILLEGAL, "处置对象缺告警指纹")
    if disposition.get("status") not in DISPOSITION_STATUSES:
        raise reject("payload.alarm_disposition.status", E_STATE_ILLEGAL, "处置状态只能是 acked/resolved")
    by = disposition.get("by")
    if not isinstance(by, str) or not by:
        raise reject("payload.alarm_disposition.by", E_STATE_ILLEGAL, "处置对象缺处置人")
    at = disposition.get("at")
    if not isinstance(at, str):
        raise reject("payload.alarm_disposition.at", E_STATE_ILLEGAL, "处置对象缺处置时间")
    try:
        parse_rfc3339(at)
    except TimeTextError as exc:
        raise reject("payload.alarm_disposition.at", E_TIME_INVALID, str(exc)) from exc

    fields = row.get("fields") or {}
    digest = row.get("digest") or _version_digest(row, declaration, envelope, fields)
    # 不改 fields、不加 rev（§8.1）；处置事件由壳层记入告警台账
    return LifecycleResult(
        record=_record(
            row,
            fields,
            digest,
            row.get("lifecycle"),
            signature_slots=_slots(declaration, row.get("lifecycle")),
        ),
        fields=fields,
        occurred_at=row.get("occurred_at"),
        source_lifecycle=row.get("lifecycle"),
    )


# ---------------------------------------------------------------- helpers


def _subject_row(envelope: dict, ledger_view: dict) -> dict:
    uid = envelope["subject"]["record_uid"]
    for row in ledger_view.get("same_type_records", []):
        if isinstance(row, dict) and row.get("record_uid") == uid:
            if row.get("lifecycle") != envelope["subject"].get("lifecycle"):
                raise reject(
                    "subject.lifecycle",
                    E_STATE_ILLEGAL,
                    f"subject.lifecycle 与账本不符：请求 {envelope['subject'].get('lifecycle')}，账本 {row.get('lifecycle')}",
                )
            return row
    raise reject("subject.record_uid", E_STATE_ILLEGAL, f"账本视图中没有记录：{uid}")


def _check_rev(row: dict, subject: dict) -> None:
    if int(row.get("rev", 0)) != int(subject["rev"]):
        raise reject(
            "subject.rev",
            E_REV_CONFLICT,
            f"乐观锁冲突：账本 rev={row.get('rev')}，请求 rev={subject['rev']}",
        )


def _slots(declaration, lifecycle: str | None, signed: list[dict] | None = None) -> list[dict]:
    if signed is not None:
        return signed
    state = "signed" if lifecycle in ("confirmed", "archived") else "pending"
    return [{"slot": slot, "state": state} for slot in declaration.signature_slots]


def _record(row: dict, fields: dict, digest: str, lifecycle: str | None, *, rev: int | None = None, signature_slots=None) -> dict:
    return {
        "record_uid": row.get("record_uid"),
        "rev": int(row.get("rev", 1)) if rev is None else rev,
        "lifecycle": lifecycle,
        "fields": fields,
        "digest": digest,
        "signature_slots": signature_slots if signature_slots is not None else [],
        "links": list(row.get("links") or []),
    }


def _version_digest(row: dict, declaration, envelope: dict, fields: dict) -> str:
    """当版指纹：账本行给了 occurred_at 就重算（可复核），否则回显行内 digest。"""
    occurred_at = row.get("occurred_at")
    if isinstance(occurred_at, str):
        return compute_digest(
            envelope["station"]["station_id"],
            declaration.record_type,
            occurred_at,
            declaration.schema_version,
            fields,
        )
    digest = row.get("digest")
    if isinstance(digest, str) and digest:
        return digest
    raise reject("ledger_view", E_REQUIRED, "账本行缺 occurred_at 与 digest，无法确定当版指纹")


def _digest_taken(digest: str, ledger_view: dict, row: dict) -> bool:
    """digest 精确判重：排除自身记录的历史版本（§8.2）；其余命中即拒。"""
    own_uid = row.get("record_uid")
    for other in ledger_view.get("same_type_records", []):
        if other.get("record_uid") == own_uid:
            continue
        if other.get("digest") == digest:
            return True
    return False
