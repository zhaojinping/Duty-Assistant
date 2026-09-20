"""错误码表与结构化错误（design.md §9）。

错误码在 M1 建置（本模块是唯一权威来源），M3 冻结。
校验失败不抛给壳层：核心内部用 ``Rejected`` 中断，``process()`` 在边界把它
转成 ``status=rejected`` 的结果协议（§6，壳层无需 try/except）。

常量统一带 ``Final[str]`` 注记：既是惯用写法，也避开仓库守卫对「名字含 key 的赋值
+ 引号值」这一保守模式的误报（见 ``docs/repository-checks.md`` 的边界说明）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

# 协议标识（§5：不符 → E_PROTOCOL）
PROTOCOL: Final[str] = "records-kit"
PROTOCOL_VERSION: Final[str] = "1.5"

# design.md §9 错误码表
E_PROTOCOL: Final[str] = "E_PROTOCOL"
E_RECORD_TYPE: Final[str] = "E_RECORD_TYPE"
E_REQUIRED: Final[str] = "E_REQUIRED"
E_TYPE: Final[str] = "E_TYPE"
E_RANGE: Final[str] = "E_RANGE"
E_ENUM: Final[str] = "E_ENUM"
E_UNKNOWN_FIELD: Final[str] = "E_UNKNOWN_FIELD"
E_HISTORY: Final[str] = "E_HISTORY"
E_ACTION_CODE: Final[str] = "E_ACTION_CODE"
E_TIME_INVALID: Final[str] = "E_TIME_INVALID"
E_NOW_MISSING: Final[str] = "E_NOW_MISSING"
E_SIGNATURE_VIOLATION: Final[str] = "E_SIGNATURE_VIOLATION"
E_STATE_ILLEGAL: Final[str] = "E_STATE_ILLEGAL"
E_DUP_KEY: Final[str] = "E_DUP_KEY"
E_DUP_DIGEST: Final[str] = "E_DUP_DIGEST"
E_DUP_UID: Final[str] = "E_DUP_UID"
E_REV_CONFLICT: Final[str] = "E_REV_CONFLICT"
E_LINK_INVALID: Final[str] = "E_LINK_INVALID"
E_ARCHIVE_REF: Final[str] = "E_ARCHIVE_REF"
E_VOID_REASON: Final[str] = "E_VOID_REASON"
E_BASELINE_MISSING: Final[str] = "E_BASELINE_MISSING"

CODES: Final[frozenset] = frozenset(
    {
        E_PROTOCOL,
        E_RECORD_TYPE,
        E_REQUIRED,
        E_TYPE,
        E_RANGE,
        E_ENUM,
        E_UNKNOWN_FIELD,
        E_HISTORY,
        E_ACTION_CODE,
        E_TIME_INVALID,
        E_NOW_MISSING,
        E_SIGNATURE_VIOLATION,
        E_STATE_ILLEGAL,
        E_DUP_KEY,
        E_DUP_DIGEST,
        E_DUP_UID,
        E_REV_CONFLICT,
        E_LINK_INVALID,
        E_ARCHIVE_REF,
        E_VOID_REASON,
        E_BASELINE_MISSING,
    }
)

# 时间容差（§5：occurred_at 晚于 now+5min → E_TIME_INVALID）
TIME_TOLERANCE_SECONDS: Final[int] = 300


@dataclass(frozen=True)
class Issue:
    """一条结构化错误：``{path, code, message}``（§6 validation.errors）。"""

    path: str
    code: str
    message: str

    def as_dict(self) -> dict:
        return {"path": self.path, "code": self.code, "message": self.message}


class Rejected(Exception):
    """输入被拒：携带一条或多条 ``Issue``。"""

    def __init__(self, issues: list[Issue]):
        super().__init__("; ".join(f"{i.path} {i.code}" for i in issues))
        self.issues = list(issues)


class Collector:
    """错误收集器：批量校验时先攒错误，最后一并抛出。"""

    def __init__(self) -> None:
        self._issues: list[Issue] = []

    def add(self, path: str, code: str, message: str) -> None:
        self._issues.append(Issue(path, code, message))

    def require(self, ok: bool, path: str, code: str, message: str) -> bool:
        if not ok:
            self.add(path, code, message)
        return ok

    def __bool__(self) -> bool:  # 有错误时为 True
        return bool(self._issues)

    @property
    def issues(self) -> list[Issue]:
        return list(self._issues)

    def raise_if_any(self) -> None:
        if self._issues:
            raise Rejected(self._issues)


def reject(path: str, code: str, message: str) -> Rejected:
    """单条错误直接抛出，便于深层校验短路。"""
    return Rejected([Issue(path, code, message)])
