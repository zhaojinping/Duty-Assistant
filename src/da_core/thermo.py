"""设备测温壳层：群文本 / 页面提交 → 信封 → 免签落账 → 表投影与缺陷触达。

分级由引擎 ``thermal_grade`` 计算。人工判级只核对，不改判定。
缺红外图不拒单，回执里提醒。
"""

from __future__ import annotations

import re

import records_kit

from da_core.clock import iso_now
from da_core.engine_gate import registry_for_thermo
from da_core.intake import IntakeError
from da_core.ledger import Ledger
from da_core.settings import THERMO_GROUP, THERMO_TYPE, Settings
from records_kit.util import wall_stamp

HEADER = "设备测温数据"
ROLE_ASSIGNEE = "thermo_assignee"

_GRADE_ORDER = ("正常", "一般缺陷", "严重缺陷", "危急缺陷")
_GRADE_SHORT = {"一般缺陷": "一般", "严重缺陷": "严重", "危急缺陷": "危急"}
_GRADE_ALIAS = {
    "正常": "正常",
    "一般": "一般缺陷",
    "一般缺陷": "一般缺陷",
    "严重": "严重缺陷",
    "严重缺陷": "严重缺陷",
    "危急": "危急缺陷",
    "危急缺陷": "危急缺陷",
}
_HEAT_TYPES = ("电流致热", "电压致热", "设备致热", "环境致热")
_TEST_KINDS = ("例行", "过负荷加强", "高温天气加强", "新投设备加强")
_SUBMIT_TIME_RE = re.compile(r"提交时间[:：]\s*(\d{4}-\d{2}-\d{2})[\sT]+(\d{2}:\d{2})")
_SPOT_RE = re.compile(r"\[测点\]\s*([^\n\r]+)")


def _clean(text: str) -> str:
    return (text or "").replace("**", "").replace("\u3000", " ")


def normalize_grade(text: str | None) -> str | None:
    if text is None:
        return None
    value = str(text).strip()
    if not value:
        return None
    mapped = _GRADE_ALIAS.get(value)
    if mapped is None:
        raise IntakeError(f"人工判级无法识别：{value}")
    return mapped


def normalize_heat(text: str | None) -> str:
    value = (text or "").strip()
    if not value:
        return "电流致热"
    if value not in _HEAT_TYPES:
        raise IntakeError(f"致热类型无法识别：{value}")
    return value


def grade_of(detail: str) -> str | None:
    """从引擎说明里取出短等级：危急 / 严重 / 一般 / 正常。"""
    for full, short in (
        ("危急缺陷", "危急"),
        ("严重缺陷", "严重"),
        ("一般缺陷", "一般"),
        ("正常", "正常"),
    ):
        if f"等级={full}" in (detail or ""):
            return short
    return None


def count_grades(rules: list[dict]) -> dict[str, int]:
    counts = {"严重": 0, "危急": 0, "一般": 0}
    for entry in rules or []:
        if entry.get("rule_id") != "thermal_grade" or entry.get("verdict") != "violation":
            continue
        short = grade_of(entry.get("detail") or "")
        if short in counts:
            counts[short] += 1
    return counts


def device_findings(rules: list[dict]) -> dict[str, dict]:
    """设备名 → 引擎对该组热点的说明。"""
    found: dict[str, dict] = {}
    for entry in rules or []:
        if entry.get("rule_id") != "thermal_grade":
            continue
        detail = entry.get("detail") or ""
        name = ""
        if detail.startswith("设备 ") and "：" in detail:
            name = detail.split("：", 1)[0].removeprefix("设备 ").strip()
        found[name] = {
            "grade": grade_of(detail),
            "detail": detail,
            "verdict": entry.get("verdict"),
        }
    return found


def _number(text: str, label: str):
    try:
        return float(text)
    except (TypeError, ValueError):
        raise IntakeError(f"{label}不是数字：{text}") from None


