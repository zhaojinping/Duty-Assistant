"""编排层：一次提交的完整旅程（组装 → 判定 → 落账 → 自动定稿 → 可选分发）。

自动定稿（免签落地）：create 落账后立即以零签认 confirm 定稿；
定稿失败不重放、写审计待对账（后续 reconcile 补，不静默）。
"""

from __future__ import annotations

import records_kit

from da_core import intake
from da_core.clock import iso_now
from da_core.engine_gate import registry_for_band
from da_core.ledger import Ledger
from da_core.settings import BATTERY_TYPE, Settings


def submit_submission(data: dict, *, settings: Settings, ledger: Ledger | None = None,
                      dispatch: bool = False, dry_run_dispatch: bool = False,
                      remark_tag: str | None = None) -> dict:
    """提交一批测量数据；返回按组的结果汇总（含 rejected 结构化错误）。"""
    ledger = ledger or Ledger(settings.db_path)
    ledger.seed_config(settings)

    submission_id = data.get("client_submission_id")
    if submission_id:
        cached = ledger.get_receipt(submission_id)
        if cached is not None:
            replayed = dict(cached)
            replayed["replayed"] = True
            return replayed

    station = settings.station
    if not isinstance(station, dict) or not station.get("station_id"):
        raise intake.IntakeError("部署未配置 station（settings.station），拒绝提交")
    if data.get("station") and data["station"] != station.get("station_id"):
        raise intake.IntakeError(
            f"提交站点 {data['station']!r} 与部署配置 {station.get('station_id')!r} 不一致")

    jobs = intake.parse_submission(data, settings=settings, ledger=ledger)
    thresholds = ledger.get_thresholds(BATTERY_TYPE)
    station_id = station["station_id"]

    group_results: list[dict] = []
    for job in jobs:
        envelope = job["envelope"]
        scope = job["scope"]
        if scope not in thresholds:
            group_results.append({
                "group": job["group"],
                "status": "rejected",
                "validation": {"errors": [{"code": "E_CONFIG", "path": f"thresholds.{scope}",
                                           "detail": "账本缺少该口径的阈值配置"}]},
            })
            continue

        lo, hi = thresholds[scope]
        registry = registry_for_band(lo, hi)
        # 判重/趋势依赖落账前的完整视图（含墓碑）：创建前注入
        envelope["ledger_view"] = ledger.build_ledger_view(station_id, BATTERY_TYPE)
        created = records_kit.process(envelope, registry)
        if created["status"] != "ok":
            group_results.append({
                "group": job["group"],
                "status": "rejected",
                "validation": created["validation"],
            })
            continue

        ledger.save_create(envelope, created, actor=envelope.get("submitted_by", ""),
                           group_label=job["group"], scope=scope)
        record = created["record"]

        confirm_envelope = {
            "protocol": "records-kit",
            "protocol_version": "1.5",
            "operation": "confirm",
            "record_type": BATTERY_TYPE,
            "station": dict(station),
            "now": iso_now(),
            "subject": {"record_uid": record["record_uid"],
                        "lifecycle": record["lifecycle"], "rev": record["rev"]},
            "confirmations": [],
            "ledger_view": ledger.build_ledger_view(station_id, BATTERY_TYPE),
        }
        confirmed = records_kit.process(confirm_envelope, registry)
        if confirmed["status"] == "ok":
            ledger.save_confirm(confirm_envelope, confirmed, actor="auto-confirm")
            lifecycle = confirmed["record"]["lifecycle"]
        else:
            ledger.audit("core", "auto_confirm_failed", record["record_uid"],
                         confirmed["validation"])
            lifecycle = record["lifecycle"]

        ledger.update_alarm(station_id, BATTERY_TYPE, created.get("alarm_state"),
                            envelope["occurred_at"])

        group_results.append({
            "group": job["group"],
            "scope": scope,
            "status": "ok",
            "record_uid": record["record_uid"],
            "rev": record["rev"],
            "lifecycle": lifecycle,
            "rules": created.get("rules") or [],
            "trend": created.get("trend") or [],
            "alarm_state": created.get("alarm_state"),
            "actions_hint": created.get("actions_hint") or [],
            "_payload": envelope["payload"],
            "_record": {"record_uid": record["record_uid"], "rev": record["rev"],
                        "lifecycle": lifecycle},
        })

    ok = bool(group_results) and all(one["status"] == "ok" for one in group_results)
    summary = {
        "ok": ok,
        "submitted_at": data.get("submitted_at") or iso_now(),
        "groups": group_results,
        "counts": {
            "groups": len(group_results),
            "ok": sum(1 for one in group_results if one["status"] == "ok"),
            "cells": sum(len(one.get("_payload", {}).get("items", []))
                         for one in group_results if one["status"] == "ok"),
        },
    }

    if dispatch:
        from da_core.table_projection import dispatch as dispatch_rows
        summary["dispatch"] = dispatch_rows(group_results, settings=settings, ledger=ledger,
                                            dry_run=dry_run_dispatch, remark_tag=remark_tag)

    if submission_id:
        ledger.put_receipt(submission_id, station_id, summary["submitted_at"], summary)
    return summary


