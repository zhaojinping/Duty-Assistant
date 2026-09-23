"""设备测温：引擎分级、群文本、周期、表和缺陷触达。"""

from __future__ import annotations

import datetime as dt
import json

from da_core.install_flow import create_thermo_table
from da_core.ledger import Ledger
from da_core.scheduler import ensure_thermo_baseline, next_thermo_due, scan_thermo
from da_core.settings import Settings
from da_core.thermo import (
    bind_photos,
    count_grades,
    parse_thermo_message,
    receipt_text,
    submit_thermography,
)
from da_core.user_config import THERMO_TABLE_FIELDS

STATION = {"station_id": "ST001", "station_name": "合成场"}


def _settings(tmp_path, **kwargs):
    return Settings.default(db_path=tmp_path / "ledger.sqlite", station=dict(STATION),
                            group_name="测试群", **kwargs)


def _body(**overrides):
    data = {
        "operator": "张三",
        "submitted_at": "2026-07-12T10:30:00+08:00",
        "measured_at": "2026-07-12T10:30:00+08:00",
        "test_kind": "例行",
        "env_temp": 20,
        "spots": [
            {"device_name": "1号主变", "spot": "A相", "measured_temp": 50,
             "instrument_id": "IR-01"},
            {"device_name": "1号主变", "spot": "B相", "measured_temp": 40,
             "instrument_id": "IR-01"},
        ],
    }
    data.update(overrides)
    return data


def test_due_dates_match_the_agreed_calendar():
    assert next_thermo_due(dt.date(2026, 6, 30)) == dt.date(2026, 7, 10)
    assert next_thermo_due(dt.date(2026, 7, 3)) == dt.date(2026, 7, 10)
    assert next_thermo_due(dt.date(2026, 7, 12)) == dt.date(2026, 7, 19)
    assert next_thermo_due(dt.date(2026, 9, 28)) == dt.date(2026, 10, 10)
    assert next_thermo_due(dt.date(2026, 7, 10)) == dt.date(2026, 7, 17)


def test_first_install_baseline_is_the_next_tenth(tmp_path):
    settings = _settings(tmp_path)
    ledger = Ledger(settings.db_path)
    ledger.seed_config(settings)
    assert ensure_thermo_baseline(ledger, settings, today=dt.date(2026, 9, 5)) == "2026-09-10"
    assert ensure_thermo_baseline(ledger, settings, today=dt.date(2026, 9, 23)) == "2026-09-10"
    report = scan_thermo(ledger, settings, now="2026-09-05T08:00:00+08:00")
    assert report["group"]["next_due"] == "2026-09-10"


def test_group_text_and_photo_order():
    text = """# 设备测温数据
# 提交时间: 2026-09-23 10:30
[测温性质] 例行
[环境温度] 28
[负荷电流] 120
[测点] 1|1号主变|A相高压套管接头|52.3|IR-01
[测点] 2|1号主变|B相高压套管接头|49.8|IR-01|3.1|12|正常|电流致热
"""
    parsed = parse_thermo_message(text)
    assert parsed["env_temp"] == 28
    assert parsed["spots"][1]["defect_grade"] == "正常"
    assert parsed["spots"][0].get("heat_type") is None
    refs = bind_photos(parsed["spots"], ["pic-1", "pic-2"])
    assert [item["item_key"] for item in refs] == [1, 2]


def test_missing_photo_warns_and_auto_confirms(tmp_path):
    settings = _settings(tmp_path)
    ledger = Ledger(settings.db_path)
    summary = submit_thermography(_body(), settings=settings, ledger=ledger, dispatch=True)
    assert summary["ok"] is True
    group = summary["groups"][0]
    assert group["lifecycle"] == "confirmed"
    assert any(entry["rule_id"] == "attachment_photo" for entry in group["rules"])
    assert count_grades(group["rules"]) == {"严重": 0, "危急": 0, "一般": 0}
    assert group["_payload"]["items"][0]["heat_type"] == "电流致热"
    assert summary["dispatch"]["skipped"] is True
    assert summary["notify"]["todo"] is None
    text = receipt_text(summary)
    assert "账本已记下" in text
    assert "缺红外图" in text
    assert text.endswith("——AI助手")


def test_severe_todo_and_critical_dm(tmp_path):
    settings = _settings(tmp_path)
    ledger = Ledger(settings.db_path)
    ledger.seed_config(settings)
    ledger.set_contact("ST001", "thermo_assignee", "user-thermo")
    ledger.set_contact("ST001", "reminder_escalate", "班长")
    calls = []

    def runner(args):
        calls.append(list(args))
        return 0, "{}", ""

    severe = submit_thermography(_body(
        client_submission_id="s1",
        spots=[
            {"device_name": "1号主变", "spot": "A相", "measured_temp": 90,
             "instrument_id": "IR-01", "photo_ref": "a", "defect_grade": "正常"},
            {"device_name": "1号主变", "spot": "B相", "measured_temp": 40,
             "instrument_id": "IR-01", "photo_ref": "b"},
        ],
    ), settings=settings, ledger=ledger, runner=runner)
    assert count_grades(severe["groups"][0]["rules"])["严重"] == 1
    assert "低于引擎判定" in severe["groups"][0]["rules"][0]["detail"]
    assert any(call[:2] == ["todo", "+create"] for call in calls)
    assert not any(call[:2] == ["chat", "+dm"] for call in calls)

    calls.clear()
    critical = submit_thermography(_body(
        client_submission_id="c1",
        measured_at="2026-07-13T10:30:00+08:00",
        spots=[{"device_name": "2号主变", "spot": "接头", "measured_temp": 120,
                "instrument_id": "IR-01", "photo_ref": "c"}],
    ), settings=settings, ledger=ledger, runner=runner)
    assert count_grades(critical["groups"][0]["rules"])["危急"] == 1
    assert any(call[:2] == ["chat", "+dm"] for call in calls)


