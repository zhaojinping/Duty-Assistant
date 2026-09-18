"""M0 核验①：快照表头 vs draft 草稿逐列映射的自动比对。

从 excel_struct.json 提取每份模板表头行的列名，从 dsl-gap-list-draft.md
各节「Excel 列」栏提取草稿声明，输出逐表逐列的命中/遗漏/多余清单。
只读核验，不改动任何被核对文件。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / 'scripts' / 'm0' / 'excel_struct.json'
# 定稿自 #10 起改名 dsl-gap-list.md；过渡期回退兼容 draft 名
DRAFT = ROOT / 'docs' / 'dsl-gap-list.md'
if not DRAFT.exists():
    DRAFT = ROOT / 'docs' / 'dsl-gap-list-draft.md'

# draft 各节标题里的 record_type 与快照键的对应（按节顺序）。
SECTION_KEYS = [
    ('breaker_trip_record', '断路器跳闸记录簿'),
    ('surge_arrester_action_record', '避雷器动作记录'),
    ('grounding_wire_record', '接地线装拆'),
    ('two_ticket_ledger', '两票'),
    ('infrared_thermometry_record', '测温'),
    ('insulation_test_record', '绝缘'),
    ('battery_voltage_test', '蓄电池'),
    ('transformer_core_clamp_current_record', '铁芯'),
    ('protection_switch_record', '投退'),
    ('rodent_proof_check_record', '防小动物'),
]


def snapshot_columns():
    data = json.loads(SNAPSHOT.read_text(encoding='utf-8'))
    out = {}
    for key in data:
        sheet = data[key]['sheets'][0]
        rows = {row['r']: [v for v in row['vals']] for row in sheet['rows']}
        header_r = 3
        for row in sheet['rows']:
            vals = [v for v in row['vals']]
            texts = [v.strip() for v in vals if isinstance(v, str) and v.strip()]
            if len(texts) >= 3 and ('时间' in ''.join(texts) or '日期' in ''.join(texts)
                                    or '序号' in ''.join(texts)):
                header_r = row['r']
                break
        header = [v.strip() if isinstance(v, str) else '' for v in rows[header_r]]
        out[key] = [c for c in header if c]
    return out


def draft_columns():
    text = DRAFT.read_text(encoding='utf-8')
    sections = {}
    current = None
    pending_section = None
    for line in text.splitlines():
        m = re.match(r'^## (\d+)\. ([a-z0-9_]+)', line)
        if m:
            pending_section = m.group(2)
            continue
        if pending_section and line.startswith('|'):
            cells = [c.strip() for c in line.strip().strip('|').split('|')]
            if len(cells) >= 4 and re.match(r'^\d+$', cells[0]) and 'Excel 列' not in line:
                sections.setdefault(pending_section, []).append(
                    cells[1].replace('**', '').replace('`', '').strip())
    return sections


def main():
    snap = snapshot_columns()
    draft = draft_columns()
    problems = []
    print(f'快照表数: {len(snap)} | 草稿节表数: {len(draft)}')
    snap_keys = list(snap)
    for idx, (key, label) in enumerate(SECTION_KEYS):
        if key in snap:
            s_key, s_cols = key, snap[key]
        else:
            # 草稿节名与快照键可能不同（如测温命名差异），按节序兜底
            s_key = snap_keys[idx] if idx < len(snap_keys) else key
            s_cols = snap.get(s_key, [])
        key_disp = key if s_key == key else f'{key} (快照键:{s_key})'
        d_cols = draft.get(key, [])
        if not d_cols:
            problems.append(f'{key_disp}: 草稿无映射行')
            continue
        missing = [c for c in s_cols if not any(c in d or d in c for d in d_cols)]
        extra = [d for d in d_cols if not any(d in c or c in d for c in s_cols)]
        status = 'OK' if not missing and not extra else 'DIFF'
        print(f'{status} {key_disp}: 快照{s_cols and len(s_cols)}列 vs 草稿{len(d_cols)}列'
              + (f' | 表头行: {s_cols}' if status == 'DIFF' else ''))
        if missing:
            problems.append(f'{key_disp} 快照有而草稿缺: {missing}')
        if extra:
            problems.append(f'{key_disp} 草稿有而快照无: {extra}')
    print('---')
    if problems:
        print('发现问题:')
        for p in problems:
            print(' -', p)
    else:
        print('全部表的逐列映射与快照一致。')


if __name__ == '__main__':
    main()
