"""文法扩展（M2）单测：``when`` 四算子 + 条件型规则 + 跨记录 ``monotonic``。

依据 ``docs/dsl-grammar-ext-proposal.md``（阶段 2 终稿）§6：**17 例边界例
「实现 PR 必须逐例落为测试用例」**——本文件逐例对应，用例名与 docstring 标注例号
（``§6.1-1`` 等），便于与文稿逐条对单。

全部为合成数据（synthetic）：站名/人名沿用 conftest 占位约定；声明经 meta 校验
真实加载（坏声明拒绝用例同样走加载路径），账本行为合成行。
"""

from __future__ import annotations

import pytest

from records_kit.engine.rules import evaluate_rules, when_matches
from records_kit.registry.declaration import DeclarationError, render_when

OCCURRED = "2026-09-17T10:00:00+08:00"
EARLIER = "2026-07-01T10:00:00+08:00"
EARLIEST = "2026-06-01T10:00:00+08:00"
ACTION_CODES = ["REPORT_NON_STORM_ACTION", "CHECK_COUNTER_ABNORMAL"]

# 与避雷器动作记录簿同形的合成字段（§4.3 / §5.4 的两条规则落此形）
ARRESTER_FIELDS = [
    {"key": "phase", "name": "相别", "type": "enum", "options": ["A", "B", "C"], "required": True},
    {"key": "install_location", "name": "安装位置", "type": "enum", "options": ["线路侧", "母线侧"], "required": True},
    {"key": "counter_reading", "name": "计数器累计读数", "type": "number", "decimals": 0, "min": 0.0, "required": True},
    {"key": "weather", "name": "天气", "type": "enum",
     "options": ["雷雨", "操作过电压", "大风", "冰雹", "冰雪", "其他"], "required": False},
]

SPOT_FIELDS = [
    {"key": "spot_no", "name": "部位编号", "type": "text", "required": True},
    {"key": "spot", "name": "检查部位", "type": "enum",
     "options": ["配电室", "电缆沟", "门窗孔洞"], "required": True},
]

NON_STORM_RULE = {
    "id": "non_storm_action",
    "kind": "condition",
    "tier": 1,
    "when": {"weather": "not_in:雷雨"},
    "level": "warn",
    "action": "REPORT_NON_STORM_ACTION",
}
COUNTER_RULE = {
    "id": "counter_monotonic",
    "kind": "limit",
    "tier": 3,
    "expr": "monotonic:counter_reading,key=phase+install_location,op=ge",
    "level": "warn",
    "action": "CHECK_COUNTER_ABNORMAL",
}
COUNTER_PLAIN_RULE = {
    "id": "counter_monotonic",
    "kind": "limit",
    "tier": 3,
    "expr": "monotonic:counter_reading",
    "level": "warn",
    "action": "CHECK_COUNTER_ABNORMAL",
}
SPOT_RULE = {
    "id": "off_site_check",
    "kind": "condition",
    "tier": 1,
    "when": {"items.spot": "not_in:配电室,电缆沟"},
    "level": "warn",
}


# ---------------------------------------------------------------- 声明 / 判定夹具


def _rule_block(helpers, rule: dict) -> str:
    """规则段的 TOML 文本：``when`` 内联表的键一律加引号。

    conftest 的序列化对键不加引号，``items.spot`` 会被解析成嵌套表 ``{items = {spot = …}}``；
    带引号才是「点号路径字符串」这一实际声明形态。
    """
    lines = ["[[rules]]"]
    for name, value in rule.items():
        if name == "when":
            pairs = ", ".join(f'"{key}" = {helpers.toml_value(item)}' for key, item in value.items())
            lines.append(f"when = {{ {pairs} }}")
        else:
            lines.append(f"{name} = {helpers.toml_value(value)}")
    return "\n".join(lines)


def _build(synthetic_registry, helpers, *, record_type, fields, rules, items=False):
    """合成声明 → 经 meta 真实加载的 ``Declaration``。"""
    text = helpers.build_toml(
        meta={
            "record_type": record_type,
            "layout": "item_list" if items else "flat",
            "dedupe_key": ["station", "occurred_day"],
            "action_codes": list(ACTION_CODES),
        },
        fields=fields,
        items=items,
        rules=[],
        trend=[],
    )
    for rule in rules:
        text += "\n" + _rule_block(helpers, rule) + "\n"
    return synthetic_registry(toml_text=text, filename=f"{record_type}.toml")[record_type]


