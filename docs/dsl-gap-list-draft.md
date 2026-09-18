# DSL 缺口清单（初稿）

- 日期：2026-09-18
- 状态：**M0-③ 初稿**（未提交，待主控审批后推送）
- 依据：`docs/design.md`（审定稿，协议 1.5）§7.2 声明格式、§7.3 字段类型与 T1/T2/T3 算子、§7.4 记录清单、§11 M0 待定项
- 核对对象：`D:\集成测试\记录表格\` 下 **10 份** Excel 模板（消防器材、保护定值压板两份已停用，未读）
- 提取方式：`scripts/m0_extract_struct.py` + `scripts/m0_extract_extra.py`（openpyxl 3.1.5，逐份读出 sheet/表头行/列名/合并单元格/数据有效性/批注/脚注；结构快照存 `scripts/m0/excel_struct.json`）

## 0. 总体事实与通用映射约定

**模板共性**（10 份一致）：

- 每份仅 1 个 sheet；第 1 行大标题（全列合并）；第 2~3 行为"站名/年月"信息行 + 列表头（**表头行=第 3 行**）；第 4 行起为空白数据行；末行（第 25 行，蓄电池第 27 行）为合并单元格"填写说明"脚注。
- **全部为空白模板**：无示例数据行、无数据有效性下拉、无批注。判定需求只能从**列名（含括号内选项提示）+ 脚注制度要求**归纳，业务数值一律待现场规程核对（呼应主设 §12）。
- 蓄电池表为**表单式**布局（顶部记录级字段 + 下方逐只条目，序号 1–20 预填、注明"按实际数量增删"）；其余 9 份为**一行一事件**的台账式。
- 模板不含真实场站名/人名（空表）；本文示例统一用 `ST001` / `张三` / `李四` 占位。

**通用映射约定**（不是缺口，是列→协议的归位规则，M1 落 TOML 时统一执行）：

| Excel 通用列 | 归位 | 说明 |
|---|---|---|
| 站名（表头行/信息行） | `envelope.station` | 不进 payload，避免双源 |
| 各表首列时间（跳闸时间/动作时间/时间/试验日期/测试日期/检查日期/测温时间） | `envelope.occurred_at`（RFC3339 带时区） | 不在 payload 重复声明 |
| 记录人/试验人/测试人/检查人/操作人/监护人/签发人/许可人 | `meta.signature_slots` | 只读留白，人签（铁律 7），不进 payload |
| "值班次/年月"信息行 | 不落 payload | 由 `occurred_at` 派生，壳层渲染用 |

---

## 1. breaker_trip_record 断路器跳闸记录簿（flat）

**表格结构**：sheet `断路器跳闸记录簿`，A1:H25，表头第 3 行，**8 列**，数据行 4–24 空。脚注：*"断路器每次跳闸均应记录；接近'允许事故开闸次数'时重点跟踪。"*

**逐列映射**：

| # | Excel 列 | 草案 key | 类型 | required | 判定需求/说明 | 结论 |
|---|---|---|---|---|---|---|
| 1 | 跳闸时间 | —（occurred_at） | datetime | 是 | 信封承载 | 约定 |
| 2 | 站名 | —（envelope.station） | — | — | 信封承载 | 约定 |
| 3 | 开关编号 | `breaker_id` | text | 是 | T3 aggregate 分组键 + 基线查询键 | OK |
| 4 | 保护动作情况 | `protection_action` | text | 是 | 自由描述（可能多段） | OK |
| 5 | 重合闸情况 | `reclose_status` | enum | 是 | 建议枚举化（成功/不成功/未动作/拒动），模板未给选项 → 选项待现场定 | 决策项 |
| 6 | 累计跳闸次数 | `trip_count` | number(整数,≥1) | 是 | 人工累计值 vs 核心 aggregate 逐年累计的**双源**；"接近允许次数"需比较+基线 | **GAP-A2 / GAP-B1 / GAP-H6** |
| 7 | 天气及异常情况 | `weather_note` | text | 否 | 纯描述，无规则 | OK |
| 8 | 记录人 | —（signature_slots=["记录人"]） | — | — | 签字槽 | 约定 |

**草案 TOML 骨架**：

```toml
[meta]
record_type = "breaker_trip_record"
title = "断路器跳闸记录簿"
layout = "flat"
dedupe_key = ["station", "occurred_minute", "breaker_id"]   # 同分钟同开关多次跳闸需人工区分，暂按分钟+开关
signature_slots = ["记录人"]