def parse_spot_line(line: str) -> dict:
    """``序号|设备|部位|温度|仪器[|相间温差|δt|判级|致热类型]``。"""
    parts = [part.strip() for part in line.split("|")]
    if len(parts) < 5 or any(not parts[index] for index in range(5)):
        raise IntakeError("测点需含序号、设备名称、测点部位、实测温度、仪器编号")
    spot = {
        "spot_no": int(_number(parts[0], "测点序号")),
        "device_name": parts[1],
        "spot": parts[2],
        "measured_temp": _number(parts[3], "实测温度"),
        "instrument_id": parts[4],
    }
    if len(parts) >= 6 and parts[5]:
        spot["phase_temp_diff"] = _number(parts[5], "相间温差")
    if len(parts) >= 7 and parts[6]:
        spot["delta_t"] = _number(parts[6], "δt")
    if len(parts) >= 8 and parts[7]:
        spot["defect_grade"] = normalize_grade(parts[7])
    if len(parts) >= 9 and parts[8]:
        spot["heat_type"] = normalize_heat(parts[8])
    return spot


def _labeled(text: str, label: str) -> str | None:
    match = re.search(rf"\[{label}\]\s*([^\s\[]+)", text)
    if not match or match.group(1) == "未填":
        return None
    return match.group(1)


def parse_thermo_message(text: str) -> dict:
    """解析群文本。返回测温事实，不含图片。"""
    cleaned = _clean(text)
    if HEADER not in cleaned:
        raise IntakeError("不是设备测温数据消息")
    submitted_at = None
    match = _SUBMIT_TIME_RE.search(cleaned)
    if match:
        submitted_at = f"{match.group(1)}T{match.group(2)}:00+08:00"

    test_kind = _labeled(cleaned, "测温性质") or "例行"
    if test_kind not in _TEST_KINDS:
        raise IntakeError(f"测温性质无法识别：{test_kind}")
    env_text = _labeled(cleaned, "环境温度")
    if env_text is None:
        raise IntakeError("缺少环境温度")
    env_temp = _number(env_text, "环境温度")
    load_text = _labeled(cleaned, "负荷电流")
    load_current = _number(load_text, "负荷电流") if load_text is not None else None

    spots = [parse_spot_line(line) for line in _SPOT_RE.findall(cleaned)]
    if not spots:
        raise IntakeError("消息里没有测点")
    _reject_duplicate_spots(spots)
    body = {
        "submitted_at": submitted_at,
        "test_kind": test_kind,
        "env_temp": env_temp,
        "spots": spots,
    }
    if load_current is not None:
        body["load_current"] = load_current
    return body


def _reject_duplicate_spots(spots: list[dict]) -> None:
    seen: set[int] = set()
    for spot in spots:
        number = int(spot["spot_no"])
        if number in seen:
            raise IntakeError(f"测点序号重复：{number}")
        seen.add(number)


def normalize_spots(raw_spots: list) -> list[dict]:
    """页面测点：中英文字段名都收，缺省致热类型为电流致热。"""
    if not isinstance(raw_spots, list) or not raw_spots:
        raise IntakeError("缺少测点")
    spots: list[dict] = []
    for index, raw in enumerate(raw_spots):
        if not isinstance(raw, dict):
            raise IntakeError(f"测点[{index}] 必须是对象")
        if "device_name" in raw or "measured_temp" in raw:
            spot = _spot_from_keys(raw, index)
        else:
            line = raw.get("line")
            if not isinstance(line, str):
                raise IntakeError(f"测点[{index}] 缺设备名称或实测温度")
            spot = parse_spot_line(line)
        spot["heat_type"] = normalize_heat(spot.get("heat_type"))
        if spot.get("defect_grade"):
            spot["defect_grade"] = normalize_grade(spot["defect_grade"])
        spots.append(spot)
    _reject_duplicate_spots(spots)
    return spots


def _spot_from_keys(raw: dict, index: int) -> dict:
    try:
        spot = {
            "spot_no": int(raw.get("spot_no") if raw.get("spot_no") is not None else index + 1),
            "device_name": str(raw["device_name"]).strip(),
            "spot": str(raw.get("spot") or raw.get("position") or "").strip(),
            "measured_temp": float(raw["measured_temp"]),
            "instrument_id": str(raw["instrument_id"]).strip(),
        }
    except (KeyError, TypeError, ValueError):
        raise IntakeError(
            f"测点[{index}] 需含设备名称、测点部位、实测温度、仪器编号"
        ) from None
    if not spot["device_name"] or not spot["spot"] or not spot["instrument_id"]:
        raise IntakeError(f"测点[{index}] 需含设备名称、测点部位、实测温度、仪器编号")
    if raw.get("phase_temp_diff") not in (None, ""):
        spot["phase_temp_diff"] = float(raw["phase_temp_diff"])
    if raw.get("delta_t") not in (None, ""):
        spot["delta_t"] = float(raw["delta_t"])
    if raw.get("defect_grade"):
        spot["defect_grade"] = raw["defect_grade"]
    if raw.get("heat_type"):
        spot["heat_type"] = raw["heat_type"]
    if raw.get("photo_ref"):
        spot["photo_ref"] = str(raw["photo_ref"])
    return spot


