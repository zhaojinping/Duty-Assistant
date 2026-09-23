# Duty-Assistant（运行值班助理）核心设计文档

- 日期：2026-09-18
- 状态：**审定稿**
- 工作名：`Duty-Assistant`（可改，改名不影响设计）

> **修订 A（2026-09-18，主控拍板）**：依据 M0 缺口清单（docs/dsl-gap-list.md §13 四组决策，全部拍板通过）：
> 1. **E1/E2** 接地线、保护投退改**一行一动作建模**（flat），装-拆/投-退跨记录配对走 T3 pairing（复合键支持），不再用条目内 T2 设想；
> 2. **G1/G2** ledger_view 行结构增设 `confirmed_fields`，**T3 默认取最后已确认版**；并存窗口内 **archive 目标=最后确认版**；
> 3. **F1** 测温 δt% **人工报数起步**（T1 band 表达），派生字段机制列 M2 扩展；
> 4. **A2/B1** aggregate 增 `reset_ref` 声明（**缺省不复位**）；允许事故开闸次数表定稿 **keyed-by-device** 形态。
> 受影响条款：§7.4 两行、§7.3 算子表、§8.1 archive 行、§8.3 行结构与两处口径、§11 M0、§12 待定项。

> **修订 B（2026-09-20）**：依据定稿 §4 粒度核对（一行=一票）与 M0 终审保留项②：**§7.4 两票 layout 由 `item_list` 修正为 `flat`**；连续性判定从 `ledger_view` 行 fields 取值（与 layout 无关），按票种分组、复位周期（工作票按月/操作票按年）随 GAP-E4 落声明。受影响条款：§7.4 两票行。

> **修订 C（2026-09-20，M2 收口同步）**：文法扩展终稿（PR #29）与 trend 口径拍板（#28 评论）并入：§7.3（`when` 值侧前缀微文法 + `kind="condition"` + `monotonic` 算子 + trend 口径）、§7.4 避雷器/跳闸行、§12 待定项收敛。受影响条款：§7.3 四行、§7.4 两行、§12 两条。

> **修订 D（2026-09-21，对接现场表拍板）**：蓄电池与现场录入表（钉钉 AI 表格「电压测量记录」）对接口径并入：① §7.2 样例 `dedupe_key` 纳入 `dc_system_id`（同日同站多组测试不再互撞业务判重）；② 电压带上限 2.25 → **2.30**（按现场口径；原值为主设样例）。受影响条款：§7.2 两行。

> **修订 E（2026-09-21，P1 改造定案）**：① 蓄电池类**免签**：`signature_slots` 置空；协议/生命周期/元校验三处容错——**无槽位声明**允许空签认 confirm（有槽位类型语义不变），提交后由壳层自动定稿。② `voltage_band` 默认值定为 **1.85–2.35**（2V 单体口径；12V 组 11.85–13.80 由部署配置注入覆盖），取代修订 D 的 2.30 过渡口径。受影响条款：§7.2 两行、§8.1 confirm/correct 行。

## 1. 背景与目标

围绕 **10 类**运行值班记录（断路器跳闸、避雷器动作、接地线装拆、两票登记、设备测温、绝缘测试、蓄电池电压测试、主变铁芯夹件电流测试、保护投退、防小动物检查），构建一个**全新、独立**的工具包。

本次只做**核心部分**：记录的装配、校验、周期/限值规则判定、趋势/预警判定、草稿生命周期管理。数据采集、文件存储、通知推送、报表渲染等 I/O 一律切出核心，通过**输入/输出接口协议**对接。

依据材料：
- 现场 Excel 模板（本包 10 类各一份，不随仓库分发；消防器材、保护定值压板两份模板随范围变更停用，不再作为声明依据）
- 《协合运维生产管理制度汇编 2023 版》三章 §5.3（运行记录 25 项口径的出处）

## 2. 范围

**本次做（核心）**
- 统一记录引擎：装配、字段校验、分层规则判定（T1/T2/T3，见 §7.3）、趋势判定、生命周期操作（§8）
- 周期台账探针 `cycle_status`：判定"到期/超期/漏做"（纯函数，见 §7.3）
- 告警指纹与处置回执校验（判定在核心，告警台账由壳层持有，见 §6.2）
- 声明式记录注册表（registry，TOML 载体）：**10 类**记录 = **10 份**声明数据
- 输入/输出协议：`RecordEnvelope` / `RecordResult`，随包分发 JSON Schema
- payload 校验 Schema 生成器：由 registry 声明生成（见 §7.1）

**本次不做（I/O 壳层，协议切出）**
- 数据采集（人报数入口、SCADA 导入、照片识别、旧 Excel 适配）
- 落地（Excel 渲染/写库/EAM/钉钉通知）
- 用户界面、确认交互、权限系统、**身份鉴权/防代签**（见铁律 7）
- **持久化**：生命周期账本、历史库、**告警台账**均由壳层持有；核心只对**传入的视图**做判断
- **触发**：定时探针的调度由壳层负责（核心只提供 `cycle_status` 纯函数）

## 3. 设计原则（铁律）

