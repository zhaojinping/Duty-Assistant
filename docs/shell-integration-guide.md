# 壳层对接说明（完整版）

- 版本：2026-09-20 · 对应协议 `records-kit` v1.5（main `e3ae21c8` 实测）
- 依据：`docs/design.md` §5（输入协议）、§6（输出协议）、§8（生命周期操作集）、§9（错误码表）、§10（测试策略）
- 关联：`docs/error-codes.md`（错误码与告警码总表，附录 A 摘要）、`README.md`（入口）
- 状态：**M3 交付物**。§8 示例均为**实测响应片段**（合成数据 ST001/XX风电场/张三/李四，非虚构）。

## 0. 读者与边界

- **读者**：壳层实现方（采集适配 / 鉴权 / 持久化 / 推送 / 调度）。
- **核心不做**：鉴权、持久化、时间注入、推送、调度、外部取数——**核心不记忆**，判定所需事实全部由输入给出。
- **壳层不做**：业务规则判定（规则唯一来自声明 + 核心）。
- 一句话边界：**壳层负责「事实的收集与落地」，核心负责「事实的判定」**。

## 1. 概述与职责

```
┌─ I/O 壳层 ──────────────────────────────────────────────┐
│ 采集适配器（人报数 / SCADA / 照片 / 旧Excel）             │
│ 鉴权（防代签）｜now 注入｜create_seq 生成｜账本/历史/告警/  │
│ 基线取数｜结果落地｜推送与升级｜周期调度｜归档留痕          │
└──────────────┬────────────────────────────▲────────────┘
               │ RecordEnvelope（输入）      │ RecordResult（输出）
┌──────────────▼────────────────────────────┴────────────┐
│ records-kit 核心（纯函数；无 IO、无时钟、无状态）          │
│ 校验 → 规则(T1/T2/T3) → 趋势 → 生命周期 → 探针 → 告警     │
└─────────────────────────────────────────────────────────┘
```

引擎入口：`records_kit.process(envelope, registry=None) -> dict`。同输入必同输出。

**结果状态四值（写入类操作，ActionResult）**：`not_sent | unknown | verified | not_applied`。**写入不等于完成**：`unknown` 只回查不重放；只有回读核对（readback）才推进为 `verified`。

## 2. 输入协议 `RecordEnvelope`

### 2.1 字段与必填矩阵

`●`=必填；`○`=视需（如触发对应判定/操作）；`·`=不适用。

| 字段 | create | confirm | return | correct | void | archive | alarm_ack | cycle_probe |
|---|---|---|---|---|---|---|---|---|
| `protocol` / `protocol_version`（`records-kit`/`1.5`） | ● | ● | ● | ● | ● | ● | ● | ● |
| `operation` | ● | ● | ● | ● | ● | ● | ● | ● |
| `record_type` | ● | ● | ● | ● | ● | ● | ● | ○（省=全类型逐一探测） |
| `station` `{station_id, station_name}` | ● | ● | ● | ● | ● | ● | ● | ● |
| `occurred_at`（RFC3339 带时区） | ● | ○ | ○ | ● | ○ | ○ | ○ | ○ |
| `now`（RFC3339 带时区，壳层注入） | ● | ● | ● | ● | ● | ● | ● | ● |
| `create_seq`（同站同类型同分钟序号，从 1 起） | ● | · | · | · | · | · | · | · |
| `subject` `{record_uid, lifecycle, rev}` | · | ● | ● | ● | ● | ● | ● | · |
| `payload` | ● | · | · | ● | · | · | ●（仅 `alarm_disposition`） | · |
| `history` | ○ | ○ | ○ | ○ | ○ | ○ | ○ | ○ |
| `ledger_view` | ○ | ○ | ○ | ○ | ○ | ○ | ○ | ○ |
| `alarm_history` | ○ | ○ | ○ | ○ | ○ | ○ | ○ | ○ |
| `baselines` | ○ | ○ | ○ | ○ | ○ | ○ | ○ | ○ |
| `links` | ○ | ○ | ○ | ○ | ○ | ○ | ○ | ○ |
| `attachments_ref` | ○（按声明 `require_attachment`） | · | · | ○ | · | · | · | · |
| `submitted_by` | ● | · | · | ● | · | · | · | · |
| `confirmations`（按声明槽位逐个签认） | · | ● | · | · | · | · | · | · |
| `return_reason` | · | · | ● | · | · | · | · | · |
| `archive_ref` / `archived_by` | · | · | · | · | · | ● | · | · |
| `void_reason` / `voided_by` | · | · | · | · | ● | · | · | · |

