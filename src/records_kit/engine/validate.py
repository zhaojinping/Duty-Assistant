"""payload 与关联结构校验（design.md §5/§7.3/§7.6）。

- payload：未知字段默认拒绝（``extra=reject``）、7 种字段类型、数值边界、
  条目容器约束（``min_items`` + ``key_field`` 去重）；
- 签字栏只读留白：任何试图把签字值塞进 payload 的输入 → ``E_SIGNATURE_VIOLATION``；
- links：类型须在声明 ``link_types`` 内、引用须存在于 ``ledger_view.linked_records``、
  ``supersedes`` 由核心自动写入（调用方自传 → ``E_LINK_INVALID``）。
"""

from __future__ import annotations

from records_kit.errors import (
    Collector,
    E_DUP_KEY,
    E_ENUM,
    E_LINK_INVALID,
    E_RANGE,
    E_REQUIRED,
    E_SIGNATURE_VIOLATION,
    E_TYPE,
    E_UNKNOWN_FIELD,
    reject,
)
from records_kit.registry.declaration import ITEMS_KEY, TRI_BOOL_OPTIONS, FieldSpec
from records_kit.util import TimeTextError, parse_rfc3339

# 签字栏形态的保留键：出现即视为替人填签字
SIGNATURE_RESERVED_KEYS = (
    "signature_slots",
    "signatures",
    "signature_values",
    "confirmations",
    "signed_by",
)


def _describe(value: object) -> str:
    return f"{value!r}"


def check_signature_absence(declaration, payload: dict, errors: Collector) -> None:
    """签字槽是只读留白（铁律 7）：payload 里不允许出现任何签字形态。"""
    reserved = set(SIGNATURE_RESERVED_KEYS)
    for slot in declaration.signature_slots:
        if slot in payload:
            errors.add(f"payload.{slot}", E_SIGNATURE_VIOLATION, "签字栏为只读留白，不得由输入填充")
    for name in payload:
        if name in reserved:
            errors.add(f"payload.{name}", E_SIGNATURE_VIOLATION, "签字/签认信息不走 payload")
    items = payload.get(ITEMS_KEY)
    if isinstance(items, list):
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            for slot in declaration.signature_slots:
                if slot in item:
                    errors.add(f"payload.{ITEMS_KEY}[{index}].{slot}", E_SIGNATURE_VIOLATION, "签字栏为只读留白")
            for name in item:
                if name in reserved:
                    errors.add(f"payload.{ITEMS_KEY}[{index}].{name}", E_SIGNATURE_VIOLATION, "签字信息不走 payload")


def _check_scalar(field: FieldSpec, value: object, path: str, errors: Collector) -> None:
    kind = field.kind
    if kind == "text":
        if not isinstance(value, str):
            errors.add(path, E_TYPE, f"{field.name} 必须是文本")
    elif kind == "bool":
        if not isinstance(value, bool):
            errors.add(path, E_TYPE, f"{field.name} 必须是布尔")
    elif kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            errors.add(path, E_TYPE, f"{field.name} 必须是数值")
            return
        if value != value or value in (float("inf"), float("-inf")):
            errors.add(path, E_TYPE, f"{field.name} 不接受 NaN/±Inf")
            return
        if field.minimum is not None and value < field.minimum:
            errors.add(path, E_RANGE, f"{field.name} 小于下限 {field.minimum}")
        if field.maximum is not None and value > field.maximum:
            errors.add(path, E_RANGE, f"{field.name} 大于上限 {field.maximum}")
        if field.decimals is not None:
            rounded = round(value, field.decimals)
            if abs(rounded - value) > 1e-9:
                errors.add(path, E_TYPE, f"{field.name} 小数位超过 {field.decimals}")
    elif kind == "enum":
        if value not in field.options:
            errors.add(path, E_ENUM, f"{field.name} 取值不在枚举 {list(field.options)} 内：{_describe(value)}")
    elif kind == "tri_bool":
        if value not in TRI_BOOL_OPTIONS:
            errors.add(path, E_ENUM, f"{field.name} 必须是 {'/'.join(TRI_BOOL_OPTIONS)}")
    elif kind == "datetime":
        if not isinstance(value, str):
            errors.add(path, E_TYPE, f"{field.name} 必须是 RFC3339 文本")
            return
        try:
            parse_rfc3339(value)
        except TimeTextError as exc:
            errors.add(path, E_TYPE, f"{field.name} 时间不合法：{exc}")