1. **核心不读时钟**：当前时间 `now` 由调用方放入信封传入。周期/趋势判定的"现在"永远来自输入。
2. **核心不碰文件与网络**：照片/附件只以 `attachments_ref`（引用 + 元数据）透传，核心不解析内容。
3. **核心无持久状态**：核心**无自己的存储**，但一切判定依赖的**事实由调用方作为输入传入**：当前记录及其 lifecycle/rev、生命周期账本视图（`ledger_view`，见 §8.3）、历史序列（`history`，§5.1）、**告警台账视图（`alarm_history`，§6.2）**、**外部基准数据（`baselines`，§5）**。核心只做纯判定。
4. **uid 唯一且稳定**：`record_uid` 在 create 时一次生成，**终身不变**，与内容无关；内容变动通过版本号 `rev` 递增表达，每版有自己的 `digest`。
5. **纯函数**：同输入必同输出，不依赖随机数。uid 中的顺序号 `create_seq` 由**调用方**依据账本视图生成并随信封传入（见 §5/§6.1）。
6. **建议与执行分离**：核心只输出 `actions_hint`（建议码 + 文本），永不执行任何动作；告警的处置走协议回流（§6.2）。
7. **签字责任在人**：签字栏以 `signature_slots` 声明为只读留白；校验层拒绝任何试图替人填签字值的输入；confirm 按声明槽位**逐人签认**（§8）。**防代签边界**：核心只校验 `confirmations` 的格式与槽位完整性，**"by 是否本人"由壳层鉴权保证**——协议层不承担身份认证；壳层可分步收集签认后一次提交。

## 4. 架构总览

```
┌─ I/O 壳层（本次不做，协议切出）─────────────────────────────────┐
│ 采集适配器（人报数bot / SCADA导出器 / 照片识别 / 旧Excel适配）    │
│ 落地适配器（Excel渲染 / EAM写入 / 钉钉待办 / 存储/账本/告警台账）│
│ 定时触发器（调 cycle_status 的 cron）｜身份鉴权（防代签）        │
└──────────────┬───────────────────────────────▲────────────────┘
               │ RecordEnvelope（输入协议）     │ RecordResult（输出协议）
┌──────────────▼───────────────────────────────┴────────────────┐
│              records-kit 核心（纯函数，无IO、无时钟）            │
│  validate → assemble → rules(T1/T2) → rules(T3,需ledger_view) │
│  → trend(需history) → lifecycle(操作+乐观锁) → cycle_status   │
│  → alarm(需alarm_history)                                     │
│  registry/：10份TOML声明 + protocol/：双协议Schema + payload   │
│             Schema生成器                                       │
└───────────────────────────────────────────────────────────────┘
```

环境假设：Python ≥ 3.12（开发机 3.14）。校验器为**手写实现**（依据本协议 Schema），不引入 `jsonschema` 等第三方运行时依赖；标准库仅使用 `tomllib`、`json`、`hashlib` 等。测试用 pytest。

### 组织方式（方案 A）

- **统一引擎 + 声明式注册表**：一套引擎处理全部 10 类记录；每类记录是一份 TOML 声明。
- **"零改动"的准确边界**：新增记录、增改字段、增改 T1 规则 = 只改声明，引擎零代码改动；**新增 T2/T3 规则形态需要扩引擎**（但对该形态之后的记录，仍只需声明）。
- 否决方案 B（每类记录一个独立模块）、方案 C（引擎+插件双轨）。

## 5. 输入协议 `RecordEnvelope`

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `protocol` / `protocol_version` | string | 是 | `"records-kit"` / `"1.5"`；不符 → `E_PROTOCOL` |
| `operation` | string | 是 | `create` / `confirm` / `return` / `correct` / `void` / `archive` / `alarm_ack` / `cycle_probe`，见 §8 |
| `record_type` | string | 是* | registry 键，10 个枚举之一（*`cycle_probe` 可省 = 对 registry 全部类型逐一探测） |
| `station` | object | 是 | `{station_id, station_name}` |
| `occurred_at` | string(RFC3339 带时区) | create/correct 必填 | 业务发生时间；confirm/return/void/archive/alarm_ack/cycle_probe 可省（操作对象时间以账本为准）；**correct 若修改业务时间，新值在本字段传入**；**晚于 `now`+容差（5min）→ `E_TIME_INVALID`** |
| `now` | string(RFC3339 带时区) | 是 | 调用方注入的当前时间；缺失 → `E_NOW_MISSING` |
| `create_seq` | integer | create 必填 | 同站同类型同分钟的创建序号（从 1 起），由壳层依据**含 voided 墓碑的完整账本视图**（§8.3）生成；核心对传入 `ledger_view` 复核，重复 → `E_DUP_UID`（双层防护，见 §6.1） |
| `subject` | object | confirm/return/correct/void/archive/alarm_ack 必填 | 被操作记录 `{record_uid, lifecycle, rev}`——调用方从账本取出传入，核心校验一致性；`lifecycle` 不符 → `E_STATE_ILLEGAL`；**`subject.rev` 即乐观锁版本号（信封不另设 rev 字段，只传 `subject.rev` 一处）**，与账本不符 → `E_REV_CONFLICT` |
| `payload` | object | create/correct/alarm_ack 必填 | 按 `record_type` 声明 schema；**未知字段默认拒绝**（`E_UNKNOWN_FIELD`；声明可 `extra="allow"` 放行）；数值 NaN/±Inf → `E_TYPE`；`alarm_ack` 时仅含 `alarm_disposition`（§6.2） |
| `history` | array | 趋势判定需要 | 见 §5.1 历史数据契约 |
| `ledger_view` | object | T3/操作/探针需要 | 账本视图，行结构见 §8.3 |
| `alarm_history` | array | 告警判定需要 | 同站同类型未消解告警 `[{fingerprint, first_seen_at, occur_count, last_status, escalated_count}]`（壳层从告警台账取出；**`escalated_count` 供壳层控制升级通知频率，核心判定不消费**） |
| `baselines` | array | external_baseline 规则需要 | 外部基准数据 `[{ref, kind, name, data}]`——如"允许事故开闸次数表"、调度定值单；壳层从外部源取出传入，`external_baseline:ref` 规则按 `ref` 取数；**引用的 ref 不在 `baselines` → `E_BASELINE_MISSING`**；数据形态随 M0 缺口清单定稿 |
| `links` | array | 否 | 成对关系 `[{"type","record_uid"}]`，type 须在声明 `link_types` 内；**`supersedes` 由核心在 correct 时自动写入，调用方自传 → `E_LINK_INVALID`** |
| `attachments_ref` | array | 按声明 | `[{kind, ref, note}]`；`kind` 枚举：`photo/file/csv/pdf`；声明了 `require_attachment` 的字段缺附件 → `E_REQUIRED` |
| `submitted_by` | string | create/correct 必填 | 提交人（≠签字人） |
| `confirmations` | array | confirm 必填 | `[{slot:"测试人", by:"李四", at:"..."}]`，按声明槽位逐个签认；防代签归壳层鉴权（铁律 7） |
| `return_reason` | string | return 必填 | 退回原因（留痕） |
| `archive_ref` / `archived_by` | string | archive 必填 | 归档引用（文件/EAM 单号）与归档人；归档时间取 `now` |
| `void_reason` / `voided_by` | string | void 必填 | 作废原因与作废人；作废时间取 `now` |

