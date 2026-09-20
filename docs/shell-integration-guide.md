# 壳层对接说明（框架，M3 交付物骨架）

> 卡：#28 ②（M3 设计包）。**本文件是章节框架与必写要点**；正文（含实测示例与错误码冻结评审）在 M2 定版后依 #5 落地为 M3 交付物。
> 依据：`docs/design.md` §5（输入协议）、§6（输出协议）、§8（生命周期操作集）、§9（错误码表）、§10（测试策略）；`AGENTS.md`（协作约束）。

## 0. 读者与边界（开篇先写清）

- **读者**：壳层实现方（采集适配 / 鉴权 / 持久化 / 推送 / 调度）。
- **核心不做**：鉴权、持久化、时间注入、推送、调度、外部取数——**核心不记忆**，判定所需事实全部由输入给出。
- **壳层不做**：业务规则判定（规则唯一来自声明 + 核心）。
- 一句话边界：**壳层负责「事实的收集与落地」，核心负责「事实的判定」**。

## 1. 章节结构（交付物目录）

| 节 | 内容 | 必备要素 |
|---|---|---|
| 1 | 概述与边界 | 上述 §0；一张「谁负责什么」图（文本版即可） |
| 2 | 输入协议 `RecordEnvelope` | 字段表 + **必填矩阵（operation × 字段）** + 三例最小示例 |
| 3 | 输出协议 `RecordResult` | 字段表 + `lifecycle`/`rev`/`digest` 语义 + 账本视图行结构 |
| 4 | 生命周期操作集 | 操作 × 前置状态矩阵；`correct` 双语义；`void` 墓碑；乐观锁 |
| 5 | 错误处理约定 | 错误码表（冻结版）+ **重试与幂等**口径 + 记录级拒绝 vs 规则级 `skipped` |
| 6 | 壳层职责清单 | 逐项可勾选（见 §3） |
| 7 | 端到端接入示例 | 三条实测链路（§4） |
| 8 | 版本与兼容 | `protocol_version` / registry `schema_version` 变更流程 |
| 9 | 不做什么 | 显式边界声明（防越界实现） |
| 10 | 附录 | 错误码全表、术语表、变更记录 |

## 2. 必写要点

### 2.1 输入协议（对应主设 §5）

- 字段：`protocol` / `protocol_version` / `operation` / `record_type` / `station` / `occurred_at` / `now` / `create_seq` / `subject` / `payload` / `history` / `ledger_view` / `alarm_history` / `baselines` / `links` / `attachments_ref` / `submitted_by` / `confirmations` / `return_reason` / `archive_ref`+`archived_by` / `void_reason`+`voided_by`。
- **必填矩阵**（按 `operation` 逐列勾）：`create` / `confirm` / `return` / `correct` / `void` / `archive` / `alarm_ack` / `cycle_probe`——须给成表格，不留文字描述。
- 三条硬约束须显著标注：
  1. `now` **必须由壳层注入**（缺失 → `E_NOW_MISSING`）；
  2. `create_seq` 由壳层依**含 voided 墓碑的完整账本**生成，核心复核（重复 → `E_DUP_UID`）；
  3. `subject.rev` 是唯一乐观锁字段，与账本不符 → `E_REV_CONFLICT`。

### 2.2 输出协议（对应主设 §6）

- 必写：`record_uid`（永不随内容变）与 `digest`（每版一变）的**分工表**；`rules` / `trend` / `cycle` / `alarm_state` / `actions` 各数组的形状与 `verdict` 枚举。
- 会话重点：三处「不静默通过」——规则 `skipped`、趋势 `insufficient_history`、周期 `missing`，须写明**壳层应如何展示**（不得当通过处理）。

### 2.3 错误处理与幂等

- 错误码表（主设 §9，21 项）冻结为附录全表，正文只列**壳层最常撞的 8 个**并给处置动作；
- 重试口径：按 `digest`/`record_uid` 双层防重；`E_REV_CONFLICT` 的处置是**重取账本后重放**，不是盲目重发（呼应 AGENTS.md「外部写入受理不明时先回查」）；
- **记录级拒绝**（如 `E_BASELINE_MISSING`、`E_DUP_KEY`）与**规则级提示**（`skipped`）的区别要给判定树。

## 3. 壳层职责清单（交付物正文按此逐项展开，可勾选）

- [ ] 采集适配：Excel/纸质旧数据 → payload 字段映射与补齐（§5.1 旧数据适配）
- [ ] 身份与签字：`submitted_by` 与 `confirmations` 槽位鉴权（防代签在壳层）
- [ ] `now` 注入与时钟口径（RFC3339 带时区）
- [ ] `create_seq` 生成（对照含墓碑账本）
- [ ] `history` 整理：排序（严格递增）+ 按 `occurred_at` + `dedupe_key` 去重
- [ ] `ledger_view` / `alarm_history` / `baselines` 取数与注入
- [ ] 附件登记（`attachments_ref`，按声明 `require_attachment`）
- [ ] 结果落地：生命周期状态与版本行写入（`confirmed_fields` 供 T3 消费）
- [ ] 告警闭环：推送、抑制（`suppressed`）、升级（`escalate_after`）、`alarm_ack` 回流
- [ ] 周期调度：探针调用与「漏做」提醒
- [ ] 归档落库与审计留痕

## 4. 端到端接入示例（M3 正文必须实测后写入）

1. `create → confirm → cycle_probe`：最小闭环（含真实响应片段，脱敏）；
2. `correct` 双语义：draft 编辑 vs confirmed 更正（含 `supersedes` 自动写入、`rev` 变化）；
3. `void` 墓碑 + 重发同键：`E_DUP_UID` / `E_DUP_DIGEST` 的实测响应。

## 5. 待办（M3 内）

- [ ] 正文撰写（依赖 M2 定版：算子与声明形态冻结）；
- [ ] §9 错误码表**冻结评审**（M3 目标之一）；
- [ ] 示例实测（命令 + 真实响应片段，脱敏后入库）；
- [ ] 两页精简版（供外部实现方快速接入）；
- [ ] 与 `README.md` 的入口互链。

## 6. 本框架的自查口径

- 框架文件，不含未定稿的业务数值与真实数据；字段与错误码均引自主设既有条款（§5/§6/§8/§9）。
- 示例待 M2 定版后以实测响应补入，**不得先行虚构响应体**。
