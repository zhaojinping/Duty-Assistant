"""da_core —— Duty-Assistant 核心系统（账本 + 编排 + 适配）。

边界（改造方案 §2）：
- ``records_kit`` 是纯引擎（零 IO、零时钟、零状态）；本包是它的第一个正式壳层：
  接入口组装信封 → 引擎判定 → SQLite 账本落账 → 自动定稿 → 分发（输出投影）。
- 依赖方向单向：本包可 import ``records_kit``；反向禁止（CI 检查）。
"""

from da_core.ledger import Ledger
from da_core.service import submit_submission
from da_core.settings import Settings

__all__ = ["Ledger", "Settings", "submit_submission"]
