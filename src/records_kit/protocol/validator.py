"""手写 JSON Schema 子集校验器（design.md §4：校验器为手写实现，不引入 jsonschema）。

支持的关键字：``type`` / ``enum`` / ``const`` / ``required`` / ``properties`` /
``additionalProperties`` / ``items`` / ``minItems`` / ``maxItems`` / ``minimum`` /
``maximum`` / ``exclusiveMinimum`` / ``exclusiveMaximum`` / ``minLength`` / ``pattern``
/ ``oneOf`` / ``anyOf`` / ``allOf`` / ``$ref``（同文档 ``#/`` 指针）。

用途：
- §10.1 协议合规：输入/输出各一组 JSON 实例过手写权威 Schema；
- §10.6 Schema 生成器自身测试：生成的 payload Schema 对全部黄金样本输入通过。
"""

from __future__ import annotations

import re

_JSON_TYPES = {
    "object": dict,
    "array": list,
    "string": str,
    "boolean": bool,
    "number": (int, float),
    "integer": int,
    "null": type(None),
}


class SchemaError(ValueError):
    """Schema 自身不合法（写错了 Schema，不是实例的问题）。"""


def _type_ok(value: object, name: str) -> bool:
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "boolean":
        return isinstance(value, bool)
    expected = _JSON_TYPES[name]
    if expected is not dict and isinstance(value, bool) and expected is not bool:
        return False
    return isinstance(value, expected)


def _resolve(schema: dict, root: dict) -> dict:
    seen = 0
    while isinstance(schema, dict) and "$ref" in schema:
        ref = schema["$ref"]
        if not ref.startswith("#/"):
            raise SchemaError(f"仅支持同文档 $ref：{ref}")
        node = root
        for part in ref[2:].split("/"):
            if not isinstance(node, dict) or part not in node:
                raise SchemaError(f"$ref 目标不存在：{ref}")
            node = node[part]
        schema = node
        seen += 1
        if seen > 20:
            raise SchemaError(f"$ref 循环：{ref}")
    return schema


def validate(instance: object, schema: dict) -> list[str]:
    """逐项校验，返回人类可读的问题列表（空列表 = 通过）。"""
    problems: list[str] = []
    _walk(instance, schema, schema, "$", problems)
    return problems


def _walk(value: object, schema: dict, root: dict, path: str, out: list[str]) -> None:
    schema = _resolve(schema, root)

    if "type" in schema:
        names = schema["type"]
        names = [names] if isinstance(names, str) else list(names)
        if not any(_type_ok(value, name) for name in names):
            out.append(f"{path}: 类型应为 {'/'.join(names)}")
            return

    if "enum" in schema and value not in schema["enum"]:
        out.append(f"{path}: 取值不在枚举 {schema['enum']}")
    if "const" in schema and value != schema["const"]:
        out.append(f"{path}: 取值应为常量 {schema['const']!r}")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        _numeric(value, schema, path, out)

    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            out.append(f"{path}: 长度小于 {schema['minLength']}")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            out.append(f"{path}: 不匹配模式 {schema['pattern']}")

    if isinstance(value, dict):
        _object(value, schema, root, path, out)
    elif isinstance(value, list):
        _array(value, schema, root, path, out)

    for keyword in ("allOf", "anyOf", "oneOf"):
        if keyword not in schema:
            continue
        branches = schema[keyword]
        matched = 0
        for branch in branches:
            branch_out: list[str] = []
            _walk(value, branch, root, path, branch_out)
            if not branch_out:
                matched += 1
        if keyword == "allOf" and matched != len(branches):
            out.append(f"{path}: 不满足 allOf 全部分支")
        elif keyword == "anyOf" and matched == 0:
            out.append(f"{path}: 不满足 anyOf 任一分支")
        elif keyword == "oneOf" and matched != 1:
            out.append(f"{path}: oneOf 命中 {matched} 个分支（要求恰好 1 个）")


def _numeric(value: float, schema: dict, path: str, out: list[str]) -> None:
    if "minimum" in schema and value < schema["minimum"]:
        out.append(f"{path}: 小于最小值 {schema['minimum']}")
    if "maximum" in schema and value > schema["maximum"]:
        out.append(f"{path}: 大于最大值 {schema['maximum']}")
    if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
        out.append(f"{path}: 不大于 {schema['exclusiveMinimum']}")
    if "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]:
        out.append(f"{path}: 不小于 {schema['exclusiveMaximum']}")


def _object(value: dict, schema: dict, root: dict, path: str, out: list[str]) -> None:
    for name in schema.get("required", []):
        if name not in value:
            out.append(f"{path}.{name}: 缺少必填字段")
    properties = schema.get("properties", {})
    additional = schema.get("additionalProperties", True)
    for name, item in value.items():
        child = f"{path}.{name}"
        if name in properties:
            _walk(item, properties[name], root, child, out)
        elif additional is False:
            out.append(f"{child}: 不允许的字段")
        elif isinstance(additional, dict):
            _walk(item, additional, root, child, out)
    if "minProperties" in schema and len(value) < schema["minProperties"]:
        out.append(f"{path}: 字段数少于 {schema['minProperties']}")


def _array(value: list, schema: dict, root: dict, path: str, out: list[str]) -> None:
    if "minItems" in schema and len(value) < schema["minItems"]:
        out.append(f"{path}: 元素数少于 {schema['minItems']}")
    if "maxItems" in schema and len(value) > schema["maxItems"]:
        out.append(f"{path}: 元素数多于 {schema['maxItems']}")
    if schema.get("uniqueItems"):
        seen = []
        for item in value:
            if item in seen:
                out.append(f"{path}: 元素重复")
                break
            seen.append(item)
    item_schema = schema.get("items")
    if isinstance(item_schema, dict):
        for index, item in enumerate(value):
            _walk(item, item_schema, root, f"{path}[{index}]", out)
