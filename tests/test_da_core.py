"""da_core 核心系统：账本往返 / 装配 / 全链（免签自动定稿）/ 幂等 / 口径隔离。"""

from __future__ import annotations

import pytest

from da_core import table_projection
from da_core.intake import IntakeError
from da_core.ledger import Ledger
from da_core.service import submit_submission
from da_core.settings import Settings

STATION = {"station_id": "ST001", "station_name": "XX风电场"}
OCCURRED = "2026-09-21T10:00:00+08:00"


def make_settings(tmp_path) -> Settings:
    return Settings.default(db_path=tmp_path / "ledger.sqlite", station=dict(STATION))


def submission_12v(**overrides) -> dict:
    data = {
        "client_submission_id": "test-12v-001",
        "operator": "张三",
        "submitted_at": OCCURRED,
        "groups": [
            {
                "group": "3号组(12只)",
                "dc_system_id": "DC-003",
                "float_voltage": 13.5,
                "test_kind": "定期",
                "env_temp": 25,
                "items": [{"no": i, "volt": round(13.30 + i * 0.01, 2)} for i in range(1, 12)]
                + [{"no": 12, "volt": 13.95}],
            }
        ],
    }
    data.update(overrides)
    return data


def violations_for(rules: list[dict], cell_no: int) -> list[dict]:
    import re

    marker = re.compile(rf"cell_no={cell_no}(?!\d)")
    return [entry for entry in rules
            if entry.get("verdict") == "violation"
            and marker.search(str(entry.get("detail", "")))]


def test_full_chain_auto_confirm(tmp_path):
    settings = make_settings(tmp_path)
    ledger = Ledger(settings.db_path)
    summary = submit_submission(submission_12v(), settings=settings, ledger=ledger)

    assert summary["ok"] is True
    group = summary["groups"][0]
    assert group["status"] == "ok"
    assert group["lifecycle"] == "confirmed"
    assert group["record_uid"] == "ST001-battery_voltage_test-20260921-1000-1"

    view = ledger.build_ledger_view("ST001", "battery_voltage_test")
    assert len(view["same_type_records"]) == 1
    assert view["same_type_records"][0]["lifecycle"] == "confirmed"
    assert len(view["confirmed_digests"]) == 1
    assert ledger.counts()["records"] == 1
    assert ledger.counts()["record_versions"] == 1

    assert violations_for(group["rules"], 12), "12 号单体越上限应判 violation"
    assert not violations_for(group["rules"], 1)


def test_scope_isolation_2v_vs_12v(tmp_path):
    settings = make_settings(tmp_path)
    data = submission_12v(client_submission_id="test-two-scopes")
    data["groups"].append({
        "group": "1号组(104只)",
        "dc_system_id": "DC-001",
        "float_voltage": 241.5,
        "test_kind": "定期",
        "env_temp": 24,
        "items": [{"no": 1, "volt": 1.95}, {"no": 2, "volt": 1.97}],
    })
    summary = submit_submission(data, settings=settings)
    assert summary["ok"] is True
    assert summary["counts"]["cells"] == 14
    by_group = {group["group"]: group for group in summary["groups"]}

    twelve = by_group["3号组(12只)"]
    thresholds_12 = {entry["threshold"] for entry in twelve["rules"]
                     if entry["rule_id"] == "voltage_band"}
    assert thresholds_12 == {"band:11.85,13.8"}

    two = by_group["1号组(104只)"]
    thresholds_2 = {entry["threshold"] for entry in two["rules"]
                    if entry["rule_id"] == "voltage_band"}
    assert thresholds_2 == {"band:1.85,2.35"}
    assert not violations_for(two["rules"], 1), "1.95 在新带 1.85–2.35 内，不应越界"


def test_idempotent_replay(tmp_path):
    settings = make_settings(tmp_path)
    first = submit_submission(submission_12v(), settings=settings)
    second = submit_submission(submission_12v(), settings=settings)
    assert second.get("replayed") is True
    assert second["groups"][0]["record_uid"] == first["groups"][0]["record_uid"]
    ledger = Ledger(settings.db_path)
    assert ledger.counts()["records"] == 1


def test_dedupe_same_group_same_day_rejected(tmp_path):
    settings = make_settings(tmp_path)
    assert submit_submission(submission_12v(), settings=settings)["ok"] is True
    replay = submission_12v(client_submission_id="test-12v-002")
    summary = submit_submission(replay, settings=settings)
    assert summary["ok"] is False
    rejected = summary["groups"][0]
    assert rejected["status"] == "rejected"
    codes = {(error["code"], error["path"]) for error in rejected["validation"]["errors"]}
    assert ("E_DUP_KEY", "payload") in codes


def test_row_composition(tmp_path):
    settings = make_settings(tmp_path)
    summary = submit_submission(submission_12v(), settings=settings)
    group = summary["groups"][0]
    field_ids = settings.table["field_ids"]
    rows = table_projection.compose_rows(group, field_ids=field_ids)
    assert len(rows) == 12

    ok_row = rows[0]
    assert ok_row[field_ids["是否异常"]] == "正常"
    assert field_ids["判定说明"] not in ok_row
    assert ok_row[field_ids["电池组别"]] == "3号组(12只)"
    assert ok_row[field_ids["直流系统编号"]] == "DC-003"
    assert ok_row[field_ids["浮充电压(V)"]] == 13.5
    assert ok_row[field_ids["环境温度(℃)"]] == 25

    bad_row = rows[-1]
    assert bad_row[field_ids["是否异常"]] == "异常"
    assert "cell_no=12" in bad_row[field_ids["判定说明"]]

    assert ok_row[field_ids["账本UID"]] == group["record_uid"]
    assert ok_row[field_ids["账本Rev"]] == 1
    assert ok_row[field_ids["账本状态"]] == "已定稿"


def test_dispatch_dry_run(tmp_path):
    settings = make_settings(tmp_path)
    summary = submit_submission(submission_12v(), settings=settings)
    result = table_projection.dispatch(summary["groups"], settings=settings, dry_run=True)
    assert result["dry_run"] is True
    assert result["rows"] == 12
    assert len(result["sample"]) == 2