### 2.2 三条硬约束

1. **`now` 必须由壳层注入**（缺失 → `E_NOW_MISSING`）。核心不读时钟；周期/趋势/时间校验的"现在"永远来自输入。
2. **`create_seq` 由壳层对照「含 voided 墓碑的完整账本」生成**（避撞）；核心对传入 `ledger_view` **独立复核**，重复 → `E_DUP_UID`。两层缺一不可——核心不依赖壳层的正确性。
3. **`subject.rev` 是唯一乐观锁字段**（信封不另设 rev），与账本不符 → `E_REV_CONFLICT`。

### 2.3 最小示例（create）

```jsonc
{
  "protocol": "records-kit", "protocol_version": "1.5",
  "operation": "create", "record_type": "battery_voltage_test",
  "station": { "station_id": "ST001", "station_name": "XX风电场" },
  "occurred_at": "2026-09-17T10:00:00+08:00",
  "now":         "2026-09-17T15:00:00+08:00",
  "create_seq":  1,
  "payload": {
    "dc_system_id": "DC-001", "float_voltage": 241.5, "float_current": 12.0,
    "env_temp": 25.0, "test_kind": "定期",
    "items": [ { "cell_no": 1, "voltage": 2.21 }, { "cell_no": 2, "voltage": 1.95 } ]
  },
  "submitted_by": "张三"
}
```

## 3. 输出协议 `RecordResult`

### 3.1 字段表

| 字段 | 说明 |
|---|---|
| `protocol` / `protocol_version` / `operation` / `record_type` | 回显（`record_type` 回显=M1 协议补足项） |
| `status` | `ok` \| `rejected`（校验不过 → 结构化错误，壳层无需 try/except） |
| `validation` | `{ok, errors:[{path, code, message}]}`，码表见附录 A |
| `record` | `{record_uid, rev, lifecycle, fields, digest, signature_slots, links}`；**`links` 随结果回传=M1 协议补足项**（含 correct 自动写入的 `supersedes`），壳层据此持久化 |
| `rules` | `[{rule_id, kind, tier, verdict, threshold, level, detail}]`；`verdict ∈ pass/violation/skipped`；周期结论**不在此**（走 `cycle`） |
| `trend` | `[{metric, verdict, level, evidence}]`；`verdict ∈ stable/dropping/insufficient_history` |
| `actions_hint` | `[{code, text}]`；`code` 必在声明的 `action_codes` 内 |
| `alarm_state` | `{fingerprint, state, suppressed, evidence}`；state ∈ `new/recurred/escalated`；`evidence`=协议补足项（判定依据可追溯） |
| `cycle` | 探针输出 `[{record_type, verdict:"due"/"overdue"/"missing", due_at, overdue_since, detail}]` |
| `digest` | 便捷冗余，恒等于 `record.digest` |

### 3.2 uid 与 digest 分工

| 对象 | 生成时机 | 是否随内容变 | 用途 |
|---|---|---|---|
| `record_uid` | create 一次（`{station_id}-{record_type}-{yyyyMMdd}-{HHmm}-{create_seq}`） | **永不** | 审计、归档挂接、跨记录引用 |
| `digest` | 每版内容：`sha256(canonical_json({station_id, record_type, occurred_at, schema_version, payload}))` | 每版一变 | 幂等判重、防篡改、更正链 |

**digest 含站/类型/发生时间**——不同日期的合法复测不会误判 `E_DUP_DIGEST`。

### 3.3 账本视图行结构（`ledger_view`）

```jsonc
{
  "confirmed_digests": [ "sha256:…" ],
  "same_type_records": [            // 同站同类型，【含 voided 墓碑】
    { "record_uid", "lifecycle": "draft|confirmed|archived|voided",
      "rev", "occurred_at", "digest", "fields",           // 最新版全量
      "dedupe_key_values",
      "confirmed_fields",           // 最后已确认版字段投影（correct 并存窗口，修订A）
      "confirmed_rev" }             // 最后已确认版 rev（可选，2026-09-20 方案A；缺失时核心兼容兜底 row_rev−1）
  ],
  "linked_records": [ { "record_uid", "lifecycle", "rev", "occurred_at", "digest", "fields" } ]
}
```

**版本行语义**：一行一记录；`voided` 为吸收态（先于完成态判定）；`cycle_status`"计入做过"=非 voided 且存在 confirmed/archived 版本。

