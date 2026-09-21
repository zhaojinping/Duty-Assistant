"""表 ↔ 账本对账：只读比对，找差异（不自动修）。

口径：
- 仅比对**非作废**记录的投影一致性：行数 = items 数、账本Rev、账本状态、逐只电压值；
- 表内无「账本UID」的行视为**存量/legacy 行**（首期迁移前历史），只计数不告警；
- 表内出现账本没有的 UID → orphan-rows 差异；
- correct/void 的表格同步为后续迭代（P3 投影增强）；本模块先覆盖 create 投影。
"""

from __future__ import annotations

import json

from da_core import dws_cli
from da_core.settings import BATTERY_TYPE

_LIFECYCLE_LABELS = {"draft": "草稿", "confirmed": "已定稿",
                     "archived": "已归档", "voided": "已作废"}


def fetch_table_rows(settings, *, runner=None) -> list[dict]:
    rc, out, err = (runner or dws_cli.run_dws)([
        "aitable", "record", "query",
        "--base-id", settings.table["base_id"],
        "--table-id", settings.table["table_id"],
        "--all", "--page-limit", "200", "--format", "json"])
    if rc != 0:
        raise RuntimeError(f"读表失败：{(err or out).strip()[:200]}")
    try:
        payload = json.loads(out).get("data") or {}
    except (TypeError, ValueError):
        raise RuntimeError("读表返回不是 JSON") from None
    return payload.get("records") or []


def _text(cell) -> str | None:
    if cell is None:
        return None
    if isinstance(cell, dict):
        return str(cell.get("name") or cell.get("text") or "")
    return str(cell)


def _number(cell):
    if cell is None:
        return None
    if isinstance(cell, dict):
        cell = cell.get("value", cell.get("text"))
    try:
        return float(str(cell).strip())
    except (TypeError, ValueError):
        return None


def reconcile(ledger, settings, *, table_rows: list[dict] | None = None,
              runner=None) -> dict:
    """比对表投影与账本事实；返回 ``{rows_total, legacy_rows, ledger_records, diffs, ok}``。"""
    rows = table_rows if table_rows is not None else fetch_table_rows(settings, runner=runner)
    field_ids = settings.table["field_ids"]
    station_id = settings.station["station_id"]

    by_uid: dict[str, list[dict]] = {}
    legacy = 0
    for row in rows:
        cells = row.get("cells") or {}
        uid = _text(cells.get(field_ids["账本UID"]))
        if not uid:
            legacy += 1
            continue
        by_uid.setdefault(uid, []).append(cells)

    diffs: list[dict] = []
    all_uids: set[str] = set()
    compared = 0
    for rec in ledger.conn.execute(
            "SELECT * FROM records WHERE station_id=? AND record_type=?",
            (station_id, BATTERY_TYPE)):
        uid = rec["record_uid"]
        all_uids.add(uid)
        if rec["lifecycle"] == "voided":
            continue
        compared += 1
        version = ledger.conn.execute(
            "SELECT fields_json FROM record_versions WHERE record_uid=? AND rev=?",
            (uid, rec["current_rev"])).fetchone()
        fields = json.loads(version["fields_json"])
        items = fields.get("items") or []
        got = by_uid.get(uid, [])
        if len(got) != len(items):
            diffs.append({"uid": uid, "kind": "row-count",
                          "ledger": len(items), "table": len(got)})
            continue
        rev_value = _number(got[0].get(field_ids["账本Rev"]))
        if rev_value is None or int(rev_value) != int(rec["current_rev"]):
            diffs.append({"uid": uid, "kind": "rev", "ledger": rec["current_rev"],
                          "table": got[0].get(field_ids["账本Rev"])})
        expected_label = _LIFECYCLE_LABELS.get(rec["lifecycle"], rec["lifecycle"])
        if _text(got[0].get(field_ids["账本状态"])) != expected_label:
            diffs.append({"uid": uid, "kind": "state", "ledger": expected_label,
                          "table": _text(got[0].get(field_ids["账本状态"]))})
        for item in items:
            match = [cells for cells in got
                     if _number(cells.get(field_ids["电池序号"])) == float(item["cell_no"])]
            if not match:
                diffs.append({"uid": uid, "kind": "cell-missing",
                              "cell": item["cell_no"]})
                continue
            value = _number(match[0].get(field_ids["电压值(V)"]))
            if value is None or abs(value - float(item["voltage"])) > 1e-9:
                diffs.append({"uid": uid, "kind": "voltage", "cell": item["cell_no"],
                              "ledger": item["voltage"],
                              "table": match[0].get(field_ids["电压值(V)"])})

    for uid in by_uid:
        if uid not in all_uids:
            diffs.append({"uid": uid, "kind": "orphan-rows", "rows": len(by_uid[uid])})

    return {"rows_total": len(rows), "legacy_rows": legacy,
            "ledger_records": compared, "diffs": diffs, "ok": not diffs}