def test_alarm_ledger_counts(tmp_path):
    ledger = Ledger(tmp_path / "alarm.sqlite")
    state = {"fingerprint": "fp-test", "state": "new", "suppressed": False}
    ledger.update_alarm("ST001", "battery_voltage_test", state, OCCURRED)
    ledger.update_alarm("ST001", "battery_voltage_test",
                        {**state, "state": "recurred"}, OCCURRED)
    history = ledger.build_alarm_history("ST001", "battery_voltage_test")
    assert len(history) == 1
    assert history[0]["occur_count"] == 2
    assert history[0]["last_status"] == "recurred"


def test_intake_errors_are_readable(tmp_path):
    settings = make_settings(tmp_path)
    bad = {"client_submission_id": "x", "operator": "张三", "groups": [{"group": "9号组"}]}
    with pytest.raises(IntakeError, match="缺字段"):
        submit_submission(bad, settings=settings)


def test_extract_record_ids_from_create_response():
    from da_core.table_projection import _extract_record_ids

    stdout = '{"data": {"newRecordIds": ["rec1", "rec2"]}, "status": "success"}'
    assert _extract_record_ids(stdout) == ["rec1", "rec2"]
    assert _extract_record_ids("not json") == []
    assert _extract_record_ids('{"data": {"records": [{"recordId": "r3"}]}}') == ["r3"]


def test_correct_then_auto_confirm(tmp_path):
    from da_core.service import correct_submission

    settings = make_settings(tmp_path)
    summary = submit_submission(submission_12v(), settings=settings)
    uid = summary["groups"][0]["record_uid"]
    ledger = Ledger(settings.db_path)

    corrected_payload = {
        "dc_system_id": "DC-003",
        "float_voltage": 13.5,
        "test_kind": "定期",
        "env_temp": 25,
        "items": [{"no": number, "volt": 13.40} for number in range(1, 13)],
    }
    result = correct_submission(corrected_payload, record_uid=uid, actor="李四",
                                settings=settings, ledger=ledger)

    assert result["status"] == "ok"
    assert result["rev"] == 2
    assert result["lifecycle"] == "confirmed"
    assert [link["type"] for link in result["links"]] == ["supersedes"]

    record = ledger.get_record(uid)
    assert record["rev"] == 2 and record["lifecycle"] == "confirmed"
    view = ledger.build_ledger_view("ST001", "battery_voltage_test")
    row = view["same_type_records"][0]
    assert row["rev"] == 2 and row["lifecycle"] == "confirmed"
    assert all(item["voltage"] == 13.4 for item in row["fields"]["items"])
    assert len(view["confirmed_digests"]) == 2  # rev1 + rev2 并存
    assert not violations_for(result["rules"], 12)


def test_void_then_resubmit_allowed(tmp_path):
    from da_core.service import void_record

    settings = make_settings(tmp_path)
    summary = submit_submission(submission_12v(), settings=settings)
    uid = summary["groups"][0]["record_uid"]
    ledger = Ledger(settings.db_path)

    result = void_record(uid, reason="录入错误", actor="李四", settings=settings, ledger=ledger)
    assert result["status"] == "ok"
    assert result["lifecycle"] == "voided"
    assert ledger.get_record(uid)["lifecycle"] == "voided"

    again = submission_12v(client_submission_id="test-12v-after-void")
    summary2 = submit_submission(again, settings=settings)
    assert summary2["ok"] is True
    assert summary2["groups"][0]["record_uid"] != uid


def test_cycle_scan_and_task_sync(tmp_path):
    from da_core.scheduler import scan, sync_tasks

    settings = make_settings(tmp_path)
    ledger = Ledger(settings.db_path)
    ledger.seed_config(settings)

    # 无历史 + baseline 未配置 → awaiting_baseline，不产生任务
    report = scan(ledger, settings, now="2026-09-21T10:00:00+08:00")
    assert report["groups"][0]["next_due"] is None
    assert report["groups"][0]["status"] == "awaiting_baseline"
    actions = sync_tasks(ledger, settings, now="2026-09-21T10:00:00+08:00")
    assert len(actions["awaiting_baseline"]) == 4
    assert not actions["created"]

    # 配置 baseline → 每组建当期任务（due = baseline）
    ledger.set_cycle_config(cycle_days=30, baseline="2026-08-01", updated_by="测试")
    actions = sync_tasks(ledger, settings, now="2026-07-30T10:00:00+08:00")
    assert len(actions["created"]) == 4
    tasks = ledger.list_tasks("ST001", states=("open", "overdue"))
    three = next(t for t in tasks if "3号组" in t["task_id"])
    assert three["due_at"] == "2026-08-01" and three["state"] == "open"

    # 迟到完成（08-20 > due 08-01）→ 旧任务 done_late 结案，锚点滚动到 09-19
    late = submission_12v(client_submission_id="t2-late",
                          submitted_at="2026-08-20T10:00:00+08:00")
    assert submit_submission(late, settings=settings)["ok"] is True
    actions = sync_tasks(ledger, settings, now="2026-08-25T10:00:00+08:00")
    assert [c["state"] for c in actions["closed"]] == ["done_late"]
    assert "3号组" in actions["closed"][0]["task_id"]
    assert [c["due"] for c in actions["created"]] == ["2026-09-19"]

    # 按时完成（09-15 ≤ due 09-19）→ done 结案，锚点滚动到 10-15
    ontime = submission_12v(client_submission_id="t2-ontime",
                            submitted_at="2026-09-15T10:00:00+08:00")
    assert submit_submission(ontime, settings=settings)["ok"] is True
    actions = sync_tasks(ledger, settings, now="2026-09-21T10:00:00+08:00")
    assert [c["state"] for c in actions["closed"]] == ["done"]
    assert [c["due"] for c in actions["created"]] == ["2026-10-15"]

    # 逾期推进：now 超过 due 10-15 → 任务转 overdue
    actions = sync_tasks(ledger, settings, now="2026-10-20T10:00:00+08:00")
    assert [u["state"] for u in actions["updated"]] == ["overdue"]
    current = ledger.current_task("ST001", "battery_voltage_test", "3号组(12只)")
    assert current["state"] == "overdue" and current["overdue_since"] == "2026-10-16"


