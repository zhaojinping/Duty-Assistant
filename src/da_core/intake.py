"""接入口：提交（原始事实）→ RecordEnvelope。

提交契约（改造方案 §4.3）::

    {
      "client_submission_id": "…",          # 幂等键（重试安全）
      "station": "ST001",                   # 可选；与部署配置一致性校验
      "operator": "赵金平",
      "submitted_at": "RFC3339",            # 可选；缺省 = 服务当前时刻
      "groups": [
        {
          "group": "3号组(12只)",           # 组别（决定口径 scope）
          "dc_system_id": "DC-003",         # 直流系统编号（判重键成员）
          "float_voltage": 13.5,            # 浮充电压（记录级）
          "test_kind": "定期",              # 测试性质
          "env_temp": 25,                   # 可选
          "measured_at": "RFC3339",         # 可选；缺省 = submitted_at
          "items": [{"no": 1, "volt": 13.45, "remark": "…"}]
        }
      ]
    }

装配口径：每个（组别）一条记录——聚合在调用方完成（App 只报原始事实，
核心按组别×日期把一组单体的读数装成一条记录，使整组规则可判定）。
"""

from __future__ import annotations

from records_kit.util import wall_stamp

from da_core.clock import iso_now
from da_core.settings import BATTERY_TYPE


class IntakeError(ValueError):
    """提交结构性问题（面向输入方的可读错误）。"""


_REQUIRED_GROUP_KEYS = ("group", "dc_system_id", "float_voltage", "test_kind", "items")


def normalize_payload(group: dict) -> dict:
    """组载荷 → 引擎 payload（``no``/``volt`` → ``cell_no``/``voltage``；可选键透传）。"""
    items: list[dict] = []
    for index, raw in enumerate(group.get("items") or []):
        if not isinstance(raw, dict):
            raise IntakeError(f"items[{index}] 必须是对象")
        try:
            cell_no = int(raw["no"])
            voltage = float(raw["volt"])
        except (KeyError, TypeError, ValueError):
            raise IntakeError(
                f"items[{index}] 需形如 {{'no': 1, 'volt': 2.23}}"
            ) from None
        entry = {"cell_no": cell_no, "voltage": voltage}
        if raw.get("remark"):
            entry["remark"] = raw["remark"]
        items.append(entry)
    payload = {
        "dc_system_id": group.get("dc_system_id"),
        "float_voltage": group.get("float_voltage"),
        "test_kind": group.get("test_kind"),
        "items": items,
    }
    if group.get("env_temp") is not None:
        payload["env_temp"] = group["env_temp"]
    return payload


def parse_submission(data: dict, *, settings, ledger) -> list[dict]:
    """解析并组装信封列表；不落账、不判定（纯装配）。"""
    if not isinstance(data, dict):
        raise IntakeError("提交必须是 JSON 对象")
    operator = data.get("operator")
    if not operator:
        raise IntakeError("缺少 operator（提交人）")
    groups = data.get("groups")
    if not isinstance(groups, list) or not groups:
        raise IntakeError("缺少 groups（至少一组）")
    submitted_at = data.get("submitted_at") or iso_now()

    station = settings.station
    if not isinstance(station, dict) or not station.get("station_id"):
        raise IntakeError("部署未配置 station，拒绝提交")

    group_kinds = ledger.get_group_kinds()
    pending_seq: dict[tuple, int] = {}  # 同一时刻多组时序号顺延（落账前本地递增）
    jobs: list[dict] = []

    for index, group in enumerate(groups):
        if not isinstance(group, dict):
            raise IntakeError(f"groups[{index}] 必须是对象")
        missing = [k for k in _REQUIRED_GROUP_KEYS if group.get(k) in (None, "", [])]
        if missing:
            raise IntakeError(f"groups[{index}] 缺字段：{'、'.join(missing)}")
        label = group["group"]
        scope = group_kinds.get(label)
        if scope is None:
            raise IntakeError(f"groups[{index}] 未知电池组别：{label!r}")

        occurred_at = group.get("measured_at") or submitted_at
        payload = normalize_payload(group)

        day, moment = wall_stamp(occurred_at)
        stamp_key = (station["station_id"], BATTERY_TYPE, day, moment)
        if stamp_key in pending_seq:
            raw_seq = pending_seq[stamp_key] + 1
        else:
            raw_seq = ledger.next_create_seq(station["station_id"], BATTERY_TYPE, occurred_at)
        pending_seq[stamp_key] = raw_seq

        jobs.append({
            "group": label,
            "scope": scope,
            "envelope": {
                "protocol": "records-kit",
                "protocol_version": "1.5",
                "operation": "create",
                "record_type": BATTERY_TYPE,
                "station": dict(station),
                "occurred_at": occurred_at,
                "now": iso_now(),
                "create_seq": raw_seq,
                "submitted_by": operator,
                "payload": payload,
            },
        })
    return jobs