def _judge(declaration, fields, *, rows=(), occurred_at=OCCURRED, current_record_uid=None):
    """跑一轮规则判定，返回结论条目（合成账本视图）。"""
    report = evaluate_rules(
        declaration,
        fields,
        ledger_view={"confirmed_digests": [], "same_type_records": list(rows), "linked_records": []},
        baselines=None,
        occurred_at=occurred_at,
        current_record_uid=current_record_uid,
    )
    return report.entries


def _row(record_uid, fields, *, lifecycle="confirmed", occurred_at=OCCURRED, rev=1, confirmed_fields=None):
    row = {
        "record_uid": record_uid,
        "lifecycle": lifecycle,
        "rev": rev,
        "occurred_at": occurred_at,
        "digest": "sha256:" + "0" * 64,
        "fields": dict(fields),
    }
    if confirmed_fields is not None:
        row["confirmed_fields"] = dict(confirmed_fields)
    return row


def _rule_of(declaration, kind, rule_id=None):
    for rule in declaration.rules:
        if rule.kind == kind and (rule_id is None or rule.id == rule_id):
            return rule
    raise AssertionError(f"声明里没有 {kind} 规则：{rule_id}")


@pytest.fixture
def arrestor_declaration(synthetic_registry, helpers):
    return _build(
        synthetic_registry,
        helpers,
        record_type="synthetic_arrester",
        fields=ARRESTER_FIELDS,
        rules=[NON_STORM_RULE, COUNTER_RULE],
    )


@pytest.fixture
def counter_declaration(synthetic_registry, helpers):
    """单序列（``key`` 省略）单调声明：§6.3 例 1–5。"""
    return _build(
        synthetic_registry,
        helpers,
        record_type="synthetic_counter",
        fields=ARRESTER_FIELDS,
        rules=[COUNTER_PLAIN_RULE],
    )


@pytest.fixture
def spot_declaration(synthetic_registry, helpers):
    """条目字段（存在量词）声明：§6.1 例 7–8。"""
    return _build(
        synthetic_registry,
        helpers,
        record_type="synthetic_spot",
        fields=[{"key": "checker", "name": "检查人", "type": "text", "required": True}],
        items={"key_field": "spot_no", "fields": SPOT_FIELDS},
        rules=[SPOT_RULE],
    )


def _by_id(entries, rule_id):
    return [entry for entry in entries if entry["rule_id"] == rule_id]


# ---------------------------------------------------------------- §6.1 when（8 例）


def test_when_ex1_not_in_hits_non_storm_weather(arrestor_declaration):
    """§6.1-1：``not_in:雷雨`` + 操作过电压 → 命中（C1 正解：非雷雨动作 → warn）。"""
    rule = _rule_of(arrestor_declaration, "condition", "non_storm_action")
    assert when_matches(arrestor_declaration, {"weather": "操作过电压"}, rule.when) is True
    entry = _by_id(_judge(arrestor_declaration, {"weather": "操作过电压"}), "non_storm_action")[0]
    assert entry["verdict"] == "violation"


def test_when_ex2_not_in_misses_storm_weather(arrestor_declaration):
    """§6.1-2：``not_in:雷雨`` + 雷雨 → 不命中（雷雨是白名单值，不告警）。"""
    rule = _rule_of(arrestor_declaration, "condition", "non_storm_action")
    assert when_matches(arrestor_declaration, {"weather": "雷雨"}, rule.when) is False
    entry = _by_id(_judge(arrestor_declaration, {"weather": "雷雨"}), "non_storm_action")[0]
    assert entry["verdict"] == "pass"


def test_when_ex3_absent_field_never_hits(arrestor_declaration):
    """§6.1-3：``weather`` 缺省（可选字段）→ 不命中；未填 ≠ 非雷雨，不因缺列误报。"""
    rule = _rule_of(arrestor_declaration, "condition", "non_storm_action")
    assert when_matches(arrestor_declaration, {}, rule.when) is False
    entry = _by_id(_judge(arrestor_declaration, {}), "non_storm_action")[0]
    assert entry["verdict"] == "pass"


