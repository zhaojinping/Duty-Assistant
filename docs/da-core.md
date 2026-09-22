# da_core —— Duty-Assistant 核心系统（架构与运维速览）

> 定位：`records_kit`（纯引擎）之上的第一个正式壳层——账本、编排、触达、投影。
> 依赖铁律：`records_kit` 永不 import `da_core`；反向允许（CI 检查守住）。

## 模块图

```
输入 ─ intake（提交解析/信封组装）／group_intake（群消息接入口：降级文本 → 核心）
        ↓
   engine_gate（注册表注入：阈值按口径覆盖）→ records_kit.process（纯引擎判定）
        ↓
   service（create → 落账 → 零签认自动定稿；correct / void）
        ↕
   ledger（SQLite 权威账本：记录/版本/判重/告警/任务/回执/审计/配置）
        ↕
   scheduler（周期扫描：滚动/月锚到期 + tasks 台账对账）
        ↓
   escalation（升级链六级，幂等）→ outbox（群消息 + 待办 + DM，回执留痕）
        ／entry_card（入口卡投放：X-APM 机器人；级别 1 随发，按周期去重）
        ↓
   table_projection（钉钉 AI 表格逐行投影，单一写者；含更正/作废行同步）
   ／reconcile（表↔账本对账，只读）／reporting（月报与按时率 + 收口推送，只读）
```

## CLI 一览（`python -m da_core.cli <命令>`）

| 命令 | 用途 |
|---|---|
| `submit` | 提交（`--dispatch` 写投影 / `--dry-run` 演练 / `--remark-tag` 联调标记） |
| `correct` | 定稿更正（全量替换 payload，免签自动重新定稿；默认同步表格行） |
| `void` | 作废（墓碑占号，判重键释放；默认同步表格状态列） |
| `scan [--sync] [--now]` | 周期扫描：只读视图 / 落 tasks 台账（`--now` 模拟时刻） |
| `escalate [--now] [--dry-run]` | 按升级链执行触达（幂等，每级每渠道一次） |
| `notify [--task-id] [--dry-run]` | 按任务当前状态触达（联调/运维入口） |
| `contacts --set role=target` | 触达目标：reminder_group / reminder_assignee / reminder_escalate |
| `defer --task-id --until --reason --approved-by` | 延期（班长批），有效截止顺延 |
| `reconcile` | 表↔账本对账（只读；无账本UID的行按 legacy 计数） |
| `cycle --set-baseline/--set-cycle-days/--set-mode/--set-anchor-day` | 周期配置（`rolling_days` 滚动天数 / `monthly_day` 月锚，每月锚日缺省 15） |
| `pull-group [--limit] [--dry-run] [--no-reply]` | 群消息接入口：拉群 → 降级提交入库 → 群内回执（游标防重放） |
| `pull-remote --base-url [--token] [--dry-run]` | 拉取式输入源：从应用侧只读接口拉取新提交（内网零暴露；幂等=client_submission_id；契约见 `docs/input-channel-v2.md`） |
| `report [--month YYYY-MM] [--json]` | 月报：按时率/测量/更正作废/触达（只读） |
| `inspect` | 账本概览（各表计数） |

## 日常跑（无人值守）

```bash
uv run python scripts/da_daily.py --db <ledger.sqlite> \
    --station-id ST001 --station-name XX风电场 [--dry-run]
```

= `scan --sync → escalate（级别 1 随发入口卡）→ 月报收口推送（每月 16–18 日）→ reconcile`；建议每日 08:30（cron / 任务计划）。

## 账本

- 落点：`$DA_DATA_DIR/ledger.sqlite`（本部署：`D:\Hermes\DutyPlus\data\ledger.sqlite`，WAL 模式）。
- 13 表：records / record_versions / dedupe_index / confirmations / alarms / tasks /
  task_events / deferrals / config_thresholds / config_contacts / config_params /
  ops_audit / intake_receipts。
- 一切写操作留审计（ops_audit）；触达留回执（task_events，含失败与重试计数）。
- tasks 状态机：open / overdue → done（按时）、done_late（迟到）、**rebased**（周期配置变更滚期产物，达成统计不计入，勿混入考核口径）。

## 触达语义（outbox）

- 双轨：群消息 + 待办（待办常驻）；升级级别 ≥4 追加对升级对象的 DM。
- 幂等键 `(task_id, level, channel)`；同键重试上限 3 次；失败如实留痕。
- 级别：1 前3天 / 2 当天 / 3 逾期+1 / 4 逾期+3（升班长）/ 5 逾期+7（升班长）/
  6 月底；（0 保留给联调/手工）。
- **入口卡**（entry_card，级别 1 随发）：X-APM 机器人经开放平台 `createAndDeliver`
  投模板互动卡（🔋蓄电池电压测量，[点击录入][查看记录]）；按钮跳转=模板变量，
  随卡携带候选参数集；按「到期周期」去重（`config_params.entry_card_last_due`）；
  卡片失败不阻断文字触达，提醒期内次日重试（daily 幂等框架）。

## 群消息接入口（P3）

- 拉取「APM测试」群最近消息（每 5 分钟，Hermes cron `da_group_watch.py`；异常才提醒）。
- 解析：兼容原样换行与**钉钉 Markdown 规整**（`#` 行会被转成 `**加粗**`、换行折叠为空格）两种形态；判定标记＝文本含「蓄电池电压测量数据」。
- 摄入：幂等键 = 群消息 messageId；结构性问题（如缺必填字段）转 `rejected` 摘要 → 群内回执提示补齐；游标存 `config_params.group_intake`。
- 契约（给录入应用侧）见 `docs/entry-app-contract.md`。

## 月报（P3）与收口推送

```bash
uv run python -m da_core.cli report --db <ledger.sqlite> --month 2026-09 [--json]
```
- 任务按 `due_at` 归月（按时/迟到/逾期/在办 + 按时率）；测量按 `occurred_at` 归月；
- 更正（record_versions.op=correct）/ 作废（voided_at）/ 催办触达（task_events.sent_at）/ 延期计数；
- 全程只读，不产生任何写操作。
- **收口推送**（`reporting.push_monthly_report`）：每月 16–18 日把当月月报正文 +
  入口卡发到提醒群；按自然月幂等（`config_params.report_last_pushed`）。

## 配置（以账本 config_* 表为运行期权威）

- `config_thresholds`：`2V单体` 1.85–2.35 / `12V电池` 11.85–13.80（可配）。
- `config_params.battery_cycle`：`cycle_days=30`、`baseline`、`cycle_mode`
  （`rolling_days` 滚动 / `monthly_day` 月锚）、`anchor_day`（锚日，缺省 15）。
- `config_params.entry_card_last_due` / `report_last_pushed`：入口卡/月报推送幂等标记。
- `config_contacts`：触达目标三角色（见上）。

## 纪律（改代码前先读）

1. **单一写者**：钉钉表格只由核心投影写入；人工改数走正式通道（更正）。
2. **引擎零 IO**：判定只在 `records_kit`；时钟/网络/数据库只在 `da_core`。
3. **对外写后必核验**：dws 写操作的返回只是回执；状态以独立回读为准。
4. **未知状态先回查不重放**：超时/断连后先对账，再决定是否重试。
5. 新增表格列 / 改阈值：先改配置与文档，再动数据。
