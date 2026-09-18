"""protocol —— 输入/输出接口协议（design.md §5/§6）。

- ``RecordEnvelope``：输入协议（§5），调用方注入 now/history/ledger_view 等事实。
- ``RecordResult``：输出协议（§6），status=ok|rejected，错误结构化。

协议 Schema 为手写权威文件，随包分发 JSON Schema（M1 交付）。
本模块当前为骨架占位，不含业务实现。
"""

from typing import TypedDict

__all__ = ["RecordEnvelope", "RecordResult"]


class RecordEnvelope(TypedDict, total=False):
    """输入协议占位类型（design.md §5 字段表，M1 定稿字段级 Schema）。"""

    protocol: str
    protocol_version: str
    operation: str
    record_type: str
    now: str
    payload: dict


class RecordResult(TypedDict, total=False):
    """输出协议占位类型（design.md §6 字段表，M1 定稿字段级 Schema）。"""

    protocol: str
    protocol_version: str
    operation: str
    status: str
    validation: dict
