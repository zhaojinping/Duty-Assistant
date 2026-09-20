# 错误码与告警码总表（冻结版）

- 依据：`src/records_kit` 代码实现（main 现状）+ 9 类已转正声明汇总；关联 #38 / #5（《壳层对接说明》附录接入点）
- **状态：M3 冻结（2026-09-20）**——21 项错误码与本文词汇表随《壳层对接说明》（`docs/shell-integration-guide.md` 附录 A）冻结；**变更须 PR 同步双文档**。
- 口径：**写入不等于完成**；错误只进 `validation.errors`（`{path, code, message}`），不静默通过；结果状态只有 `not_sent | unknown | verified | not_applied` 四值。
- 层级：**记录级**（validation.errors 拒绝该次操作）/ **规则级**（rules 条目 verdict=violation/warn，不拒单、出告警候选）。

## 1. 协议层错误码（E_*，Schema/校验器/lifecycle 共用）

| 码 | 含义 | 触发条件 | 层级 | 示例 |
|---|---|---|---|---|
| E_PROTOCOL | 协议不符 | 缺 protocol 字段/协议名不对 | 记录级 | envelope 缺 `"protocol": "records-kit"` |
| E_RECORD_TYPE | 未知记录类型 | record_type 不在 registry | 记录级 | `record_type="meter"` |
| E_REQUIRED | 必填缺失 | 必填字段/参数缺省 | 记录级 | 测温缺 env_temp |
| E_TYPE | 类型不符 | 字段/视图类型错误 | 记录级 | number 字段传字符串 |
| E_RANGE | 数值越界 | 超出声明 min/max | 记录级 | 电压 260 > max 15（蓄电池条目） |
| E_ENUM | 枚举外取值 | 不在声明 options | 记录级 | 投/退写成"退出" |
| E_UNKNOWN_FIELD | 未知字段 | extra=reject 时未声明字段 | 记录级 | payload 多出 `note2` |
| E_HISTORY | 历史行非法 | 非 archived 入历史/时间不递增/去重键重复/digest 缺失 | 记录级 | history 两行同 occurred_at |
| E_ACTION_CODE | 动作码未声明 | 规则 action 不在 action_codes | 记录级（声明加载/判定期） | 规则 action=NOPE |
| E_TIME_INVALID | 时间非法 | 非 RFC3339/时区缺失/晚于 now+5min | 记录级 | occurred_at=明天 |
| E_NOW_MISSING | 缺 now | 需要时钟的操作未传 now | 记录级 | cycle_probe 无 now |
| E_SIGNATURE_VIOLATION | 签字违规 | 签名槽被 payload 占用/已签再改 | 记录级 | payload 含"记录人"键 |
| E_STATE_ILLEGAL | 状态机非法 | 操作与 lifecycle 不匹配 | 记录级 | confirm 一条 confirmed 记录 |
| E_DUP_KEY | 业务判重命中 | dedupe_key 与台账已确认记录重复 | 记录级 | 同站同日同测点重复登记 |
| E_DUP_DIGEST | 内容判重命中 | digest 已在 confirmed_digests | 记录级 | 重放同一笔 |
| E_DUP_UID | UID 重复 | record_uid 已存在 | 记录级 | 重放同 uid |
| E_REV_CONFLICT | 乐观锁冲突 | rev 不等于台账现行版 | 记录级 | 按 rev=1 改 rev=2 的行 |
| E_LINK_INVALID | 关联非法 | link 类型未声明/目标缺失/指向非法 | 记录级 | references 指向不存在票 |
| E_ARCHIVE_REF | 归档引用缺失 | archive 缺 archive_of/来源不对 | 记录级 | archive 无 subject |
| E_VOID_REASON | 作废缺理由 | void 未带 reason | 记录级 | void 空 reason |
| E_BASELINE_MISSING | 基线缺失 | external_baseline/aggregate 的 ref 整表缺失（§13 B1：整表=记录级；单键缺失=规则级 skipped/提示） | 记录级 | baselines 无 allowance 表 |

## 2. 引擎层产出词汇（rules/lifecycle/trend/cycle/alarm）

### 2.1 规则判定（T1/T2/T3 通用条目）

| 词汇 | 含义 | 层级 |
|---|---|---|
| verdict=pass | 通过 | info |
| verdict=violation | 违规（附 level=warn/alarm；alarm 出告警候选） | warn/alarm |
| verdict=skipped | 操作数缺省/不可算（**不静默通过**，人工复核） | info |
| tier | 1=T1 单字段、2=T2 表内派生、3=T3 台账/跨记录 | — |
| kind | limit / cycle / pairing / continuity / aggregate / recovery_within（T3 走 limit+tier=3，见 docs/dsl/README 冻结文法） | — |