### 3.4 三处「不静默通过」（壳层必须显式展示，不得当通过处理）

1. 规则 `skipped`（操作数缺省/不可算）→ 人工复核提示；
2. 趋势 `insufficient_history`（窗口不足，`evidence` 注明 n/window）→ 不输出趋势结论；
3. 周期缺口（`cycle` 里 `overdue/missing`）→ 催办通道。

## 4. 生命周期操作集

### 4.1 状态机（correct 双语义）

```
create ─► draft
draft     ──confirm(签齐)──► confirmed        （冻结当版 fields+digest）
draft     ──return(留痕)──► draft             （rev 不变；修改走 correct）
draft     ──correct(编辑修订)──► draft         （rev+1，不加 supersedes）
draft/confirmed ──void──► voided              （墓碑永久占号）
confirmed ──correct(定稿更正)──► draft(新版)   （rev+1，自动加 supersedes 指向前版，须重签）
confirmed ──archive──► archived               （archived 禁 void，冲正走壳层）
alarm_ack：任意非 voided；不改 fields、不加 rev
```

### 4.2 关键约束（壳层视角）

- **乐观锁**：confirm/return/correct/void/archive 均带 `subject.rev`；陈旧 → `E_REV_CONFLICT`。**处置=重取账本后重放**（不是盲目重发）。
- **判重三层**：`dedupe_key` 业务判重（对**非 voided**行；correct 改业务键时重判、排除自身）；`digest` 精确判重（对 `confirmed_digests`；排除自身 uid 历史版本）；uid/seq 查重（对**含墓碑**完整视图）。补录/修订一律走 `correct`。
- **correct 并存窗口**（修订A + 方案A，2026-09-20）：定稿更正后、新版签认前——`lifecycle`=confirmed 但 `rev/fields` 为草稿新版；T3 规则**一律取 `confirmed_fields`**（最后已确认版）；该窗口内 `archive` 目标版=最后确认版，版本号优先读行内 **`confirmed_rev`**、缺失兜底 `row_rev−1`，`subject.rev ∈ {row_rev, confirmed_rev}`。壳层落地该窗口时**应把 `confirmed_rev` 一并写入行结构**（推荐，非强制）。
- **void 墓碑**：uid/seq 永久占号（防审计链断裂）；重发同 seq → `E_DUP_UID`（"撞上墓碑"）；作废后同业务键重录**允许**（作废让位）。

## 5. 引擎语义契约（壳层消费必读）

### 5.1 跨记录 `monotonic` 算子（六边界语义，2026-09-20 定）

用于按月核对底数类单调性（如避雷器计数器）。`op=` 具名、缺省 `ge`（**含等于**，倒退→warn）；`key` 可选（同站同类型单序列可省略）。**六条边界语义**（冻结口径，建设必测同款）：

1. **无候选前值** → `skipped`（不静默通过）；
2. 等于前值 → `pass`（`ge` 含等于；`gt` 才要求严格递增）；
3. 倒退 → `violation`（warn，`detail` 给出前值比对）；
4. `voided` 行 → **跳过**（不参与序列）；
5. 并存窗口取 **`confirmed_fields`**；**低版本不作前值**；**排除自身 correct 链**（改回/更正不自我比对）；
6. `key` 分组 → **按 key 各自独立成序列**，互不干扰。

### 5.2 `trend` 判定口径（2026-09-20 定）

- 基准=**窗口首值**；相对比 `drop_warn`（零基准配可选 `drop_abs`，两者互斥=声明报错）；
- 判定 `≥` 含等于（`dropping`=warn / `stable`=info）；
- 可选 `group = "<顶层字段>"` 隔离序列（缺省不分组；主变 `group = "transformer_id"`）；
- 窗口不足或取不到值 → `insufficient_history`（info，`evidence` 注明 n/window）。

### 5.3 `condition` 条件型规则（2026-09-20 定）

- `kind="condition"`：条件**未命中**时输出 `pass` 条目（显式留痕，不静默）；值侧前缀微文法（与 `expr` 同源）：`"not_in:A,B"` 等表达否定/集合不匹配；
- 条目字段沿用**存在量词**：`items.x` 表示「存在一条目满足」；
- 比较/日历窗口类（如防小动物"雨季/秋季重点期"）**保留后续排期**——当前不得依赖。

### 5.4 告警计数语义（§6.2）

`occur_count`=该指纹**此前**出现次数（不含本次）；判定按 `escalated` 优先：`occur_count+1 ≥ escalate_after → escalated`；`occur_count ≥ 1 → recurred`；否则 `new`。`suppressed`：`recurred → true`；`new/escalated → false`（升级通知不受抑制）。`alarm_history.escalated_count` 仅供壳层控频，核心不消费。