def test_non_current_heat_is_not_graded(tmp_path):
    settings = _settings(tmp_path)
    summary = submit_thermography(_body(spots=[{
        "device_name": "避雷器", "spot": "上节", "measured_temp": 130,
        "instrument_id": "IR-01", "heat_type": "电压致热", "photo_ref": "p",
    }]), settings=settings, ledger=Ledger(settings.db_path))
    rules = summary["groups"][0]["rules"]
    thermal = next(entry for entry in rules if entry["rule_id"] == "thermal_grade")
    assert thermal["verdict"] == "skipped"
    assert summary["notify"]["todo"] is None


def test_general_phase_diff_stays_in_the_receipt(tmp_path):
    settings = _settings(tmp_path)
    summary = submit_thermography(_body(
        env_temp=10,
        spots=[
            {"device_name": "开关", "spot": "A", "measured_temp": 40,
             "instrument_id": "IR-01", "photo_ref": "a"},
            {"device_name": "开关", "spot": "B", "measured_temp": 20,
             "instrument_id": "IR-01", "photo_ref": "b"},
        ],
    ), settings=settings, ledger=Ledger(settings.db_path))
    assert count_grades(summary["groups"][0]["rules"])["一般"] == 1
    text = receipt_text(summary)
    assert "一般 1" in text
    assert text.startswith("✅ 已入库")


def test_poll_binds_following_images(tmp_path):
    from da_core.group_intake import poll_group

    settings = _settings(tmp_path)
    ledger = Ledger(settings.db_path)
    ledger.seed_config(settings)
    text = (
        "# 设备测温数据\n# 提交时间: 2026-09-23 10:30\n"
        "[环境温度] 28\n"
        "[测点] 1|1号主变|A相|52.3|IR-01\n"
        "[测点] 2|1号主变|B相|49.8|IR-01\n"
    )
    messages = {
        "messages": [
            {"messageId": "t1", "createTime": "2026-09-23 10:30:00", "senderId": "u1", "text": text},
            {"messageId": "p1", "createTime": "2026-09-23 10:30:05", "senderId": "u1",
             "msgType": "image", "content": {"downloadCode": "img-1"}},
            {"messageId": "p2", "createTime": "2026-09-23 10:30:08", "senderId": "u1",
             "msgType": "image", "content": {"downloadCode": "img-2"}},
        ]
    }

    def runner(args):
        if args[:2] == ["chat", "+chat-messages"]:
            return 0, json.dumps(messages), ""
        return 0, "{}", ""

    result = poll_group(ledger, settings, runner=runner, actor="李四")
    assert result["processed"][0]["ok"] is True
    row = ledger.conn.execute("SELECT lifecycle FROM records").fetchone()
    assert row["lifecycle"] == "confirmed"
    assert "缺红外图" not in (result["processed"][0]["reply"] or "")
    assert ledger.get_param("group_intake")["last_msg"] == "p2"


def test_july_completion_rolls_seven_days(tmp_path):
    settings = _settings(tmp_path)
    ledger = Ledger(settings.db_path)
    submit_thermography(_body(), settings=settings, ledger=ledger)
    report = scan_thermo(ledger, settings, now="2026-07-12T11:00:00+08:00")
    assert report["group"]["next_due"] == "2026-07-19"


def test_thermo_table_is_created_in_two_batches():
    seen = []

    def runner(args):
        seen.append(args)
        if args[1] == "table":
            fields = json.loads(args[args.index("--fields") + 1])
            assert len(fields) == 15
            body = {"data": {"tableId": "tbl-thermo", "fields": [
                {"fieldName": item["fieldName"], "fieldId": f"a{index}"}
                for index, item in enumerate(fields)
            ]}}
        else:
            fields = json.loads(args[args.index("--fields") + 1])
            assert len(fields) == len(THERMO_TABLE_FIELDS) - 15
            body = {"data": {"fields": [
                {"fieldName": item["fieldName"], "fieldId": f"b{index}"}
                for index, item in enumerate(fields)
            ]}}
        return 0, json.dumps(body), ""

    created = create_thermo_table(base_id="base-user", runner=runner)
    assert created["table_id"] == "tbl-thermo"
    assert set(created["field_ids"]) == {name for name, _kind in THERMO_TABLE_FIELDS}
    assert seen[0][1] == "table"
    assert seen[1][1] == "field"
