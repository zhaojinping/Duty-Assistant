"""registry —— 声明式记录注册表（design.md §7）。

10 类记录 = 10 份 TOML 声明；**registry 声明是记录 payload 的唯一权威**。
M1 交付：TOML 格式 + loader（含 meta-test 自检）+ 蓄电池 1 类声明。
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping

from records_kit.registry.declaration import (  # noqa: F401  (对外再导出)
    Declaration,
    DeclarationError,
    FieldSpec,
    ItemSpec,
    RuleSpec,
    TrendSpec,
    resolve_path,
)
from records_kit.registry.loader import declarations_dir, load, load_declaration, load_dir

__all__ = [
    "Registry",
    "Declaration",
    "DeclarationError",
    "FieldSpec",
    "ItemSpec",
    "RuleSpec",
    "TrendSpec",
    "default_registry",
    "load",
    "load_declaration",
    "load_dir",
    "declarations_dir",
    "resolve_path",
]


class Registry(Mapping):
    """不可变注册表：按 ``record_type`` 取声明（纯数据，可注入测试声明）。"""

    def __init__(self, declarations: Mapping[str, Declaration] | list[Declaration] | tuple[Declaration, ...]):
        if isinstance(declarations, Mapping):
            items = dict(declarations)
        else:
            rows = list(declarations)
            names = [declaration.record_type for declaration in rows]
            duplicates = sorted({name for name in names if names.count(name) > 1})
            if duplicates:
                raise DeclarationError("注册表内 record_type 重复：" + "、".join(duplicates))
            items = {declaration.record_type: declaration for declaration in rows}
        for name, declaration in items.items():
            if name != declaration.record_type:
                raise DeclarationError(f"注册表键与声明不符：{name} != {declaration.record_type}")
        self._items = items

    def __getitem__(self, key: str) -> Declaration:
        return self._items[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    @property
    def record_types(self) -> tuple[str, ...]:
        return tuple(self._items)

    @property
    def titles(self) -> dict[str, str]:
        return {name: declaration.title for name, declaration in self._items.items()}

    @classmethod
    def from_dir(cls, directory: str | None = None) -> "Registry":
        return cls(load_dir(directory if directory is not None else declarations_dir()))

    @classmethod
    def from_files(cls, paths: list[str]) -> "Registry":
        return cls([load_declaration(path) for path in paths])


_default: Registry | None = None


def default_registry() -> Registry:
    """随包声明构成的默认注册表（进程内缓存，只读）。"""
    global _default
    if _default is None:
        _default = Registry.from_dir()
    return _default
