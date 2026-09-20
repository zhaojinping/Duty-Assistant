"""protocol —— 输入/输出接口协议（design.md §5/§6）。

- ``RecordEnvelope``：输入协议（§5），调用方注入 now/history/ledger_view 等事实。
- ``RecordResult``：输出协议（§6），status=ok|rejected，错误结构化。

协议 Schema 为**手写权威文件**（``protocol/schemas/*.schema.json``），随包分发；
payload 的 JSON Schema 由 registry 生成（不作权威，见 §7.1）。
本模块是核心内「读取 Schema 文件」的边界之一（另一处是 ``registry.loader``）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TypedDict

from records_kit.protocol.validator import SchemaError, validate  # noqa: F401

__all__ = [
    "RecordEnvelope",
    "RecordResult",
    "SCHEMA_DIR",
    "VALID_OPERATIONS",
    "load_schema",
    "envelope_schema",
    "result_schema",
    "check_instance",
]

SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"
ENVELOPE_SCHEMA_FILE = "record_envelope.schema.json"
RESULT_SCHEMA_FILE = "record_result.schema.json"
VALID_OPERATIONS = (
    "create",
    "confirm",
    "return",
    "correct",
    "void",
    "archive",
    "alarm_ack",
    "cycle_probe",
)

_schema_cache: dict[str, dict] = {}


class RecordEnvelope(TypedDict, total=False):
    """输入协议（design.md §5 字段表）。"""

    protocol: str
    protocol_version: str
    operation: str
    record_type: str
    station: dict
    occurred_at: str
    now: str
    create_seq: int
    subject: dict
    payload: dict
    history: list
    ledger_view: dict
    alarm_history: list
    baselines: list
    links: list
    attachments_ref: list
    submitted_by: str
    confirmations: list
    return_reason: str
    archive_ref: str
    archived_by: str
    void_reason: str
    voided_by: str


class RecordResult(TypedDict, total=False):
    """输出协议（design.md §6 字段表）。"""

    protocol: str
    protocol_version: str
    operation: str
    record_type: str
    status: str
    validation: dict
    record: dict
    rules: list
    trend: list
    actions_hint: list
    alarm_state: dict
    cycle: list
    digest: str


def load_schema(filename: str) -> dict:
    """读取手写权威 Schema（同一进程内缓存）。"""
    if filename not in _schema_cache:
        path = SCHEMA_DIR / filename
        _schema_cache[filename] = json.loads(path.read_text(encoding="utf-8"))
    return _schema_cache[filename]


def envelope_schema() -> dict:
    return load_schema(ENVELOPE_SCHEMA_FILE)


def result_schema() -> dict:
    return load_schema(RESULT_SCHEMA_FILE)


def check_instance(instance: object, schema: dict) -> list[str]:
    """按手写 Schema 校验一个 JSON 实例（§10.1 协议合规测试入口）。"""
    return validate(instance, schema)