def bind_photos(spots: list[dict], refs: list[str] | None) -> list[dict]:
    """图片按测点序号 1、2、3… 绑定；页面已带 photo_ref 的测点不再占用群图片。"""
    by_no = {int(spot["spot_no"]): spot for spot in spots}
    attachments: list[dict] = []
    used: set[int] = set()
    for spot in spots:
        ref = spot.get("photo_ref")
        if ref:
            attachments.append({
                "kind": "photo", "ref": str(ref), "item_key": int(spot["spot_no"]),
            })
            used.add(int(spot["spot_no"]))
    pending = [ref for ref in (refs or []) if ref]
    for offset, ref in enumerate(pending, start=1):
        if offset in by_no and offset not in used:
            attachments.append({"kind": "photo", "ref": str(ref), "item_key": offset})
            used.add(offset)
            continue
        for spot in spots:
            number = int(spot["spot_no"])
            if number not in used:
                attachments.append({"kind": "photo", "ref": str(ref), "item_key": number})
                used.add(number)
                break
    return attachments


def is_thermo_submission(data: dict) -> bool:
    if not isinstance(data, dict):
        return False
    if data.get("record_type") in (THERMO_TYPE, "设备测温"):
        return True
    return "spots" in data and "groups" not in data


def _payload_from(body: dict) -> tuple[dict, list[dict]]:
    spots = normalize_spots(body.get("spots") or [])
    test_kind = body.get("test_kind") or "例行"
    if test_kind not in _TEST_KINDS:
        raise IntakeError(f"测温性质无法识别：{test_kind}")
    if body.get("env_temp") is None:
        raise IntakeError("缺少环境温度")
    payload = {"test_kind": test_kind, "env_temp": float(body["env_temp"]), "items": []}
    if body.get("load_current") is not None:
        payload["load_current"] = float(body["load_current"])
    for spot in spots:
        item = {
            "spot_no": int(spot["spot_no"]),
            "device_name": spot["device_name"],
            "spot": spot["spot"],
            "measured_temp": float(spot["measured_temp"]),
            "instrument_id": spot["instrument_id"],
            "heat_type": spot.get("heat_type") or "电流致热",
        }
        if spot.get("phase_temp_diff") is not None:
            item["phase_temp_diff"] = float(spot["phase_temp_diff"])
        if spot.get("delta_t") is not None:
            item["delta_t"] = float(spot["delta_t"])
        if spot.get("defect_grade"):
            item["defect_grade"] = spot["defect_grade"]
        payload["items"].append(item)
    attachments = bind_photos(spots, body.get("image_refs"))
    return payload, attachments


def receipt_text(summary: dict) -> str:
    """群回执。重放不发。表没写完不说已入库。"""
    if summary.get("replayed"):
        return ""
    if not summary.get("ok"):
        reasons = []
        for group in summary.get("groups") or []:
            errors = (group.get("validation") or {}).get("errors") or []
            codes = {error.get("code") for error in errors}
            if "E_DUP_KEY" in codes:
                reasons.append("同日同测温性质已有记录，无需重发")
            elif "E_REQUIRED" in codes:
                reasons.append("缺必填项（环境温度、设备、部位、温度、仪器）")
            else:
                first = (errors[:1] or [{}])[0]
                reasons.append(first.get("message") or first.get("detail") or first.get("code") or "校验未通过")
        if not reasons:
            reasons.append("未入库")
        return "⚠️ 未入库：" + "；".join(reasons) + "\n——AI助手"

    group = (summary.get("groups") or [{}])[0]
    payload = group.get("_payload") or {}
    count = len(payload.get("items") or [])
    grades = count_grades(group.get("rules") or [])
    photo_lines = [
        entry.get("detail") or "有测点缺红外图"
        for entry in (group.get("rules") or [])
        if entry.get("rule_id") == "attachment_photo"
    ]
    dispatch = summary.get("dispatch")
    if isinstance(dispatch, dict) and not dispatch.get("dry_run"):
        if dispatch.get("skipped"):
            lines = ["⚠️ 账本已记下，测温表还没建，先不要当作已写入钉钉表。"]
        elif dispatch.get("failures") or not dispatch.get("written"):
            lines = ["⚠️ 账本已记下，钉钉表没有写完，先不要当作已入库。"]
        else:
            lines = [
                f"✅ 已入库：设备测温 {count} 个测点"
                f"（严重 {grades['严重']} 危急 {grades['危急']} 一般 {grades['一般']}）"
            ]
    else:
        lines = [
            f"✅ 已入库：设备测温 {count} 个测点"
            f"（严重 {grades['严重']} 危急 {grades['危急']} 一般 {grades['一般']}）"
        ]
    lines.extend(photo_lines)
    lines.append("——AI助手")
    return "\n".join(lines)


