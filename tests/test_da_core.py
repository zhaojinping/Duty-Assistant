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
