"""records_kit —— Duty-Assistant 核心：纯函数记录引擎。

设计依据 docs/design.md（§4 架构）：
- 核心不做 I/O、不读时钟、无持久状态（铁律 1/2/3）；
- 对外统一入口 ``process(envelope, registry=None) -> dict``，输入/输出协议见 §5/§6；
- 一切判定依赖的事实由调用方传入：``now`` / ``history`` / ``ledger_view`` /
  ``alarm_history`` / ``baselines``（铁律 3）。

M1 交付：协议 Schema（手写权威）+ TOML registry（含蓄电池声明）+ 全链
``create→confirm→return→correct→void→archive→alarm_ack`` + T1/T2 规则 +
趋势 + 周期探针 + 告警判定 + 黄金样本 + payload Schema 生成器。
"""

from __future__ import annotations

from records_kit.engine import (
    alarm as alarm_engine,
    lifecycle as lifecycle_engine,
    probe_cycle,
    rules as rules_engine,
    trend as trend_engine,
    validate as validate_engine,
    views,
)
from records_kit.errors import (
    E_PROTOCOL,
    E_RECORD_TYPE,
    E_REQUIRED,
    PROTOCOL,
    PROTOCOL_VERSION,
    Issue,
    Rejected,
    reject,
)
from records_kit.protocol import VALID_OPERATIONS, RecordEnvelope, RecordResult
from records_kit.protocol.envelope import check_envelope
from records_kit.registry import Registry, default_registry

__all__ = [
    "RecordEnvelope",
    "RecordResult",
    "Registry",
    "PROTOCOL",
    "PROTOCOL_VERSION",
    "default_registry",
    "process",
]

EMPTY_SECTIONS = {
    "record": None,
    "rules": [],
    "trend": [],
    "actions_hint": [],
    "alarm_state": None,
    "cycle": None,
    "digest": None,
}
# 不改 fields / 判定已由处置回流承担的操作用户不需要规则与告警判定
NO_JUDGEMENT_OPERATIONS = ("alarm_ack",)


def process(envelope: dict, registry: Registry | None = None) -> dict:
    """统一入口：校验、装配、规则/趋势/周期/告警判定与生命周期操作。

    Args:
        envelope: ``RecordEnvelope`` 形状的输入协议（design.md §5）。
        registry: 声明注册表；缺省用随包声明（``default_registry()``，可注入合成声明做测试）。

    Returns:
        ``RecordResult`` 形状的输出协议（design.md §6）；校验不过时 ``status="rejected"``，
        错误结构化在 ``validation.errors``，壳层无需 try/except。
    """
    scope = _scope(envelope)
    try:
        check_envelope(envelope)
    except Rejected as exc:
        return _rejected(scope, exc.issues)
    try:
        return _dispatch(envelope, registry or default_registry(), scope)
    except Rejected as exc:
        return _rejected(scope, exc.issues)


def _scope(envelope: object) -> dict:
    """结果协议的回显骨架：非法 operation 归一为 None（错值留在 validation.errors）。"""
    source = envelope if isinstance(envelope, dict) else {}
    operation = source.get("operation")
    record_type = source.get("record_type")
    return {
        "operation": operation if operation in VALID_OPERATIONS else None,
        "record_type": record_type if isinstance(record_type, str) else None,
    }


def _rejected(scope: dict, issues: list[Issue]) -> dict:
    result = {
        "protocol": PROTOCOL,
        "protocol_version": PROTOCOL_VERSION,
        "operation": scope.get("operation"),
        "record_type": scope.get("record_type"),
        "status": "rejected",
        "validation": {"ok": False, "errors": [issue.as_dict() for issue in issues]},
    }
    result.update(EMPTY_SECTIONS)
    return result


def _resolved(scope: dict, operation: str, record_type) -> dict:
    return {"operation": operation, "record_type": record_type}


