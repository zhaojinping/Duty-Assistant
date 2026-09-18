"""告警判定（design.md §6.2）。

核心不记忆告警：事实全在输入的 ``alarm_history`` 里。

判定顺序与计数语义（§6.2）：``occur_count`` 是该指纹**此前**的出现次数（不含本次）；
``occur_count + 1 ≥ escalate_after`` → ``escalated``；``occur_count ≥ 1`` → ``recurred``；否则 ``new``。
抑制：``recurred → suppressed=true``；``new / escalated → suppressed=false``（升级走独立通道）。
``alarm_history`` 缺失时按首次出现（``new``）并在 evidence 注明。
"""

from __future__ import annotations

import hashlib

from records_kit.errors import E_ACTION_CODE, reject
from records_kit.engine.rules import RulesReport
from records_kit.engine.views import alarm_row_for
from records_kit.util import canonical_json


def fingerprint(station_id: str, record_type: str, rule_id: str, label: str) -> str:
    """告警指纹：``fp:`` + sha256(站/类型/规则/命中对象) 前 32 位。

    规则：一条记录只产生一个指纹——取声明顺序中**首个** alarm 级违规规则与其中
    首个命中对象（§6.2 的 ``alarm_state`` 是单对象，不是数组）。
    """
    body = canonical_json(
        {"station_id": station_id, "record_type": record_type, "rule_id": rule_id, "target": label}
    )
    return "fp:" + hashlib.sha256(body.encode("utf-8")).hexdigest()[:32]


def evaluate_alarm(
    declaration,
    station_id: str,
    record_type: str,
    report: RulesReport,
    envelope: dict,
) -> dict:
    """由 ``rules`` 的告警候选 + ``alarm_history`` 视图给出 ``alarm_state``。"""
    if not report.alarm_candidates:
        return {
            "fingerprint": None,
            "state": None,
            "suppressed": False,
            "evidence": "本记录无 alarm 级违规，不产生告警指纹",
        }
    rule_id, label = report.alarm_candidates[0]
    known_rule_ids = {entry["rule_id"] for entry in report.entries}
    if rule_id not in known_rule_ids:  # pragma: no cover - 防御式
        raise reject("rules", E_ACTION_CODE, "告警候选必须来自已判定的规则")

    mark = fingerprint(station_id, record_type, rule_id, label)
    history_present = envelope.get("alarm_history") is not None
    row = alarm_row_for(envelope, mark) if history_present else None
    occur_count = int(row.get("occur_count", 0)) if row else 0
    escalate_after = declaration.escalate_after

    if occur_count + 1 >= escalate_after:
        state = "escalated"
    elif occur_count >= 1:
        state = "recurred"
    else:
        state = "new"
    suppressed = state == "recurred"

    evidence = f"命中规则 {rule_id}（{label}）；occur_count={occur_count}，escalate_after={escalate_after}"
    if not history_present:
        evidence += "；未传 alarm_history，按首次出现判定（§6.2）"
    return {"fingerprint": mark, "state": state, "suppressed": suppressed, "evidence": evidence}
