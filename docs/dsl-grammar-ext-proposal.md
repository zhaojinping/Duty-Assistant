# DSL 文法扩展提案：`when` 否定/集合不匹配 + 跨记录单调算子

> 状态：**设计提案（骨架，不含实现）**。关联 Issue #4（M2）；对应《DSL 缺口清单》定稿 **GAP-C1**、**GAP-A3**。
> 依据：`docs/design.md` §7.2 / §7.3 / §7.4、`docs/dsl-gap-list.md` §11①/②、`docs/dsl/README.md`「T3 规则段冻结文法」。
> 本文件只定**文法与算子签名**；引擎实现、meta-test 改造、声明转正各自独立 PR。

## 1. 目标与范围

| 项 | 现状缺什么 | 本提案目标形态 |
|---|---|---|
| ① GAP-C1 | `when` 只支持字段等值匹配，「非雷雨天气动作 → warn」无处可写 | `when` 支持否定 `ne` 与集合不匹配 `not_in`（预留比较类位置） |
| ② GAP-A3 | T3 家族无「与前一记录值比较/单调」算子 | 新增 T3 算子 `monotonic`（默认单调不减） |

不含：C2 日历窗口 / C3 比较类落地（仅在文法中预留、写了即报错）；实现代码；声明转正。

风格约束：沿用 §7 既有「字符串微文法 + 逗号分隔 + `key=` 具名参数」；`when` 仍是 **TOML inline table**，不引入新的结构层。

## 2. ① `when` 否定 / 集合不匹配

### 2.1 现状（可复核）

- 载体：声明内 `when = { … }`——键 = 字段路径（顶层字段或 `items.<字段>`），值 = 字面量，多条件 **AND**，空表 = 恒命中。
- 引擎：`src/records_kit/engine/rules.py::when_matches`（顶层取记录字段、条目字段取「任一条目命中」）。
- 校验：`src/records_kit/registry/meta.py::_check_when`（enum 值须落在 `options` 内、`tri_bool` 须三态取值）。
- 缺口：无否定、无集合；`weather` 为枚举但只能写「等于某值」。

### 2.2 提案文法

```ebnf
when        = "{" [ cond { "," cond } ] "}"          (* TOML inline table；空表 = 恒命中（现状） *)
cond        = field_path "=" cond_value
field_path  = top_field | "items." item_field
cond_value  = literal | prefixed
literal     = 与声明字段类型一致的 TOML 标量        (* 现状形态，语义 = eq *)
prefixed    = op ":" operand { "," operand }        (* 与 expr 微文法同源：split_expr 形态 *)
op          = "eq" | "ne" | "in" | "not_in"         (* 本提案落地 *)
            | "gt" | "gte" | "lt" | "lte"           (* 预留：C3 比较类，本次不落地，写即报错 *)
operand     = scalar                                (* 与字段类型一致的取值文本 *)
```

- **向后兼容**：值不匹配任一已登记算子前缀（`^op:`，op 在白名单内）时，一律按**等值**处理——现有 `when = { test_kind = "定期" }` 语义不变，存量声明无需改动。
- **歧义守卫**：若某字段 `options` 中存在以已登记算子前缀开头的取值，**声明加载即报错**（要求改选项集，或该处改用显式 `eq:` 形态）。现存 10 类声明的枚举取值（M0 定稿 §14）均无此形态。
- **校验**：`in` / `not_in` 的成员须落在字段 `options` 内（enum）或类型一致；未知成员 → 声明加载即报错。

### 2.3 语义

| 算子 | 写法 | 命中条件 | 例 |
|---|---|---|---|
| `eq`（缺省） | `"值"` 或 `"eq:值"` | 字段值 = 值 | `when = { weather = "雷雨" }` |
| `ne` | `"ne:值"` | 字段值 ≠ 值 | `when = { weather = "ne:雷雨" }` |
| `in` | `"in:值1,值2"` | 字段值 ∈ 集合 | `when = { weather = "in:大风,冰雹,冰雪" }` |
| `not_in` | `"not_in:值1,值2"` | 字段值 ∉ 集合 | `when = { weather = "not_in:雷雨" }` |

- 多条件仍是 **AND**；OR 不在本提案范围。
- **空值口径**：字段未填（可选字段缺省）→ 条件**不命中**（`when` 不成立）。否定算子**不改**这条——不把「未填」当成「非雷雨」，避免旧数据缺列直接触发误报。
- **条目字段（item_list）量化**：现状 = 存在量词（任一条目命中即命中）。本提案**沿用存在量词**：`items.<字段> = "not_in:A"` 表示「存在一个条目的该字段 ∉ A」即命中。「全部条目满足」需全称量词，列为待拍板（§5），本提案不引入 `all:` 修饰，以免同时改动两处语义。

### 2.4 配套：条件型规则（C1 落地的必要一环）

