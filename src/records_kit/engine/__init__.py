"""engine —— 记录引擎流水线（design.md §4）。

```
validate → assemble → rules(T1/T2) → rules(T3,需ledger_view/baselines)
→ trend → lifecycle(操作+乐观锁) → cycle_status
```

本包为纯函数实现：不读时钟、不做 I/O、无持久状态（铁律 1/2/3）。
"""

from __future__ import annotations

from records_kit.engine.alarm import evaluate_alarm, fingerprint
from records_kit.engine.cycle import probe_cycle
from records_kit.engine.lifecycle import LifecycleResult, run as run_lifecycle
from records_kit.engine.rules import RulesReport, evaluate_rules, when_matches
from records_kit.engine.trend import evaluate_trend
from records_kit.engine.validate import (
    attachment_warnings,
    missing_attachments,
    require_attachments,
    validate_links,
    validate_payload,
)
from records_kit.engine.views import alarm_history, baselines, history, ledger

__all__ = [
    "LifecycleResult",
    "RulesReport",
    "alarm_history",
    "attachment_warnings",
    "baselines",
    "evaluate_alarm",
    "evaluate_rules",
    "evaluate_trend",
    "fingerprint",
    "history",
    "ledger",
    "missing_attachments",
    "probe_cycle",
    "require_attachments",
    "run_lifecycle",
    "validate_links",
    "validate_payload",
    "when_matches",
]
