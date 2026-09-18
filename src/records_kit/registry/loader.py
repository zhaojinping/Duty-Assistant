"""registry.loader —— TOML 声明的读取与自检（design.md §7.1/§10.2）。

本模块是核心内「读文件」的两个边界之一（另一处是 ``protocol`` 读手写 Schema）。
``tomllib`` 为标准库，零第三方依赖（§4 环境假设）。
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from records_kit.registry.declaration import Declaration, DeclarationError
from records_kit.registry.meta import validate_declaration

__all__ = ["load", "load_declaration", "load_dir", "declarations_dir", "DECLARATIONS_DIRNAME"]

DECLARATIONS_DIRNAME = "declarations"


def declarations_dir() -> Path:
    """随包分发的声明目录（调用方也可另指定目录，见 ``load_dir``）。"""
    return Path(__file__).resolve().parent / DECLARATIONS_DIRNAME


def load(path: str | Path) -> dict:
    """解析一份声明 TOML，返回原始 dict（不做语义自检）。"""
    target = Path(path)
    try:
        with target.open("rb") as handle:
            parsed = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise DeclarationError(f"声明文件不存在：{target}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise DeclarationError(f"{target}: TOML 解析失败：{exc}") from exc
    if not isinstance(parsed, dict):
        raise DeclarationError(f"{target}: 声明根必须是表")
    return parsed


def load_declaration(path: str | Path) -> Declaration:
    """解析并自检一份声明；坏声明抛 ``DeclarationError``（§10.2）。"""
    target = Path(path)
    return validate_declaration(load(target), source=str(target))


def load_dir(directory: str | Path) -> tuple[Declaration, ...]:
    """加载目录下全部 ``*.toml`` 声明，按 ``record_type`` 去重。"""
    root = Path(directory)
    if not root.is_dir():
        raise DeclarationError(f"声明目录不存在：{root}")
    declarations: list[Declaration] = []
    seen: dict[str, str] = {}
    for path in sorted(root.glob("*.toml")):
        declaration = load_declaration(path)
        if declaration.record_type in seen:
            raise DeclarationError(
                f"record_type 重复：{declaration.record_type}（{seen[declaration.record_type]} 与 {path}）"
            )
        seen[declaration.record_type] = str(path)
        declarations.append(declaration)
    if not declarations:
        raise DeclarationError(f"声明目录内没有 TOML 声明：{root}")
    return tuple(declarations)