「非雷雨天气动作 → warn」的判定对象是**条件本身**，不是数值比较；而现行 `kind` 只有 `cycle | limit`，`limit` 强制要求 `target` + 数值/派生算子（`meta.py::_check_rule`）。因此**只加 §2.2 的文法仍写不出可加载的规则**。建议配套新增条件型规则：

```toml
[[rules]]
id = "non_storm_action"
kind = "condition"        # 新增：条件型；不设 target / expr
tier = 1
when = { weather = "not_in:雷雨" }
level = "warn"
action = "REPORT_NON_STORM_ACTION"
```

- 判定：`when` 命中 → `violation`；未命中 → `pass`。
- **与现有 `when` 语义的差别（须写进 §7.3）**：现行非 `cycle` 规则在 `when` 未命中时**不出条目**；条件型规则**始终出条目**（审计要能看到「查过且通过」）。
- 校验：`kind = "condition"` 必须声明 `when`（否则恒命中 = 恒违规）；不得同时声明 `target` / `expr`。

### 2.5 采纳后的避雷器规则段（示意）

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

## 3. ② 跨记录单调算子 `monotonic`

### 3.1 现状与来源

- 《DSL 缺口清单》A3 建议形态 `monotonic:ge,key=设备`。
- 现行 T3 家族（已冻结）：`continuity` / `pairing` / `external_baseline` / `recovery_within` / `aggregate` / `date_diff`（跨记录）——均为「与台账或基线对照」，无「与**前一记录自身值**比较」。
- 语义来源：避雷器「每月核对计数器底数：本次 ≥ 上次，倒退告警」。

### 3.2 算子签名

```ebnf
monotonic_expr = "monotonic:" field [ "," "key=" key_field { "+" key_field } ]
                                  [ "," "op=" ( "ge" | "gt" ) ]
                                  [ "," "window=" Nd ]
```

- 归层 **T3**（读 `ledger_view`）；落地为 `kind = "limit"`、`tier = 3`、**不设 `target`**——与冻结 T3 文法一致。
- 与 A3 草稿的差别：`op` 改为**具名参数**、首个位置参数留给**被判定字段**，与 `continuity:<编号字段>` / `external_baseline:<ref>` / `recovery_within:<Nd>` 的位置参数约定一致；`op` 缺省 `ge`（单调不减），多数场景可不写。
- `key`：分组键，可复合（`+` 连接，同 `pairing` 复合键写法）；**省略 = 同站同类型构成单一序列**。避雷器应显式写 `key=phase+install_location`（计数器按相别/安装位置各自计数）。
- `op`：`ge` = 本次 ≥ 前值即通过（默认，允许持平）；`gt` = 必须严格递增。
- `window`（可选）：只取 `occurred_at` 在 Nd 天内的前值，超出视为无前值（`skipped`）；**缺省不限窗口**（「每月核对底数」是全局前值语义）。
- 参数与字段校验：`field` 须为 `number` 字段；`key` 字段须存在且类型为 enum/text/number；`window` 须为 `Nd` 形态。

### 3.3 前值选取与判定

1. **候选行**：`ledger_view` 中同 `record_type`、同分组键值的行；排除 voided；**排除当前记录自身及其 correct 链**（同 `record_uid`）。
2. **取值口径**：`confirmed_fields` 优先（correct 并存窗口消费最后已确认版）——与冻结 T3 文法一致。
3. **排序**：按 `occurred_at` 取最大者；`occurred_at` 相同 → 按 `create_seq` 降序（仍相同 → 按 `record_uid` 字典序），保证判定确定性。
4. **判定**：本次值满足 `op` 前值 → `pass`；否则 `violation`（`level` 按声明，建议 `warn`）。
5. **不可判定 → `skipped`**（不静默通过）：无候选行（首次记录）、前值字段缺省或非数值、落在 `window` 之外。
6. **`detail` 文本**：含本次值、前值、前值 `record_uid`、前值 `occurred_at`——可复核、无二次解释（沿用 `threshold` 取 `expr` 原文的既有口径）。

### 3.4 采纳后的避雷器规则段（示意）

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

## 4. 边界例

### 4.1 `when`（否定 / 集合）

| # | 声明 | 输入 | 期望 | 说明 |
|---|---|---|---|---|
| 1 | `when = { weather = "not_in:雷雨" }` | `weather = "操作过电压"` | **命中** | C1 正解：非雷雨动作 → warn |
| 2 | 同上 | `weather = "雷雨"` | 不命中 | 雷雨是白名单值，不告警 |
| 3 | 同上 | `weather` 缺省（可选字段） | 不命中 | 未填 ≠ 非雷雨；不因缺列误报 |
| 4 | `when = { weather = "not_in:雷雨,大风" }` | `weather = "冰雹"` | 命中 | 集合不匹配多成员 |
| 5 | `when = { weather = "in:雷雨,冰雹" }` | `weather = "冰雪"` | 不命中 | `in` 的补集即 `not_in` 语义（两条写法等价性可测） |
| 6 | 声明 `options` 含取值 `not_in:x` | 加载 | **拒绝加载** | 歧义守卫：选项集与算子前缀冲突 |
| 7 | 条目表：`when = { "items.spot" = "not_in:配电室,电缆沟" }` | 条目 `[配电室, 门窗孔洞]` | 命中 | 存在量词：**存在**一个部位在集合外即命中 |
| 8 | 同上 | 条目 `[配电室, 电缆沟]` | 不命中 | 全部条目都在集合内才不命中 |