### 5.5 周期探针三态

`due`（到期应做）→ `overdue`（超期，`overdue_since` 落点）→ `missing`（漏做）；悬空配对半边在 `detail` 列出。触发调度归壳层。

## 6. 错误处理约定

### 6.1 两级判定树

```
status=rejected（记录级）──► 修正后重放（按码表处置，见 6.2）
status=ok：
  ├─ rules[].verdict=violation（规则级，warn/alarm）──► 出告警候选/展示，不拒单
  ├─ rules[].verdict=skipped ──► 人工复核提示（不静默）
  ├─ trend=insufficient_history ──► 明示窗口不足
  └─ cycle=overdue/missing ──► 催办通道
```

### 6.2 壳层最常撞的错误码与处置

| 码 | 场景 | 壳层处置 |
|---|---|---|
| `E_NOW_MISSING` | 忘传 `now` | 补注入重发（这是壳层 bug） |
| `E_REV_CONFLICT` | rev 陈旧 | **重取账本 → 重放**（禁止盲目重发） |
| `E_STATE_ILLEGAL` | 操作与 lifecycle 不符 | 回查账本确认现状（如已 confirmed 又 confirm） |
| `E_DUP_UID` | seq 重复/撞墓碑 | 对照含墓碑账本重生成 seq |
| `E_DUP_KEY` | 业务判重命中 | 走 `correct` 修订既有记录（勿新建） |
| `E_DUP_DIGEST` | 同版本重复提交 | 幂等：视为已受理，回读既有记录 |
| `E_TIME_INVALID` | occurred_at 晚于 now+5min | 校时/修数据后重发 |
| `E_REQUIRED` | 必填缺失（含必附照片缺附件） | 补齐；附件见 §7 职责清单 |
| `E_PROTOCOL` | 协议/版本不符 | 对齐 `protocol_version=1.5` |
| `E_BASELINE_MISSING` | external_baseline 引用的 ref 整表缺失 | 补取数；单键缺失=规则级 skipped |

### 6.3 重试与幂等口径

按 `digest`/`record_uid` 双层防重：重试前先回查（`unknown` 只回查、不重放）；只有 `not_sent`/`not_applied` 且 recoverable 才重放。

## 7. 壳层职责清单（逐项落地）

- [ ] 采集适配：Excel/纸质旧数据 → payload 字段映射与补齐（§5.1 旧数据适配）
- [ ] 身份与签字：`submitted_by` 与 `confirmations` 槽位鉴权（**防代签在壳层**）
- [ ] `now` 注入与时钟口径（RFC3339 带时区）
- [ ] `create_seq` 生成（对照**含墓碑**完整账本）
- [ ] `history` 整理：排序（严格递增）+ 按 `occurred_at` + `dedupe_key` 去重；**只有 archived 行可入**
- [ ] `ledger_view` / `alarm_history` / `baselines` 取数与注入（含 `confirmed_fields` / `confirmed_rev` 的落地）
- [ ] 附件登记（`attachments_ref`，按声明 `require_attachment`）
- [ ] 结果落地：生命周期状态与版本行写入（`record.links` 含 supersedes 需持久化）
- [ ] 告警闭环：推送、抑制（`suppressed`）、升级（`escalate_after`）、`alarm_ack` 回流
- [ ] 周期调度：探针调用与「漏做」提醒
- [ ] 归档落库与审计留痕（`archive_ref` 必填）

## 8. 端到端接入示例（实测口径）

> 素材：main `e3ae21c8` 全量实跑，合成数据；`fields` 块在节选中以下划线标注省略，其余值为**原样实测**。
> 另注：`lagging_detect` 的 detail 文案（"偏差 6.25% ≤ 5.0%"）为现行实现的展示文本，判为越界系偏差绝对值超阈值——**文本口径小瑕疵已知，不影响判定**，后续修订统一。

### 8.1 create → confirm → cycle_probe（最小闭环）

**① create 成功**（节选）：

