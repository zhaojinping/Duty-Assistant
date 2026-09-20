# DSL 文法扩展提案（终稿）：`when` 否定/集合不匹配 + 跨记录单调算子

> 状态：**终稿（阶段 2）**。骨架版（PR #21）已合入 main；原 7 项待拍板已由总控 2026-09-20 **全部按建议采纳**（Issue #4 拍板评论）。本稿为**可据以实现的完整文法**，不含实现代码。
> 关联 Issue #4（M2）；对应《DSL 缺口清单》定稿 **GAP-C1**、**GAP-A3**。
> 依据：`docs/design.md` §7.2/§7.3/§7.4、`docs/dsl-gap-list.md` §11①/②、`docs/dsl/README.md`「T3 规则段冻结文法」（#20 已实现）、`src/records_kit/registry/declaration.py`（算子白名单常量）。

## 1. 拍板结论（7 项，落文即口径）

| # | 事项 | 拍板结果 | 落入 |
|---|---|---|---|
| 1 | `when` 扩展形态 | **值侧前缀微文法**（`"not_in:雷雨"`） | §3 |
| 2 | 新增 `kind = "condition"` | **采纳**（C1 的规则形态） | §4 |
| 3 | 条目字段否定条件的量词 | **沿用存在量词**（全称量词另案） | §3.4 |
| 4 | `monotonic` 的 `op` | **具名 `op=`，缺省 `ge`** | §5.2 |
| 5 | `monotonic` 的 `key` | **可选**（单序列可省略；避雷器显式写） | §5.2 |
| 6 | 计数器换表归零 | **不引入隐式复位**（随 `reset_ref` / 人工志记） | §5.5 |
| 7 | `window` | **保留可选，缺省不限窗口** | §5.2 |

## 2. 范围

- **① `when` 否定/集合不匹配**（GAP-C1）：让「非雷雨天气动作 → warn」可表达。
- **② 跨记录单调算子 `monotonic`**（GAP-A3）：让「计数器累计读数只增不减」可表达。
- 不含：C2 日历窗口 / C3 比较类落地（**仅预留算子位，声明写入即拒绝加载**）；实现代码；现场数值缺口。

风格约束：沿用 §7「字符串微文法 + 逗号分隔 + `key=` 具名参数」；`when` 仍是 **TOML inline table**，不引入新结构层。

## 3. `when` 完整文法

### 3.1 现状与载体

- 载体：声明内 `when = { … }`——键 = 字段路径（顶层字段或 `items.<字段>`），值 = 字面量，多条件 **AND**，空表 = 恒命中。
- 引擎：`engine/rules.py::when_matches`（顶层取记录字段；条目字段取「任一条目命中」）。
- 校验：`registry/meta.py::_check_when`（enum 值须落在 `options` 内、`tri_bool` 须三态取值）。
- 缺口：无否定、无集合——`weather` 已是枚举，但只能写「等于某值」。

### 3.2 EBNF

```ebnf
when        = "{" [ cond { "," cond } ] "}"          (* TOML inline table；空表 = 恒命中（现状） *)
cond        = field_path "=" cond_value
field_path  = top_field | "items." item_field
cond_value  = literal | prefixed
literal     = 与声明字段类型一致的 TOML 标量        (* 现状形态，语义 = eq *)
prefixed    = op ":" operand { "," operand }        (* 与 expr 微文法同源（split_expr 形态） *)
op          = "eq" | "ne" | "in" | "not_in"         (* 本次落地 *)
            | "gt" | "gte" | "lt" | "lte"           (* 预留：C3 比较类，未落地，写入即拒绝加载 *)
operand     = scalar                                (* 与字段类型一致的取值文本 *)
```

### 3.3 算子语义

| 算子 | 写法 | 命中条件 | 例 |
|---|---|---|---|
| `eq`（缺省） | `"值"` 或 `"eq:值"` | 字段值 = 值 | `when = { weather = "雷雨" }` |
| `ne` | `"ne:值"` | 字段值 ≠ 值 | `when = { weather = "ne:雷雨" }` |
| `in` | `"in:值1,值2"` | 字段值 ∈ 集合 | `when = { weather = "in:大风,冰雹,冰雪" }` |
| `not_in` | `"not_in:值1,值2"` | 字段值 ∉ 集合 | `when = { weather = "not_in:雷雨" }` |

