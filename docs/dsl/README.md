# docs/dsl/ — M2 前置：TOML 声明草案

其余九类记录的 TOML 声明草案（主控 2026-09-18 派工）。**规则段一律 TODO 占位**，待 M1 引擎接口冻结后按 `docs/design.md` §7.3 DSL 统一补齐；字段段已按 `docs/dsl-gap-list.md` 定稿逐列落成。

## 当前内容（样例 2 类，待格式确认后铺开其余 7 类）

| 文件 | record_type | layout | 覆盖的格式要素 |
|---|---|---|---|
| surge_arrester_action_record.toml | 避雷器动作记录簿 | flat | 拍板 enum 六值（§14）、required 天气、number{decimals,min}、规则/周期 TODO 注释 |
| infrared_thermography_record.toml | 设备测温（红外）记录簿 | item_list | 顶层+条目双层字段、items.key_field、require_attachment 待启用注记、δt% 人工报数口径（F1 拍板）、四级缺陷判定（F3 建议） |

## 格式约定（请主控/AI项目经理确认）

1. **TODO 注释形态**：`# TODO rules:` / `# TODO cycle:` 集中在文件尾部，逐条列出已拍板的规则意图（id/kind/expr 意向/关联 GAP），M1 冻结后按注释逐条转正为 `[[rules]]`。
2. **拍板留痕**：字段注释标注拍板出处（§13/§14）；待现场数值（限值/周期天数）以"待现场"注释标明，不虚构数值。
3. **字段类型仅用 7 种**：text / number / enum / tri_bool / bool / datetime / item_list（§7.3）；物理边界（min）只写有依据的（≥0、%≤100），业务限值一律不写死。
4. **dedupe_key**：按业务键合理性声明（避雷器=站+日+相别+位置+读数；测温=站+日+测温性质），待现场核对后在 M2 定稿确认。
5. **schema_version**：跟随主设版本 "1.5"。

## T3 规则段冻结文法（M2 引擎已实现）

`[[rules]]` 转正时 T3 规则按以下文法落 `expr`（一律 `tier = 3`、**不设 target**；
台账取数 `confirmed_fields` 优先，即 correct 并存窗口内消费最后已确认版）：

| 算子 | expr 文法 | 判定语义 |
|---|---|---|
| continuity | `continuity:<编号字段>[,group=<分组字段>][,reset=monthly\|yearly]` | 同组+同复位窗口（月/年，取 occurred_at）内编号末尾数字段无跳号，跳号 → violation |
| pairing | `pairing:<type>[,key=<字段\|字段+字段>]`（type=`grounding`/`protection`） | 同键重复占用、悬空释放 → violation；占用后未释放的悬空半边 → 探针 `cycle[].detail`（verdict=due） |
| external_baseline | `external_baseline:<ref>[,field=<字段>][,key=<查询键>][,op=gte\|lte]` | 字段值 vs 基线 `data[查询键值]`；整表缺 ref → `E_BASELINE_MISSING`（记录级拒绝）；单键缺失 → skipped（规则级提示） |
| recovery_within | `recovery_within:<Nd>,key=<字段+字段>` | protection「投」行与其配对「退」行间隔 ≤ N 天，超时 → violation；仅恢复行判定 |
| aggregate | `aggregate:count_over[,key=<分组字段>][,ref=<baseline_ref>]` | 同分组非 voided 计数（含当前）+ 基线对照，`≥` 允许值 → violation；缺 ref → `E_BASELINE_MISSING` |
| date_diff（跨记录） | `date_diff:<from>,<to>,<ge\|le>:<Nd>`；`from`/`to` 可带 `linked.` 前缀，`occurred_at` 为信封伪字段 | 跨记录时间差；`linked.<字段>` 从 `linked_records` 取数 |

**配对动作判别由引擎内置**（新增配对类型需扩引擎，§7.3 边界）：`grounding` → action 字段
「装设=占用 / 拆除=释放」；`protection` → action 字段「投=占用 / 退=释放」。复合键用 `+`
连接（§13 拍板数组形式的字符串表达，`split_expr` 以逗号为分隔）。

**待现场项**（转正时保留 TODO 注释，勿虚构数值）：recovery_within / date_diff 的 Nd、
两票编号段解析格式（现实现取编号末尾数字段，GAP-E4）、cycle_baseline 起算日、
允许事故开闸次数表数值（`baselines` 数据由壳层注入，声明只写 ref）。

## 待确认后铺开清单（其余 7 类）

breaker_trip_record、grounding_wire_record（flat，E1 拍板）、two_ticket_ledger（flat 待定，§14）、insulation_test_record、battery_voltage_test（M1 参照类，声明随 M1 或此处一并出，待主控定）、transformer_core_clamp_current_record、protection_switch_record（flat，E2 拍板）、rodent_proof_check_record。