def test_when_ex4_not_in_multi_member_hits(arrestor_declaration, synthetic_registry, helpers):
    """§6.1-4：``not_in:雷雨,大风`` + 冰雹 → 命中（集合不匹配多成员）。"""
    declaration = _build(
        synthetic_registry,
        helpers,
        record_type="synthetic_when_multi",
        fields=ARRESTER_FIELDS,
        rules=[{**NON_STORM_RULE, "when": {"weather": "not_in:雷雨,大风"}}],
    )
    assert when_matches(declaration, {"weather": "冰雹"}, {"weather": "not_in:雷雨,大风"}) is True
    entry = _by_id(_judge(declaration, {"weather": "冰雹"}), "non_storm_action")[0]
    assert entry["verdict"] == "violation"


def test_when_ex5_in_misses_outside_member(arrestor_declaration, synthetic_registry, helpers):
    """§6.1-5：``in:雷雨,冰雹`` + 冰雪 → 不命中（``in`` / ``not_in`` 互补性可测）。"""
    declaration = _build(
        synthetic_registry,
        helpers,
        record_type="synthetic_when_in",
        fields=ARRESTER_FIELDS,
        rules=[{**NON_STORM_RULE, "when": {"weather": "in:雷雨,冰雹"}}],
    )
    assert when_matches(declaration, {"weather": "冰雪"}, {"weather": "in:雷雨,冰雹"}) is False
    entry = _by_id(_judge(declaration, {"weather": "冰雪"}), "non_storm_action")[0]
    assert entry["verdict"] == "pass"


def test_when_ex5_in_hits_member(arrestor_declaration, synthetic_registry, helpers):
    """§6.1-5 对侧：``in:雷雨,冰雹`` + 冰雹 → 命中（``in`` 与 ``not_in`` 互补）。"""
    declaration = _build(
        synthetic_registry,
        helpers,
        record_type="synthetic_when_in_hit",
        fields=ARRESTER_FIELDS,
        rules=[{**NON_STORM_RULE, "when": {"weather": "in:雷雨,冰雹"}}],
    )
    assert when_matches(declaration, {"weather": "冰雹"}, {"weather": "in:雷雨,冰雹"}) is True


def test_when_ex6_ambiguous_enum_value_refused(synthetic_registry, helpers):
    """§6.1-6：字段 ``options`` 含算子前缀取值 ``not_in:x`` → 拒绝加载（歧义守卫）。"""
    fields = [
        {"key": "weather", "name": "天气", "type": "enum",
         "options": ["雷雨", "not_in:x"], "required": True},
    ]
    rule = {**NON_STORM_RULE, "when": {"weather": "not_in:x"}}
    with pytest.raises(DeclarationError) as excinfo:
        _build(
            synthetic_registry,
            helpers,
            record_type="synthetic_ambiguous",
            fields=fields,
            rules=[rule],
        )
    assert "歧义守卫" in str(excinfo.value), str(excinfo.value)


def test_when_ex7_items_existential_hits(spot_declaration):
    """§6.1-7：``items.spot = "not_in:配电室,电缆沟"`` + 条目 [配电室, 门窗孔洞] → 命中。

    存在量词：**存在**一个部位在集合外即命中。
    """
    payload = {"checker": "张三", "items": [
        {"spot_no": "S-1", "spot": "配电室"},
        {"spot_no": "S-2", "spot": "门窗孔洞"},
    ]}
    rule = _rule_of(spot_declaration, "condition", "off_site_check")
    assert when_matches(spot_declaration, payload, rule.when) is True
    assert _judge(spot_declaration, payload)[0]["verdict"] == "violation"


def test_when_ex8_items_existential_misses(spot_declaration):
    """§6.1-8：同上 + 条目 [配电室, 电缆沟] → 不命中（全部条目都在集合内才不命中）。"""
    payload = {"checker": "张三", "items": [
        {"spot_no": "S-1", "spot": "配电室"},
        {"spot_no": "S-2", "spot": "电缆沟"},
    ]}
    rule = _rule_of(spot_declaration, "condition", "off_site_check")
    assert when_matches(spot_declaration, payload, rule.when) is False
    assert _judge(spot_declaration, payload)[0]["verdict"] == "pass"


