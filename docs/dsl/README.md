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

## 待确认后铺开清单（其余 7 类）

breaker_trip_record、grounding_wire_record（flat，E1 拍板）、two_ticket_ledger（flat 待定，§14）、insulation_test_record、battery_voltage_test（M1 参照类，声明随 M1 或此处一并出，待主控定）、transformer_core_clamp_current_record、protection_switch_record（flat，E2 拍板）、rodent_proof_check_record。