### 4.2 `monotonic`

| # | 台账前值 | 本次 | 期望 | 说明 |
|---|---|---|---|---|
| 1 | 无候选行 | `counter_reading = 7` | **skipped** | 首次记录，无前值可核；不静默通过 |
| 2 | 07-01 读数 7 | `7` | `pass` | 「核对底数」允许持平（`ge`） |
| 3 | 07-01 读数 7 | `6` | `violation`(warn) | 倒退；detail 给出 6 / 7 / 前值 uid 与日期 |
| 4 | 07-01 行已 voided，其上 06-01 读数 5 | `6` | `pass` | 前值跳过 voided、取再前一条有效行 |
| 5 | 同链旧版本读数 7，最后已确认版读数 9 | `9` | `pass` | correct 并存窗口取**最后已确认版**（`confirmed_fields`），且排除自身 correct 链，避免自比 |
| 6 | A 相 7、B 相 3（`key=phase+install_location`） | A 相 `6` | `violation`(warn) | 分组隔离：不同相别/位置不互相比较；B 相不参与本次判定 |

## 5. 待拍板项（决策请求）

| # | 项 | 本提案建议 | 影响 |
|---|---|---|---|
| 1 | `when` 扩展形态：值侧前缀微文法（`"not_in:雷雨"`） vs 结构化内联表（`{ op = "not_in", values = [...] }`） vs 键名内嵌运算符（`"weather!=" = "雷雨"`） | **值侧前缀微文法**——与 `expr` 同源、TOML 合法性无争议、存量声明零改动 | 决定性：`when` 全文法与 meta-test 校验形态 |
| 2 | 是否新增 `kind = "condition"`（§2.4） | **采纳**——否则 C1 只有文法没有可用规则形态 | `RULE_KINDS`、流水线条目语义（未命中出 pass 条目） |
| 3 | 条目字段否定条件的量词 | **沿用存在量词**（§2.3），全称量词另案 | 声明语义与测试用例 |
| 4 | `monotonic` 的 `op` 位置 | **具名 `op=`，缺省 `ge`** | 与缺口清单草稿的写法差异 |
| 5 | `monotonic` 的 `key` 是否必填 | 可选（省略 = 同站同类型单序列）；避雷器显式写 | 声明健壮性；若要求必填则存量单序列场景需显式声明键 |
| 6 | 计数器更换/换表导致读数归零（业务合法倒退）如何处理 | **本提案不引入隐式复位**；现场口径确认后按 A2 的 `reset_ref` 同款机制或人工志记处理 | 现场规程口径 |
| 7 | `window` 是否需要（月核对场景是否限定「只与上月比较」） | 保留可选参数，**缺省不限窗口** | 现场核对口径 |

## 6. 影响面（采纳后各 PR 的分工）

- `docs/design.md`：§7.3（`when` 注改写 + T1/T3 算子表增 `monotonic`、`condition` 说明）、§7.4 避雷器行、§12 待定项收敛（C1/A3 由「待 M2」转「已定」）。
- `docs/dsl/README.md`：冻结文法表增 `monotonic` 行；`when` 扩展注记。
- `src/records_kit/registry/meta.py`：`when` 算子白名单 + 集合成员校验 + 歧义守卫；`monotonic` 参数校验；若采纳 §2.4，`RULE_KINDS` 增 `condition`。
- `src/records_kit/engine/rules.py`：`when_matches` 支持前缀算子；T3 judge 表增 `monotonic`；条件型规则判定分支。
- 声明转正：`docs/dsl/surge_arrester_action_record.toml` 两条 TODO 规则（`non_storm_action`、`counter_monotonic`）即本提案的两个用例。
- 测试：`when` 四算子 + 空值 + 歧义守卫反例（§4.1 全 8 例）；`monotonic` 六边界（§4.2 全 6 例）；条件型规则「未命中出 pass 条目」。

## 7. 本文件自查口径

- 设计提案，**不含实现**：示例声明片段仅作文法演示，未在引擎中加载执行；实际可加载性由实施 PR 的测试证明。
- 引用均为仓库内既有文件与已合入条款（§7.2/§7.3 文法、冻结 T3 文法表、缺口清单 C1/A3）。
- 无真实业务数据、单号、人员信息、本机路径。