# ---------------------------------------------------------------- §6.2 条件型规则（3 例）


def test_condition_ex1_hit_emits_violation_with_normalized_threshold(arrestor_declaration):
    """§6.2-1：``when`` 命中 → ``violation`` 条目，``threshold`` = 规范化 ``when`` 文本。"""
    entry = _by_id(_judge(arrestor_declaration, {"weather": "操作过电压"}), "non_storm_action")[0]
    assert entry["verdict"] == "violation"
    assert entry["kind"] == "condition"
    assert entry["tier"] == 1
    assert entry["threshold"] == "weather not_in 雷雨"
    assert entry["level"] == "warn"
    assert "操作过电压" in entry["detail"]


def test_condition_ex2_miss_still_emits_pass_entry(arrestor_declaration):
    """§6.2-2：``when`` 未命中 → 出 ``pass`` 条目（**不是**不出条目），detail 可见「查过且通过」。"""
    entries = _judge(arrestor_declaration, {"weather": "雷雨"})
    entry = _by_id(entries, "non_storm_action")[0]
    assert entry["verdict"] == "pass"
    assert entry["threshold"] == "weather not_in 雷雨"
    assert "查过且通过" in entry["detail"]
    assert "雷雨" in entry["detail"]


@pytest.mark.parametrize(
    ("fragment", "rule"),
    [
        ("必须声明非空 when", {"id": "r", "kind": "condition", "tier": 1, "level": "warn"}),
        ("禁止 expr", {"id": "r", "kind": "condition", "tier": 1, "when": {"weather": "not_in:雷雨"}, "expr": "lte:5.0"}),
        ("禁止 target", {"id": "r", "kind": "condition", "tier": 1, "when": {"weather": "not_in:雷雨"}, "target": "counter_reading"}),
        ("tier 固定 1", {"id": "r", "kind": "condition", "tier": 3, "when": {"weather": "not_in:雷雨"}}),
        ("禁止 expr", {"id": "r", "kind": "condition", "tier": 1, "when": {"weather": "not_in:雷雨"}, "expr": "band:1,2"}),
    ],
)
def test_condition_ex3_bad_declarations_are_refused(synthetic_registry, helpers, fragment, rule):
    """§6.2-3：声明缺 ``when``（或同时给了 ``target`` / ``expr``）→ 拒绝加载。"""
    with pytest.raises(DeclarationError) as excinfo:
        _build(
            synthetic_registry,
            helpers,
            record_type="synthetic_bad_condition",
            fields=ARRESTER_FIELDS,
            rules=[rule],
        )
    assert fragment in str(excinfo.value), str(excinfo.value)


# ---------------------------------------------------------------- §6.3 monotonic（6 例）


def test_monotonic_ex1_no_candidate_skips(counter_declaration):
    """§6.3-1：无候选行 + 本次 7 → ``skipped``（首次记录无前值可核；不静默通过）。"""
    entry = _judge(counter_declaration, {"counter_reading": 7}, rows=[])[0]
    assert entry["verdict"] == "skipped"
    assert "不静默通过" in entry["detail"]


def test_monotonic_ex2_equal_reading_passes(counter_declaration):
    """§6.3-2：07-01 读数 7 + 本次 7 → ``pass``（``ge`` 允许持平）。"""
    rows = [_row("u1", {"counter_reading": 7}, occurred_at=EARLIER)]
    entry = _judge(counter_declaration, {"counter_reading": 7}, rows=rows)[0]
    assert entry["verdict"] == "pass"
    assert entry["threshold"] == "monotonic:counter_reading"


def test_monotonic_ex3_regression_violates_with_previous_detail(counter_declaration):
    """§6.3-3：07-01 读数 7 + 本次 6 → ``violation``(warn)；detail 给出 6 / 7 / 前值 uid 与日期。"""
    rows = [_row("u1", {"counter_reading": 7}, occurred_at=EARLIER)]
    report = evaluate_rules(
        counter_declaration,
        {"counter_reading": 6},
        ledger_view={"confirmed_digests": [], "same_type_records": rows, "linked_records": []},
        baselines=None,
        occurred_at=OCCURRED,
    )
    entry = report.entries[0]
    assert entry["verdict"] == "violation"
    assert entry["level"] == "warn"
    for fragment in ("6", "7", "u1", EARLIER, "倒退"):
        assert fragment in entry["detail"], entry["detail"]
    # 动作码随 violation 出提示；warn 级不进告警候选（与既有 limit 规则同口径）
    assert any(action["code"] == "CHECK_COUNTER_ABNORMAL" for action in report.actions)
    assert report.alarm_candidates == []