def _dispatch(envelope: dict, registry: Registry, scope: dict) -> dict:
    operation = envelope["operation"]
    if operation == "cycle_probe":
        return _cycle_probe(envelope, registry, scope)

    record_type = envelope.get("record_type")
    if not isinstance(record_type, str) or not record_type:
        raise reject("record_type", E_REQUIRED, "该操作必须指定 record_type")
    declaration = registry.get(record_type)
    if declaration is None:
        raise reject("record_type", E_RECORD_TYPE, f"未知记录类型：{record_type}")
    _check_schema_version(envelope, declaration)
    scope = _resolved(scope, operation, record_type)

    ledger_view = views.ledger(envelope, declaration, required=operation != "create")
    history_rows = views.history(envelope, declaration) if envelope.get("history") is not None else []
    baseline_rows = views.baselines(envelope)
    views.alarm_history(envelope)

    missing_attachments: list[dict] = []
    if operation in ("create", "correct"):
        payload = validate_engine.validate_payload(declaration, envelope["payload"])
        missing_attachments = validate_engine.missing_attachments(declaration, payload, envelope)
        if missing_attachments and declaration.attachment_missing == "reject":
            raise validate_engine.attachment_issue(missing_attachments)
        links = validate_engine.validate_links(declaration, envelope, ledger_view["linked_records"])
    else:
        links = []

    life = lifecycle_engine.run(operation, envelope, declaration, ledger_view)

    if links:
        life.record["links"] = list(life.record.get("links") or []) + links

    if operation in NO_JUDGEMENT_OPERATIONS:
        return _ok(scope, life.record, [], [], [], None)

    report = rules_engine.evaluate_rules(
        declaration,
        life.fields,
        ledger_view=ledger_view,
        baselines=baseline_rows,
        occurred_at=life.occurred_at,
        # correct 场景账本视图里有同 record_uid 的旧版行：供 monotonic 排除自身 correct 链
        current_record_uid=life.record.get("record_uid"),
    )
    trend_entries = trend_engine.evaluate_trend(declaration, life.fields, history_rows)
    alarm_state = alarm_engine.evaluate_alarm(
        declaration, envelope["station"]["station_id"], declaration.record_type, report, envelope
    )
    rules_entries = list(report.entries)
    if missing_attachments:
        # attachment_missing="warn"：不拒单，在 rules 末尾追加一条 warn 级提醒（不进告警候选）
        rules_entries.extend(validate_engine.attachment_warnings(missing_attachments))
    return _ok(scope, life.record, rules_entries, trend_entries, report.actions, alarm_state)


def _cycle_probe(envelope: dict, registry: Registry, scope: dict) -> dict:
    record_type = envelope.get("record_type")
    if record_type is not None:
        if record_type not in registry:
            raise reject("record_type", E_RECORD_TYPE, f"未知记录类型：{record_type}")
        names = [record_type]
    else:
        names = list(registry.record_types)
    ledger_view = views.ledger(envelope, None, required=True)
    cycle = probe_cycle(registry, ledger_view, envelope["now"], names)
    scope = _resolved(scope, "cycle_probe", record_type)
    result = _ok(scope, None, [], [], [], None)
    result["cycle"] = cycle
    return result


def _check_schema_version(envelope: dict, declaration) -> None:
    """主版本一致（§5/§7.1）：不按月号细分，主版本不同即拒。"""
    given = str(envelope.get("protocol_version", "")).split(".", 1)[0]
    declared = str(declaration.schema_version).split(".", 1)[0]
    if given and declared and given != declared:
        raise reject(
            "protocol_version",
            E_PROTOCOL,
            f"协议主版本与声明不符：信封 {envelope.get('protocol_version')}，声明 {declaration.schema_version}",
        )


def _ok(scope: dict, record: dict | None, rules_entries: list, trend_entries: list, actions: list, alarm_state) -> dict:
    return {
        "protocol": PROTOCOL,
        "protocol_version": PROTOCOL_VERSION,
        "operation": scope.get("operation"),
        "record_type": scope.get("record_type"),
        "status": "ok",
        "validation": {"ok": True, "errors": []},
        "record": record,
        "rules": rules_entries,
        "trend": trend_entries,
        "actions_hint": actions,
        "alarm_state": alarm_state,
        "cycle": None,
        "digest": record["digest"] if record else None,
    }