def _check_container(declaration, payload: dict, errors: Collector) -> None:
    items_spec = declaration.items
    provided = payload.get(ITEMS_KEY)
    if items_spec is None:
        if provided is not None:
            errors.add(f"payload.{ITEMS_KEY}", E_UNKNOWN_FIELD, "该记录类型未声明条目容器")
        return
    if provided is None:
        if items_spec.required:
            errors.add(f"payload.{ITEMS_KEY}", E_REQUIRED, f"条目必填（min_items={items_spec.min_items}）")
        return
    if not isinstance(provided, list):
        errors.add(f"payload.{ITEMS_KEY}", E_TYPE, "条目容器必须是数组")
        return
    if len(provided) < items_spec.min_items:
        errors.add(f"payload.{ITEMS_KEY}", E_REQUIRED, f"条目数少于 min_items={items_spec.min_items}")
    declared = {spec.key for spec in items_spec.fields}
    seen_keys: set = set()
    for index, item in enumerate(provided):
        path = f"payload.{ITEMS_KEY}[{index}]"
        if not isinstance(item, dict):
            errors.add(path, E_TYPE, "条目必须是对象")
            continue
        if declaration.extra == "reject":
            for name in item:
                if name not in declared:
                    errors.add(f"{path}.{name}", E_UNKNOWN_FIELD, "条目含未知字段")
        for spec in items_spec.fields:
            value = item.get(spec.key)
            if value is None:
                if spec.required:
                    errors.add(f"{path}.{spec.key}", E_REQUIRED, f"条目必填：{spec.name}")
                continue
            _check_scalar(spec, value, f"{path}.{spec.key}", errors)
        key_value = item.get(items_spec.key_field)
        if key_value is not None:
            if key_value in seen_keys:
                errors.add(f"{path}.{items_spec.key_field}", E_DUP_KEY, f"条目 {items_spec.key_field} 重复")
            seen_keys.add(key_value)


def validate_payload(declaration, payload: dict) -> dict:
    """校验 payload 并返回其副本（不注入默认值，缺省即缺省）。"""
    if not isinstance(payload, dict):
        raise reject("payload", E_TYPE, "payload 必须是对象")
    errors = Collector()
    check_signature_absence(declaration, payload, errors)
    declared = {spec.key for spec in declaration.fields}
    for name in payload:
        if name == ITEMS_KEY:
            if declaration.items is None:
                errors.add(f"payload.{name}", E_UNKNOWN_FIELD, "该记录类型未声明条目容器")
            continue
        if name not in declared and declaration.extra == "reject":
            errors.add(f"payload.{name}", E_UNKNOWN_FIELD, "未知字段（extra=reject）")
    for spec in declaration.fields:
        value = payload.get(spec.key)
        if value is None:
            if spec.required:
                errors.add(f"payload.{spec.key}", E_REQUIRED, f"必填：{spec.name}")
            continue
        _check_scalar(spec, value, f"payload.{spec.key}", errors)
    _check_container(declaration, payload, errors)
    errors.raise_if_any()
    return dict(payload)


def missing_attachments(declaration, payload: dict, envelope: dict) -> list[dict]:
    """声明 ``require_attachment`` 的字段缺附件的结构化清单（空 = 齐全，§5）。

    - 记录级字段：只要有一条 ``kind`` 匹配的附件即可（保持原语义）；
    - 条目级字段：该字段非空的**每个条目**都要有 ``kind`` 匹配且 ``item_key`` 与该条目
      ``key_field`` 值相等（按字符串比较）的附件；
    - 每条 ``{kind, item_key, text}``，``item_key`` 为条目键值（记录级为 None），
      ``text`` 形如 ``"photo: 测点 spot_no=3 缺红外图"``。
    """
    available = envelope.get("attachments_ref") or []
    entries = [entry for entry in available if isinstance(entry, dict)]
    kinds = {entry.get("kind") for entry in entries}
    missing: list[dict] = []
    for spec in declaration.fields:
        if spec.require_attachment and payload.get(spec.key) is not None and spec.require_attachment not in kinds:
            missing.append(
                {
                    "kind": spec.require_attachment,
                    "item_key": None,
                    "text": f"{spec.require_attachment}: 记录字段 {spec.key} 缺{_attachment_noun(spec.require_attachment)}",
                }
            )
    if declaration.items is None:
        return missing
    items = payload.get(ITEMS_KEY)
    if not isinstance(items, list):
        return missing
    key_field = declaration.items.key_field
    bound = {(entry.get("kind"), str(entry.get("item_key"))) for entry in entries if "item_key" in entry}
    for spec in declaration.items.fields:
        if not spec.require_attachment:
            continue
        for item in items:
            if not isinstance(item, dict) or item.get(spec.key) is None:
                continue
            key_value = item.get(key_field)
            if (spec.require_attachment, str(key_value)) not in bound:
                missing.append(
                    {
                        "kind": spec.require_attachment,
                        "item_key": key_value,
                        "text": (
                            f"{spec.require_attachment}: 测点 {key_field}={key_value} "
                            f"缺{_attachment_noun(spec.require_attachment)}"
                        ),
                    }
                )
    return missing