def test_monotonic_ex4_voided_row_is_skipped(counter_declaration):
    """§6.3-4：07-01 行已 voided，其上 06-01 读数 5 + 本次 6 → ``pass``（跳过 voided 取再前一条）。"""
    rows = [
        _row("u1", {"counter_reading": 7}, occurred_at=EARLIER, lifecycle="voided"),
        _row("u2", {"counter_reading": 5}, occurred_at=EARLIEST),
    ]
    entry = _judge(counter_declaration, {"counter_reading": 6}, rows=rows)[0]
    assert entry["verdict"] == "pass"
    assert "5" in entry["detail"]
    assert "u2" in entry["detail"]


def test_monotonic_ex5_confirmed_fields_wins(counter_declaration):
    """§6.3-5：同链旧版本读数 7、最后已确认版读数 9 + 本次 9 → ``pass``（``confirmed_fields`` 优先）。

    断言写「前值 9.0」而非裸 ``9``——否则「本次 9」也会满足弱断言，前值方向取错照样通过。
    """
    rows = [
        _row("u1", {"counter_reading": 7}, occurred_at=EARLIER, rev=1),
        _row("u1", {"counter_reading": 7}, occurred_at=EARLIER, rev=2, confirmed_fields={"counter_reading": 9}),
    ]
    entry = _judge(counter_declaration, {"counter_reading": 9}, rows=rows)[0]
    assert entry["verdict"] == "pass"
    assert "前值 9.0" in entry["detail"], entry["detail"]


def test_monotonic_ex5_low_version_is_not_used_as_previous(counter_declaration):
    """§6.3-5 判别性变体：本次 8 < 最后已确认版 9 → ``violation``。

    若同刻并列取到低版本行（前值 7），``8 ≥ 7`` 会假通过——读数倒退被漏报。
    """
    rows = [
        _row("u1", {"counter_reading": 7}, occurred_at=EARLIER, rev=1),
        _row("u1", {"counter_reading": 7}, occurred_at=EARLIER, rev=2, confirmed_fields={"counter_reading": 9}),
    ]
    entry = _judge(counter_declaration, {"counter_reading": 8}, rows=rows)[0]
    assert entry["verdict"] == "violation"
    assert "前值 9.0" in entry["detail"], entry["detail"]


def test_monotonic_ex5_own_correct_chain_is_excluded(counter_declaration):
    """§6.3-5 对侧：当前记录自己的 correct 链不入前值 —— 排除后无候选 → ``skipped``（不自比）。"""
    rows = [
        _row("u1", {"counter_reading": 7}, occurred_at=EARLIER, rev=1),
        _row("u1", {"counter_reading": 7}, occurred_at=EARLIER, rev=2, confirmed_fields={"counter_reading": 9}),
    ]
    entry = _judge(counter_declaration, {"counter_reading": 9}, rows=rows, current_record_uid="u1")[0]
    assert entry["verdict"] == "skipped"


def test_monotonic_ex6_key_isolation(arrestor_declaration):
    """§6.3-6：A 相 7、B 相 3（``key=phase+install_location``）+ A 相本次 6 → ``violation``；B 相不参与。"""
    rows = [
        _row("u1", {"phase": "A", "install_location": "线路侧", "counter_reading": 7, "weather": "雷雨"},
             occurred_at=EARLIER),
        _row("u2", {"phase": "B", "install_location": "线路侧", "counter_reading": 3, "weather": "雷雨"},
             occurred_at=EARLIER),
    ]
    entry = _by_id(
        _judge(
            arrestor_declaration,
            {"phase": "A", "install_location": "线路侧", "counter_reading": 6, "weather": "雷雨"},
            rows=rows,
        ),
        "counter_monotonic",
    )[0]
    assert entry["verdict"] == "violation"
    assert "7" in entry["detail"]  # 前值取 A 相，而不是 B 相的 3