- 多条件仍是 **AND**；OR 不在本次范围（如需另案，载体不变）。
- 集合成员须落在字段 `options` 内（enum）或类型一致；未知成员 → 声明加载即报错。

### 3.4 空值与量词口径

- **空值**：字段未填（可选字段缺省）→ 条件**不命中**（`when` 不成立）。否定算子**不改**这条——不把「未填」当成「非雷雨」，避免旧数据缺列直接触发误报。
- **条目字段（item_list）**：沿用**存在量词**（与现状一致）——`items.<字段> = "not_in:A"` 表示「**存在一个**条目的该字段 ∉ A」即命中。全称量词（「全部条目满足」）另案，本次不引入 `all:` 修饰。

### 3.5 校验规则（不满足 → 声明拒绝加载）

| # | 规则 |
|---|---|
| 1 | 值匹配 `^(eq\|ne\|in\|not_in):` 时按算子解析；匹配预留算子（`gt/gte/lt/lte`）→ 报错（未落地） |
| 2 | `in` / `not_in` 的每个成员须落在字段 `options` 内（enum）或与字段类型一致 |
| 3 | **歧义守卫**：字段 `options` 中存在以已登记算子前缀开头的取值 → 报错（要求改选项集，或该处用显式 `eq:` 形态） |
| 4 | 键路径须存在；指向条目容器（`items`）本身 → 报错 |
| 5 | 空表 `when` 保持现状语义（恒命中），不报错 |

### 3.6 向后兼容

值不匹配任一已登记算子前缀时，一律按**等值**处理——存量声明（如 `when = { test_kind = "定期" }`）语义不变、无需改动。现存 10 类声明枚举取值（M0 定稿 §14）均不与算子前缀冲突。

## 4. 条件型规则 `kind = "condition"`

「非雷雨天气动作 → warn」的判定对象是**条件本身**，不是数值比较；现行 `kind` 只有 `cycle | limit`，而 `limit` 强制 `target` + 数值/派生算子——**仅加 §3 的文法仍写不出可加载的规则**，故需条件型规则。

### 4.1 声明字段

| 键 | 必填 | 约束 |
|---|---|---|
| `id` | 是 | 同一声明内唯一 |
| `kind` | 是 | 恒为 `"condition"` |
| `tier` | 是 | 恒为 `1`（`when` 只读本记录 payload；若将来需读台账，按归层原则升 3，另案） |
| `when` | 是 | 非空——空表恒命中 = 恒违规，直接拒绝加载 |
| `level` | 否 | 默认 `warn`；枚举 `warn \| alarm` |
| `action` | 否 | 须在声明 `action_codes` 内 |
| `target` / `expr` | **禁止** | 出现即拒绝加载 |

### 4.2 判定语义

- `when` **命中** → `verdict = violation`（`level` 按声明；`violation` 与其他规则同等进入告警指纹通道）。
- `when` **未命中** → `verdict = pass`——**与现行非 `cycle` 规则「`when` 未命中则不出条目」不同**：条件型规则**始终出条目**，审计要能看到「查过且通过」。
- 输出条目形状（沿用既有规则条目）：`{rule_id, kind, tier, verdict, threshold, level, detail}`
  - `threshold` = **`when` 的规范化文本**（如 `weather not_in 雷雨`）——沿用「取声明原文、无二次解释」的既有口径；
  - `detail` = 条件与结论（命中时附字段实际取值）。

### 4.3 声明形态

```toml
# 非雷雨天气动作 → warn（脚注：非雷雨天气动作应检查上报）
[[rules]]
id = "non_storm_action"
kind = "condition"
tier = 1
when = { weather = "not_in:雷雨" }
level = "warn"
action = "REPORT_NON_STORM_ACTION"
```

## 5. `monotonic` 完整文法

### 5.1 归层与家族