def require_attachments(declaration, payload: dict, envelope: dict) -> list[str]:
    """缺附件清单的文本形态：``["photo: 测点 spot_no=3 缺红外图", ...]``（空 = 齐全）。

    处置（记录级拒绝 / 规则级提醒）由调用方按声明 ``attachment_missing`` 决定。
    """
    return [item["text"] for item in missing_attachments(declaration, payload, envelope)]


def _attachment_noun(kind: str) -> str:
    return "红外图" if kind == "photo" else f"{kind} 附件"


def attachment_issue(missing: list[dict]):
    """缺附件的记录级拒绝（``attachment_missing="reject"``，路径 ``attachments_ref``）。"""
    return reject("attachments_ref", E_REQUIRED, f"缺少必附附件：{'；'.join(item['text'] for item in missing)}")


def attachment_warnings(missing: list[dict]) -> list[dict]:
    """缺附件的规则级提醒（``attachment_missing="warn"``）：每种 kind 一条，追加在 rules 末尾。

    形状与规则条目一致：``rule_id="attachment_<kind>"``、``kind="attachment"``、``tier=1``、
    ``verdict="violation"``、``level="warn"``；不进告警候选。
    """
    by_kind: dict[str, list] = {}
    for item in missing:
        by_kind.setdefault(item["kind"], []).append(item["item_key"])
    entries: list[dict] = []
    for kind, keys in by_kind.items():
        item_keys = [str(key) for key in keys if key is not None]
        record_level = len(item_keys) < len(keys)
        parts = []
        if item_keys:
            parts.append(f"测点 {'、'.join(item_keys)}")
        if record_level:
            parts.append("记录级附件")
        entries.append(
            {
                "rule_id": f"attachment_{kind}",
                "kind": "attachment",
                "tier": 1,
                "verdict": "violation",
                "threshold": f"require_attachment:{kind}",
                "level": "warn",
                "detail": f"缺{_attachment_noun(kind)}：{'；'.join(parts)}",
            }
        )
    return entries


def validate_links(declaration, envelope: dict, linked_rows: list[dict]) -> list[dict]:
    """成对关系：类型白名单、引用存在性、``supersedes`` 不可自传（§7.6）。"""
    links = envelope.get("links")
    if links is None:
        return []
    if not isinstance(links, list):
        raise reject("links", E_TYPE, "links 必须是数组")
    known = {row.get("record_uid") for row in linked_rows if isinstance(row, dict)}
    result: list[dict] = []
    for index, link in enumerate(links):
        path = f"links[{index}]"
        if not isinstance(link, dict):
            raise reject(path, E_LINK_INVALID, "成对关系必须是对象")
        link_type = link.get("type")
        if link_type not in declaration.link_types:
            raise reject(f"{path}.type", E_LINK_INVALID, f"成对类型未在声明 link_types 内：{link_type!r}")
        if link_type == "supersedes":
            raise reject(path, E_LINK_INVALID, "supersedes 由核心在 correct 时自动写入，不接受调用方传入")
        target = link.get("record_uid")
        if not isinstance(target, str) or not target:
            raise reject(f"{path}.record_uid", E_LINK_INVALID, "成对关系必须带 record_uid")
        if target not in known:
            raise reject(f"{path}.record_uid", E_LINK_INVALID, f"引用的记录不在 ledger_view.linked_records 内：{target}")
        result.append({"type": link_type, "record_uid": target})
    return result