```jsonc
{
  "protocol": "records-kit", "protocol_version": "1.5",
  "operation": "create",
  "record_type": "battery_voltage_test",
  "station":   { "station_id": "ST001", "station_name": "XX风电场" },
  "occurred_at": "2026-09-17T10:00:00+08:00",
  "now":         "2026-09-17T15:00:00+08:00",
  "create_seq":  1,
  "payload":   { "test_kind": "定期", "float_voltage": 241.5,
                 "items": [ {"cell_no": 1, "voltage": 2.21} ] },
  "history":   [ ... ],
  "ledger_view": { ... },
  "alarm_history": [ ... ],
  "baselines": [ {"ref":"BL-001","kind":"count_table","name":"允许事故开闸次数表","data":{ ... }} ],
  "attachments_ref": [ {"kind":"photo","ref":"...","note":"电池柜全景"} ],
  "submitted_by": "张三"
}
```

### 5.1 历史数据契约

- **行结构**：`{"occurred_at", "digest", "lifecycle":"archived", "fields"}`。只有**归档**记录可入历史；非 archived 行 → `E_HISTORY`。
- **谁整理**：排序由**壳层**负责，核心校验时间**严格递增**，乱序 → `E_HISTORY`。**历史行去重键 = `occurred_at` + dedupe_key 各字段值**——这是给旧数据兜底的**宽松口径**，防误删合法旧行；新数据的唯一性由 create 业务判重（§8.2）事前保证，历史去重不承担判重职责。重复行由壳层去除后传入。
- **旧数据适配**：老 Excel/纸质补录由**壳层采集适配器**转换补齐字段后才可入历史；缺逐只明细的旧行允许只带聚合值，但声明 `trend.source` 取不到所需值时，该行跳过并计入 `evidence`。
- **窗口不足**：可用行数 < 声明 `window` 时，趋势**不静默通过**——输出 `verdict:"insufficient_history"`（level=info，evidence 注明"n/window"）。绝不输出趋势结论。

## 6. 输出协议 `RecordResult`

| 字段 | 说明 |
|---|---|
| `protocol` / `protocol_version` / `operation` | 回显 |
| `status` | `ok` \| `rejected`（校验不过，错误结构化，壳层无需 try/except） |
| `validation` | `{ok, errors:[{path, code, message}]}`，code 见 §9 |
| `record` | `{record_uid, rev, lifecycle, fields, digest}` |
| `record.record_uid` | `{station_id}-{record_type}-{yyyyMMdd}-{HHmm}-{create_seq}`，create 时生成、**终身不变** |
| `record.rev` / `record.digest` | 版本号（create=1，每次内容变更+1）与该版内容 sha256 指纹（规范序列化见 §6.3） |
| `record.signature_slots` | `[{slot, state:"pending"\|"signed", by?, at?}]`，只读留白，人签 |
| `rules` | 限值/派生结论 `[{rule_id, kind, tier, verdict, threshold, level, detail}]`；`verdict` 建议枚举 `pass \| violation`（level 区分 warn/alarm，随 M0 定稿）；**周期结论不走 rules——唯一出口为探针 `cycle` 字段（见 §7.3 归层豁免）** |
| `trend` | 趋势结论 `[{metric, verdict, level, evidence}]`；`verdict` 建议枚举 `stable \| dropping \| insufficient_history`（随 M0 定稿）；窗口不足 → `verdict:"insufficient_history"`（level=info，evidence 注明"n/window"） |
| `actions_hint` | 建议动作 `[{code, text}]`；`code` 必须在该记录声明的 `action_codes` 枚举内 → 否则 `E_ACTION_CODE` |
| `alarm_state` | `{fingerprint, state, suppressed}`。state 枚举 `new \| recurred \| escalated`；判定依据输入 `alarm_history`；**suppressed 判定（建议默认，待 M0 确认）：`state==recurred → suppressed=true`（重复告警抑制常规推送）；`new / escalated → suppressed=false`（escalated 走升级通知通道，不抑制）**；不传 alarm_history 时默认 `new`/`suppressed=false` 并在 evidence 注明 |
| `cycle` | 探针输出：`[{record_type, verdict:"due"\|"overdue"\|"missing", due_at, overdue_since, detail}]`；pairing 悬空配对在 `detail` 列出 |
| `digest` | 便捷冗余字段，值恒等于 `record.digest`（便于壳层单点取用） |