# ---------------------------------------------------------------- monotonic 参数扩展（op / window / 分组）


def test_monotonic_gt_requires_strict_increase(synthetic_registry, helpers):
    """``op=gt``：必须严格递增，持平即 violation（§5.2 参数表）。"""
    declaration = _build(
        synthetic_registry,
        helpers,
        record_type="synthetic_counter_gt",
        fields=ARRESTER_FIELDS,
        rules=[{**COUNTER_PLAIN_RULE, "expr": "monotonic:counter_reading,op=gt"}],
    )
    rows = [_row("u1", {"counter_reading": 7}, occurred_at=EARLIER)]
    assert _judge(declaration, {"counter_reading": 7}, rows=rows)[0]["verdict"] == "violation"
    assert _judge(declaration, {"counter_reading": 8}, rows=rows)[0]["verdict"] == "pass"


def test_monotonic_window_out_of_range_skips(synthetic_registry, helpers):
    """``window=30d``：前值超出窗口 → 视为无前值 → ``skipped``（§5.2 / §5.3-5）。"""
    declaration = _build(
        synthetic_registry,
        helpers,
        record_type="synthetic_counter_window",
        fields=ARRESTER_FIELDS,
        rules=[{**COUNTER_PLAIN_RULE, "expr": "monotonic:counter_reading,window=30d"}],
    )
    stale = [_row("u1", {"counter_reading": 7}, occurred_at=EARLIEST)]
    entry = _judge(declaration, {"counter_reading": 6}, rows=stale)[0]
    assert entry["verdict"] == "skipped"
    assert "超出窗口" in entry["detail"]
    fresh = [_row("u1", {"counter_reading": 7}, occurred_at="2026-09-10T10:00:00+08:00")]
    assert _judge(declaration, {"counter_reading": 6}, rows=fresh)[0]["verdict"] == "violation"


def test_monotonic_latest_occurred_at_wins(synthetic_registry, helpers):
    """前值选取第 3 步：``occurred_at`` 最大者优先（乱序传入也不影响结论）。"""
    declaration = _build(
        synthetic_registry,
        helpers,
        record_type="synthetic_counter_order",
        fields=ARRESTER_FIELDS,
        rules=[COUNTER_PLAIN_RULE],
    )
    rows = [
        _row("u1", {"counter_reading": 9}, occurred_at="2026-05-01T10:00:00+08:00"),
        _row("u2", {"counter_reading": 4}, occurred_at=EARLIER),
        _row("u3", {"counter_reading": 2}, occurred_at=EARLIEST),
    ]
    entry = _judge(declaration, {"counter_reading": 5}, rows=rows)[0]
    assert entry["verdict"] == "pass"
    assert "4" in entry["detail"]  # 前值取 07-01 的 4，而不是更早/更晚的行


def test_monotonic_missing_key_field_skips(arrestor_declaration):
    """分组键字段缺省 → 不可判定（``skipped``），不当作空键参与比较。"""
    entry = _by_id(_judge(arrestor_declaration, {"counter_reading": 6, "weather": "雷雨"}), "counter_monotonic")[0]
    assert entry["verdict"] == "skipped"


# ---------------------------------------------------------------- meta 校验（坏声明 → 拒绝加载）


@pytest.mark.parametrize(
    ("fragment", "rule"),
    [
        ("需要且仅需要 1 个位置参数", {"id": "r", "kind": "limit", "tier": 3, "expr": "monotonic"}),
        ("必须是顶层 number 字段", {"id": "r", "kind": "limit", "tier": 3, "expr": "monotonic:weather"}),
        ("引用不存在的字段", {"id": "r", "kind": "limit", "tier": 3, "expr": "monotonic:nope"}),
        ("op 只能是", {"id": "r", "kind": "limit", "tier": 3, "expr": "monotonic:counter_reading,op=le"}),
        ("window 需要 Nd 形式", {"id": "r", "kind": "limit", "tier": 3, "expr": "monotonic:counter_reading,window=30"}),
        ("window 需要 Nd 形式", {"id": "r", "kind": "limit", "tier": 3, "expr": "monotonic:counter_reading,window=0d"}),
        ("未知参数", {"id": "r", "kind": "limit", "tier": 3, "expr": "monotonic:counter_reading,ref=x"}),
        ("引用不存在的字段", {"id": "r", "kind": "limit", "tier": 3, "expr": "monotonic:counter_reading,key=nope"}),
        ("不设 target", {"id": "r", "kind": "limit", "tier": 3, "target": "counter_reading", "expr": "monotonic:counter_reading"}),
    ],
)
def test_monotonic_bad_declarations_are_refused(synthetic_registry, helpers, fragment, rule):
    """monotonic 坏声明一律拒绝加载（§5.2 参数约束）。"""
    with pytest.raises(DeclarationError) as excinfo:
        _build(
            synthetic_registry,
            helpers,
            record_type="synthetic_bad_monotonic",
            fields=ARRESTER_FIELDS,
            rules=[rule],
        )
    assert fragment in str(excinfo.value), str(excinfo.value)


