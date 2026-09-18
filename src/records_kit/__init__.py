"""records_kit —— Duty-Assistant 核心：纯函数记录引擎。

设计依据 docs/design.md（§4 架构）：
- 核心不做 I/O、不读时钟、无持久状态（铁律 1/2/3）；
- 对外统一入口 ``process(envelope: dict) -> dict``，输入/输出协议见 §5/§6。

本模块当前为 M1 骨架占位，不含业务实现。
"""

from records_kit.protocol import RecordEnvelope, RecordResult  # noqa: F401  (占位再导出)

__all__ = ["process", "RecordEnvelope", "RecordResult"]

PROTOCOL = "records-kit"
PROTOCOL_VERSION = "1.5"


def process(envelope: dict) -> dict:
    """统一入口：装配、校验、规则判定、趋势判定、生命周期操作。

    Args:
        envelope: ``RecordEnvelope`` 形状的输入协议（design.md §5）。

    Returns:
        ``RecordResult`` 形状的输出协议（design.md §6）。

    Raises:
        NotImplementedError: M1 骨架阶段尚未实现。
    """
    raise NotImplementedError("records_kit.process: M1 骨架占位，业务实现见后续里程碑")