### 6.1 uid 与 digest 的分工

| 对象 | 生成时机 | 是否随内容变 | 用途 |
|---|---|---|---|
| `record_uid` | create 一次（站+类型+日期+HHmm+create_seq） | **永不** | 审计、归档挂接、跨记录引用、成对关系 |
| `digest` | 每版内容 | 每版一变 | 幂等判重、防篡改校验、更正链 |

uid 冲突防护（**双层**）：①壳层生成 `create_seq` 时对照完整账本（含 voided 墓碑）避撞；②核心对传入 `ledger_view` 复核，同分钟重复 → `E_DUP_UID`。两层缺一不可——核心复核不依赖壳层的正确性（呼应铁律 5）。

### 6.2 告警闭环

- **判定输入**：核心依据信封 `alarm_history` 判定。**判定顺序与计数语义**：`occur_count` 为该指纹**此前**的历史出现次数（不含本次）；判定按 `escalated` 优先——`occur_count + 1 ≥ escalate_after` → `escalated`；`occur_count ≥ 1` → `recurred`；否则 `new`。核心不记忆，事实全在输入。
- **处置回流**：独立轻量操作 `operation:"alarm_ack"`，`payload.alarm_disposition = {fingerprint, status:"acked"\|"resolved", by, at}`；处置事件由壳层记入告警台账。
- **抑制与升级**：`suppressed=true` 表示壳层不应再推送（同指纹未消解重复告警）；`escalated` 表示连续 `escalate_after` 次未处置，应升级通知。**suppressed 判定规则（建议默认，待 M0 确认）：`state==recurred → suppressed=true`；`new / escalated → suppressed=false`——升级通知独立于常规推送通道，不被抑制。`alarm_history.escalated_count` 为该指纹已连续升级次数，供壳层控制升级通知频率；核心判定不消费该字段。**

### 6.3 digest 规范序列化

`digest = sha256( canonical_json({station_id, record_type, occurred_at, schema_version, payload}) )`。canonical_json：键按字典序排序、无空白分隔、字符串 UTF-8 原样、时间保持 RFC3339 原文。摘要含站/类型/发生时间——不同日期的合法复测不会误判 `E_DUP_DIGEST`。

## 7. 记录注册表（registry，TOML 载体）

### 7.1 载体与权威划分

registry 声明为 **TOML 文件**（标准库 `tomllib` 读取，仍零依赖）：可注释、非程序员可改。权威划分：**registry 声明是记录 payload 的唯一权威**；envelope/result 协议 Schema 是手写权威文件；payload 的 JSON Schema 由 registry **生成**（M1 交付生成器），不作权威。

### 7.2 声明格式（以蓄电池为例，合法 TOML）

```toml
[meta]
record_type = "battery_voltage_test"
title = "蓄电池电压测试记录簿"
schema_version = "1.5"
layout = "item_list"                # flat(横表台账) | item_list(逐条测量)
dedupe_key = ["station", "occurred_day", "test_kind", "dc_system_id"]
link_types = ["retest_of"]
extra = "reject"                    # 未知字段处理：reject(默认) | allow
signature_slots = []                 # 免签类型置空：零签认 confirm（壳层自动定稿）
action_codes = ["MARK_LAGGING_CELL", "RETEST_CELL"]
escalate_after = 3                  # 同指纹告警未处置N次后升级

[[fields]]
key = "test_kind"
name = "测试性质"
type = "enum"
options = ["定期", "核对性放电"]
required = true

[[fields]]
key = "float_voltage"
name = "浮充电压"
type = "number"
unit = "V"
min = 0.0
max = 300.0

[items]
key_field = "cell_no"

[[items.fields]]
key = "cell_no"                     # key_field 引用的字段必须在 items.fields 中声明，
name = "电池编号"                    # 否则为坏声明（meta-test 拒绝加载）
type = "number"
decimals = 0
required = true

[[items.fields]]
key = "voltage"
name = "单体电压"
type = "number"
unit = "V"
min = 0.0
max = 15.0
required = true

[[items.fields]]
key = "lagging"
name = "是否落后"
type = "tri_bool"                   # 是/否/不适用

[[rules]]
id = "cycle_regular"
kind = "cycle"
tier = 1                            # 周期规则豁免归层，见 §7.3；不进 create/confirm 流水线
expr = "periodic:30d"
when = { test_kind = "定期" }

[[rules]]
id = "voltage_band"
kind = "limit"
tier = 1
target = "items.voltage"
expr = "band:1.85,2.35"             # 默认口径（2V 单体）；12V 组由部署配置注入覆盖
level = "warn"

[[rules]]
id = "lagging_detect"
kind = "limit"
tier = 2
target = "items.voltage"
expr = "deviation:mean,5%"
level = "alarm"
action = "MARK_LAGGING_CELL"

[[trend]]
metric = "cell_voltage_min"
source = "min(items.voltage)"       # 文法见 §7.3 trend 注
window = 6
drop_warn = 0.10
```