def test_outbox_deliver_receipt_and_idempotency(tmp_path):
    from da_core import outbox

    ledger = Ledger(tmp_path / "outbox.sqlite")
    task_id = "ST001|3号组(12只)|2026-10-21"
    ledger.insert_task(task_id=task_id, station_id="ST001", record_type="battery_voltage_test",
                       period_key="2026-10-21", due_at="2026-10-21", state="open",
                       overdue_since=None, opened_at="2026-09-21T10:00:00+08:00")

    calls = []

    def ok_runner(args):
        calls.append(list(args))
        return 0, '{"success": true}', ""

    spec = {"task_id": task_id, "level": 1, "channel": "group",
            "target": "APM测试", "text": "hello"}
    first = outbox.deliver(ledger, spec, runner=ok_runner)
    assert first["sent"] is True and len(calls) == 1
    assert calls[0][:3] == ["chat", "+send-to-group", "--group"]

    replay = outbox.deliver(ledger, spec, runner=ok_runner)
    assert replay["sent"] is False and replay["reason"] == "already-sent"
    assert len(calls) == 1

    def bad_runner(args):
        calls.append(list(args))
        return 1, "", "boom"

    spec2 = {"task_id": task_id, "level": 2, "channel": "group",
             "target": "APM测试", "text": "hello2"}
    failed = outbox.deliver(ledger, spec2, runner=bad_runner)
    assert failed["sent"] is False
    events = ledger.list_task_events(task_id)
    assert events[-1]["result"] == "failed" and events[-1]["retry_count"] == 0

    retried = outbox.deliver(ledger, spec2, runner=ok_runner)
    assert retried["sent"] is True and retried["retry_count"] == 1


def test_outbox_render_and_dual_channel(tmp_path):
    from da_core import outbox

    message = outbox.render_cycle_message(
        group="3号组(12只)", due="2026-10-21",
        last_done="2026-09-21T11:03:00+08:00", days_to_due=2)
    assert "3号组(12只)" in message["text"] and "10-21" in message["text"]
    assert message["text"].endswith("——AI助手")

    ledger = Ledger(tmp_path / "dual.sqlite")
    task = {"task_id": "ST001|x|2026-10-21", "due_at": "2026-10-21"}
    item = {"group": "x", "last_done_at": None, "days_to_due": 2, "overdue_days": 0}
    sent = []

    def runner(args):
        sent.append(list(args))
        return 0, "{}", ""

    results = outbox.deliver_cycle(ledger, task, item,
                                   contacts={outbox.ROLE_GROUP: "APM测试",
                                             outbox.ROLE_ASSIGNEE: "user123"},
                                   level=0, runner=runner)
    assert [r["channel"] for r in results] == ["group", "todo"]
    assert all(r["sent"] for r in results)
    assert len(sent) == 2 and sent[1][:2] == ["todo", "+create"]

    # 未配置目标 → 如实记录 no-target，不发送
    ledger2 = Ledger(tmp_path / "dual2.sqlite")
    results2 = outbox.deliver_cycle(ledger2, task, item, contacts={}, level=0,
                                    runner=runner)
    assert all(r["sent"] is False and r["reason"] == "no-target" for r in results2)


def test_escalation_ladder_and_dedup(tmp_path):
    from da_core import escalation
    from da_core.scheduler import sync_tasks

    settings = make_settings(tmp_path)
    ledger = Ledger(settings.db_path)
    ledger.seed_config(settings)
    ledger.set_cycle_config(cycle_days=30, baseline="2026-08-01", updated_by="测试")
    sync_tasks(ledger, settings, now="2026-07-25T10:00:00+08:00")
    ledger.set_contact("ST001", "reminder_group", "测试群")
    ledger.set_contact("ST001", "reminder_assignee", "user1")
    ledger.set_contact("ST001", "reminder_escalate", "班长")

    calls = []

    def runner(args):
        calls.append(list(args))
        return 0, "{}", ""

    # 前3天（due 08-01，now 07-29）→ level 1：4 组 × (群+待办)
    out = escalation.run_escalation(ledger, settings,
                                    now="2026-07-29T10:00:00+08:00", runner=runner)
    assert {o["level"] for o in out} == {1}
    assert len(out) == 4
    assert len(calls) == 8

    # 幂等：同级别重跑零发送
    calls.clear()
    escalation.run_escalation(ledger, settings,
                              now="2026-07-29T10:00:00+08:00", runner=runner)
    assert calls == []

    # 逾期 +3（now 08-04）→ level 4：群+待办+升级 DM
    calls.clear()
    out = escalation.run_escalation(ledger, settings,
                                    now="2026-08-04T10:00:00+08:00", runner=runner)
    assert {o["level"] for o in out} == {4}
    dm_calls = [call for call in calls if call[:2] == ["chat", "+dm"]]
    assert len(dm_calls) == 4
    assert len(calls) == 12


def test_compute_stage_month_end():
    import datetime as dt

    from da_core import escalation

    item = {"next_due": "2026-08-01", "overdue_days": 30, "days_to_due": -30}
    assert escalation.compute_stage(item, dt.date(2026, 8, 31)) == 6
    assert escalation.compute_stage(item, dt.date(2026, 8, 25)) == 5
    item3 = {"next_due": "2026-08-01", "overdue_days": 2, "days_to_due": -2}
    assert escalation.compute_stage(item3, dt.date(2026, 8, 3)) == 3
    item4 = {"next_due": "2026-08-01", "overdue_days": 0, "days_to_due": 0}
    assert escalation.compute_stage(item4, dt.date(2026, 8, 1)) == 2
    item5 = {"next_due": None, "overdue_days": 0, "days_to_due": None}
    assert escalation.compute_stage(item5, dt.date(2026, 8, 1)) is None