```jsonc
{
  "protocol": "records-kit", "protocol_version": "1.5", "operation": "create",
  "record_type": "battery_voltage_test", "status": "ok",
  "validation": { "ok": true, "errors": [] },
  "record": {
    "record_uid": "ST001-battery_voltage_test-20260917-1000-1",
    "rev": 1, "lifecycle": "draft",
    "fields": "___（与输入 payload 一致，节选省略）___",
    "digest": "sha256:0c5e36a732963e8ae1ecd5889645c848637fd2bbc96833d1b7ddbef7d691562b",
    "signature_slots": [ { "slot": "测试人", "state": "pending" } ],
    "links": []
  },
  "rules": [
    { "rule_id": "voltage_band", "kind": "limit", "tier": 1, "verdict": "pass",
      "threshold": "band:2.00,2.25", "level": "info",
      "detail": "条目 cell_no=1 取值 2.21，带 [2.0, 2.25]：通过" },
    { "rule_id": "voltage_band", "kind": "limit", "tier": 1, "verdict": "violation",
      "threshold": "band:2.00,2.25", "level": "warn",
      "detail": "条目 cell_no=2 取值 1.95，带 [2.0, 2.25]：越界" },
    { "rule_id": "lagging_detect", "kind": "limit", "tier": 2, "verdict": "violation",
      "threshold": "deviation:mean,5%", "level": "alarm",
      "detail": "条目 cell_no=1 取值 2.21，基准 mean=2.08，偏差 6.25% ≤ 5.0%：越界" }
  ],
  "trend": [ { "metric": "cell_voltage_min", "verdict": "insufficient_history", "level": "info",
    "evidence": "1/6（窗口不足，按 §5.1 不输出结论）" } ],
  "actions_hint": [ { "code": "MARK_LAGGING_CELL", "text": "lagging_detect：…" } ],
  "alarm_state": { "fingerprint": "fp:13da55796c851990ca69f2aebb43a3f2", "state": "new",
    "suppressed": false,
    "evidence": "命中规则 lagging_detect（items[0].cell_no=1）；occur_count=0，escalate_after=3；未传 alarm_history，按首次出现判定（§6.2）" },
  "cycle": null,
  "digest": "sha256:0c5e36a732963e8ae1ecd5889645c848637fd2bbc96833d1b7ddbef7d691562b"
}
```

**② confirm 成功**（节选）：`record.lifecycle=confirmed`，签名槽 `{"slot":"测试人","state":"signed","by":"李四","at":"…"}`；冻结当版 fields+digest。

**③ cycle_probe（现状 now=2026-09-17）**：`cycle:[{"record_type":"battery_voltage_test","verdict":"due","due_at":"2026-10-17T10:00:00+08:00","overdue_since":null,"detail":"周期 periodic:30d；最后完成 2026-09-17T10:00:00+08:00；下次应做 2026-10-17T10:00:00+08:00"}]`

**④ 时间后移（now=2026-10-20）**：`verdict:"overdue"`，`overdue_since:"2026-10-17T10:00:00+08:00"`——同一账本、不同 `now`，三态随输入推进（核心不读时钟的直观验证）。

### 8.2 correct 双语义

**① confirmed 上 correct（定稿更正）**（节选）：

```jsonc
"record": {
  "record_uid": "ST001-battery_voltage_test-20260917-1000-1",
  "rev": 2, "lifecycle": "draft",
  "digest": "sha256:bb531e08c1b02d808920ca8fead9b94018444cfd45ca26309b36d1304ff15707",
  "signature_slots": [ { "slot": "测试人", "state": "pending" } ],
  "links": [ { "type": "supersedes", "record_uid": "ST001-battery_voltage_test-20260917-1000-1" } ]
},
"alarm_state": { "fingerprint": null, "state": null, "suppressed": false,
  "evidence": "本记录无 alarm 级违规，不产生告警指纹" }
```

→ rev+1、回 draft、**`supersedes` 自动写入**、须重签；变更后 `lagging_detect` 由 violation 转 pass（规则随新版重判）。

**② draft 上 correct（编辑修订）**：`rev:3`、仍 draft、**`links:[]`（不加 supersedes）**——双语义的判别性差异。

### 8.3 判重与墓碑（实测拒绝响应）

**① 同 seq 重发** → `status:"rejected"`：

```jsonc
{ "path": "create_seq", "code": "E_DUP_UID",
  "message": "uid 冲突：同分钟 create_seq 重复或撞上墓碑" }
```

**② 新 seq 同内容** → `status:"rejected"`：

```jsonc
{ "path": "payload", "code": "E_DUP_KEY",
  "message": "业务判重命中（station+occurred_day+test_kind）" }
```

（业务判重先于 digest 判重命中；幂等重放场景按 `confirmed_digests` 走 `E_DUP_DIGEST`。）