> TOML 语法约束：每对 key=value 独立成行（无分号同行）；记录级键必须位于任何 `[[...]]` 表数组头部**之前**（本例置于 `[meta]`）。meta-test 含 tomllib 解析校验。

### 7.3 字段类型与规则分层

**字段类型（7 种）**：`text` / `number{min,max,unit,decimals}`（显式拒 NaN/±Inf）/ `enum{options}` / `tri_bool{是|否|不适用}` / `bool` / `datetime` / `item_list{key_field,fields}`（仅顶层容器）。字段可声明 `require_attachment="photo"`。

**归层原则**：**按判定所需输入的最高层归层，不按算子**——`date_diff` 等算子可在 T2/T3 出现；用到 `ledger_view`/`links`/`baselines` 的规则一律 T3。

> **周期规则豁免**：`kind="cycle"` 的规则（`periodic` / `monthly` / `quarterly`）**不进入 create/confirm 的 rules 判定流水线**——周期判定必然读台账，与"T1=表内单字段"的输入定义天然不符。其判定唯一出口是探针 `cycle_status(ledger_view, now, registry)`，周期结论只出现在探针输出的 `cycle` 字段，`rules` 中永不含周期结论。声明中 `tier` 固定为 1，不因探针读台账而改 T3（归层原则约束的是 create/confirm 流水线内的规则）。

| 层 | 判定所需输入 | DSL 算子 | 示例 |
|---|---|---|---|
| **T1 表内数值** | 本记录 payload 单字段 | `periodic:Nd` `monthly` `quarterly`（三者仅供探针，见豁免注） `band:lo,hi` `gt/gte/lt/lte:v` | 电压带、浮充电压上限、蓄电池周期（periodic:30d，探针执行）；`quarterly` 现存 10 类暂无用例（算子保留，留给后续增补记录） |
| **T2 表内派生** | 本记录多字段/条目间 | `ratio:num,den,lo,hi` `deviation:baseline,pct` `date_diff:from,to,le:Nd`（同记录内两日期） `diff:a,b,ge/le:N`（差值） | 吸收比 R60s/R15s、落后电池偏差、接地线条目内装-拆间隔、测温温升/温差 |
| **T3 台账/跨记录** | `ledger_view` / `links` / `baselines` | `continuity` `pairing:type[,key=字段]`（配对键） `external_baseline:ref` `recovery_within:Nd` `date_diff`（跨记录） `aggregate:count_over,reset_ref`（聚合，**`reset_ref` 缺省不复位——A2/B1 已拍板**） `monotonic:op=ge[,key=字段]`（跨记录单调，2026-09-20 定） | 两票编号连续性、接地线配对（键=接地线编号）、保护投退配对（键=装置功能）、跳闸次数逼近允许值（聚合+基线） |

- **周期规则补"漏做"**：纯函数 `cycle_status(ledger_view, now, registry)` 输出 `due / overdue / missing`。"做过"按 §8.3 版本行语义判定——**记录非 voided 且存在 confirmed/archived 版本**（confirmed 后被作废的记录不算"做过"），correct 并存窗口不产生假"漏做"。触发调度归壳层。
- **`when` 条件**（2026-09-20 扩展定稿）：值侧前缀微文法（与 `expr` 同源）——`"not_in:A,B"` 等前缀算子表达否定/集合不匹配；新增条件型规则 `kind="condition"`（未命中输出 pass 条目）；条目字段沿用**存在量词**（`items.x` 表示「存在一条目满足」）。比较/日历窗口类（防小动物"雨季/秋季重点期"）保留后续排期。
- **`trend.source` 文法**：`agg(field_path)`，`agg ∈ {min, max, avg, count, last}`；`field_path` 须为声明中存在的字段（items 字段或顶层字段）；meta-test 校验 agg 白名单与字段存在性。**（trend 口径已定，2026-09-20）**：基准=**窗口首值**；相对比 `drop_warn`（零基准配可选 `drop_abs`，两者互斥则声明报错）；判定 `≥` 含等于（`dropping`=warn / `stable`=info）；可选 `group = "<顶层字段>"` 隔离序列（缺省不分组；主变 `group = "transformer_id"`）；窗口不足或取不到值 → `insufficient_history`（info，evidence 注明 n/window）。
- **pairing 配对键**：`pairing:type[,key=字段]` 按声明键配对（接地线→接地线编号；投退→装置功能）；**重复占用**（同键未拆/未退又新增）由 pairing 规则告警；悬空半边在探针 `cycle[].detail` 输出。
- **external_baseline 数据形态**：**已定**——keyed-by-device 数值型基线表（现存用户=跳闸次数表，A2/B1 拍板）。
- **`monotonic` 跨记录算子**（2026-09-20 定）：按月核对底数类单调性（避雷器计数器等）；`op=` 具名、缺省 `ge`（含等于，倒退→warn）；`key` 可选（同站同类型单序列可省略）；跳过 voided 行、取最后已确认版、排除自身 correct 链——六边界例为建设必测。

