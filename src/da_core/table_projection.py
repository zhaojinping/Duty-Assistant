"""输出投影器：判定结果 → 钉钉 AI 表格《电压测量记录》逐只写行。

- 单一写者 = 核心系统（应用不再直写表格）。
- 判定列合成：条目级规则（detail 含 ``cell_no=N``）映射到对应单体行；
  记录级条目暂不入行（P1 两类规则均为条目级）。
- 写入走 dws CLI：``--records-file`` 规避 Windows 命令行长度限制；
  分片 ≤100 行/批；``--client-token`` 幂等键随批生成。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

_TIMEOUT_SECONDS = 120
_CHUNK_SIZE = 100

# 生命周期状态 → 表格中文标签（账本状态列）
_LIFECYCLE_LABELS = {
    "draft": "草稿",
    "confirmed": "已定稿",
    "archived": "已归档",
    "voided": "已作废",
}


def compose_rows(group_result: dict, *, field_ids: dict,
                 remark_tag: str | None = None) -> list[dict]:
    """把一个组的判定结果合成为表格行（每行一只单体，cells 键 = fieldId）。"""
    payload = group_result["_payload"]
    record = group_result["_record"]
    rules = group_result["rules"]

    def put(cells: dict, label: str, value) -> None:
        if value is None or value == "":
            return
        field_id = field_ids.get(label)
        if field_id:
            cells[field_id] = value

    rows: list[dict] = []
    for item in payload["items"]:
        cell_no = item["cell_no"]
        marker = re.compile(rf"cell_no={cell_no}(?!\d)")
        hits = [entry for entry in rules if marker.search(str(entry.get("detail", "")))]
        violations = [entry for entry in hits if entry.get("verdict") == "violation"]
        detail = "；".join(dict.fromkeys(entry["detail"] for entry in violations))

        cells: dict = {}
        put(cells, "电池组别", group_result["group"])
        put(cells, "电池序号", cell_no)
        put(cells, "电压值(V)", item["voltage"])
        put(cells, "环境温度(℃)", payload.get("env_temp"))
        put(cells, "直流系统编号", payload.get("dc_system_id"))
        put(cells, "浮充电压(V)", payload.get("float_voltage"))
        put(cells, "测试性质", payload.get("test_kind"))
        put(cells, "是否异常", "异常" if violations else "正常")
        put(cells, "判定说明", detail)
        remark = item.get("remark")
        if remark_tag:
            remark = f"{remark}｜{remark_tag}" if remark else remark_tag
        put(cells, "备注", remark)
        put(cells, "账本UID", record["record_uid"])
        put(cells, "账本Rev", record["rev"])
        put(cells, "账本状态",
            _LIFECYCLE_LABELS.get(record["lifecycle"], record["lifecycle"]))
        rows.append(cells)
    return rows


def compose_thermo_rows(group_result: dict, *, field_ids: dict) -> list[dict]:
    """一个测点一行。引擎判级按设备带上；判定说明写在该设备热点行。"""
    from da_core.thermo import device_findings

    payload = group_result["_payload"]
    record = group_result["_record"]
    findings = device_findings(group_result.get("rules") or [])
    hottest: dict[str, dict] = {}
    for item in payload["items"]:
        name = item["device_name"]
        previous = hottest.get(name)
        if previous is None or float(item["measured_temp"]) >= float(previous["measured_temp"]):
            hottest[name] = item

    def put(cells: dict, label: str, value) -> None:
        if value is None or value == "":
            return
        field_id = field_ids.get(label)
        if field_id:
            cells[field_id] = value

    rows: list[dict] = []
    for item in payload["items"]:
        name = item["device_name"]
        finding = findings.get(name) or {}
        cells: dict = {}
        put(cells, "测温时间", group_result.get("occurred_at"))
        put(cells, "测温性质", payload.get("test_kind"))
        put(cells, "环境温度(℃)", payload.get("env_temp"))
        put(cells, "负荷电流(A)", payload.get("load_current"))
        put(cells, "测点序号", item["spot_no"])
        put(cells, "设备名称", name)
        put(cells, "测点部位", item.get("spot"))
        put(cells, "致热类型", item.get("heat_type"))
        put(cells, "实测温度(℃)", item["measured_temp"])
        put(cells, "相间温差(K)", item.get("phase_temp_diff"))
        put(cells, "相对温差δt(%)", item.get("delta_t"))
        put(cells, "引擎判级", _engine_grade(finding.get("grade")))
        put(cells, "人工判级", item.get("defect_grade"))
        put(cells, "仪器编号", item.get("instrument_id"))
        if item is hottest.get(name) and finding.get("detail"):
            put(cells, "判定说明", finding["detail"])
        put(cells, "账本UID", record["record_uid"])
        put(cells, "账本Rev", record["rev"])
        put(cells, "账本状态", _LIFECYCLE_LABELS.get(record["lifecycle"], record["lifecycle"]))
        rows.append(cells)
    return rows


def _engine_grade(short: str | None) -> str | None:
    return {
        "正常": "正常",
        "一般": "一般缺陷",
        "严重": "严重缺陷",
        "危急": "危急缺陷",
    }.get(short or "")


def dispatch_thermo(group_result: dict, *, settings, ledger=None, dry_run: bool = False) -> dict:
    """写《设备测温记录》。表还没建则跳过，不把账本当成失败。"""
    table = getattr(settings, "thermo_table", None)
    if not table:
        return {"skipped": True, "reason": "还没有测温表"}
    rows = compose_thermo_rows(group_result, field_ids=table.get("field_ids") or {})
    if dry_run:
        return {"dry_run": True, "rows": len(rows), "sample": rows[:2]}
    result = write_rows(table["base_id"], table["table_id"], rows)
    if ledger is not None:
        ledger.audit("core", "thermo_projection", table["table_id"],
                     {"rows": len(rows), "written": result.get("written")})
    return result


def dispatch(group_results: list[dict], *, settings, ledger=None,
             dry_run: bool = False, remark_tag: str | None = None) -> dict:
    """组装全部成功组的行并写表（dry_run 时只组装、不写）。"""
    rows: list[dict] = []
    for group in group_results:
        if group.get("status") != "ok":
            continue
        rows.extend(compose_rows(group, field_ids=settings.table["field_ids"],
                                 remark_tag=remark_tag))
    if dry_run:
        return {"dry_run": True, "rows": len(rows), "sample": rows[:2]}
    result = write_rows(settings.table["base_id"], settings.table["table_id"], rows)
    if ledger is not None:
        ledger.audit("core", "table_projection", settings.table["table_id"],
                     {"rows": len(rows), "written": result.get("written")})
    return result


def write_rows(base_id: str, table_id: str, rows: list[dict], *,
               chunk_size: int = _CHUNK_SIZE) -> dict:
    """调 dws 批量写行（分片）。返回 ``{written, record_ids, failures}``。"""
    if not rows:
        return {"written": 0, "record_ids": [], "failures": []}
    exe = shutil.which("dws")
    if not exe:
        raise RuntimeError("未找到 dws CLI（请确认 PATH 配置）")

    written = 0
    record_ids: list[str] = []
    failures: list[dict] = []
    for offset in range(0, len(rows), chunk_size):
        batch = rows[offset:offset + chunk_size]
        payload = json.dumps([{"cells": row} for row in batch], ensure_ascii=False)
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8") as handle:
            handle.write(payload)
            records_file = handle.name
        try:
            command = _dws_command(exe, [
                "aitable", "record", "create",
                "--base-id", base_id,
                "--table-id", table_id,
                "--records-file", records_file,
                "--client-token", str(uuid.uuid4()),
                "--format", "json",
            ])
            proc = subprocess.run(command, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace",
                                  timeout=_TIMEOUT_SECONDS)
            if proc.returncode != 0:
                failures.append({
                    "offset": offset,
                    "returncode": proc.returncode,
                    "stderr": (proc.stderr or proc.stdout or "").strip()[:300],
                })
                continue
            chunk_ids, failure = _accept_ids(
                _extract_record_ids(proc.stdout), offset=offset, returncode=proc.returncode)
            if failure:
                failures.append(failure)
                continue
            written += len(chunk_ids)
            record_ids.extend(chunk_ids)
        finally:
            Path(records_file).unlink(missing_ok=True)
    return {"written": written, "record_ids": record_ids, "failures": failures}


def _accept_ids(chunk_ids: list[str], *, offset: int, returncode: int) -> tuple[list[str], dict | None]:
    """没有记录号的批次不算写入。"""
    if not chunk_ids:
        return [], {
            "offset": offset,
            "returncode": returncode,
            "stderr": "返回成功但没有记录号，本批不算写入",
        }
    return chunk_ids, None


def _extract_record_ids(stdout: str) -> list[str]:
    try:
        data = json.loads(stdout)
    except (TypeError, ValueError):
        return []
    payload = data.get("data") if isinstance(data, dict) else None
    if not isinstance(payload, dict):
        return []
    new_ids = payload.get("newRecordIds")
    if isinstance(new_ids, list):
        return [str(value) for value in new_ids if value]
    # 兼容记录列表形态（records/results/items）
    candidates = []
    for key in ("records", "results", "items"):
        if isinstance(payload.get(key), list):
            candidates = payload[key]
            break
    ids = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        value = item.get("recordId") or item.get("record_id")
        if value:
            ids.append(str(value))
    return ids


def _dws_command(exe: str, args: list[str]) -> list[str]:
    if exe.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", exe, *args]
    return [exe, *args]


# ── 更正 / 作废 的行同步（投影增强） ────────────────────────────────

def _submit_updates(settings, updates: list[dict], *, runner=None) -> dict:
    """批量更新表格行（+record-update，--yes 表既有授权）。"""
    if not updates:
        return {"written": 0, "failures": []}
    from da_core import dws_cli

    rc, out, err = (runner or dws_cli.run_dws)([
        "aitable", "+record-update",
        "--base-id", settings.table["base_id"],
        "--table-id", settings.table["table_id"],
        "--records", json.dumps(updates, ensure_ascii=False),
        "--format", "json", "--yes"])
    if rc != 0:
        return {"written": 0, "failures": [(err or out).strip()[:300]]}
    return {"written": len(updates), "failures": []}


def _row_index(settings, record_uid: str, *, runner=None) -> dict:
    """读表并按（电池序号）索引该 UID 的行 → ``{cell_no: recordId}``。"""
    from da_core.reconcile import _text, fetch_table_rows

    field_ids = settings.table["field_ids"]
    index: dict[int, str] = {}
    for row in fetch_table_rows(settings, runner=runner):
        cells = row.get("cells") or {}
        if _text(cells.get(field_ids["账本UID"])) != record_uid:
            continue
        try:
            index[int(float(cells.get(field_ids["电池序号"])))] = row.get("recordId")
        except (TypeError, ValueError):
            continue
    return index


def sync_updated_record(group_result: dict, *, settings, runner=None,
                        dry_run: bool = False) -> dict:
    """更正后同步：按（账本UID + 电池序号）更新既有行（缺行记入 missing_cells）。"""
    uid = group_result["_record"]["record_uid"]
    rows = compose_rows(group_result, field_ids=settings.table["field_ids"])
    if dry_run:
        return {"dry_run": True, "rows": len(rows)}
    index = _row_index(settings, uid, runner=runner)
    updates, missing = [], []
    for cells in rows:
        try:
            cell_no = int(cells[settings.table["field_ids"]["电池序号"]])
        except (KeyError, TypeError, ValueError):
            continue
        record_id = index.get(cell_no)
        if not record_id:
            missing.append(cell_no)
            continue
        updates.append({"recordId": record_id, "cells": cells})
    result = _submit_updates(settings, updates, runner=runner)
    return {"updated": result.get("written", 0), "missing_cells": missing,
            "failures": result.get("failures", [])}


def sync_void_record(settings, record_uid: str, *, runner=None,
                     dry_run: bool = False) -> dict:
    """作废后同步：该 UID 的全部行 → 账本状态=已作废。"""
    if dry_run:
        return {"dry_run": True}
    from da_core.reconcile import _text, fetch_table_rows

    field_ids = settings.table["field_ids"]
    updates = []
    for row in fetch_table_rows(settings, runner=runner):
        cells = row.get("cells") or {}
        if _text(cells.get(field_ids["账本UID"])) != record_uid:
            continue
        updates.append({"recordId": row.get("recordId"),
                        "cells": {field_ids["账本状态"]: "已作废"}})
    result = _submit_updates(settings, updates, runner=runner)
    return {"updated": result.get("written", 0),
            "failures": result.get("failures", [])}
