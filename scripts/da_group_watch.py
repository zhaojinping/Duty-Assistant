"""每 5 分钟拉一次录入和群消息。计划任务调用本文件或 python -m da_core.cli watch。"""

from __future__ import annotations

from da_core.cli import main

if __name__ == "__main__":
    raise SystemExit(main(["watch"]))