### 7.4 10 类记录清单

| record_type | 标题 | layout | 签字栏 | 关键规则（层） |
|---|---|---|---|---|
| breaker_trip_record | 断路器跳闸记录簿 | flat | 记录人 | 跳闸次数逼近允许开闸次数（T3, aggregate+external_baseline，`reset_ref` 缺省不复位） |
| surge_arrester_action_record | 避雷器动作记录簿 | flat | 记录人 | 非雷雨天气动作→warn（T1 条件 `non_storm_action`：when 值侧前缀）；计数器倒退→warn（T3 `counter_monotonic`，键=相别+安装位置） |
| grounding_wire_record | 接地线装拆记录簿 | flat（修订A：一行一动作） | 操作人、监护人 | 装拆配对（T3, pairing 键=接地线编号，含重复占用）；拆除超时（T3：link 工作票跨记录 date_diff）；装-拆间隔（T3 跨记录 date_diff，修订A：不再走条目内 T2） |
| two_ticket_ledger | 两票登记台账 | flat（修订B：一行=一票） | 许可人、签发人 | 编号连续性（T3, continuity：按票种分组、复位周期 工作票按月/操作票按年——E4；从 ledger_view 行 fields 取值） |
| infrared_thermography_record | 设备测温（红外）记录簿 | item_list | 免签 | 电流致热分级（**T2**, thermal_grade）；缺红外图提醒不拒单 |
| insulation_test_record | 绝缘测试记录簿 | flat | 试验人 | 吸收比带（T2, ratio）；阻值下限（T1）；周期（探针执行，§7.3 豁免注） |
| battery_voltage_test | 蓄电池电压测试记录簿 | item_list | 测试人 | 电压带（T1）；落后偏差（T2）；周期（探针执行，§7.3 豁免注） |
| transformer_core_clamp_current_record | 主变铁芯/夹件接地电流测试记录簿 | flat | 测试人 | 电流限值（T1）；增长趋势（trend） |
| protection_switch_record | 保护投退记录簿 | flat（修订A：一行一动作） | 操作人、监护人 | 投退配对（T3, pairing 复合键=[装置,功能]，含重复占用，修订A）；恢复时限（T3, recovery_within，跨记录配对语义） |
| rodent_proof_check_record | 防小动物检查记录簿 | item_list | 检查人 | 雨季/秋季重点期（T1 条件，when 日历窗口类进 M0 清单） |

fire_equipment_check_record（消防器材检查）、protection_setting_plate_check_record（保护定值压板检查）：**已移除**（业主决策），不在本包范围。`two_ticket_ledger` 只是登记台账，签发/许可/终结流程不在核心范围。各记录完整列级 schema 以 `记录表格/` 对应 Excel 模板为准，M1 起逐列落成 TOML 声明。

### 7.5 与制度 25 项的关系

制度三章 §5.3 列运行记录 25 项，本包 10 类是其中**本次 AI 化的子集**，两者无冲突：其余 15 项（含已移除的消防器材检查、保护定值压板检查，以及检修交代、万用钥匙、主变分接开关、厂级人员交接、资料借阅、工器具借用、技术培训、事故预想、反事故演习、运行日志、交接班、运行报表等）不在本包范围，日后按同一 registry 机制增补声明即可，引擎零改动。

### 7.6 成对关系

记录可携带 `links:[{type, record_uid}]`；声明 `link_types` 枚举：`retest_of`、`pairs_with`、`supersedes`、`references`。被引用记录由壳层放入 `ledger_view.linked_records`，T3 规则据此判定。缺引用/引用不存在 → `E_LINK_INVALID`。**`supersedes` 为核心保留类型：correct 时自动写入，调用方自传 → `E_LINK_INVALID`**。

## 8. 生命周期操作集

### 8.1 操作与状态（correct 双语义）

```
create ──► draft
draft  ──confirm(全员签齐)──► confirmed        （签齐冻结当版 fields+digest）
draft  ──return(退回留痕, rev 不变)──► draft    （修改走 correct(draft)，改完再 confirm）
draft  ──correct(编辑修订: rev+1, 仍 draft, 不加 supersedes)──► draft
draft  ──void(reason)──► voided
confirmed ──correct(定稿更正: rev+1 新版回 draft, 自动加 supersedes 指向前版,
                     原 confirmed 版并存, 新版须重新签认)──► draft(新版)
confirmed ──void(reason)──► voided
confirmed ──archive(by人, ref)──► archived     （archived 禁 void，冲正走壳层）

alarm_ack：适用于任意非 voided 记录；不改 fields、不加 rev（告警处置回执，§6.2）
```

