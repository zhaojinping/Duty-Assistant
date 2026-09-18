# -*- coding: utf-8 -*-
"""提取 Excel 模板结构：sheet 名、合并单元格、表头行、列名、示例行。

用法：python scripts/m0_extract_struct.py <模板目录> <输出JSON路径>
目录与输出路径从 CLI 参数读取（不硬编码机器路径）。
"""
import json, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
from openpyxl import load_workbook
from pathlib import Path

# 10 份在范围（消防器材、保护定值压板已停用）
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
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None

def extract(path):
    wb = load_workbook(path, data_only=True)
    out = []
    for ws in wb.worksheets:
        info = {
            "sheet": ws.title,
            "dims": ws.dimensions,
            "max_row": ws.max_row,
            "max_col": ws.max_column,
            "merged": [str(r) for r in ws.merged_cells.ranges],
            "rows": [],
        }
        # 抓前 12 行原始内容（含空格子标记），足够覆盖表头+示例
        for row in ws.iter_rows(min_row=1, max_row=min(ws.max_row, 12), max_col=ws.max_column):
            vals = [cellstr(c.value) for c in row]
            # 行全空跳过记录但保留行号信息
            info["rows"].append({"r": row[0].row, "vals": vals})
        out.append(info)
    return out

result = {}
if len(sys.argv) != 3:
    print(__doc__)
    raise SystemExit(2)
SRC = Path(sys.argv[1])
for rt, fn in FILES:
    p = SRC / fn
    result[rt] = {"file": fn, "exists": p.exists(), "sheets": extract(p) if p.exists() else []}

dst = Path(sys.argv[2])
dst.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
print("written", dst)
for rt, d in result.items():
    print(rt, d["file"], "sheets:", [(s["sheet"], s["max_row"], s["max_col"], len(s["merged"])) for s in d["sheets"]])
