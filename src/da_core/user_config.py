"""用户安装配置。开发者的群、网址、人员和表格不能写入。"""

from __future__ import annotations

import json
import re
import urllib.parse
from pathlib import Path

from da_core.paths import config_path, ledger_path
from da_core.settings import DEFAULT_THRESHOLDS, Settings

DEVELOPER_GROUP = "APM测试"
DEVELOPER_ENTRY_HOST = "battery-voltage-entry.app.workbuddy.host"
DEVELOPER_USER_ID = "20240411222100620-4905-014C76478"
DEVELOPER_BASE_ID = "np9zOoBVBYALR6aeuenZZglmW1DK0g6l"
KIND_2V = "2V单体"
KIND_12V = "12V电池"
ALLOWED_KINDS = (KIND_2V, KIND_12V)
_STATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")

THERMO_TABLE_FIELDS: tuple[tuple[str, str], ...] = (
    ("测温时间", "text"),
    ("测温性质", "text"),
    ("环境温度(℃)", "number"),
    ("负荷电流(A)", "number"),
    ("测点序号", "number"),
    ("设备名称", "text"),
    ("测点部位", "text"),
    ("致热类型", "text"),
    ("实测温度(℃)", "number"),
    ("相间温差(K)", "number"),
    ("相对温差δt(%)", "number"),
    ("引擎判级", "text"),
    ("人工判级", "text"),
    ("仪器编号", "text"),
    ("判定说明", "text"),
    ("账本UID", "text"),
    ("账本Rev", "number"),
    ("账本状态", "text"),
)

# 建表接口单次最多 15 列，其余列建表后再补。
THERMO_TABLE_CREATE_LIMIT = 15

TABLE_FIELDS: tuple[tuple[str, str], ...] = (
    ("电池组别", "text"),
    ("电池序号", "number"),
    ("电压值(V)", "number"),
    ("环境温度(℃)", "number"),
    ("直流系统编号", "text"),
    ("浮充电压(V)", "number"),
    ("测试性质", "text"),
    ("是否异常", "text"),
    ("判定说明", "text"),
    ("备注", "text"),
    ("账本UID", "text"),
    ("账本Rev", "number"),
    ("账本状态", "text"),
)


class ConfigError(ValueError):
    """用户配置不合法。"""


def check_https_url(url: str, *, label: str) -> str:
    text = (url or "").strip()
    parsed = urllib.parse.urlparse(text)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ConfigError(f"{label}必须是 https 网址")
    if parsed.hostname.lower() == DEVELOPER_ENTRY_HOST:
        raise ConfigError(
            f"{label}用了开发者的录入页。请在 WorkBuddy 新建用户自己的录入页后再填。")
    return text