### 2.2 写入结果状态（ActionResult，写入不等于完成）

| 状态 | 含义 | 可重放 | 层级 |
|---|---|---|---|
| not_sent | 未发出 | 是（recoverable 时） | — |
| unknown | 结果未知（**只回查，不重放**） | 否 | — |
| verified | 已回读核对（必须有 readback） | — | 仅此推进状态 |
| not_applied | 确认未落地 | 是（recoverable 时） | — |

### 2.3 告警台账（alarm_history）

| 词汇 | 含义 |
|---|---|
| fingerprint | 告警指纹（站+类型+规则键派生），同指纹只一条台账 |
| last_status / escalated_count | 最近状态与升级次数；alarm_ack 后抑制，escalate_after 天未处理升级 |

### 2.4 周期探针（cycle_status）

| 词汇 | 含义 |
|---|---|
| verdict=due | 到期未办（含 pairing 悬空半边的 detail 落点） |
| verdict=ok | 窗口内已办 |
| lifecycle 缺失/配置缺失 | 明细列出，不猜测 |

## 3. registry/meta 校验（声明加载期，DeclarationError）

声明坏即**拒绝加载**（无 code，按 `path + message` 定位）。触发类目：

- meta 缺失/非法（record_type、schema_version、layout ∈ flat/item_list、extra ∈ reject/allow、escalate_after ≥2、signature_slots 非空且不与字段重名、action_codes 为字符串数组）
- dedupe_key 引用不存在字段（允许信封派生伪键 station/occurred_day/occurred_at）
- 字段：type 必须在 7 种内、enum 无 options/重复取值、min>max、decimals/require_attachment 只允许对应类型、key 重复
- items：item_list 必须声明 [items]、key_field 未声明、min_items <1
- 规则：id 重复/kind 不在 RULE_KINDS/limit 的 tier ∉ (1,2)/T3 规则 tier≠3、T3 各算子参数文法（pairing/continuity/count_over/recovery_within）、expr 未知算子、target 引用不存在字段、action 不在 action_codes
- trend：source 必须引用存在字段、agg 不在白名单、window/drop_warn 非法

## 4. action_codes 白名单（9 类已转正声明汇总；红外类随 #34 合入补录）

| 声明 | action_codes | 触发场景 |
|---|---|---|
| battery_voltage_test | MARK_LAGGING_CELL / RETEST_CELL | 落后电池标记 / 不合格复测 |
| breaker_trip_record | TRACK_TRIP_COUNT / REPORT_BASELINE_APPROACH | 累计跳闸跟踪 / 接近允许开闸次数 |
| grounding_wire_record | WARN_WIRE_DANGLING / WARN_WIRE_REOCCUPY / WARN_REMOVE_OVERDUE | 悬空半边 / 重复占用 / 拆除超时（待现场 Nd） |
| insulation_test_record | MARK_UNQUALIFIED / ARRANGE_RETEST | 不合格标记 / 安排复测 |
| protection_switch_record | WARN_SWITCH_DANGLING / WARN_SWITCH_REOCCUPY / WARN_RECOVER_OVERDUE | 悬空投 / 重复投 / 恢复超时（待现场 Nd） |
| rodent_proof_check_record | REPORT_ISSUE / URGE_BLOCK_HOLE | 发现问题 / 催封堵 |
| surge_arrester_action_record | REPORT_NON_STORM_ACTION / CHECK_COUNTER_ABNORMAL | 非雷雨动作上报 / 计数器异常核对 |
| transformer_core_clamp_current_record | CHECK_CORE_CURRENT_ABNORMAL / ARRANGE_OUTAGE_INSPECTION | 铁芯/夹件电流异常 / 安排停电检查 |
| two_ticket_ledger | WARN_TICKET_NO_GAP / WARN_TICKET_UNCLOSED | 票号跳号 / 票未终结 |
| infrared_thermography_record | REPORT_DEFECT / REMEASURE_SPOT / ARRANGE_THERMOGRAPHY | 缺陷上报 / 测点复测 / 安排红外普测（#34 合入后补录） |

## 5. 边界声明

- 本表为 **L1/L2 代码事实汇编**，不代表业务验收通过；现场数值（限值/周期/Nd）仍待规程核对（§12）。

- 后续新增错误码/动作码一律经 PR 修订本表，保持与实现同步（改实现必改本表）。