def _assignee(contacts: dict) -> str | None:
    return contacts.get(ROLE_ASSIGNEE) or contacts.get("reminder_assignee")


def notify_defects(ledger, settings: Settings, group: dict, *, runner=None) -> dict:
    """严重及以上发待办；危急再私聊班长。一般只留在群回执。"""
    grades = count_grades(group.get("rules") or [])
    if grades["严重"] + grades["危急"] == 0:
        return {"todo": None, "dm": None}
    station_id = (settings.station or {}).get("station_id") or ""
    contacts = ledger.get_contacts(station_id) if station_id else {}
    uid = group.get("record_uid") or ""
    task_id = f"thermo-defect|{uid}"
    from da_core import outbox

    result: dict = {}
    assignee = _assignee(contacts)
    title = f"设备测温{'危急' if grades['危急'] else '严重'} {grades['严重'] + grades['危急']} 处"
    if assignee:
        result["todo"] = outbox.deliver(ledger, {
            "task_id": task_id, "level": 1, "channel": "todo",
            "target": assignee, "title": title,
        }, runner=runner)
    else:
        result["todo"] = {"sent": False, "reason": "no-target"}
    if grades["危急"]:
        escalate = contacts.get("reminder_escalate")
        if escalate:
            result["dm"] = outbox.deliver(ledger, {
                "task_id": task_id, "level": 1, "channel": "dm",
                "target": escalate,
                "text": f"【危急】设备测温有 {grades['危急']} 处危急缺陷（账本 {uid}），请跟进。\n——AI助手",
            }, runner=runner)
        else:
            result["dm"] = {"sent": False, "reason": "no-target"}
    return result