def _require_station(settings: Settings) -> dict:
    station = settings.station
    if not isinstance(station, dict) or not station.get("station_id"):
        raise intake.IntakeError("部署未配置 station（settings.station），拒绝操作")
    return station


def correct_submission(payload_group: dict, *, record_uid: str, actor: str,
                       settings: Settings, ledger: Ledger | None = None) -> dict:
    """定稿更正：payload 全量替换 → 新版 rev+1（draft）→ 免签自动重新定稿。"""
    ledger = ledger or Ledger(settings.db_path)
    station = _require_station(settings)
    target = ledger.get_record(record_uid)
    if target is None:
        raise intake.IntakeError(f"账本中无记录：{record_uid}")
    if target["station_id"] != station["station_id"]:
        raise intake.IntakeError(
            f"记录站点 {target['station_id']!r} 与部署配置 {station['station_id']!r} 不一致")
    scope = target.get("scope")
    thresholds = ledger.get_thresholds(BATTERY_TYPE)
    if scope not in thresholds:
        raise intake.IntakeError(
            f"记录口径未定（scope={scope!r}），无法更正；请先补全记录口径")

    registry = registry_for_band(*thresholds[scope])
    envelope = {
        "protocol": "records-kit",
        "protocol_version": "1.5",
        "operation": "correct",
        "record_type": BATTERY_TYPE,
        "station": dict(station),
        "occurred_at": target["occurred_at"],
        "now": iso_now(),
        "submitted_by": actor,
        "subject": {"record_uid": record_uid, "lifecycle": target["lifecycle"],
                    "rev": target["rev"]},
        "payload": intake.normalize_payload(payload_group),
        "ledger_view": ledger.build_ledger_view(station["station_id"], BATTERY_TYPE),
    }
    corrected = records_kit.process(envelope, registry)
    if corrected["status"] != "ok":
        return {"status": "rejected", "validation": corrected["validation"]}
    ledger.save_correct(envelope, corrected, actor=actor)

    record = corrected["record"]
    confirm_envelope = {
        "protocol": "records-kit",
        "protocol_version": "1.5",
        "operation": "confirm",
        "record_type": BATTERY_TYPE,
        "station": dict(station),
        "now": iso_now(),
        "subject": {"record_uid": record_uid, "lifecycle": record["lifecycle"],
                    "rev": record["rev"]},
        "confirmations": [],
        "ledger_view": ledger.build_ledger_view(station["station_id"], BATTERY_TYPE),
    }
    confirmed = records_kit.process(confirm_envelope, registry)
    lifecycle = record["lifecycle"]
    if confirmed["status"] == "ok":
        ledger.save_confirm(confirm_envelope, confirmed, actor="auto-confirm")
        lifecycle = confirmed["record"]["lifecycle"]
    else:
        ledger.audit("core", "auto_confirm_failed", record_uid, confirmed["validation"])
    ledger.update_alarm(station["station_id"], BATTERY_TYPE, corrected.get("alarm_state"),
                        envelope["occurred_at"])
    return {"status": "ok", "record_uid": record_uid, "rev": record["rev"],
            "lifecycle": lifecycle, "rules": corrected.get("rules") or [],
            "links": record.get("links") or []}


def void_record(record_uid: str, *, reason: str, actor: str,
                settings: Settings, ledger: Ledger | None = None) -> dict:
    """作废：墓碑占号（rev/内容不变），判重键释放。"""
    ledger = ledger or Ledger(settings.db_path)
    station = _require_station(settings)
    target = ledger.get_record(record_uid)
    if target is None:
        raise intake.IntakeError(f"账本中无记录：{record_uid}")
    if target["station_id"] != station["station_id"]:
        raise intake.IntakeError(
            f"记录站点 {target['station_id']!r} 与部署配置 {station['station_id']!r} 不一致")
    if not reason:
        raise intake.IntakeError("作废必须给原因（reason）")
    scope = target.get("scope")
    thresholds = ledger.get_thresholds(BATTERY_TYPE)
    if scope not in thresholds:
        raise intake.IntakeError(
            f"记录口径未定（scope={scope!r}），无法作废")

    registry = registry_for_band(*thresholds[scope])
    envelope = {
        "protocol": "records-kit",
        "protocol_version": "1.5",
        "operation": "void",
        "record_type": BATTERY_TYPE,
        "station": dict(station),
        "now": iso_now(),
        "void_reason": reason,
        "voided_by": actor,
        "subject": {"record_uid": record_uid, "lifecycle": target["lifecycle"],
                    "rev": target["rev"]},
        "ledger_view": ledger.build_ledger_view(station["station_id"], BATTERY_TYPE),
    }
    voided = records_kit.process(envelope, registry)
    if voided["status"] != "ok":
        return {"status": "rejected", "validation": voided["validation"]}
    ledger.save_void(envelope, voided, actor=actor)
    return {"status": "ok", "record_uid": record_uid,
            "rev": voided["record"]["rev"],
            "lifecycle": voided["record"]["lifecycle"]}