def validate_install(payload: dict) -> dict:
    """检查首次安装必填项，返回规整后的配置（不含账本路径）。"""
    station_name = str(payload.get("station_name") or "").strip()
    station_id = str(payload.get("station_id") or "").strip()
    if not station_name:
        raise ConfigError("缺少场站名称")
    if not _STATION_ID.match(station_id):
        raise ConfigError("场站短码只许字母、数字、下划线和短横线，且不超过 32 位")

    group_name = str(payload.get("group_name") or "").strip()
    if not group_name:
        raise ConfigError("缺少生产群名称")
    if group_name == DEVELOPER_GROUP:
        raise ConfigError("生产群不能用开发者的联调群「APM测试」，请换用户自己的群")

    assignee_name = str(payload.get("assignee_name") or "").strip()
    assignee_id = str(payload.get("assignee_id") or "").strip()
    if not assignee_name and not assignee_id:
        raise ConfigError("缺少待办责任人")
    if assignee_id == DEVELOPER_USER_ID:
        raise ConfigError("待办责任人用了开发者的测试人员，请改成现场的人")

    escalate_name = str(payload.get("escalate_name") or "").strip()
    escalate_id = str(payload.get("escalate_id") or "").strip()
    if escalate_id == DEVELOPER_USER_ID:
        raise ConfigError("班长用了开发者的测试人员，请改成现场的人，或留空")

    thermo_name = str(payload.get("thermo_assignee_name") or "").strip()
    thermo_id = str(payload.get("thermo_assignee_id") or "").strip()
    if thermo_id == DEVELOPER_USER_ID:
        raise ConfigError("测温责任人用了开发者的测试人员，请改成现场的人，或留空")

    entry_url = check_https_url(str(payload.get("entry_url") or ""), label="录入网址")
    pull_url = str(payload.get("pull_url") or "").strip() or entry_url
    pull_url = check_https_url(pull_url, label="拉取地址")

    groups = payload.get("groups")
    if not isinstance(groups, list) or not groups:
        raise ConfigError("至少要有一组电池。请说明有几组、每组是 2V 还是 12V、多少只")
    normalized_groups = []
    for item in groups:
        if not isinstance(item, dict):
            raise ConfigError("电池组别格式不对")
        name = str(item.get("name") or "").strip()
        kind = str(item.get("kind") or "").strip()
        if not name:
            raise ConfigError("电池组别缺少名称")
        if kind not in ALLOWED_KINDS:
            raise ConfigError(f"组别「{name}」的电压类型只能是「2V单体」或「12V电池」")
        count = item.get("count")
        if count is not None and (not isinstance(count, int) or isinstance(count, bool) or count < 1):
            raise ConfigError(f"组别「{name}」的只数必须是正整数")
        normalized_groups.append({"name": name, "kind": kind, "count": count})

    base_id = str(payload.get("base_id") or "").strip()
    if base_id == DEVELOPER_BASE_ID:
        raise ConfigError("钉钉表用了开发者的测试表。请新建用户自己的表。")

    anchor = payload.get("anchor_day", 15)
    if not isinstance(anchor, int) or isinstance(anchor, bool) or not 1 <= anchor <= 28:
        raise ConfigError("每月几号做，只能填 1 到 28")

    return {
        "station_id": station_id,
        "station_name": station_name,
        "group_name": group_name,
        "group_cid": str(payload.get("group_cid") or "").strip(),
        "assignee_name": assignee_name,
        "assignee_id": assignee_id,
        "escalate_name": escalate_name,
        "escalate_id": escalate_id,
        "entry_url": entry_url,
        "pull_url": pull_url,
        "pull_token": str(payload.get("pull_token") or ""),
        "groups": normalized_groups,
        "anchor_day": anchor,
        "baseline": payload.get("baseline") or None,
        "base_id": base_id,
        "table_id": str(payload.get("table_id") or "").strip(),
        "field_ids": dict(payload.get("field_ids") or {}),
        "ledger_url": str(payload.get("ledger_url") or "").strip(),
        "thermo_assignee_name": thermo_name,
        "thermo_assignee_id": thermo_id,
        "thermo_table": payload.get("thermo_table") if isinstance(payload.get("thermo_table"), dict) else None,
    }


def group_kinds_from(groups: list[dict]) -> dict[str, str]:
    return {item["name"]: item["kind"] for item in groups}


def save_config(data_dir: Path, payload: dict, *, writer: dict) -> Path:
    path = config_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "version": 1,
        **payload,
        "thresholds": {
            KIND_2V: list(DEFAULT_THRESHOLDS[KIND_2V]),
            KIND_12V: list(DEFAULT_THRESHOLDS[KIND_12V]),
        },
        "cycle_mode": "monthly_day",
        "writer": writer,
    }
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def load_config(data_dir: Path) -> dict | None:
    path = config_path(data_dir)
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def settings_from_config(data_dir: Path, payload: dict) -> Settings:
    field_ids = dict(payload.get("field_ids") or {})
    table = {
        "base_id": payload.get("base_id") or "",
        "table_id": payload.get("table_id") or "",
        "field_ids": field_ids,
    }
    return Settings.default(
        db_path=ledger_path(data_dir),
        station={"station_id": payload["station_id"], "station_name": payload["station_name"]},
        group_kinds=group_kinds_from(payload.get("groups") or []),
        table=table,
        group_name=payload.get("group_name") or "",
        group_cid=payload.get("group_cid") or "",
        entry_url=payload.get("entry_url") or "",
        ledger_url=payload.get("ledger_url") or "",
        thermo_table=payload.get("thermo_table") if isinstance(payload.get("thermo_table"), dict) else None,
    )