[[fields]]  # breaker_id / protection_action / reclose_status / trip_count / weather_note
```

**规则草案**：`trip_approaching`：kind=`aggregate`（T3），按 `breaker_id` 累计非 voided 跳闸数，对照 `external_baseline`（允许事故开闸次数表）输出逼近告警——aggregate 语义（复位参照：允许次数是否"按检修周期复位"）与基线形态均未定 → GAP-A2/B1。
**周期**：无（事件触发型）。

---

## 2. surge_arrester_action_record 避雷器动作记录簿（flat）

**表格结构**：sheet `避雷器动作记录簿`，A1:G25，表头第 3 行，**7 列**。脚注：*"避雷器动作后记录；每月核对计数器底数；非雷雨天气动作应检查上报。"*

**逐列映射**：

| # | Excel 列 | 草案 key | 类型 | required | 判定需求/说明 | 结论 |
|---|---|---|---|---|---|---|
| 1 | 动作时间 | —（occurred_at） | datetime | 是 | 信封承载 | 约定 |
| 2 | 相别(A/B/C) | `phase` | enum{A,B,C} | 是 | 模板自带选项 | OK |
| 3 | 安装位置(线路侧/母线侧) | `install_location` | enum{线路侧,母线侧} | 是 | 模板自带选项 | OK |
| 4 | 计数器累计读数 | `counter_reading` | number(整数,≥0) | 是 | "每月核对底数"：跨记录**单调不减**核对（本次≥上次，倒退→warn） | **GAP-A3**（跨记录比较/单调算子缺失） |
| 5 | 动作电流(kA) | `action_current` | number, unit=kA, min=0 | 否 | 限值数值待现场（§12） | OK |
| 6 | 天气(雷雨等) | `weather` | enum{雷雨,…} | 是 | "非雷雨动作→warn"需 **when 否定/集合不匹配**；选项集待现场定 | **GAP-C1** |
| 7 | 记录人 | —（signature_slots） | — | — | 签字槽 | 约定 |

**规则草案**：`non_storm_action`：kind=limit(T1)，`when weather != 雷雨` → warn——现行 `when` 仅等值匹配，**否定缺失** → GAP-C1。
**周期**：`monthly`（计数器核对，探针执行 ✓ 算子已有；missing 起算日 `cycle_baseline` 待定，§12）。

---

## 3. grounding_wire_record 接地线装拆记录簿

**表格结构**：sheet `接地线装拆记录簿`，A1:G25，表头第 3 行，**7 列**。脚注：*"交接班须交代使用中接地线及装设地点；工作票终结后应及时拆除。"*

**结构性发现（本表最大问题）**：Excel 是**一行一动作**台账，`类别(装设/拆除)` 列区分两种事件；而主设 §7.4 给的 layout=`item_list` 且关键规则写"条目内装-拆间隔（T2, date_diff）"——**与表格形态冲突**。两种建模：

- **案 A（并案 item_list）**：一记录=一次装设+对应拆除（条目含装设时间/拆除时间两列，拆除可空直至回填）。契合"条目内 T2"，但拆除滞后数日须 correct 回填、生命周期摩擦大；"使用中"状态靠 draft 可见性表达。
- **案 B（每动作一记录 flat + T3 pairing）**：一行=一记录，`action` enum 区分装/拆，pairing 键=接地线编号 跨记录配对；装-拆间隔变为跨记录 date_diff（T3）。

**建议案 B**（贴合 Excel 形态与配对/重复占用语义），主设 §7.4 该行的 layout 与"条目内 T2"表述需同步修订 → **GAP-E1**。

**逐列映射（按案 B）**：

| # | Excel 列 | 草案 key | 类型 | required | 判定需求/说明 | 结论 |
|---|---|---|---|---|---|---|
| 1 | 类别(装设/拆除) | `action` | enum{装设,拆除} | 是 | pairing 类型判别 | OK |
| 2 | 时间 | —（occurred_at） | datetime | 是 | 信封承载 | 约定 |
| 3 | 接地线编号 | `wire_id` | text | 是 | **pairing 配对键** | OK（键细节 GAP-E 组） |
| 4 | 装设地点(设备编号) | `location` | text | 装设=是/拆除=建议携带 | "使用中接地线"台账展示；拆除行是否强制=装设一致值 → 条件必填细节 | **GAP-H2**（条件必填） |
| 5 | 命令来源(令号/票号) | `order_ref` | text | 是 | links `references`→two_ticket_ledger；"拆除超时"= 票终结时间（在**另一记录 fields** 内）与拆除时间的跨记录 date_diff | **GAP-E5**（跨记录取数链路） |
| 6 | 操作人 | —（signature_slots） | — | — | 槽位 1 | 约定 |
| 7 | 监护人 | —（signature_slots） | — | — | 槽位 2 | 约定 |

**规则草案**：`wire_pairing`：T3 `pairing:grounding,key=wire_id`（含重复占用告警、悬空半边进探针 detail）✓ 算子已有；`remove_overdue`：T3 跨记录 date_diff（票终结→拆除 ≤Nd），**取数对象经 linked_records.fields** 的细节未定 → GAP-E5。

---

## 4. two_ticket_ledger 两票（工作票/操作票）登记记录簿

**表格结构**：sheet `两票（工作票／操作票）登记记录簿`，A1:I25，表头第 3 行，**9 列**。脚注：*"操作票按年编号装订、工作票按月编号装订，封面统计数字，保存期一年。"*

**粒度核对**：一行=一票 → 建议一票一记录、layout 修为 `flat`（主设 §7.4 标 item_list，与表格"一行一票"不符，**连续性从 ledger_view 行 fields 取值，与 layout 无关**）→ 并入 GAP-E4 说明。

**逐列映射**：

| # | Excel 列 | 草案 key | 类型 | required | 判定需求/说明 | 结论 |
|---|---|---|---|---|---|---|
| 1 | 票种 | `ticket_kind` | enum{工作票,操作票} | 是 | continuity 分组键；**复位周期不同**（工作票按月/操作票按年） | OK（算子细节 **GAP-E4**） |
| 2 | 编号 | `ticket_no` | text | 是 | 连续性判定需解析编号内序号段（格式如 `2026-09-001`，现场待核对） | **GAP-E4** |
| 3 | 工作(操作)任务 | `task` | text | 是 | — | OK |
| 4 | 工作(操作)负责人 | `person_in_charge` | text | 是 | 业务人名（非签字人），入 payload | OK |
| 5 | 许可人 | —（signature_slots） | — | — | 槽位 1 | 约定 |
| 6 | 签发人 | —（signature_slots） | — | — | 槽位 2 | 约定 |
| 7 | 开始时间 | `start_at` | datetime | 是 | — | OK |
| 8 | 结束时间 | `end_at` | datetime | **否（未终结可空）** | 票未终结时空 → 现行类型系统无"条件可空+回填"语义；T2 `date_diff:start_at,end_at,le:Nd` 校验时长（含倒挂拒绝） | **GAP-H1** |
| 9 | 合格率评价 | `evaluation` | 待定 | 否 | **列名歧义**：单票评价（enum 合格/不合格）还是月度统计合格率（T3 aggregate 派生，不应人工填）？需现场核对 | **GAP-A4** |

> 注：本表无独立"登记时间"列——`occurred_at` 建议口径=**壳层登记时刻**（create 时刻），业务时间用顶层 `start_at` 表达，二者分离；若现场以开始时间为准再调整。

**规则草案**：`no_continuity`：T3 `continuity`（从 §8.3 行 fields 取 `ticket_no`）——需支持**按 `ticket_kind` 分组 + 差异复位周期（月/年）+ 编号段解析** → GAP-E4。

---

## 5. infrared_thermography_record 设备测温（红外检测）记录簿

**表格结构**：sheet `设备测温（红外检测）记录簿`，A1:K25，表头第 3 行，**11 列**。脚注：*"定期测温；过负荷、高温天气、新投设备应加强测温。"*

**粒度核对**：一行=一测点；同一次测温多测点 → 一记录多 items，**layout=item_list 合理**。记录级（顶层）：测温时间、环境温度、负荷电流、记录人；条目级（items）：设备/测点/温度类/判定/仪器。

**逐列映射**：

| # | Excel 列 | 草案 key（层级） | 类型 | required | 判定需求/说明 | 结论 |
|---|---|---|---|---|---|---|
| 1 | 测温时间 | 顶层 —（occurred_at） | datetime | 是 | 信封承载 | 约定 |
| 2 | 设备名称 | items.`device_name` | text | 是 | 条目归属 | OK |
| 3 | 测点部位 | items.`spot` | text | 是 | **每测点必附红外图**：`require_attachment="photo"` | **GAP-H5**（附件绑定到条目） |
| 4 | 环境温度(℃) | 顶层 `env_temp` | number, unit=℃ | 是 | δt 公式的 T0 | OK |
| 5 | 负荷电流(A) | 顶层 `load_current` | number, unit=A | 否 | DL/T 664 分级与负荷率相关 | OK |
| 6 | 实测温度(℃) | items.`measured_temp` | number, unit=℃ | 是 | — | OK |
| 7 | 相间温差(K) | items.`phase_temp_diff` | number, unit=K | 否 | =同设备**正常相**实测 − 本相：需"条目间选基准相"语义，非简单 diff:a,b | **GAP-F2** |
| 8 | 相对温差δt | items.`delta_t` | number(%) | 否 | **δt=(T_hot−T_norm)/(T_hot−T0)×100%** 组合公式，ratio/diff 均不可表达（人工报数则 T1 band 可降级表达） | **GAP-F1**（主设预警项，实测列真实存在） |
| 9 | 缺陷判定 | items.`defect_grade` | enum{一般,严重,危急,…} | 否 | 分级阈值来源（DL/T 664 表 or 现场规程）未定 | **GAP-F3** |
| 10 | 仪器编号 | items.`instrument_id` | text | 是 | — | OK |
| 11 | 记录人 | —（signature_slots=["记录人"]） | — | — | 签字槽 | 约定 |

**规则草案**：`temp_rise`：T2 diff（实测−环境）✓；`delta_t_check`：T1 band（若人工报数）或派生字段机制（若核心计算）→ GAP-F1；`defect_grade_check`：分级规则 → GAP-F3；脚注"高温天气加强测温"→ when 日历/比较窗口（弱需求）→ GAP-C3。
**周期**：`periodic:Nd`（天数待现场）。

---

## 6. insulation_test_record 绝缘测试记录簿（flat）

**表格结构**：sheet `绝缘测试记录簿`，A1:K25，表头第 3 行，**11 列**。脚注：*"按规程周期进行绝缘测试并记录。"*

**逐列映射**：

| # | Excel 列 | 草案 key | 类型 | required | 判定需求/说明 | 结论 |
|---|---|---|---|---|---|---|
| 1 | 试验日期 | —（occurred_at） | datetime | 是 | 信封承载 | 约定 |
| 2 | 设备名称 | `device_name` | text | 是 | dedupe_key 成员 | OK |
| 3 | 试验性质(定期/检修后) | `test_kind` | enum{定期,检修后} | 是 | 模板自带选项；dedupe_key 成员 | OK |
| 4 | R15s(MΩ) | `r15` | number, unit=MΩ, min=0 | 是 | ratio 分母（除零边界已有测试要求） | OK |
| 5 | R60s(MΩ) | `r60` | number, unit=MΩ, min=0 | 是 | — | OK |
| 6 | 吸收比 | `absorption_ratio` | number | 否 | =r60/r15，T2 `ratio:r60,r15,lo,hi` ✓ 可派生；模板列人工填报 → **双源**（填报 vs 计算一致性核对算子缺失） | **GAP-H6** |
| 7 | 极化指数 | `polarization_index` | number | 否 | =R10min/R60s，但 **R10min 未设列**，核心无法派生，只能人工报数（T1 band） | 决策项（或增列 `r600`） |
| 8 | 仪器及电压等级 | `instrument_and_voltage` | text | 是 | **复合列**（仪器名+电压等级 kV）→ 建议拆 `instrument_id` + `test_voltage_kv` | **GAP-H3** |
| 9 | 环境温湿度 | `env_th` | text | 是 | **复合列**（如"25℃/60%RH"）→ 建议拆 `env_temp`(number) + `env_humidity`(number)；绝缘电阻按温度换算判限则需公式换算能力（开放） | **GAP-H3** |
| 10 | 结论 | `conclusion` | enum{合格,不合格,…} | 是 | 选项待现场定 | 决策项 |
| 11 | 试验人 | —（signature_slots=["试验人"]） | — | — | 签字槽 | 约定 |

**规则草案**：`absorption_band`：T2 ratio ✓；`r60_lower`：T1 `gte:下限`（数值待现场 §12）。
**周期**：`periodic:Nd`（探针执行，§7.3 豁免注 ✓；天数与 `cycle_baseline` 待定）。

---

## 7. battery_voltage_test 蓄电池电压测试记录簿（item_list，表单式）

**表格结构**：sheet `蓄电池电压测试记录簿`，A1:E27。**表单式**：第 2–3 行为记录级字段（测试日期/直流系统编号/浮充电压/浮充电流/环境温度/测试性质/测试人，共 7 项），第 4 行为条目表头，第 5–26 行预填序号 1–20（脚注："按实际数量增删"），**逻辑字段 7+5=12**。脚注：*"AI 自动做电压一致性分析并标记落后电池、给更换建议。"*

**逐列映射**：

| # | 位置 | Excel 列 | 草案 key | 类型 | required | 判定需求/说明 | 结论 |
|---|---|---|---|---|---|---|---|
| 1 | 顶部 | 测试日期 | —（occurred_at） | datetime | 是 | 信封承载 | 约定 |
| 2 | 顶部 | 直流系统编号 | `dc_system_id` | text | 是 | — | OK |
| 3 | 顶部 | 浮充电压(V) | `float_voltage` | number, unit=V, min=0, max=300 | 是 | T1 上限（主设样例原样） | OK |
| 4 | 顶部 | 浮充电流(A) | `float_current` | number, unit=A, min=0 | 否 | 主设 §7.2 样例未列，本列模板实有 → 增补声明 | OK（增补） |
| 5 | 顶部 | 环境温度(℃) | `env_temp` | number, unit=℃ | 否 | — | OK |
| 6 | 顶部 | 测试性质(定期/核对性放电) | `test_kind` | enum{定期,核对性放电} | 是 | dedupe_key 成员；cycle 规则 `when` | OK |
| 7 | 顶部 | 测试人 | —（signature_slots=["测试人"]） | — | — | 签字槽 | 约定 |
| 8 | 条目 | 电池序号 | items.`cell_no` | number(整数) | 是 | key_field；模板预填 1–20 可增删 → **条目数量/序号连续性约束**（≥1、去重已有、连续性待定） | **GAP-H7**（弱） |
| 9 | 条目 | 单体电压(V) | items.`voltage` | number, unit=V, min=0, max=15 | 是 | T1 band + T2 deviation（主设样例原样） | OK |
| 10 | 条目 | 是否落后电池 | items.`lagging` | tri_bool | 否 | 人工勾选 vs 核心 deviation 派生 → **双源** | **GAP-H6** |
| 11 | 条目 | 备注 | items.`remark` | text | 否 | — | OK |
| 12 | 条目 | 复测值(V) | items.`retest_voltage` | number, unit=V, min=0, max=15 | 否 | 复测链（`retest_of` link）落点；主设 §7.2 样例未列 → 增补声明 | OK（增补） |

**结论**：本表即 §7.2 样例原型，7 种字段类型**全部可表达**，仅增补 2 列（浮充电流、复测值）。规则/趋势与主设样例一致。

---

## 8. transformer_core_clamp_current_record 主变铁芯/夹件接地电流测试记录簿（flat）

**表格结构**：sheet `主变铁芯／夹件接地电流测试记录簿`，A1:H25，表头第 3 行，**8 列**。脚注：*"定期测试主变铁芯及夹件接地电流；电流异常增大应安排停运检查。"*

**逐列映射**：

| # | Excel 列 | 草案 key | 类型 | required | 判定需求/说明 | 结论 |
|---|---|---|---|---|---|---|
| 1 | 测试日期 | —（occurred_at） | datetime | 是 | 信封承载 | 约定 |
| 2 | 主变编号 | `transformer_id` | text | 是 | dedupe_key + trend 分组键 | OK |
| 3 | 铁芯接地电流(mA) | `core_current` | number, unit=mA, min=0 | 是 | T1 上限（如 100mA，数值待现场） | OK |
| 4 | 夹件接地电流(mA) | `clamp_current` | number, unit=mA, min=0 | 是 | T1 上限（待现场） | OK |
| 5 | 负荷情况 | `load_condition` | text→建议 enum | 否 | 自由文本（"50%负荷"等）→ 建议 enum{满载,半载,轻载,…}，选项待现场 | 决策项 |
| 6 | 与上次对比 | `vs_last_note` | text | 否 | 语义=趋势结论的人工版（"较上次增大"）→ 与 trend 判定**双源**；trend 算法口径本身是 §12 待定 | **GAP-H6** |
| 7 | 结论 | `conclusion` | enum{正常,异常,…} | 是 | 选项待现场定 | 决策项 |
| 8 | 测试人 | —（signature_slots=["测试人"]） | — | — | 签字槽 | 约定 |

**规则草案**：`current_limit`：T1 `lte:上限` ×2 字段 ✓；`current_trend`：trend（source=`core_current`/`clamp_current`，按 `transformer_id` 序列；drop_warn 口径、比较基准、阈值均 §12 待定）。
**周期**：`periodic:Nd`（待现场）。

---

## 9. protection_switch_record 保护投退记录簿

**表格结构**：sheet `保护投退记录簿`，A1:H25，表头第 3 行，**8 列**。脚注：*"保护及自动装置投退变更情况纳入交接班交代。"*

**结构性发现**：`投/退` 为行判别列，且同行含 `恢复时间` —— "退"行自带恢复时间（行内配对），但"投"（恢复投运）是否也单独成行？两种口径：**行内配对**（一记录=一退一恢复，recovery_within 查本记录两时间 ✓ 最简）vs **投退两行跨记录 pairing**（主设 §7.4 pairing 键=装置功能 的原始设想）。模板无法自证，需现场核对 → **GAP-E2**。

**逐列映射**：

| # | Excel 列 | 草案 key | 类型 | required | 判定需求/说明 | 结论 |
|---|---|---|---|---|---|---|
| 1 | 时间 | —（occurred_at） | datetime | 是 | 信封承载 | 约定 |
| 2 | 装置及功能(重合闸/主保护等) | `device_id` + `function`（拆分） | text + enum | 是 | **复合列**；pairing 配对键=组合键（装置+功能）——pairing 是否支持多字段键待定 | **GAP-E3 / GAP-H3** |
| 3 | 投/退 | `action` | enum{投,退} | 是 | 配对类型判别 | OK |
| 4 | 原因 | `reason` | text | 是 | — | OK |
| 5 | 调度令号 | `dispatch_order_no` | text | 否 | — | OK |
| 6 | 操作人 | —（signature_slots） | — | — | 槽位 1 | 约定 |
| 7 | 监护人 | —（signature_slots） | — | — | 槽位 2 | 约定 |
| 8 | 恢复时间 | `restored_at` | datetime | **条件**（退行必填于恢复时；投行恒空） | 现行类型无"条件必填/可空后补"语义；`recovery_within:Nd`（T3）依赖它 | **GAP-H1 / GAP-E2** |

**规则草案**：`switch_pairing`：T3 `pairing:protection,key=device_id+function`（复合键、重复占用告警）→ GAP-E3；`recover_in_time`：T3 `recovery_within:Nd` ✓ 算子已有（时限数值待现场）。

---

## 10. rodent_proof_check_record 防小动物检查记录簿

**表格结构**：sheet `防小动物检查记录簿`，A1:E25，表头第 3 行，**5 列**。脚注：*"定期检查防小动物设施，孔洞封堵完好。"*

**逐列映射**（一次检查多部位 → layout=item_list，条目=部位）：

| # | Excel 列 | 草案 key（层级） | 类型 | required | 判定需求/说明 | 结论 |
|---|---|---|---|---|---|---|
| 1 | 检查日期 | 顶层 —（occurred_at） | datetime | 是 | 信封承载 | 约定 |
| 2 | 检查部位(配电室/电缆沟/挡鼠板/门窗孔洞) | items.`spot` | enum{配电室,电缆沟,挡鼠板,门窗孔洞} | 是 | 模板括号自带 4 选项；条目键 | OK |
| 3 | 发现问题 | items.`issue_found` | text | 否 | 无问题留空/写"无" → 建议改 tri_bool `has_issue` + text？保持 text+空语义即可 | 决策项 |
| 4 | 处理措施 | items.`action_taken` | text | **条件**（发现问题非空→必填） | 现行 DSL 无条件必填 | **GAP-H4** |
| 5 | 检查人 | —（signature_slots=["检查人"]） | — | — | 签字槽 | 约定 |

**规则草案**："雨季/秋季重点期"（主设 §7.4 所列）→ when **日历窗口** 扩展 → GAP-C2。
**周期**：`periodic:Nd`（待现场；重点期加密=when 窗口内周期缩短，探针增强需求，开放）。

---

## 11. 全局 GAP 汇总（对应主设 §11 M0 八项待定项）

> 编号规则：A=aggregate、B=external_baseline、C=when 扩展、E=pairing/连续性、F=测温判级、G=ledger_view/archive 口径、H=八项之外新增、L=每项归类。计数=明确列级缺口数。

### ① when 三类扩展（否定/比较/日历窗口）——3 项

| 编号 | 内容 | 来源 | 结论/建议 |
|---|---|---|---|
| C1 | **否定/集合不匹配**：避雷器"天气≠雷雨 → warn" | 避雷器·天气列+脚注 | 需 `when` 支持 `!=` / `not_in`；同时把天气列 enum 化（选项集待现场） |
| C2 | **日历窗口**：防小动物"雨季/秋季重点期"（周期加密） | 主设 §7.4 | 需 `when` 支持 `calendar_window`（如 6–9 月雨季）；窗口定义数据放声明 |
| C3 | **日历/比较（弱）**：测温"高温天气/过负荷加强测温" | 测温·脚注 | 弱需求：高温阈值比较 + 窗口；可与 C2 同一机制；仍开放（现场口径） |

### ② aggregate 聚合算子语义（含复位参照）——3 项

| 编号 | 内容 | 来源 | 结论/建议 |
|---|---|---|---|
| A2 | 断路器**累计跳闸次数**逐次累计、跨记录聚合，人工列 vs 核心聚合双源 | 断路器·列6 | 建议算子 `aggregate:count_over`；**复位参照**必须拍板：允许次数是否随检修复位（建议声明 `reset_ref`，缺省不复位） |
| A3 | 避雷器**计数器读数单调性**核对（每月核对底数：本次≥上次，倒退告警） | 避雷器·列4+脚注 | 现有算子无"跨记录前值比较/单调"——建议 `monotonic:ge,key=设备` 纳入 aggregate 家族或 T3 新算子 |
| A4 | 两票**合格率评价**列名歧义：单票评价 or 统计合格率（后者=T3 聚合派生，不应人工填） | 两票·列9 | 需现场核对列语义；若统计口径→由 aggregate 派生进 `actions_hint`/报表，列改派生 |

### ③ pairing 配对键细节（含 continuity）——5 项

| 编号 | 内容 | 来源 | 结论/建议 |
|---|---|---|---|
| E1 | **接地线装/拆建模两案**（并案 item_list vs 每动作一记录+T3 pairing）；主设 §7.4"条目内装-拆间隔（T2）"表述与 Excel 一行一动作形态冲突 | 接地线·结构 | 建议案 B（flat+pairing），修订 §7.4 该行；影响 M2 引擎形态，**进 M1 前拍板** |
| E2 | **投退配对口径**：行内"恢复时间"配对 vs 投/退两行跨记录 pairing，模板无法自证 | 保护投退·结构 | 需现场核对；行内配对则 recovery_within 最简，pairing 仅做重复占用 |
| E3 | pairing 配对键为**复合键**（装置+功能，需先拆列）；多字段键支持未声明 | 保护投退·列2 | 建议 `key=[device_id,function]` 数组形式 |
| E4 | **continuity 细节**：按票种分组、复位周期不同（工作票按月/操作票按年）、编号段（序号）解析规则 | 两票·列1/2+脚注 | 建议 `continuity:ticket_no,group=ticket_kind,reset=monthly|yearly`，编号解析格式入声明 |
| E5 | **跨记录取数链路**：接地线"拆除超时"= 工作票终结时间（在 linked_records.fields）与拆除时间的 date_diff；取哪张票（令号/票号→references 匹配）细节未定 | 接地线·列5 | 建议 references 按 order_ref 精确匹配 + T3 date_diff 消费 linked_records |

### ④ external_baseline 数据形态（含 E_BASELINE_MISSING 触发细则）——1 项

| 编号 | 内容 | 来源 | 结论/建议 |
|---|---|---|---|
| B1 | **允许事故开闸次数表**：按开关编号查询允许值 → 基线数据需 keyed-by-device 形态 `{ref,kind:"count_table",data:{breaker_id: 允许次数}}`；`ref` 在 `baselines` 缺失→`E_BASELINE_MISSING`（按记录抛 or 按设备键抛？建议：整表缺失=记录级错误；单设备键缺失=规则级 warn） | 断路器 | 现存唯一数值型基线用户；建议以此定稿形态 |

### ⑤ ledger_view correct 并存窗口取值口径（T3 消费何版 fields）——1 项（全局性）

| 编号 | 内容 | 影响 | 结论/建议 |
|---|---|---|---|
| G1 | 定稿更正后、新版签认前，账本行 lifecycle（confirmed）与 rev/fields（draft 新版）来自不同版本；T3（两票 continuity、接地线/投退 pairing、断路器 aggregate）若消费行 fields 会吃到**未签认数据** | 两票/接地线/投退/断路器全部 T3 规则 | 采纳主设候选方案：行结构增设 `confirmed_fields`，**T3 默认取最后已确认版**；进 M1 前优先拍板（主设 §8.3 已建议） |

### ⑥ archive 并存窗口目标版本口径——1 项（全局性）

| 编号 | 内容 | 影响 | 结论/建议 |
|---|---|---|---|
| G2 | 并存窗口内 archive：目标=最新版（draft）or 最后确认版？ | 全类型归档；两票"保存期一年/封面统计"、断路器年度统计依赖归档口径 | 建议 **archive 目标=最后确认版**（只归签认内容，与 G1 同口径）；archived 禁 void 不变 |

### ⑦ 测温判级口径核对（δt% 组合公式/派生字段机制）——3 项

| 编号 | 内容 | 来源 | 结论/建议 |
|---|---|---|---|
| F1 | **δt=(T_hot−T_norm)/(T_hot−T0)×100%** 组合公式：diff/ratio 均不可表达。模板中"相对温差δt"列**真实存在** | 测温·列8 | 两案：(a) 人工报数→T1 band 降级表达（零改动）；(b) 核心计算→需**派生字段机制**（声明 `derive="公式"`）或组合公式算子。建议 (a) 起步、(b) 列 M2 扩展 |
| F2 | 相间温差需"**同设备正常相**"基准选点（条目间选点语义，非 diff:a,b） | 测温·列7 | 若人工报数则无缺口；核心计算需"选基准条目"算子（开放） |
| F3 | 缺陷判定**分级阈值来源**（DL/T 664 判据表 or 现场规程）与选项集未定 | 测温·列9 | 需现场核对；建议 enum{正常,一般缺陷,严重缺陷,危急缺陷}，阈值表随声明 |

### ⑧ 性能预算值——2 项

| 编号 | 内容 | 来源 | 结论/建议 |
|---|---|---|---|
| P1 | **台账全量传入基准**：两票/断路器/接地线为高频台账（年千行级），T3（continuity/pairing/aggregate）全量 `ledger_view` 传入的规模上限 | 两票/断路器/接地线 | 建议预算：单类型 ≤5,000 行账本 + ≤10,000 行 history 时，create+全规则 P95 ≤ 200ms（Python 参考机）；M1 黄金样本纳入 |
| P2 | **item_list 大数组**：蓄电池 108 单体 × 经年 history 全量 | 蓄电池 | 建议 items ≤ 200/记录、trend 窗口 ≤ 12 时不降级；写入 §10 性能基准 |

### 八项之外新增缺口（H 组，供主设 §11/§12 增补）——14 项

| 编号 | 内容 | 来源 | 建议 |
|---|---|---|---|
| H1 | **条件必填/可空后补语义缺失**（7 类型无 required 条件式）：两票结束时间（未终结空）、投退恢复时间（恢复后补） | 两票·列8 / 投退·列8 | 声明增加 `required_when`（如 `required_when={action:"退"}` + 允许 draft 阶段可空、confirm 前重判） |
| H2 | 接地线拆除行的地点列必填口径（建议：携带且须与装设行一致） | 接地线·列4 | 同 H1 机制 + pairing 值一致性核对 |
| H3 | **复合列拆分**（3 列）：仪器及电压等级、环境温湿度、装置及功能 | 绝缘·列8/9、投退·列2 | M1 声明拆为两个原子字段；旧 Excel 适配器负责拆列 |
| H4 | 防小动物"处理措施"条件必填（发现问题非空→必填） | 防小动物·列4 | 同 H1 机制 |
| H5 | **附件与条目绑定缺失**：`attachments_ref` 为记录级数组，无法绑定 item_list 具体测点的红外图 | 测温·列3 | 建议 attachments_ref 增加 `item_key` 字段（如 cell_no/spot 值） |
| H6 | **双源一致性核对算子缺失**（人工填报派生值 vs 核心计算，5 列）：吸收比、是否落后电池、与上次对比、累计跳闸次数、相间温差/δt | 绝缘/蓄电池/主变/断路器/测温 | 建议 `consistency:reported,derived,tol` 算子（T2）或策略上"派生列只读、核心生成"；需拍板采集口径 |
| H7 | item_list **条目数量/序号连续性约束**（蓄电池预填 1–20 可增删） | 蓄电池·条目 | 弱需求：建议 `min_items=1` + key_field 去重已有；序号连续性交采集端 |
| 决策项（不计 GAP） | 重合闸情况/结论/负荷情况/发现问题 等 enum 选项集；极化指数是否增列 R10min；两票 layout 改 flat；避雷器天气选项 | 各表 | 均待现场规程核对后定稿（§12"限值数值"同类） |

---

## 12. 统计与核对结论

- **读完模板**：10/10（每份单 sheet、表头第 3 行、空白模板无示例行；蓄电池表单式+序号预填 1–20）
- **逐列核对逻辑字段合计**：**86**（物理列 79；蓄电池按 7 顶层+5 条目=12 计）：断路器 8、避雷器 7、接地线 7、两票 9、测温 11、绝缘 11、蓄电池 12、主变 8、投退 8、防小动物 5
- **GAP 合计（按条目）26**：八项待定内 19 = ①when 3 ②aggregate 3 ③pairing/连续性 5 ④external_baseline 1 ⑤并存窗口取值 1 ⑥archive 目标版本 1 ⑦测温判级 3 ⑧性能预算 2；**八项之外新增 H 组 7 条**（H1 条件必填、H2 拆行值一致、H3 复合列×3、H4 条件必填、H5 附件绑条目、H6 双源一致性、H7 条目数量约束），覆盖 14 个列级落点
- **可表达性**：7 种字段类型覆盖 86 列中 61 列（含 envelope/签字归位 28 列）；其余为复合列拆分（3）、enum 选项待定（5，决策项）、条件必填（4）、双源派生（5）等
- **T1/T2 表内判定**：电压带/阻值下限/电流上限/吸收比/温升差值 均可表达 ✓；**不可表达**的判定集中在：δt 组合公式（F1）、跨记录单调/累计（A2/A3）、条件触发（C1/C2/C3）
- **隐私**：模板为空表，无真实场站名/人名；本文示例统一 `ST001`/`张三`/`李四` 占位

## 13. 建议优先拍板顺序（进 M1 前）

1. **E1+E2**（接地线/投退建模两案）——决定 M2 引擎 pairing 形态
2. **G1+G2**（并存窗口 T3 取值 + archive 目标版本）——主设 §8.3 已建议优先，影响 ledger_view 行结构（是否增设 `confirmed_fields`）
3. **F1**（δt 人工报数 or 派生机制）——决定 M1 是否需要派生字段机制
4. **A2+B1**（aggregate 复位参照 + 允许次数表形态）——断路器核心规则依赖