def submit_thermography(data: dict, *, settings: Settings, ledger: Ledger | None = None,
                        dispatch: bool = False, dry_run_dispatch: bool = False,
                        runner=None) -> dict:
    """一次测温一份记录。免签，提交后自动定稿。"""
    ledger = ledger or Ledger(settings.db_path)
    ledger.seed_config(settings)
    if not isinstance(data, dict):
        raise IntakeError("提交必须是 JSON 对象")
    submission_id = data.get("client_submission_id")
    if submission_id:
        cached = ledger.get_receipt(submission_id)
        if cached is not None:
            replayed = dict(cached)
            replayed["replayed"] = True
            return replayed

    station = settings.station
    if not isinstance(station, dict) or not station.get("station_id"):
        raise IntakeError("部署未配置 station，拒绝提交")
    if data.get("station") and data["station"] != station.get("station_id"):
        raise IntakeError(
            f"提交站点 {data['station']!r} 与部署配置 {station.get('station_id')!r} 不一致")
    operator = data.get("operator")
    if not operator:
        raise IntakeError("缺少 operator（提交人）")

    payload, attachments = _payload_from(data)
    occurred_at = data.get("measured_at") or data.get("submitted_at") or iso_now()
    station_id = station["station_id"]
    _day, _moment = wall_stamp(occurred_at)
    raw_seq = ledger.next_create_seq(station_id, THERMO_TYPE, occurred_at)
    envelope = {
        "protocol": "records-kit",
        "protocol_version": "1.5",
        "operation": "create",
        "record_type": THERMO_TYPE,
        "station": dict(station),
        "occurred_at": occurred_at,
        "now": iso_now(),
        "create_seq": raw_seq,
        "submitted_by": operator,
        "payload": payload,
        "ledger_view": ledger.build_ledger_view(station_id, THERMO_TYPE),
        "alarm_history": ledger.build_alarm_history(station_id, THERMO_TYPE),
    }
    if attachments:
        envelope["attachments_ref"] = attachments

    registry = registry_for_thermo(ledger.get_thermo_thresholds())
    created = records_kit.process(envelope, registry)
    if created["status"] != "ok":
        summary = _rejected(data, created)
        if submission_id:
            ledger.put_receipt(submission_id, station_id, data.get("submitted_at") or iso_now(), summary)
        return summary

    ledger.save_create(envelope, created, actor=operator,
                       group_label=THERMO_GROUP, scope=THERMO_GROUP)
    record = created["record"]
    confirm_envelope = {
        "protocol": "records-kit",
        "protocol_version": "1.5",
        "operation": "confirm",
        "record_type": THERMO_TYPE,
        "station": dict(station),
        "now": iso_now(),
        "subject": {"record_uid": record["record_uid"],
                    "lifecycle": record["lifecycle"], "rev": record["rev"]},
        "confirmations": [],
        "ledger_view": ledger.build_ledger_view(station_id, THERMO_TYPE),
    }
    confirmed = records_kit.process(confirm_envelope, registry)
    if confirmed["status"] == "ok":
        ledger.save_confirm(confirm_envelope, confirmed, actor="auto-confirm")
        lifecycle = confirmed["record"]["lifecycle"]
    else:
        ledger.audit("core", "auto_confirm_failed", record["record_uid"], confirmed["validation"])
        lifecycle = record["lifecycle"]
    ledger.update_alarm(station_id, THERMO_TYPE, created.get("alarm_state"), occurred_at)

    group = {
        "group": THERMO_GROUP,
        "status": "ok",
        "record_uid": record["record_uid"],
        "rev": record["rev"],
        "lifecycle": lifecycle,
        "occurred_at": occurred_at,
        "rules": created.get("rules") or [],
        "actions_hint": created.get("actions_hint") or [],
        "_payload": payload,
        "_record": {"record_uid": record["record_uid"], "rev": record["rev"],
                    "lifecycle": lifecycle},
    }
    summary = {
        "ok": True,
        "kind": "thermography",
        "submitted_at": data.get("submitted_at") or iso_now(),
        "groups": [group],
        "counts": {"groups": 1, "ok": 1, "cells": len(payload["items"])},
    }
    if dispatch:
        from da_core.table_projection import dispatch_thermo
        summary["dispatch"] = dispatch_thermo(
            group, settings=settings, ledger=ledger, dry_run=dry_run_dispatch)
    summary["notify"] = notify_defects(ledger, settings, group, runner=runner)
    if submission_id:
        ledger.put_receipt(submission_id, station_id, summary["submitted_at"], summary)
    return summary


def _rejected(data: dict, created: dict) -> dict:
    return {
        "ok": False,
        "kind": "thermography",
        "submitted_at": data.get("submitted_at") or iso_now(),
        "groups": [{
            "group": THERMO_GROUP,
            "status": "rejected",
            "validation": created.get("validation") or {},
        }],
        "counts": {"groups": 1, "ok": 0, "cells": 0},
    }


def sender_of(message: dict) -> str:
    for key in ("senderId", "senderStaffId", "openDingTalkId", "senderNick"):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def image_ref(message: dict) -> str | None:
    """从一条群消息里取出图片引用。没有图片返回 None。"""
    candidates: list[str] = []
    content = message.get("content")
    if isinstance(content, dict):
        for key in ("downloadCode", "imageUrl", "pictureUrl", "mediaId", "url", "photoUrl", "picUrl"):
            value = content.get(key)
            if isinstance(value, str) and value.strip():
                candidates.append(value.strip())
    elif isinstance(content, str) and content.strip().startswith("http"):
        candidates.append(content.strip())
    for key in ("imageUrl", "pictureUrl", "downloadCode", "mediaId"):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            candidates.append(value.strip())
    kind = str(message.get("msgType") or message.get("messageType") or message.get("contentType") or "").lower()
    if candidates:
        return candidates[0]
    if kind in ("image", "picture", "photo", "img") and message.get("messageId"):
        return str(message["messageId"])
    return None


def following_images(messages: list[dict], start: int, sender: str) -> tuple[list[str], int]:
    """紧跟在文本后、同一发送人的图片，按到达顺序。下一条文本结束。"""
    refs: list[str] = []
    index = start
    while index < len(messages):
        message = messages[index]
        who = sender_of(message)
        if sender and who and who != sender:
            break
        text = message.get("text") or ""
        if HEADER in text or "蓄电池电压测量数据" in text:
            break
        ref = image_ref(message)
        if not ref:
            break
        refs.append(ref)
        index += 1
    return refs, index
