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
        put(cells, "账本状态", record["lifecycle"])
        rows.append(cells)
    return rows


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
            chunk_ids = _extract_record_ids(proc.stdout)
            written += len(chunk_ids) if chunk_ids else len(batch)
            record_ids.extend(chunk_ids)
        finally:
            Path(records_file).unlink(missing_ok=True)
    return {"written": written, "record_ids": record_ids, "failures": failures}


def _extract_record_ids(stdout: str) -> list[str]:
    try:
        data = json.loads(stdout)
    except (TypeError, ValueError):
        return []
    payload = data.get("data") if isinstance(data, dict) else None
    candidates = []
    if isinstance(payload, dict):
        for key in ("records", "results", "items"):
            if isinstance(payload.get(key), list):
                candidates = payload[key]
                break
    elif isinstance(payload, list):
        candidates = payload
    ids = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        value = item.get("recordId") or item.get("record_id")
        if value:
            ids.append(value)
    return ids


def _dws_command(exe: str, args: list[str]) -> list[str]:
    if exe.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", exe, *args]
    return [exe, *args]
