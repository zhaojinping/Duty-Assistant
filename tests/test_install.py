"""安装与用户配置。"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from da_core.health import collect_health
from da_core.install_flow import (
    create_user_table,
    init_install,
    plan_dependencies,
    windows_task_commands,
)
from da_core.settings import Settings
from da_core.table_projection import _accept_ids
from da_core.user_config import TABLE_FIELDS, ConfigError, validate_install


def _payload(**overrides):
    data = {
        "station_id": "XP",
        "station_name": "香坪风电场",
        "group_name": "香坪运行群",
        "assignee_name": "张三",
        "entry_url": "https://xiangping.app.workbuddy.host/entry",
        "groups": [{"name": "1号组(18只)", "kind": "12V电池", "count": 18}],
    }
    data.update(overrides)
    return data


def test_reject_developer_group_url_and_person():
    with pytest.raises(ConfigError, match="APM测试"):
        validate_install(_payload(group_name="APM测试"))
    with pytest.raises(ConfigError, match="开发者的录入页"):
        validate_install(_payload(entry_url="https://battery-voltage-entry.app.workbuddy.host/"))
    with pytest.raises(ConfigError, match="https"):
        validate_install(_payload(entry_url="http://xiangping.example/entry"))
    with pytest.raises(ConfigError, match="测试人员"):
        validate_install(_payload(assignee_id="20240411222100620-4905-014C76478"))


def test_init_writes_empty_ledger_and_refuses_second_computer(tmp_path):
    plan = init_install(_payload(), data_dir=tmp_path, hostname="pc-a", confirm=False)
    assert plan["needs_confirm"] is True
    assert not (tmp_path / "config.json").exists()

    done = init_install(_payload(), data_dir=tmp_path, hostname="pc-a", confirm=True)
    assert done["needs_confirm"] is False
    assert (tmp_path / "ledger.sqlite").is_file()
    saved = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert saved["cycle_mode"] == "monthly_day"
    assert saved["anchor_day"] == 15
    assert saved["group_name"] == "香坪运行群"
    assert "np9zOoBVBYALR6aeuenZZglmW1DK0g6l" not in json.dumps(saved)

    with pytest.raises(ConfigError, match="pc-a"):
        init_install(_payload(), data_dir=tmp_path, hostname="pc-b", confirm=True)


def test_dependency_plan_is_python_and_dws_only():
    steps = plan_dependencies(
        python_ok=False, dws_found=False, npm_found=False, brew_found=False, os_name="win32")
    joined = " ".join(" ".join(step["command"]) for step in steps)
    assert "Python.Python.3.12" in joined
    assert "dingtalk-workspace-cli" in joined
    assert "git" not in joined.lower()
    assert "uv" not in joined.split()


def test_health_uses_builtin_sqlite(tmp_path):
    report = collect_health(
        data_dir=tmp_path, hostname="pc-a", python_ok=True, dws_found=True,
        dws_authenticated=True, on_sync=False, os_name="win32")
    sqlite = next(item for item in report["checks"] if item["code"] == "sqlite")
    assert sqlite["ok"] is True
    assert report["ready_to_install"] is True


def test_create_table_reads_user_ids_not_developer_base():
    def runner(args):
        if args[1] == "base":
            body = {"data": {"baseId": "base-user"}}
        else:
            body = {"data": {
                "tableId": "tbl-user",
                "fields": [
                    {"fieldName": name, "fieldId": f"f{index}"}
                    for index, (name, _kind) in enumerate(TABLE_FIELDS)
                ],
            }}
        return 0, json.dumps(body), ""

    created = create_user_table(runner=runner)
    assert created["base_id"] == "base-user"
    assert created["field_ids"]["电压值(V)"] == "f2"
    assert "np9zOoBVBYALR6aeuenZZglmW1DK0g6l" not in created["ledger_url"]


def test_missing_record_ids_are_not_a_successful_write():
    ids, failure = _accept_ids([], offset=0, returncode=0)
    assert ids == [] and failure["stderr"]


def test_unconfigured_card_does_not_raise(tmp_path):
    from da_core.entry_card import deliver_entry_card
    from da_core.ledger import Ledger

    settings = Settings.default(
        db_path=tmp_path / "ledger.sqlite",
        station={"station_id": "XP", "station_name": "香坪风电场"},
        group_cid="", entry_url="")
    ledger = Ledger(settings.db_path)
    result = deliver_entry_card(ledger, {"due_at": "2026-10-15"}, settings=settings)
    assert result["reason"] == "card-not-configured"


def test_poll_without_group_refuses(tmp_path):
    from da_core.group_intake import poll_group
    from da_core.ledger import Ledger

    settings = Settings.default(
        db_path=tmp_path / "ledger.sqlite",
        station={"station_id": "XP", "station_name": "香坪风电场"})
    ledger = Ledger(settings.db_path)
    with pytest.raises(RuntimeError, match="未配置生产群"):
        poll_group(ledger, settings, runner=lambda _args: (0, "{}", ""))


def test_windows_tasks_are_five_minutes_and_morning():
    commands = windows_task_commands(r"C:\DutyAssistant\venv\Scripts\python.exe")
    assert commands[0][5:9] == ["/SC", "MINUTE", "/MO", "5"]
    assert commands[1][5:9] == ["/SC", "DAILY", "/ST", "08:30"]


def test_credentials_accept_result_segment():
    from da_core.entry_card import credentials_from_payload, send_entry_card

    assert credentials_from_payload(
        {"result": {"appKey": "ak", "appSecret": "sk"}})["appKey"] == "ak"
    assert credentials_from_payload(
        {"data": {"appKey": "ak", "appSecret": "sk"}})["appSecret"] == "sk"

    def runner(args):
        assert args[:2] == ["devapp", "+credentials-get"]
        return 0, json.dumps({"result": {"appKey": "ak", "appSecret": "sk"}}), ""

    def poster(url, payload, headers):
        if url.endswith("/oauth2/accessToken"):
            assert payload["appKey"] == "ak"
            return {"accessToken": "tok"}
        return {"success": True}

    sent = send_entry_card(runner=runner, poster=poster, out_track_id="t1")
    assert sent["sent"] is True


def test_watch_keeps_group_when_pull_fails(tmp_path, monkeypatch):
    import da_core.group_intake as group_intake
    import da_core.pull_intake as pull_intake
    from da_core.install_cli import _watch_body

    def boom(*_args, **_kwargs):
        raise RuntimeError("401")

    seen = {}

    def poll(*_args, **_kwargs):
        seen["group"] = True
        return {"processed": []}

    monkeypatch.setattr(pull_intake, "pull_remote", boom)
    monkeypatch.setattr(group_intake, "poll_group", poll)
    settings = Settings.default(
        db_path=tmp_path / "ledger.sqlite",
        station={"station_id": "XP", "station_name": "香坪风电场"},
        group_name="香坪运行群")
    result = _watch_body(
        settings,
        {"pull_url": "https://xiangping.example", "group_name": "香坪运行群", "pull_token": ""})
    assert result == 0
    assert seen["group"] is True


def test_next_anchor_uses_the_15th():
    from da_core.install_flow import next_anchor_date

    assert next_anchor_date(dt.date(2026, 9, 22), 15) == "2026-10-15"
    assert next_anchor_date(dt.date(2026, 9, 15), 15) == "2026-09-15"