def test_deferral_shifts_effective_due(tmp_path):
    from da_core.scheduler import scan, sync_tasks

    settings = make_settings(tmp_path)
    ledger = Ledger(settings.db_path)
    ledger.seed_config(settings)
    ledger.set_cycle_config(cycle_days=30, baseline="2026-08-01", updated_by="测试")
    sync_tasks(ledger, settings, now="2026-07-25T10:00:00+08:00")

    task_id = "ST001|3号组(12只)|2026-08-01"
    ledger.insert_deferral(deferral_id="d1", task_id=task_id, reason="现场检修不可用",
                           approved_by="班长", until_at="2026-08-10")

    report = scan(ledger, settings, now="2026-08-05T10:00:00+08:00")
    three = next(g for g in report["groups"] if g["group"] == "3号组(12只)")
    assert three["status"] == "deferred"
    assert three["deferred_until"] == "2026-08-10"
    assert three["overdue_days"] == 0
    assert three["next_due"] == "2026-08-01"  # 锚点真值不变

    # 延期过期 → 恢复逾期（从延期日算起）
    report2 = scan(ledger, settings, now="2026-08-15T10:00:00+08:00")
    three2 = next(g for g in report2["groups"] if g["group"] == "3号组(12只)")
    assert three2["overdue_days"] == 5
    assert three2["status"] == "overdue"


def test_reconcile_table_vs_ledger(tmp_path):
    from da_core import reconcile as reconcile_mod

    settings = make_settings(tmp_path)
    ledger = Ledger(settings.db_path)
    summary = submit_submission(submission_12v(), settings=settings, ledger=ledger)
    uid = summary["groups"][0]["record_uid"]
    field_ids = settings.table["field_ids"]

    voltages = {number: round(13.30 + number * 0.01, 2) for number in range(1, 12)}
    voltages[12] = 13.95

    def build_rows(*, volt_override=None, drop_cell=None, uid_value=uid):
        rows = []
        for number, volt in voltages.items():
            if drop_cell == number:
                continue
            value = volt_override if (volt_override is not None and number == 12) else volt
            rows.append({"recordId": f"rec{number}", "cells": {
                field_ids["账本UID"]: uid_value,
                field_ids["账本Rev"]: 1,
                field_ids["账本状态"]: "已定稿",
                field_ids["电池序号"]: number,
                field_ids["电压值(V)"]: value,
            }})
        return rows

    ok = reconcile_mod.reconcile(ledger, settings, table_rows=build_rows())
    assert ok["ok"] is True and ok["diffs"] == [] and ok["ledger_records"] == 1

    drifted = reconcile_mod.reconcile(ledger, settings,
                                      table_rows=build_rows(volt_override=9.9))
    assert drifted["ok"] is False
    assert any(diff["kind"] == "voltage" for diff in drifted["diffs"])

    short = reconcile_mod.reconcile(ledger, settings, table_rows=build_rows(drop_cell=12))
    assert any(diff["kind"] == "row-count" for diff in short["diffs"])

    orphan = reconcile_mod.reconcile(ledger, settings, table_rows=[
        {"recordId": "ghost", "cells": {field_ids["账本UID"]: "GHOST-UID",
                                        field_ids["账本Rev"]: 1,
                                        field_ids["账本状态"]: "已定稿"}}])
    assert any(diff["kind"] == "orphan-rows" for diff in orphan["diffs"])


OLD_FORMAT_MESSAGE = """# 蓄电池电压测量数据
# 提交时间: 2026-09-21 11:30

[组别] 3号组(12只)
[温度] 23
[数量] 2/12　[合格区间] 13.20~13.80V
[异常] 无
[数据] 1:13.36,2:13.38"""

EXT_FORMAT_MESSAGE = """# 蓄电池电压测量数据
# 提交时间: 2026-09-21 11:30

[组别] 3号组(12只)
[温度] 23
[直流系统] DC-003
[浮充电压] 13.50
[测试性质] 定期
[数据] 1:13.36,2:13.95"""


def test_group_message_parse():
    from da_core import group_intake

    parsed = group_intake.parse_group_message(OLD_FORMAT_MESSAGE)
    assert parsed["submitted_at"] == "2026-09-21T11:30:00+08:00"
    group = parsed["groups"][0]
    assert group["group"] == "3号组(12只)"
    assert group["env_temp"] == 23.0
    assert group["items"] == [{"no": 1, "volt": 13.36}, {"no": 2, "volt": 13.38}]
    assert "dc_system_id" not in group

    extended = group_intake.parse_group_message(EXT_FORMAT_MESSAGE)["groups"][0]
    assert extended["dc_system_id"] == "DC-003"
    assert extended["float_voltage"] == 13.5
    assert extended["test_kind"] == "定期"


def test_correct_syncs_table_rows(tmp_path):
    import json

    from da_core.service import correct_submission

    settings = make_settings(tmp_path)
    summary = submit_submission(submission_12v(), settings=settings)
    uid = summary["groups"][0]["record_uid"]
    fid = settings.table["field_ids"]

    calls = {"updates": []}

    def runner(args):
        if args[:3] == ["aitable", "record", "query"]:
            rows = [{"recordId": f"rec-{n}",
                     "cells": {fid["账本UID"]: uid, fid["电池序号"]: n}}
                    for n in range(1, 13)]
            return 0, json.dumps({"data": {"records": rows}}, ensure_ascii=False), ""
        if args[:2] == ["aitable", "+record-update"]:
            calls["updates"].append(list(args))
            return 0, '{"ok": true}', ""
        return 1, "", f"unexpected args: {args[:3]}"

    corrected_payload = {
        "dc_system_id": "DC-003",
        "float_voltage": 13.5,
        "test_kind": "定期",
        "env_temp": 25,
        "items": [{"no": number, "volt": 13.40} for number in range(1, 13)],
    }
    result = correct_submission(corrected_payload, record_uid=uid, actor="李四",
                                settings=settings, dispatch=True, runner=runner)

    assert result["status"] == "ok"
    assert result["table_sync"]["updated"] == 12
    assert result["table_sync"]["missing_cells"] == []

    args = calls["updates"][0]
    records = json.loads(args[args.index("--records") + 1])
    assert len(records) == 12
    assert records[0]["recordId"] == "rec-1"
    sample = records[0]["cells"]
    assert sample[fid["账本Rev"]] == 2
    assert sample[fid["账本状态"]] == "已定稿"
    assert sample[fid["电压值(V)"]] == 13.4