| 操作 | 前置状态 | 关键约束 |
|---|---|---|
| `create` | — | 生成 uid（含 create_seq），rev=1；业务判重 → `E_DUP_KEY`；uid 重复 → `E_DUP_UID` |
| `confirm` | draft | `confirmations` 覆盖全部 `signature_slots`（**无签认槽位类型零签认即可**）；未签齐 → `E_STATE_ILLEGAL`；签齐冻结当版 fields+digest |
| `return` | draft | 退回重编：记 return_reason 留痕；rev 不变，配合 **correct(draft)** 完成修改后再 confirm |
| `correct` | **draft 或 confirmed（双语义）** | **draft 上调用**=编辑修订：rev+1、仍 draft、不加 supersedes（前版未定稿无可"更正"）；**confirmed 上调用**=定稿更正：原记录保持 confirmed，生成 rev+1、回 draft 的新版本（自动加 `supersedes` 指向前版），须重新签认（无槽位类型由壳层自动重新定稿）。**两种语义下若改动触及 `dedupe_key` 字段，须对非 voided 视图重判业务判重（排除自身），命中 → `E_DUP_KEY`** |
| `void` | draft / confirmed（archived 禁 void，走壳层冲正） | 记 void_reason、voided_by，voided_at 取 now；墓碑保留不可删，**uid/seq 永久占号** |
| `archive` | confirmed | 必带 `archive_ref`/`archived_by` → archived；**correct 并存窗口内的目标版本口径见 §8.3（随 M0 定）** |
| `alarm_ack` | 任意非 voided | 告警处置回执（§6.2），不改 fields、不加 rev |

### 8.2 并发与幂等

- **乐观锁**：`confirm`/`return`/`correct`/`void`/`archive` 均带 `subject.rev`；与账本不符 → `E_REV_CONFLICT`。
- **判重语义**（口径见 §8.3）：`dedupe_key` 业务判重（create 时；**correct 改动业务键字段时对该记录重判**——排除自身）；`digest` 精确判重（防重复提交同一版本；**排除自身 uid 的历史版本——correct 把内容改回与自身旧版完全一致不误判 `E_DUP_DIGEST`**）；**uid/create_seq 查重对含 voided 墓碑的完整视图**（墓碑占号，防审计链断裂）。补录/修订一律走 `correct`。

### 8.3 ledger_view 行结构（含墓碑、版本行语义）

```jsonc
{
  "confirmed_digests":   [ "sha256:..." ],
  "same_type_records": [                                      // 同站同类型，【含 voided 墓碑】
    { "record_uid",
      "lifecycle": "draft|confirmed|archived|voided",
      "rev", "occurred_at", "digest",
      "fields": { ... },                                      // 最新版 fields 全量（T3 取值源，
                                                              //  不经 dedupe_key 投影——两概念不耦合）
      "dedupe_key_values": { ... } }
  ],
  "linked_records": [
    { "record_uid", "lifecycle", "rev", "occurred_at",
      "digest", "fields" }
  ]
}
```

**版本行语义（voided 为吸收态，先于完成态判定）**：
- **一行一记录**（不是一行一版本）；`rev/digest/fields` 取**最新版**；
- `lifecycle` 判定顺序：**先判 voided**（任一版本被 void → 行为 `voided`，吸收态，不再回退）；**非 voided 记录**才按最高完成态取：存在 archived → `archived`；否则存在 confirmed → `confirmed`；否则 `draft`（correct 并存窗口仅影响非 voided 记录）；
- `cycle_status`"计入做过"按**记录非 voided 且存在 confirmed/archived 版本**判定——correct 并存窗口不产生假"漏做"，confirmed→voided 的记录不遮掩也不虚报；
- **两类查重视图口径**：业务判重（`E_DUP_KEY`）对**非 voided** 行（作废让位，voided 行不占业务键）；uid 查重（`E_DUP_UID`）对**含 voided 墓碑的完整视图**（墓碑占号）。

**correct 并存窗口取值口径（修订A 已拍板）**：定稿更正后、新版签认前，账本行的 `lifecycle`（最高完成态=confirmed）与 `rev/fields`（最新版=draft 新版）来自不同版本——T3 规则（continuity/pairing 等）若从该行 `fields` 取值，会消费**未签认**数据。**拍板结论：ledger_view 行结构增设 `confirmed_fields`（最后已确认版字段投影），T3 默认取 `confirmed_fields`；该窗口内 `archive` 目标=最后确认版**。**版本口径（2026-09-20 拍板·方案A）**：行结构另增**可选** `confirmed_rev`（最后已确认版 rev，与 `confirmed_fields` 配对）；`archive` 目标版优先取行内 `confirmed_rev`、缺失时兼容兜底 `row_rev−1`；`subject.rev` 校验放宽为 ∈{row_rev, confirmed_rev}——多轮 correct 下不再依赖位置推断。`aggregate:count_over` 增 `reset_ref` 声明（缺省不复位）；允许事故开闸次数表为 keyed-by-device 形态。

历史与告警同理：`history`（§5.1）、`alarm_history`（§6.2）、`baselines`（§5）行/数据结构已各自钉死或随 M0 定稿；meta-test 校验各视图完整性。

## 9. 错误码表