- 归层 **T3**（读 `ledger_view`）；落地为 `kind = "limit"`、`tier = 3`、**不设 `target`**——与冻结 T3 文法一致。
- `T3_OPS` 增 `"monotonic"`；与 `aggregate` 的边界：`aggregate` = 计数 + 基线对照，`monotonic` = 与**前一记录自身值**比较（输入是单行前值，不是聚合），**不并入 aggregate**。

### 5.2 签名与参数

```ebnf
monotonic_expr = "monotonic:" field [ "," "key=" key_field { "+" key_field } ]
                                  [ "," "op=" ( "ge" | "gt" ) ]
                                  [ "," "window=" Nd ]
```

| 参数 | 形态 | 必填 | 默认 | 约束 |
|---|---|---|---|---|
| `field` | 位置参数 1 | 是 | — | 顶层 `number` 字段（记录级计数器；条目字段本次不支持） |
| `key` | `key=<字段>[+<字段>]` | 否 | 省略 = 同站同类型单序列 | 每段字段须存在、非条目容器；避雷器显式写 `key=phase+install_location` |
| `op` | `op=ge\|gt` | 否 | `ge` | 其他值 → 拒绝加载；`ge` = 本次 ≥ 前值通过（允许持平），`gt` = 必须严格递增 |
| `window` | `window=<Nd>` | 否 | 不限窗口 | N 为正整数；超出窗口的前值视为无前值 |

### 5.3 前值选取（确定性，实现须逐条一致）

1. **候选行**：`ledger_view` 中同 `record_type`、同分组键值的行；排除 voided；**排除当前记录自身及其 correct 链**（同 `record_uid`）。
2. **取值口径**：`confirmed_fields` 优先（correct 并存窗口消费最后已确认版）——与冻结 T3 文法一致。
3. **排序**：按 `occurred_at` 取最大者；相同 → 按 `create_seq` 降序；仍相同 → 按 `record_uid` 字典序（保证判定确定性）。
4. **判定**：本次值满足 `op` 前值 → `pass`；否则 `violation`（`level` 按声明，建议 `warn`）。
5. **不可判定 → `skipped`**（不静默通过）：无候选行（首次记录）、前值字段缺省或非数值、前值落在 `window` 之外。
6. **`detail`**：本次值、前值、前值 `record_uid`、前值 `occurred_at`；`threshold` = `expr` 原文。

### 5.4 声明形态

```toml
# 计数器累计读数核对：本次 < 上次 → warn（脚注：每月核对计数器底数）
[[rules]]
id = "counter_monotonic"
kind = "limit"
tier = 3
expr = "monotonic:counter_reading,key=phase+install_location,op=ge"
level = "warn"
action = "CHECK_COUNTER_ABNORMAL"
```

### 5.5 复位与边界（已拍板口径）

- **不引入隐式复位**：换计数器/换表导致读数归零属业务合法倒退，本算子如实判 `violation`，由现场志记处理；需要机读口径时按 A2 的 `reset_ref` 同款机制另案（`reset_ref` 当前未实现，见 §8）。
- 分组隔离：同键行才互相比较；不同相别/安装位置互不干扰。

## 6. 边界例（实现 PR 必须逐例落为测试用例）

### 6.1 `when`（8 例）

| # | 声明 | 输入 | 期望 | 说明 |
|---|---|---|---|---|
| 1 | `when = { weather = "not_in:雷雨" }` | `weather = "操作过电压"` | **命中** | C1 正解：非雷雨动作 → warn |
| 2 | 同上 | `weather = "雷雨"` | 不命中 | 雷雨是白名单值，不告警 |
| 3 | 同上 | `weather` 缺省（可选字段） | 不命中 | 未填 ≠ 非雷雨；不因缺列误报 |
| 4 | `when = { weather = "not_in:雷雨,大风" }` | `weather = "冰雹"` | 命中 | 集合不匹配多成员 |
| 5 | `when = { weather = "in:雷雨,冰雹" }` | `weather = "冰雪"` | 不命中 | `in` 与 `not_in` 的互补性可测 |
| 6 | `options` 含取值 `not_in:x` | 加载 | **拒绝加载** | 歧义守卫 |
| 7 | `when = { "items.spot" = "not_in:配电室,电缆沟" }` | 条目 `[配电室, 门窗孔洞]` | 命中 | 存在量词：**存在**一个部位在集合外 |
| 8 | 同上 | 条目 `[配电室, 电缆沟]` | 不命中 | 全部条目都在集合内才不命中 |