def test_void_syncs_table_status(tmp_path):
    import json

    from da_core.service import void_record

    settings = make_settings(tmp_path)
    summary = submit_submission(submission_12v(), settings=settings)
    uid = summary["groups"][0]["record_uid"]
    fid = settings.table["field_ids"]

    calls = {"updates": []}

    def runner(args):
        if args[:3] == ["aitable", "record", "query"]:
            rows = [{"recordId": f"rec-{n}",
                     "cells": {fid["账本UID"]: uid, fid["电池序号"]: n}}
                    for n in range(1, 13)]
            return 0, json.dumps({"data": {"records": rows}}, ensure_ascii=False), ""
        if args[:2] == ["aitable", "+record-update"]:
            calls["updates"].append(list(args))
            return 0, '{"ok": true}', ""
        return 1, "", f"unexpected args: {args[:3]}"

    result = void_record(uid, reason="录入错误", actor="李四", settings=settings,
                         dispatch=True, runner=runner)
    assert result["status"] == "ok"
    assert result["table_sync"]["updated"] == 12

    args = calls["updates"][0]
    records = json.loads(args[args.index("--records") + 1])
    assert all(record["cells"] == {fid["账本状态"]: "已作废"} for record in records)


def test_correct_table_sync_failure_does_not_block_ledger(tmp_path):
    from da_core.service import correct_submission

    settings = make_settings(tmp_path)
    ledger = Ledger(settings.db_path)
    summary = submit_submission(submission_12v(), settings=settings, ledger=ledger)
    uid = summary["groups"][0]["record_uid"]

    def runner(args):
        return 1, "", "dws down"

    corrected_payload = {
        "dc_system_id": "DC-003",
        "float_voltage": 13.5,
        "test_kind": "定期",
        "env_temp": 25,
        "items": [{"no": number, "volt": 13.40} for number in range(1, 13)],
    }
    result = correct_submission(corrected_payload, record_uid=uid, actor="李四",
                                settings=settings, ledger=ledger,
                                dispatch=True, runner=runner)
    assert result["status"] == "ok"  # 账本事实不受同步失败影响
    assert result["table_sync"]["ok"] is False
    assert ledger.get_record(uid)["rev"] == 2


def test_baseline_change_rebases_old_task(tmp_path):
    """周期配置变更滚期：旧任务应结案为 rebased（不冒充 done），月报不计入。"""
    from da_core.reporting import monthly_report
    from da_core.scheduler import sync_tasks

    settings = make_settings(tmp_path)
    ledger = Ledger(settings.db_path)
    ledger.seed_config(settings)
    ledger.set_cycle_config(cycle_days=30, baseline="2026-10-21", updated_by="测试")
    sync_tasks(ledger, settings, now="2026-09-21T10:00:00+08:00")

    ledger.set_cycle_config(cycle_days=30, baseline="2026-10-30", updated_by="测试")
    actions = sync_tasks(ledger, settings, now="2026-09-21T10:00:00+08:00")
    assert len(actions["closed"]) == 4
    assert {item["state"] for item in actions["closed"]} == {"rebased"}
    assert len(actions["created"]) == 4
    assert {item["due"] for item in actions["created"]} == {"2026-10-30"}

    report = monthly_report(ledger, settings, month="2026-10")
    assert report["totals"]["due_total"] == 4  # rebased 不计入
    assert report["totals"]["done"] == 0


def test_group_receipt_text_branches():
    from da_core.group_intake import _receipt_text

    dup = _receipt_text({"ok": False, "groups": [
        {"group": "3号组(12只)", "status": "rejected",
         "validation": {"errors": [{"code": "E_DUP_KEY", "message": "x"}]}}]})
    assert "无需重发" in dup and "请补" not in dup

    missing = _receipt_text({"ok": False, "groups": [
        {"group": "3号组(12只)", "status": "rejected",
         "validation": {"errors": [{"code": "E_REQUIRED", "message": "y"}]}}]})
    assert "请补" in missing and "缺必填字段" in missing

    assert _receipt_text({"ok": True, "replayed": False, "groups": [
        {"group": "3号组(12只)", "status": "ok", "_payload": {"items": [1, 2]},
         "rules": [{"verdict": "violation"}]}]}).startswith("✅ 已入库")


def test_monthly_report_counts(tmp_path):
    from da_core.reporting import monthly_report
    from da_core.scheduler import sync_tasks

    settings = make_settings(tmp_path)
    ledger = Ledger(settings.db_path)
    ledger.seed_config(settings)
    ledger.set_cycle_config(cycle_days=30, baseline="2026-08-01", updated_by="测试")
    sync_tasks(ledger, settings, now="2026-08-02T10:00:00+08:00")

    late = submission_12v(client_submission_id="rpt-late",
                          submitted_at="2026-08-20T10:00:00+08:00")
    assert submit_submission(late, settings=settings, ledger=ledger)["ok"] is True
    sync_tasks(ledger, settings, now="2026-08-25T10:00:00+08:00")

    report = monthly_report(ledger, settings, month="2026-08",
                            now="2026-09-01T09:00:00+08:00")
    assert report["month"] == "2026-08"
    totals = report["totals"]
    assert totals["due_total"] == 4
    assert totals["done_late"] == 1
    assert totals["overdue"] == 3
    assert totals["records"] == 1

    group = next(item for item in report["groups"] if "3号组" in item["group"])
    assert group["done_late"] == 1
    assert "| 3号组(12只) | 1 | 0 | 1 | 0 | 0% |" in report["text"]
    assert "——AI助手" in report["text"]