| 码 | 含义 | 码 | 含义 |
|---|---|---|---|
| `E_PROTOCOL` | 协议/schema 版本不符 | `E_SIGNATURE_VIOLATION` | 输入试图填签字栏 |
| `E_RECORD_TYPE` | 未知记录类型 | `E_STATE_ILLEGAL` | 非法流转/签认不全/告警处置对象非法 |
| `E_REQUIRED` | 必填缺失（含必附照片缺附件） | `E_DUP_KEY` | 业务判重命中 |
| `E_TYPE` | 类型不符（含 NaN/±Inf） | `E_DUP_DIGEST` | 同版本重复提交 |
| `E_RANGE` | 数值越界 | `E_DUP_UID` | uid 冲突（create_seq 重复/撞墓碑） |
| `E_ENUM` | 枚举值非法 | `E_REV_CONFLICT` | 版本冲突（乐观锁） |
| `E_UNKNOWN_FIELD` | payload 未知字段（extra=reject） | `E_LINK_INVALID` | 成对关系非法/引用不存在/自传 supersedes |
| `E_HISTORY` | 历史行不合规/乱序/非归档 | `E_ARCHIVE_REF` | 归档缺引用 |
| `E_ACTION_CODE` | 建议动作码不在声明枚举内 | `E_VOID_REASON` | 作废缺原因/缺作废人 |
| `E_TIME_INVALID` | occurred_at 晚于 now+容差 | `E_NOW_MISSING` | `now` 缺失（定义见 §5） |
| `E_BASELINE_MISSING` | external_baseline 引用的 ref 不在 baselines（数据形态随 M0 定稿） | | |

## 10. 测试策略

1. **协议合规**：输入/输出各一组 JSON 实例过 JSON Schema。
2. **声明自检（meta-test）**：TOML 声明必须能被 `tomllib` 解析；引用的字段（**含 items.key_field**）、规则 target、趋势 source（agg 白名单+字段存在性）、action_codes、link_types 必须真实存在且类型匹配；三个视图行结构完整；**坏声明反例——引用不存在字段/action_code 时引擎须拒绝加载**。
3. **引擎表驱动单测**：三种 DSL 层全部算子边界用例（含 date_diff 时区边界、ratio 除零、自然月月末、diff 差值）；状态机全流转表（含 correct 双语义（draft 编辑/confirmed 更正）、void 墓碑、archived 禁 void、并发 rev 冲突）；`cycle_status` 三态与 `now` 临界、voided 过滤、correct 并存窗口不误报、**confirmed→voided 不计入"做过"**；uid `create_seq` 冲突与**作废后序号重用被拒**；**correct 改业务键触发重判重**；digest 规范序列化（**含 correct 改回原内容不误判 E_DUP_DIGEST**）；告警判定顺序与 `occur_count` 边界、**suppressed 判定**；**alarm_ack 对 voided 记录拒绝**；T3 continuity 从行 fields 取值；**occurred_at 时间倒挂拒绝**；**baselines 缺 ref 拒绝（E_BASELINE_MISSING）**。
4. **黄金样本**：10 类记录各存「输入 → 期望输出」快照；历史窗口不足样本锁定 `insufficient_history` 降级。
5. **告警闭环专项**：`alarm_history` 驱动的 new/recurred/escalated 三态、`suppressed` 抑制、`alarm_ack` 处置回流、悬空配对探针可见。
6. **Schema 生成器自身测试**：生成产物对全部黄金样本输入通过手写校验器交叉验证。
7. **性能基准**：大账本/长历史全量传入一版基准（10 类 × 经年数据量级），预算值随 M0 缺口清单定。
8. **纯度断言**：静态扫描核心包，禁止 import 文件/网络/时间类模块。

## 11. 里程碑

| 阶段 | 交付 |
|---|---|
| **M0 前置三样** | ① 操作协议 = §5/§8（已定稿）；② 历史数据契约 = §5.1（已定稿）；③ 拿 **10 份** Excel 模板**逐列**核对，产出《DSL 缺口清单》——待定项包括：when 三类扩展（否定/比较/日历窗口）、`aggregate` 聚合算子语义（含复位参照）、pairing 配对键细节、external_baseline 数据形态（含 `E_BASELINE_MISSING` 触发细则）、**ledger_view 在 correct 并存窗口的取值口径（T3 消费何版 fields）与该窗口内 archive 目标版本口径**、**测温判级口径核对——若用 DL/T 664 相对温差 δt%（"差的比"），需组合公式算子或派生字段机制（diff/ratio 均表达不了）**、性能预算值——M1 的输入 |
| M1 | 协议 Schema + TOML registry 格式 + 蓄电池 1 类全链（create→confirm→correct→void→archive + T1/T2 + trend + 告警判定 + 黄金样本 + Schema 生成器及自身测试） |
| M2 | 其余 **9 份** TOML 声明 + T3 规则引擎（continuity/pairing/external_baseline/recovery_within/aggregate）+ `cycle_status` + 告警台账对接 |
| M3 | 错误码表冻结、《壳层对接说明》（协议两页文档） |

## 12. 待定项

- `cycle_baseline`（missing 判定起算日）按记录类型在声明中配置，具体取值待现场规程核对。
- 各限值业务数值（电压带、电流限值、周期天数、恢复时限）为声明数据，实施时以现场规程核对为准，本文档不给业务数值。
- archived 记录的冲正流程（壳层侧）不在本包范围，接口预留 `E_STATE_ILLEGAL` 语义。
- `aggregate` 复位参照语义、`external_baseline` 数据形态、`when` 扩展文法、性能预算值：**均已定（2026-09-20）**——`reset_ref` 缺省不复位；基线=keyed-by-device；`when`=值侧前缀微文法 + `kind="condition"`；性能 P1 p95≤200ms（M1 实测 ≈72ms）。
- **trend 判定算法**：**已定（2026-09-20）**——基准=窗口首值；相对比 `drop_warn`（零基准配 `drop_abs`，互斥报错）；`≥` 含等于；`group` 分组可选；见 §7.3 trend 注。（各 metric 阈值数值仍为现场口径。）