@pytest.mark.parametrize(
    ("fragment", "when"),
    [
        ("未落地的算子", {"weather": "gt:雷雨"}),
        ("未落地的算子", {"weather": "lte:5"}),
        ("不在字段 weather 的枚举内", {"weather": "not_in:暴雨"}),
        ("不在字段 weather 的枚举内", {"weather": "in:雷雨,暴雨"}),
        ("集合含空成员", {"weather": "not_in:雷雨,"}),
    ],
)
def test_when_bad_declarations_are_refused(synthetic_registry, helpers, fragment, when):
    """``when`` 坏值一律拒绝加载（§3.5-1/2）：预留算子、集合成员越界、空成员。"""
    with pytest.raises(DeclarationError) as excinfo:
        _build(
            synthetic_registry,
            helpers,
            record_type="synthetic_bad_when",
            fields=ARRESTER_FIELDS,
            rules=[{**NON_STORM_RULE, "when": when}],
        )
    assert fragment in str(excinfo.value), str(excinfo.value)


def test_when_literal_still_means_equality(arrestor_declaration):
    """§3.6 向后兼容：不带前缀的字面量仍是等值语义（存量声明零改动）。"""
    assert when_matches(arrestor_declaration, {"weather": "雷雨"}, {"weather": "雷雨"}) is True
    assert when_matches(arrestor_declaration, {"weather": "大风"}, {"weather": "雷雨"}) is False
    assert when_matches(arrestor_declaration, {"weather": "操作过电压"}, {"weather": "eq:操作过电压"}) is True


def test_render_when_normalizes_operators_and_joins_conditions(arrestor_declaration):
    """``render_when``：条件型规则 ``threshold`` 的规范化口径（多条件 AND 以「且」连接）。"""
    assert render_when({"weather": "not_in:雷雨"}) == "weather not_in 雷雨"
    assert render_when({"weather": "雷雨"}) == "weather eq 雷雨"
    assert render_when({"weather": "not_in:雷雨", "phase": "in:A,B"}) == "weather not_in 雷雨 且 phase in A,B"
    assert render_when({}) == ""


# ---------------------------------------------------------------- 两条规则同声明（接口冻结的自测）

def test_two_rules_load_and_both_verdicts_appear(arrestor_declaration):
    """验收口径 3 的合成等价：两条规则可加载，并在样例上各出一次 ``violation`` 与 ``pass``。

    真实避雷器声明的规则转正属声明线（序 2），本 PR 只冻结引擎接口；此用例证
    「同声明共存 + 各自出条目」的接口行为。
    """
    rows = [_row("u1", {"phase": "A", "install_location": "线路侧", "counter_reading": 7, "weather": "雷雨"},
                 occurred_at=EARLIER)]
    entries = _judge(
        arrestor_declaration,
        {"phase": "A", "install_location": "线路侧", "counter_reading": 7, "weather": "操作过电压"},
        rows=rows,
    )
    assert [entry["rule_id"] for entry in entries] == ["non_storm_action", "counter_monotonic"]
    assert _by_id(entries, "non_storm_action")[0]["verdict"] == "violation"  # 非雷雨动作
    assert _by_id(entries, "counter_monotonic")[0]["verdict"] == "pass"  # 读数持平
    assert _by_id(entries, "non_storm_action")[0]["threshold"] == "weather not_in 雷雨"