def test_pull_remote_process_and_idempotent_rescan(tmp_path):
    from da_core import pull_intake

    settings = make_settings(tmp_path)
    ledger = Ledger(settings.db_path)
    ledger.set_contact("ST001", "reminder_group", "APM测试", updated_by="测试")

    seen_urls = []
    sent = []

    def send_runner(args):
        sent.append(list(args))
        return 0, '{"ok": true}', ""

    def wrap(submission):
        return {"seq": 1, "received_at": "2026-09-21T14:58:00+08:00",
                "payload": submission}

    calls = {"n": 0}

    def fetcher(url, token):
        seen_urls.append(url)
        calls["n"] += 1
        assert token == "tok-1"
        if calls["n"] <= 2:  # 同一笔喂两次：模拟游标丢失后重扫
            return {"items": [wrap(submission_12v(client_submission_id="pull-001"))],
                    "next_cursor": "c-1", "has_more": False}
        return {"items": [], "next_cursor": "c-1", "has_more": False}

    first = pull_intake.pull_remote(ledger, settings, base_url="https://example.test",
                                    token="tok-1", fetcher=fetcher, dispatch=False,
                                    notify_group=True, runner=send_runner)
    assert "since=0" in seen_urls[0]  # 游标初始 0（对接规约）
    assert sent and any("✅ 已入库" in str(arg) for arg in sent[0])  # 入库回执已发群
    assert first["fetched"] == 1
    assert first["processed"][0]["ok"] is True
    assert first["processed"][0]["replayed"] is False
    assert first["cursor"] == "c-1"
    assert ledger.get_param(pull_intake.CURSOR_PARAM)["cursor"] == "c-1"
    assert ledger.counts()["records"] == 1

    second = pull_intake.pull_remote(ledger, settings, base_url="https://example.test",
                                     token="tok-1", fetcher=fetcher, dispatch=False)
    assert "since=c-1" in seen_urls[-1]
    assert second["processed"][0]["replayed"] is True
    assert ledger.counts()["records"] == 1  # 未产生第二条记录
    assert len(sent) == 1  # 重放不重复回执

    def bad_fetcher(url, token):
        raise OSError("boom")

    with pytest.raises(RuntimeError):
        pull_intake.pull_remote(ledger, settings, base_url="https://example.test",
                                token="tok-1", fetcher=bad_fetcher, dispatch=False)
    assert ledger.get_param(pull_intake.CURSOR_PARAM)["cursor"] == "c-1"  # 失败不推进


def test_completion_reopens_rebased_period(tmp_path):
    """完成测量使 next_due 回落至曾被 rebased 的周期：重开该任务行，不得崩溃。"""
    from da_core.scheduler import sync_tasks

    settings = make_settings(tmp_path)
    ledger = Ledger(settings.db_path)
    ledger.seed_config(settings)
    ledger.set_cycle_config(cycle_days=30, baseline="2026-10-21", updated_by="测试")
    sync_tasks(ledger, settings, now="2026-09-21T10:00:00+08:00")
    ledger.set_cycle_config(cycle_days=30, baseline="2026-10-30", updated_by="测试")
    sync_tasks(ledger, settings, now="2026-09-21T10:00:00+08:00")  # 10-21 行 → rebased

    assert submit_submission(submission_12v(), settings=settings,
                             ledger=ledger)["ok"] is True

    actions = sync_tasks(ledger, settings, now="2026-09-21T12:00:00+08:00")
    task_id = "ST001|3号组(12只)|2026-10-21"
    assert task_id in [u["task_id"] for u in actions["updated"]]
    assert ("ST001|3号组(12只)|2026-10-30", "done") in [
        (c["task_id"], c["state"]) for c in actions["closed"]]
    row = ledger.get_task(task_id)
    assert row["state"] == "open" and row["closed_at"] is None


MANGLED_MESSAGE = (
    "**蓄电池电压测量数据**  \n**提交时间: 2026-09-21 11:47**  \n"
    "[组别] 3号组(12只) [温度] 23 [数量] 2/12\u3000[合格区间] 13.20~13.80V "
    "[异常] 无 【联调测试】 [数据] 1:13.36,2:13.38"
)


def test_group_message_parse_mangled_by_dingtalk():
    """真实回归：钉钉把 '# ' 行转 **加粗**、换行折叠成空格后的存储形态。"""
    from da_core import group_intake

    parsed = group_intake.parse_group_message(MANGLED_MESSAGE)
    assert parsed["submitted_at"] == "2026-09-21T11:47:00+08:00"
    group = parsed["groups"][0]
    assert group["group"] == "3号组(12只)"
    assert group["env_temp"] == 23.0
    assert group["items"] == [{"no": 1, "volt": 13.36}, {"no": 2, "volt": 13.38}]


def test_group_ingest_rejects_missing_fields_then_accepts(tmp_path):
    from da_core import group_intake

    settings = make_settings(tmp_path)
    ledger = Ledger(settings.db_path)
    ledger.seed_config(settings)

    rejected = group_intake.ingest_group_message(
        OLD_FORMAT_MESSAGE, message_id="mid-1", settings=settings, ledger=ledger,
        dispatch=False)
    assert rejected["ok"] is False
    assert rejected["groups"][0]["status"] == "rejected"
    codes = {error["code"] for error in rejected["groups"][0]["validation"]["errors"]}
    assert "E_REQUIRED" in codes
    assert ledger.counts()["records"] == 0

    accepted = group_intake.ingest_group_message(
        EXT_FORMAT_MESSAGE, message_id="mid-2", settings=settings, ledger=ledger,
        dispatch=False)
    assert accepted["ok"] is True
    group = accepted["groups"][0]
    assert group["lifecycle"] == "confirmed"
    assert group["record_uid"] == "ST001-battery_voltage_test-20260921-1130-1"


