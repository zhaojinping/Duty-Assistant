# M3 预热影响评估（卡 #26）：`confirmed_digest` 行结构缺口 + protocol 三补充核查

> 卡：#26（M3 预热，M3 开工即交付）。范围：①archive 并存窗口 `confirmed_digest` 字段（行结构契约缺口）；
> ②protocol 三补充（`record_type` 回显 / `record.links` / `alarm_state.evidence`）+ 信封 `additionalProperties:false` 收紧。
> 状态：**评估（草案）**。涉契约层的改动**未实施**——按 §10 铁律与协作约束，另开契约卡经拍板后落地。

## 0. 结论摘要

| # | 项 | 现状 | 判定 |
|---|---|---|---|
| ① | 并存窗口「最后确认版」版本号 | 由 `confirmed_fields` 存在性 + `row_rev − 1` **隐式推断**（`engine/lifecycle.py:278-290`） | ⚠️ **缺口成立**：多轮 correct 下归档版本号错标（见 §1 实证） |
| ② | `record_type` 回显 | `record_result.schema.json` 已声明（`["string","null"]`） | ✅ 已落地 |
| ② | `record.links` | 已声明（描述注明「M1 协议补足」） | ✅ 已落地 |
| ② | `alarm_state.evidence` | 已声明（`{"type":"string","description":"判定依据…§6.2"}`） | ✅ 已落地 |
| ② | 信封 `additionalProperties:false` | `record_envelope.schema.json` 已是 `false`（23 个具名属性，无兜底口） | ✅ 已落地 |

② 的四项在 M1/M2 期间已随协议 Schema 落地并各有测试；本卡实际待办只有 ①。

## 1. ① 缺口：并存窗口内「最后确认版」不可知

### 1.1 现行实现（可复核）

`engine/lifecycle.py:278-296`：行带 `confirmed_fields` 即视为「correct 并存窗口」，据此：

```text
target_rev = row_rev - 1                    # 最后确认版 rev := 最新版 rev − 1
subject_rev ∈ {row_rev, row_rev - 1}        # 否则 E_REV_CONFLICT「只能归档最后确认版」
fields     = row["confirmed_fields"]        # 内容取最后确认版投影（正确）
digest     = compute_digest(..., confirmed_fields)
```

单轮 correct（`rev=2`，最后确认版=rev1）下与 §8.3 拍板一致，且已被现有用例覆盖
（`tests/test_engine_lifecycle.py` 并存窗口两例）。

### 1.2 实证（只读探针，未入库）

场景：同一记录经 **2 轮 correct** → 账本行 `rev=3`、`lifecycle="confirmed"`；**实际最后确认版是 rev 1**
（只有 rev1 经 `confirm` 签认）。归档结果应为「rev=1 + 最后确认版内容指纹」。

```text
$ <只读探针：archive 三档 subject.rev>
最后确认版内容指纹（应为归档结果 digest）: sha256:324bb9e5… 
  subject.rev=1 → 拒绝：E_REV_CONFLICT@subject.rev
  subject.rev=2 → 通过：归档记录 rev=2（正确值应为 1：❌）；内容指纹取最后确认版：✅
  subject.rev=3 → 通过：归档记录 rev=2（正确值应为 1：❌）；内容指纹取最后确认版：✅
```

要点：

- **壳层拿不到真实最后确认版**：传 rev=1 被 `E_REV_CONFLICT` 拒绝，只能传 2 或 3；
- **引擎恒把归档版本号标为 `row_rev − 1`**：重排后为 `rev=2`，而 rev2 从未被签认；
- **内容是对的、版本号是错的**：`digest` 取 `confirmed_fields` 重算（✅），但结果的 `record.rev` 失真——
  审计链上「归档的是哪一版」不可信，而 §8.3 拍板的原文正是「并存窗口内 archive 目标=**最后确认版**」。

### 1.3 根因

行结构（`engine/views.py` 的 `LEDGER_ROW_KEYS`）带 `confirmed_fields`（字段投影），**不带
`confirmed_rev` / `confirmed_digest`**——即「最后确认版」只在**内容**上可见，在**版本**上不可见。
引擎于是用 `row_rev − 1` 猜版本号；该猜测仅在「至多一轮 correct」时成立。

