"""服务侧时钟：核心不读时钟（铁律 1），``now`` 统一由此生成后注入信封。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))  # 会话时区（中国标准时间）


def iso_now() -> str:
    """当前时刻（RFC3339、秒级、+08:00）。"""
    return datetime.now(CST).replace(microsecond=0).isoformat()