def test_poll_group_processes_new_messages_and_replies(tmp_path):
    import json as json_mod

    from da_core import group_intake

    settings = make_settings(tmp_path)
    ledger = Ledger(settings.db_path)
    ledger.seed_config(settings)

    sent = []

    def runner(args):
        if args[:2] == ["chat", "+chat-messages"]:
            payload = {"count": 2, "messages": [
                {"messageId": "m-old", "createTime": "2026-09-21 11:20:00",
                 "text": "随便聊聊"},
                {"messageId": "m-new", "createTime": "2026-09-21 11:31:00",
                 "text": EXT_FORMAT_MESSAGE},
            ]}
            return 0, json_mod.dumps(payload, ensure_ascii=False), ""
        sent.append(list(args))
        return 0, '{"ok": true}', ""

    preview = group_intake.poll_group(ledger, settings, runner=runner, dry_run=True)
    assert preview["processed"][0]["would_submit"] is True

    result = group_intake.poll_group(ledger, settings, runner=runner, dispatch=False)
    assert result["scanned"] == 2
    assert len(result["processed"]) == 1
    assert result["processed"][0]["ok"] is True
    assert result["cursor"]["last_time"] == "2026-09-21 11:31:00"
    replies = [call for call in sent if call[:2] == ["chat", "+send-to-group"]]
    assert len(replies) == 1
    assert "已入库" in replies[0][5]

    # 幂等：游标之后重拉 → 不再处理
    result2 = group_intake.poll_group(ledger, settings, runner=runner, dispatch=False)
    assert result2["processed"] == []


def test_monthly_cycle_mode_due_and_advance(tmp_path):
    """月锚周期：下一到期 = 完成月次月 15 日（纯函数边界 + 落账推进）。"""
    import datetime as dt

    from da_core.scheduler import _next_monthly_due, scan, sync_tasks

    assert _next_monthly_due(dt.date(2026, 9, 21), 15) == dt.date(2026, 10, 15)
    assert _next_monthly_due(dt.date(2026, 10, 16), 15) == dt.date(2026, 11, 15)
    assert _next_monthly_due(dt.date(2026, 12, 30), 15) == dt.date(2027, 1, 15)
    assert _next_monthly_due(dt.date(2026, 1, 31), 15) == dt.date(2026, 2, 15)

    settings = make_settings(tmp_path)
    ledger = Ledger(settings.db_path)
    ledger.seed_config(settings)
    ledger.set_cycle_config(cycle_mode="monthly_day", anchor_day=15, updated_by="测试")

    first = submission_12v(client_submission_id="test-6月",
                           submitted_at="2026-06-10T10:00:00+08:00")
    assert submit_submission(first, settings=settings, ledger=ledger)["ok"] is True
    actions = sync_tasks(ledger, settings, now="2026-06-10T11:00:00+08:00")
    assert [a["due"] for a in actions["created"]] == ["2026-07-15"]

    # 7-20 完成 = 迟于 7-15（迟到结案）；下一期应为 8-15
    second = submission_12v(client_submission_id="test-7月",
                            submitted_at="2026-07-20T10:00:00+08:00")
    assert submit_submission(second, settings=settings, ledger=ledger)["ok"] is True
    actions = sync_tasks(ledger, settings, now="2026-07-20T11:00:00+08:00")
    assert [a["state"] for a in actions["closed"]] == ["done_late"]
    assert [a["due"] for a in actions["created"]] == ["2026-08-15"]

    report = scan(ledger, settings, now="2026-07-21T09:00:00+08:00")
    item = next(g for g in report["groups"] if g["group"] == "3号组(12只)")
    assert item["next_due"] == "2026-08-15"
    assert report["cycle_mode"] == "monthly_day" and report["anchor_day"] == 15


def test_cycle_mode_change_rebases_without_new_completion(tmp_path):
    """滚动→月锚且无新完成：旧任务结案 rebased（不冒充 done），新任务按新口径建。"""
    from da_core.reporting import monthly_report
    from da_core.scheduler import sync_tasks

    settings = make_settings(tmp_path)
    ledger = Ledger(settings.db_path)
    ledger.seed_config(settings)
    assert submit_submission(submission_12v(), settings=settings,
                             ledger=ledger)["ok"] is True
    sync_tasks(ledger, settings, now="2026-09-21T11:00:00+08:00")  # 滚动：due=10-21

    ledger.set_cycle_config(cycle_mode="monthly_day", anchor_day=15, updated_by="测试")
    actions = sync_tasks(ledger, settings, now="2026-09-22T09:00:00+08:00")
    assert [a["state"] for a in actions["closed"]] == ["rebased"]
    assert [a["due"] for a in actions["created"]] == ["2026-10-15"]

    report = monthly_report(ledger, settings, month="2026-10")
    assert report["totals"]["done"] == 0 and report["totals"]["done_late"] == 0
    assert report["totals"]["due_total"] == 1 and report["totals"]["open"] == 1


def test_entry_card_params_and_payload():
    from da_core import entry_card as ec

    params = ec.build_card_params()
    assert params["url1"] == ec.ENTRY_URL and params["url2"] == ec.LEDGER_URL
    assert len(params) >= 50  # 候选批量（28+28+5）

    payload = ec.build_payload(target="group", out_track_id="t-group-1")
    assert payload["cardTemplateId"] == ec.TEMPLATE_ID
    assert payload["openSpaceId"] == "dtv1.card//IM_GROUP." + ec.GROUP_CID
    assert payload["imGroupOpenDeliverModel"]["robotCode"] == ec.ROBOT_CODE
    assert payload["cardData"]["cardParamMap"]["url1"] == ec.ENTRY_URL

    dm = ec.build_payload(target="dm", user_id="u1", out_track_id="t-dm-1")
    assert dm["openSpaceId"] == "dtv1.card//IM_ROBOT.u1"
    assert dm["imRobotOpenDeliverModel"]["robotCode"] == ec.ROBOT_CODE


