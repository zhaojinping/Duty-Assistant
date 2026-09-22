"""da_core 部署配置。

P1 语义：代码内默认值 + 调用方覆盖（CLI 参数 / 环境变量 ``DA_DATA_DIR``）。
阈值、组别映射与表格落点会在首次连接账本时作为**种子**写入 SQLite；
此后账本为运行期权威（种子只补缺、不覆盖）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BATTERY_TYPE = "battery_voltage_test"

# 阈值种子（2026-09-21 拍板默认）：scope → (lo, hi)
DEFAULT_THRESHOLDS: dict[str, tuple[float, float]] = {
    "2V单体": (1.85, 2.35),
    "12V电池": (11.85, 13.80),
}

# 电池组别 → 口径 scope（与录入端 4 组对应）
DEFAULT_GROUP_KINDS: dict[str, str] = {
    "1号组(104只)": "2V单体",
    "2号组(104只)": "2V单体",
    "3号组(12只)": "12V电池",
    "4号组(12只)": "12V电池",
}

# 本部署的输出落点：钉钉 AI 表格《蓄电池电压测量记录》（单一写者 = 核心系统）。
DEFAULT_TABLE: dict = {
    "base_id": "np9zOoBVBYALR6aeuenZZglmW1DK0g6l",
    "table_id": "dqUu9rJ",
    "field_ids": {
        "电池组别": "fUJt4cj",
        "电池序号": "KuuIakz",
        "电压值(V)": "o6YY2SZ",
        "环境温度(℃)": "CnsSA5F",
        "直流系统编号": "ISxgXeE",
        "浮充电压(V)": "sWY1QSY",
        "测试性质": "vzbk3nl",
        "是否异常": "6fOsduP",
        "判定说明": "ObdNEgb",
        "备注": "jp0iVgn",
        "账本UID": "ReX3ynj",
        "账本Rev": "FBj3fm5",
        "账本状态": "4cGXQiF",
    },
}


@dataclass(frozen=True)
class Settings:
    """一次部署的配置快照。``station=None`` 表示未配置（接入口将拒绝提交）。"""

    db_path: Path
    station: dict | None = None
    thresholds: dict = field(default_factory=lambda: dict(DEFAULT_THRESHOLDS))
    group_kinds: dict = field(default_factory=lambda: dict(DEFAULT_GROUP_KINDS))
    table: dict = field(
        default_factory=lambda: {
            **DEFAULT_TABLE,
            "field_ids": dict(DEFAULT_TABLE["field_ids"]),
        }
    )
    # 用户安装后才有值。None 表示开发/测试路径，不把空字符串当成「已配置」。
    group_name: str | None = None
    group_cid: str | None = None
    entry_url: str | None = None
    ledger_url: str | None = None

    @classmethod
    def default(
        cls,
        *,
        db_path: str | Path | None = None,
        station: dict | None = None,
        data_dir: str | Path | None = None,
        thresholds: dict | None = None,
        group_kinds: dict | None = None,
        table: dict | None = None,
        group_name: str | None = None,
        group_cid: str | None = None,
        entry_url: str | None = None,
        ledger_url: str | None = None,
    ) -> "Settings":
        if db_path is None:
            base = Path(data_dir) if data_dir else Path(os.environ.get("DA_DATA_DIR", "data"))
            db_path = base / "ledger.sqlite"
        return cls(
            db_path=Path(db_path),
            station=station,
            thresholds=dict(thresholds) if thresholds else dict(DEFAULT_THRESHOLDS),
            group_kinds=dict(group_kinds) if group_kinds else dict(DEFAULT_GROUP_KINDS),
            table=table if table else {**DEFAULT_TABLE, "field_ids": dict(DEFAULT_TABLE["field_ids"])},
            group_name=group_name,
            group_cid=group_cid,
            entry_url=entry_url,
            ledger_url=ledger_url,
        )
