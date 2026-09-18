# -*- coding: utf-8 -*-
"""补充提取：脚注行（13-27）、数据有效性下拉、批注、隐藏行列。

用法：python scripts/m0_extract_extra.py <模板目录>   （JSON 结果打到 stdout）
目录从 CLI 参数读取（不硬编码机器路径）。
"""
import json, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
from openpyxl import load_workbook
from pathlib import Path

if len(sys.argv) != 2:
    print(__doc__)
    raise SystemExit(2)
SRC = Path(sys.argv[1])
FILES = [
    ("breaker_trip_record", "断路器跳闸记录簿.xlsx"),
    ("surge_arrester_action_record", "避雷器动作记录簿.xlsx"),
    ("grounding_wire_record", "接地线装拆记录簿.xlsx"),
    ("two_ticket_ledger", "两票登记记录簿.xlsx"),
    ("infrared_thermography_record", "设备测温记录簿.xlsx"),
    ("insulation_test_record", "绝缘测试记录簿.xlsx"),
    ("battery_voltage_test", "蓄电池电压测试记录簿.xlsx"),
    ("transformer_core_clamp_current_record", "主变铁芯夹件电流测试记录簿.xlsx"),
    ("protection_switch_record", "保护投退记录簿.xlsx"),
    ("rodent_proof_check_record", "防小动物检查记录簿.xlsx"),
]

def cellstr(v):
    if v is None: return None
    s = str(v).strip()
    return s if s else None

out = {}
for rt, fn in FILES:
    wb = load_workbook(SRC / fn, data_only=False)
    ws = wb.worksheets[0]
    e = {"footer_rows": [], "validations": [], "comments": []}
    # 第 13 行以后所有非空单元格
    for row in ws.iter_rows(min_row=13, max_row=ws.max_row, max_col=ws.max_column):
        for c in row:
            if c.value is not None and str(c.value).strip():
                e["footer_rows"].append(f"{c.coordinate}: {str(c.value).strip()}")
    # 数据有效性
    for dv in ws.data_validations.dataValidation:
        e["validations"].append({
            "type": dv.type, "formula1": dv.formula1,
            "formula2": dv.formula2, "ranges": str(dv.sqref),
        })
    # 批注
    for row in ws.iter_rows():
        for c in row:
            if c.comment and c.comment.text and c.comment.text.strip():
                t = " ".join(c.comment.text.split())
                e["comments"].append(f"{c.coordinate}: {t}")
    # 电池表尾行序号（判断模板预填到几号）
    if rt == "battery_voltage_test":
        seqs = []
        for row in ws.iter_rows(min_row=5, max_row=26, max_col=1):
            v = cellstr(row[0].value)
            if v: seqs.append(v)
        e["battery_prefill_seq"] = seqs
    out[rt] = e

print(json.dumps(out, ensure_ascii=False, indent=1))
