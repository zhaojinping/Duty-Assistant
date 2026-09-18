"""registry.loader —— TOML 声明加载占位（design.md §7.1）。

M1 将实现：读取 TOML 声明并做 meta-test 自检（tomllib 解析、引用字段存在性等，
见 design.md §10.2）。本模块当前为骨架占位，不含业务实现。
"""

from pathlib import Path

__all__ = ["load"]


def load(path: str | Path) -> dict:
    """加载并自检一份记录声明 TOML。

    Args:
        path: 声明文件路径（从项目根/配置解析，见 AGENTS.md 可移植性约束）。

    Returns:
        解析后的声明 dict。

    Raises:
        NotImplementedError: M1 骨架阶段尚未实现。
    """
    raise NotImplementedError("registry.loader.load: M1 骨架占位，业务实现见后续里程碑")