**③ 乐观锁陈旧 rev**（账本 rev=3，请求 rev=2）→ `status:"rejected"`：

```jsonc
{ "path": "subject.rev", "code": "E_REV_CONFLICT",
  "message": "乐观锁冲突：账本 rev=3，请求 rev=2" }
```

**④ void 成功**：`record.lifecycle=voided`；**⑤ void 后重发 seq→仍 `E_DUP_UID`**（墓碑占号）。

## 9. 版本与兼容

- `protocol_version`（信封协议版本）与 registry `schema_version`（各类声明版本）独立演进；不符 → `E_PROTOCOL`。
- 变更流程：新枚举/字段一律经 PR 修订协议 Schema 与本文档（**改实现必改本表**，见 `docs/error-codes.md` §5）。
- **兼容期约定**：`confirmed_rev` 为**可选**字段（2026-09-20 起）——壳层写入后，核心优先消费；未写入时核心兜底 `row_rev−1`（单轮 correct 等价）。宽限期后转为推荐必填，另行通告。

## 10. 不做什么（防越界实现）

- 不在壳层复刻规则/阈值判定（唯一来源=声明+核心）；
- 不拿 `detail`/`message` 文案做机器判断（以 `code`/`verdict` 为准）；
- 不在核心侧存任何状态、不依赖核心"记住"上一笔；
- 不把 `skipped`/`insufficient_history` 当通过；不在 `unknown` 状态盲目重放；
- 不替人签字（防代签在壳层鉴权，核心只校验格式与槽位完整性）。

## 附录 A. 错误码全表（21 项，冻结）

`E_PROTOCOL` `E_RECORD_TYPE` `E_REQUIRED` `E_TYPE` `E_RANGE` `E_ENUM` `E_UNKNOWN_FIELD` `E_HISTORY` `E_ACTION_CODE` `E_TIME_INVALID` `E_NOW_MISSING` `E_SIGNATURE_VIOLATION` `E_STATE_ILLEGAL` `E_DUP_KEY` `E_DUP_DIGEST` `E_DUP_UID` `E_REV_CONFLICT` `E_LINK_INVALID` `E_ARCHIVE_REF` `E_VOID_REASON` `E_BASELINE_MISSING`

（详解见 `docs/error-codes.md` §1。声明加载期错误按 `path+message` 定位、拒绝加载。）

## 附录 B. 术语表

| 术语 | 含义 |
|---|---|
| 信封 / RecordEnvelope | 输入协议整体；携带判定所需全部事实 |
| 账本视图 / ledger_view | 壳层持有的生命周期台账投影（含墓碑行） |
| 墓碑 | voided 行；uid/seq 永久占号 |
| 并存窗口 | confirmed 记录已存在新版草稿（correct 后、重签前）的窗口期 |
| confirmed_fields / confirmed_rev | 最后已确认版的字段投影 / 版本号（并存窗口口径） |
| 指纹 / fingerprint | 告警去重键（站+类型+规则派生）；同指纹一条台账 |
| 三态（探针） | due / overdue / missing |
| 三处不静默 | skipped / insufficient_history / 周期缺口 |

## 附录 C. 待现场与待启用清单（M3 冻结口径）

| 项 | 现状 | 处置 |
|---|---|---|
| 红外测温三规则（温升/温差等级/复测联动） | 声明 rules=0 未落（限值待现场 F2/F3） | **显式记待现场**：现场规程核对后落声明，引擎零改动 |
| GAP-H5 条目级附件绑定（`require_attachment` item_key） | 未启用（附件为记录级数组） | **显式记待启用**：机制设计=attachments_ref 增可选 `item_key`（如 cell_no/spot）；随现场接口定义后启用 |
| `when` 比较/日历窗口类（如"雨季/秋季重点期"） | 未实现（值侧前缀+condition 已可用） | 保留后续排期，当前不得依赖 |
| 各业务数值（限值/周期/Nd/阈值） | 声明数据待现场核对 | 以现场规程为准，文档不给业务数值 |
| `confirmed_rev` 宽限期 | 可选字段 | 见 §9 兼容期约定 |

## 附录 D. 变更记录

| 日期 | 变更 |
|---|---|
| 2026-09-20 | 完整版落地（M3）：全章节展开 + 三条实测链路 + 引擎语义契约（monotonic 六边界/trend 口径/condition）+ 待现场清单。素材 main `e3ae21c8` |
| 2026-09-20（同日） | 框架稿（#31 设计包）→ 本完整版；依据 M2 定版结论（注记一/二）补录 §5、附录 C |