### 6.2 条件型规则（3 例）

| # | 场景 | 期望 |
|---|---|---|
| 1 | `when` 命中 | 出 `violation` 条目，`threshold` = 规范化 `when` 文本 |
| 2 | `when` 未命中 | 出 `pass` 条目（**不是**不出条目）；`detail` 可见「查过且通过」 |
| 3 | 声明缺 `when`（或同时给了 `target`/`expr`） | 拒绝加载 |

### 6.3 `monotonic`（6 例）

| # | 台账前值 | 本次 | 期望 | 说明 |
|---|---|---|---|---|
| 1 | 无候选行 | `counter_reading = 7` | **skipped** | 首次记录，无前值可核；不静默通过 |
| 2 | 07-01 读数 7 | `7` | `pass` | 「核对底数」允许持平（`ge`） |
| 3 | 07-01 读数 7 | `6` | `violation`(warn) | 倒退；`detail` 给出 6 / 7 / 前值 uid 与日期 |
| 4 | 07-01 行已 voided，其上 06-01 读数 5 | `6` | `pass` | 前值跳过 voided、取再前一条有效行 |
| 5 | 同链旧版本读数 7，最后已确认版读数 9 | `9` | `pass` | 取**最后已确认版**（`confirmed_fields`），且排除自身 correct 链，避免自比 |
| 6 | A 相 7、B 相 3（`key=phase+install_location`） | A 相 `6` | `violation`(warn) | 分组隔离；B 相不参与本次判定 |

## 7. 实现分工与验收（按总控编排）

| 序 | 负责 | 改动文件 | 内容 | 依赖 |
|---|---|---|---|---|
| 1 | 引擎（来庆涵） | `src/records_kit/registry/declaration.py`、`registry/meta.py`、`engine/rules.py` | 常量与校验：`RULE_KINDS` 增 `condition`、`T3_OPS` 增 `monotonic`、`when` 前缀解析 + 集合成员校验 + 歧义守卫；实现：条件型规则判定分支、`monotonic` judge | 本稿（文法即接口） |
| 2 | 声明（陈子建） | `docs/dsl/surge_arrester_action_record.toml` | 两条规则转正：`non_storm_action`（§4.3）、`counter_monotonic`（§5.4） | 序 1 接口冻结 |
| 3 | docs 同步（AI项目经理） | `docs/design.md` §7.3/§7.4/§12、`docs/dsl/README.md` 冻结文法表 | `when` 注改写；T1/T3 算子表增 `condition` 说明与 `monotonic` 行；C1/A3 由「待 M2」转「已定」 | 可与序 1 并行 |

**共用验收口径**（三者同一标准）：

1. §6 全部 **17 例**成为测试用例且通过；
2. `python scripts/repo_checks.py` → `PASS` 且测试阶段全绿；
3. 避雷器声明可加载（meta 校验通过），两条规则在真样例上各出现一次 `violation` 与一次 `pass`；
4. **反向变异验证**：改坏 `when` 前缀解析或前值选取逻辑 → 对应用例必须失败（证明用例非空跑）。

## 8. 待现场 / 后续项（本提案范围内）

- **比较类 `when`（`gt`/`gte`/`lt`/`lte`）与日历窗口（C2/C3）**：本次不落地，写入声明即拒绝加载。
- **`monotonic` 的 `reset_ref`（换表复位）**：当前未实现；缺省不复位已定，需要机读口径时另案。
- **避雷器 `counter_reading` 是否需要 `window`**：缺省不限窗口已定；现场若改为「只与上月比」，加参数即可（不动文法）。

## 9. 自查口径

- 本稿为**终稿**：文法、语义、校验、边界例、分工、验收齐备，**不含实现**；示例声明片段仅作文法演示，未在引擎中加载执行（可加载性由序 1/序 2 的 PR 证明）。
- 引用均为仓库内既有文件与已合入条款；无真实业务数据、单号、人员信息、本机路径。