def test_entry_card_deliver_once_per_cycle(tmp_path):
    from da_core import entry_card as ec

    settings = make_settings(tmp_path)
    ledger = Ledger(settings.db_path)
    posts = []

    def runner(args):
        import json as _json

        assert args[:2] == ["devapp", "+credentials-get"]
        return 0, _json.dumps({"data": {"appKey": "test-ak",
                                        "appSecret": "test-sk"}}), ""

    def poster(url, payload, headers):
        posts.append(url)
        if url.endswith("/oauth2/accessToken"):
            return {"accessToken": "test-token"}
        return {"success": True, "deliverResults": [{"success": True}]}

    task_a = {"task_id": "T|g|2026-10-15", "due_at": "2026-10-15"}
    first = ec.deliver_entry_card(ledger, task_a, runner=runner, poster=poster)
    assert first["sent"] is True
    assert posts.count("https://api.dingtalk.com/v1.0/card/instances/createAndDeliver") == 1
    assert ledger.get_config_param("entry_card_last_due") == "2026-10-15"

    again = ec.deliver_entry_card(ledger, task_a, runner=runner, poster=poster)
    assert again["sent"] is False and again["reason"] == "already-sent-cycle"

    task_b = {"task_id": "T|g|2026-11-15", "due_at": "2026-11-15"}
    third = ec.deliver_entry_card(ledger, task_b, runner=runner, poster=poster)
    assert third["sent"] is True


def test_escalation_level1_sends_entry_card_once(tmp_path):
    """级别 1 随发入口卡：按周期只发 1 张；不占文字触达通道。"""
    from da_core import escalation
    from da_core.scheduler import sync_tasks

    settings = make_settings(tmp_path)
    ledger = Ledger(settings.db_path)
    ledger.seed_config(settings)
    ledger.set_cycle_config(cycle_days=30, baseline="2026-08-01", updated_by="测试")
    sync_tasks(ledger, settings, now="2026-07-25T10:00:00+08:00")
    ledger.set_contact("ST001", "reminder_group", "测试群")
    ledger.set_contact("ST001", "reminder_assignee", "user1")

    sends = []

    def runner(args):
        if args[:2] == ["devapp", "+credentials-get"]:
            import json as _json

            return 0, _json.dumps({"data": {"appKey": "test-ak",
                                            "appSecret": "test-sk"}}), ""
        sends.append(list(args))
        return 0, "{}", ""

    def poster(url, payload, headers):
        if url.endswith("/oauth2/accessToken"):
            return {"accessToken": "test-token"}
        return {"success": True}

    out = escalation.run_escalation(ledger, settings,
                                    now="2026-07-29T10:00:00+08:00",
                                    runner=runner, poster=poster,
                                    with_entry_card=True)
    card_channels = [c for o in out for c in o["channels"]
                     if c["channel"] == "entry_card"]
    assert sum(1 for c in card_channels if c.get("sent")) == 1
    assert sum(1 for c in card_channels
               if c.get("reason") == "already-sent-cycle") == 3
    assert len(sends) == 8  # 4 组 ×（群+待办）；卡片走 poster，不占文字通道
    assert ledger.get_config_param("entry_card_last_due") == "2026-08-01"


def test_push_monthly_report_window_and_idempotence(tmp_path):
    from da_core.reporting import push_monthly_report
    from da_core.scheduler import sync_tasks

    settings = make_settings(tmp_path)
    ledger = Ledger(settings.db_path)
    ledger.seed_config(settings)
    ledger.set_contact("ST001", "reminder_group", "测试群")
    sync_tasks(ledger, settings, now="2026-09-22T09:00:00+08:00")

    outside = push_monthly_report(ledger, settings, now="2026-09-22T08:30:00+08:00")
    assert outside["pushed"] is False and outside["reason"] == "not-in-window"

    sends = []

    def runner(args):
        if args[:2] == ["devapp", "+credentials-get"]:
            import json as _json

            return 0, _json.dumps({"data": {"appKey": "test-ak",
                                            "appSecret": "test-sk"}}), ""
        sends.append(list(args))
        return 0, "{}", ""

    def poster(url, payload, headers):
        if url.endswith("/oauth2/accessToken"):
            return {"accessToken": "test-token"}
        return {"success": True}

    first = push_monthly_report(ledger, settings, now="2026-10-16T08:30:00+08:00",
                                runner=runner, poster=poster)
    assert first["pushed"] is True and first["card"]["sent"] is True
    group_calls = [c for c in sends if c[:2] == ["chat", "+send-to-group"]]
    assert len(group_calls) == 1 and "月报" in group_calls[0][5]
    assert ledger.get_config_param("report_last_pushed") == "2026-10"

    again = push_monthly_report(ledger, settings, now="2026-10-17T08:30:00+08:00",
                                runner=runner, poster=poster)
    assert again["pushed"] is False and again["reason"] == "already-pushed"


def test_reopened_task_refreshes_opened_at(tmp_path):
    """重开任务行刷新 opened_at：其后配置滚期不得把旧完成误记 done。"""
    from da_core.scheduler import sync_tasks

    settings = make_settings(tmp_path)
    ledger = Ledger(settings.db_path)
    ledger.seed_config(settings)
    ledger.set_cycle_config(cycle_days=30, baseline="2026-10-21", updated_by="测试")
    sync_tasks(ledger, settings, now="2026-09-21T09:00:00+08:00")
    ledger.set_cycle_config(cycle_days=30, baseline="2026-10-30", updated_by="测试")
    sync_tasks(ledger, settings, now="2026-09-21T10:00:00+08:00")  # 10-21 → rebased

    assert submit_submission(submission_12v(), settings=settings,
                             ledger=ledger)["ok"] is True
    sync_tasks(ledger, settings, now="2026-09-21T12:00:00+08:00")  # 回落 10-21：重开

    row = ledger.get_task("ST001|3号组(12只)|2026-10-21")
    assert row["state"] == "open" and row["closed_at"] is None
    assert row["opened_at"] == "2026-09-21T12:00:00+08:00"  # 刷新为本次开启

    ledger.set_cycle_config(cycle_mode="monthly_day", anchor_day=15, updated_by="测试")
    actions = sync_tasks(ledger, settings, now="2026-09-22T09:00:00+08:00")
    assert [a["state"] for a in actions["closed"]] == ["rebased"]
    assert [a["due"] for a in actions["created"]] == ["2026-10-15"]
