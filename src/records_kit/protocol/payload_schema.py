"""payload JSON Schema 生成器（design.md §7.1）。

由 registry 声明生成 payload 的 JSON Schema——**不作权威**（声明才是权威），
用途是给壳层/采集端做前置校验、以及 §10.6 的生成器自身测试：
生成产物必须对全部黄金样本输入通过手写校验器交叉验证。
"""

from __future__ import annotations

RFC3339_PATTERN = (
    "^[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}"
    "(\\.[0-9]+)?([Zz]|[+-][0-9]{2}:[0-9]{2})$"
)

# 字段类型 → JSON Schema 片段（§7.3 的 7 种类型）
_TRI_BOOL_OPTIONS = ("是", "否", "不适用")


def field_schema(field) -> dict:
    """单个字段 → JSON Schema 片段。"""
    kind = field.kind
    schema: dict
    if kind == "text":
        schema = {"type": "string"}
    elif kind == "number":
        schema = {"type": "number"}
        if field.minimum is not None:
            schema["minimum"] = field.minimum
        if field.maximum is not None:
            schema["maximum"] = field.maximum
        if field.unit:
            schema["x-unit"] = field.unit
        if field.decimals is not None:
            schema["x-decimals"] = field.decimals
    elif kind == "enum":
        schema = {"type": "string", "enum": list(field.options)}
    elif kind == "tri_bool":
        schema = {"type": "string", "enum": list(_TRI_BOOL_OPTIONS)}
    elif kind == "bool":
        schema = {"type": "boolean"}
    elif kind == "datetime":
        schema = {"type": "string", "pattern": RFC3339_PATTERN}
    else:  # pragma: no cover - 声明的类型集在 meta-test 已收窄
        raise ValueError(f"未知字段类型：{kind}")
    if field.name:
        schema["title"] = field.name
    if field.require_attachment:
        schema["x-require-attachment"] = field.require_attachment
    return schema


def payload_schema(declaration) -> dict:
    """声明 → payload JSON Schema（含 items 容器与扩展注记）。"""
    properties: dict[str, dict] = {}
    required: list[str] = []
    for field in declaration.fields:
        properties[field.key] = field_schema(field)
        if field.required:
            required.append(field.key)

    if declaration.items is not None:
        item_properties: dict[str, dict] = {}
        item_required: list[str] = []
        for field in declaration.items.fields:
            item_properties[field.key] = field_schema(field)
            if field.required:
                item_required.append(field.key)
        properties[declaration.items_key] = {
            "type": "array",
            "minItems": declaration.items.min_items,
            "items": {
                "type": "object",
                "additionalProperties": declaration.extra == "allow",
                "properties": item_properties,
                "required": item_required,
            },
        }
        if declaration.items.required:
            required.append(declaration.items_key)

    schema: dict = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": f"{declaration.record_type} payload",
        "generated": "records_kit.protocol.payload_schema（生成物，权威是 registry 声明）",
        "type": "object",
        "additionalProperties": declaration.extra == "allow",
        "properties": properties,
        "required": required,
        "x-record-type": declaration.record_type,
        "x-schema-version": declaration.schema_version,
        "x-layout": declaration.layout,
        "x-dedupe-key": list(declaration.dedupe_key),
    }
    return schema


def schema_bundle(registry) -> dict:
    """整表 → ``{record_type: payload schema}``，供壳层一次性分发。"""
    return {name: payload_schema(declaration) for name, declaration in registry.items()}