### 1.4 影响面

| 位置 | 影响 |
|---|---|
| `docs/design.md` §8.3 | 行结构需增字段；并需写明「最后确认版=可携带版本号」而非位置推断 |
| `engine/views.py` | `LEDGER_ROW_KEYS` 白名单与 `_check_row` 类型校验需增项（缺省可选） |
| `engine/lifecycle.py` | `_archive` 的目标版本取数改为读行字段；缺失时兜底策略需拍板 |
| 壳层（EAM 侧） | 记账需带上最后确认版 rev；旧的「传 row_rev−1」调用方行为不破（见 3.1 兼容性） |
| 测试 | 需补「多轮 correct + archive」判别性用例（现覆盖停在单轮） |

## 2. ② protocol 三补充核查证据

| 项 | 位置 | 声明形态 |
|---|---|---|
| `record_type` 回显 | `record_result.schema.json` → `properties.record_type` | `{"type":["string","null"]}`；`required` 含 `record_type` |
| `record.links` | 同上 → `properties.record.links` | 数组；描述：「M1 协议补足：成对关系（含 correct 自动写入的 supersedes）需随结果回传」 |
| `alarm_state.evidence` | 同上 → `alarm_state.properties.evidence` | `{"type":"string"}`；`alarm_state` 为 `additionalProperties:false`，三必填 + evidence |
| 信封收紧 | `record_envelope.schema.json` → `additionalProperties` | `false`；`required: [protocol, protocol_version, operation, station, now]` |

实现侧同样已就绪：`engine/lifecycle.py:401`（record 回传 `links`）、`engine/alarm.py:67-70`（返回 `evidence`）、
`records_kit/__init__.py` 结果组装回显 `record_type`。协议合规测试见 `tests/test_protocol_schema.py`。

## 3. 建议（供拍板）

### 3.1 方案 A（推荐）：行结构增可选 `confirmed_rev`

- `same_type_records` 行可选增 `confirmed_rev`（与 `confirmed_fields` 成对，语义=最后已确认版的 `rev`）；
- `_archive` 目标版本取数：`row.get("confirmed_rev")` 优先，缺失时退回现行 `row_rev − 1`（**向后兼容**：
  老壳层不传 → 行为与今天一致，单轮 correct 场景不变）；
- `subject.rev` 校验放宽为「∈ {row_rev, confirmed_rev}」，错误文案说明两者；
- 可选再加 `confirmed_digest`（同版指纹）供壳层对账，但**非必需**——`digest` 可由引擎按 `confirmed_fields` 重算（现状已如此）。

### 3.2 方案 B：维持现约定，把限制写进口径

在 §8.3 明写「并存窗口至多一轮 correct（`confirmed_fields` 存在 ⇒ 最后确认版=`row_rev−1`）」，
并要求壳层在并存窗口内不再发起第二次 correct。

- 成本低（仅文档），但**无强制力**：引擎无从校验，越界时静默错标；
- 若现场认定「多轮 correct 不会发生」，此方案可接受，但需明确写入声明线/壳层的约束清单。

### 3.3 决策点（需主控裁断）

1. 走方案 A（契约卡 + 实现 + 测试）还是方案 B（仅口径注记）；
2. 若走 A：字段命名与语义边界（`confirmed_rev` 是否随 `confirmed_fields` 同步缺失/出现）；
3. 缺失兜底：`confirmed_fields` 存在而 `confirmed_rev` 缺失时，是否保留 `row_rev − 1` 兜底
   （建议保留，兼容期用）；
4. 是否同步增 `confirmed_digest`（建议暂不，避免字段冗余）。

## 4. 未验证项 / 开放项

- **业务前提未确认**：多轮 correct 是否会出现（需壳层/EAM 侧确认；若确认不会，① 降级为口径注记）。
- **错标的下游影响面未评估**：归档版本号失真对壳层记账、EAM 归档单、审计链的实际影响——属壳层侧。
- 本评估未改动任何实现或契约；探针为一次性只读脚本，跑完未入库（仓库无残留）。
