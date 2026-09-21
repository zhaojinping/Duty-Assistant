"""SQLite 账本（核心系统的权威存储）。

- 表结构：改造方案 §4.2（records / record_versions / dedupe_index / confirmations /
  alarms / tasks / task_events / deferrals / config_* / ops_audit）
  + intake_receipts（提交幂等回执）。
- 读侧：把库内事实物化为引擎所需视图（ledger_view / history / alarm_history）——
  引擎永远只看到「传进来的事实」。
- 写侧：create / confirm 落账 + 配置种子 + 审计。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from records_kit.util import wall_day, wall_stamp

from da_core.clock import iso_now

_SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
  record_uid   TEXT PRIMARY KEY,
  record_type  TEXT NOT NULL,
  station_id   TEXT NOT NULL,
  occurred_at  TEXT NOT NULL,
  lifecycle    TEXT NOT NULL,
  current_rev  INTEGER NOT NULL,
  created_at   TEXT NOT NULL,
  confirmed_at TEXT,
  voided_at    TEXT,
  scope        TEXT,
  group_label  TEXT
);
CREATE INDEX IF NOT EXISTS idx_records_station_type
  ON records(station_id, record_type);
CREATE TABLE IF NOT EXISTS record_versions (
  record_uid  TEXT NOT NULL,
  rev         INTEGER NOT NULL,
  fields_json TEXT NOT NULL,
  digest      TEXT NOT NULL,
  op          TEXT NOT NULL,
  created_at  TEXT NOT NULL,
  PRIMARY KEY (record_uid, rev)
);
CREATE TABLE IF NOT EXISTS dedupe_index (
  record_uid   TEXT PRIMARY KEY,
  station_id   TEXT NOT NULL,
  occurred_day TEXT NOT NULL,
  test_kind    TEXT,
  dc_system_id TEXT
);
CREATE TABLE IF NOT EXISTS confirmations (
  record_uid TEXT NOT NULL,
  rev        INTEGER NOT NULL,
  slot       TEXT NOT NULL,
  by_who     TEXT,
  at         TEXT,
  PRIMARY KEY (record_uid, rev, slot)
);
CREATE TABLE IF NOT EXISTS alarms (
  fingerprint     TEXT PRIMARY KEY,
  station_id      TEXT,
  record_type     TEXT,
  first_seen_at   TEXT,
  occur_count     INTEGER NOT NULL DEFAULT 0,
  last_status     TEXT,
  escalated_count INTEGER NOT NULL DEFAULT 0,
  updated_at      TEXT
);
CREATE TABLE IF NOT EXISTS tasks (
  task_id       TEXT PRIMARY KEY,
  station_id    TEXT,
  record_type   TEXT,
  period_key    TEXT,
  due_at        TEXT,
  overdue_since TEXT,
  state         TEXT NOT NULL DEFAULT 'open',
  level         INTEGER NOT NULL DEFAULT 0,
  opened_at     TEXT,
  closed_at     TEXT
);
CREATE TABLE IF NOT EXISTS task_events (
  event_id    INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id     TEXT,
  level       INTEGER,
  channel     TEXT,
  target      TEXT,
  sent_at     TEXT,
  ack_at      TEXT,
  result      TEXT,
  retry_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS deferrals (
  deferral_id TEXT PRIMARY KEY,
  task_id     TEXT,
  reason      TEXT,
  approved_by TEXT,
  until_at    TEXT,
  created_at  TEXT
);
CREATE TABLE IF NOT EXISTS config_thresholds (
  record_type TEXT NOT NULL,
  scope       TEXT NOT NULL,
  lo          REAL NOT NULL,
  hi          REAL NOT NULL,
  updated_at  TEXT,
  updated_by  TEXT,
  PRIMARY KEY (record_type, scope)
);
CREATE TABLE IF NOT EXISTS config_contacts (
  station_id TEXT NOT NULL,
  role       TEXT NOT NULL,
  target     TEXT,
  updated_at TEXT,
  PRIMARY KEY (station_id, role)
);
CREATE TABLE IF NOT EXISTS config_params (
  key        TEXT PRIMARY KEY,
  value_json TEXT,
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS ops_audit (
  audit_id    INTEGER PRIMARY KEY AUTOINCREMENT,
  at          TEXT,
  actor       TEXT,
  action      TEXT,
  target      TEXT,
  detail_json TEXT
);
CREATE TABLE IF NOT EXISTS intake_receipts (
  client_submission_id TEXT PRIMARY KEY,
  station_id           TEXT,
  submitted_at         TEXT,
  result_json          TEXT,
  created_at           TEXT
);
"""

_TABLES = (
    "records",
    "record_versions",
    "dedupe_index",
    "confirmations",
    "alarms",
    "tasks",
    "task_events",
    "deferrals",
    "config_thresholds",
    "config_contacts",
    "config_params",
    "ops_audit",
    "intake_receipts",
)

# 周期任务种子（首个）——baseline 待现场规程核对后配置（开口项）
_DEFAULT_CYCLE = {"cycle_days": 30, "baseline": None}


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False)


class Ledger:
    """账本句柄。一个进程一个实例即可（SQLite 单写者；WAL 并发读）。"""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(_SCHEMA)
        self._ensure_columns()
        self.conn.commit()

    def _ensure_columns(self) -> None:
        """轻量迁移：为存量库补新列（records.scope / records.group_label）。"""
        existing = {row["name"] for row in self.conn.execute("PRAGMA table_info(records)")}
        for name in ("scope", "group_label"):
            if name not in existing:
                self.conn.execute(f"ALTER TABLE records ADD COLUMN {name} TEXT")

    def close(self) -> None:
        self.conn.close()

    def get_record(self, record_uid: str) -> dict | None:
        """按 UID 取记录现状（含口径/组别），供更正/作废/催办定位 subject。"""
        row = self.conn.execute(
            "SELECT * FROM records WHERE record_uid=?", (record_uid,)
        ).fetchone()
        if row is None:
            return None
        keys = row.keys()
        return {
            "record_uid": row["record_uid"],
            "record_type": row["record_type"],
            "station_id": row["station_id"],
            "occurred_at": row["occurred_at"],
            "lifecycle": row["lifecycle"],
            "rev": row["current_rev"],
            "scope": row["scope"] if "scope" in keys else None,
            "group_label": row["group_label"] if "group_label" in keys else None,
        }

    # ── 配置：种子与读取 ─────────────────────────────────────────────

    def seed_config(self, settings) -> None:
        """把部署配置作为种子写入（只补缺、不覆盖已有值）。"""
        now = iso_now()
        for scope, (lo, hi) in settings.thresholds.items():
            self.conn.execute(
                "INSERT OR IGNORE INTO config_thresholds"
                "(record_type, scope, lo, hi, updated_at, updated_by) VALUES(?,?,?,?,?,?)",
                ("battery_voltage_test", scope, float(lo), float(hi), now, "seed"),
            )
        self.conn.execute(
            "INSERT OR IGNORE INTO config_params(key, value_json, updated_at) VALUES(?,?,?)",
            ("battery_group_kinds", _dump(settings.group_kinds), now),
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO config_params(key, value_json, updated_at) VALUES(?,?,?)",
            ("battery_cycle", _dump(_DEFAULT_CYCLE), now),
        )
        self.conn.commit()

    def get_thresholds(self, record_type: str = "battery_voltage_test") -> dict:
        rows = self.conn.execute(
            "SELECT scope, lo, hi FROM config_thresholds WHERE record_type=? ORDER BY scope",
            (record_type,),
        )
        return {row["scope"]: (row["lo"], row["hi"]) for row in rows}

    def set_threshold(self, scope: str, lo: float, hi: float, *,
                      record_type: str = "battery_voltage_test", updated_by: str = "") -> None:
        self.conn.execute(
            "INSERT INTO config_thresholds(record_type, scope, lo, hi, updated_at, updated_by) "
            "VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(record_type, scope) DO UPDATE SET lo=excluded.lo, hi=excluded.hi, "
            "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
            (record_type, scope, float(lo), float(hi), iso_now(), updated_by),
        )
        self.audit(updated_by or "core", "threshold_change", f"{record_type}:{scope}",
                   {"lo": lo, "hi": hi})
        self.conn.commit()

    def get_group_kinds(self) -> dict:
        row = self.conn.execute(
            "SELECT value_json FROM config_params WHERE key='battery_group_kinds'"
        ).fetchone()
        return json.loads(row["value_json"]) if row else {}

    def get_cycle_config(self) -> dict:
        row = self.conn.execute(
            "SELECT value_json FROM config_params WHERE key='battery_cycle'"
        ).fetchone()
        return json.loads(row["value_json"]) if row else dict(_DEFAULT_CYCLE)

    def set_cycle_config(self, *, cycle_days: int, baseline: str | None,
                         updated_by: str = "") -> None:
        current = self.get_cycle_config()
        current.update({"cycle_days": int(cycle_days), "baseline": baseline})
        self.conn.execute(
            "INSERT INTO config_params(key, value_json, updated_at) VALUES('battery_cycle',?,?) "
            "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, "
            "updated_at=excluded.updated_at",
            (_dump(current), iso_now()),
        )
        self.audit(updated_by or "core", "cycle_config_change", "battery_cycle", current)
        self.conn.commit()

    # ── 周期任务台账 ────────────────────────────────────────────────

    def current_task(self, station_id: str, record_type: str,
                     group_label: str) -> dict | None:
        """当前在办任务（open/overdue 中 due 最大者）；task_id 约定 station|group|due。"""
        row = self.conn.execute(
            "SELECT * FROM tasks WHERE task_id LIKE ? AND record_type=? "
            "AND state IN ('open','overdue') ORDER BY due_at DESC LIMIT 1",
            (f"{station_id}|{group_label}|%", record_type),
        ).fetchone()
        return dict(row) if row else None

    def insert_task(self, *, task_id: str, station_id: str, record_type: str,
                    period_key: str, due_at: str, state: str,
                    overdue_since: str | None, opened_at: str) -> None:
        self.conn.execute(
            "INSERT INTO tasks(task_id, station_id, record_type, period_key, due_at, "
            "overdue_since, state, level, opened_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (task_id, station_id, record_type, period_key, due_at, overdue_since,
             state, 0, opened_at),
        )
        self.conn.commit()

    def update_task_state(self, task_id: str, state: str, *,
                          overdue_since: str | None = None) -> None:
        self.conn.execute(
            "UPDATE tasks SET state=?, overdue_since=COALESCE(?, overdue_since) "
            "WHERE task_id=?",
            (state, overdue_since, task_id),
        )
        self.conn.commit()

    def close_task(self, task_id: str, *, state: str, closed_at: str) -> None:
        self.conn.execute(
            "UPDATE tasks SET state=?, closed_at=? WHERE task_id=?",
            (state, closed_at, task_id),
        )
        self.conn.commit()

    def list_tasks(self, station_id: str, *, states: tuple | None = None,
                   record_type: str = "battery_voltage_test") -> list[dict]:
        sql = "SELECT * FROM tasks WHERE station_id=? AND record_type=?"
        params: list = [station_id, record_type]
        if states:
            marks = ",".join("?" for _ in states)
            sql += f" AND state IN ({marks})"
            params.extend(states)
        sql += " ORDER BY due_at, task_id"
        return [dict(row) for row in self.conn.execute(sql, params)]

    # ── 序号：含墓碑的完整视图 ───────────────────────────────────────

    def next_create_seq(self, station_id: str, record_type: str, occurred_at: str) -> int:
        """同站同类型同分钟的下一个创建序号（基于全量 records 行，含 voided 墓碑）。"""
        day, moment = wall_stamp(occurred_at)
        prefix = f"{station_id}-{record_type}-{day}-{moment}-"
        seq = 0
        for row in self.conn.execute(
            "SELECT record_uid FROM records WHERE record_uid LIKE ? || '%'", (prefix,)
        ):
            tail = row["record_uid"][len(prefix):]
            if tail.isdigit():
                seq = max(seq, int(tail))
        return seq + 1

    # ── 写侧 ────────────────────────────────────────────────────────

    def save_create(self, envelope: dict, result: dict, *, actor: str = "",
                    group_label: str | None = None, scope: str | None = None) -> None:
        record = result["record"]
        fields = record["fields"]
        station_id = envelope["station"]["station_id"]
        now = iso_now()
        self.conn.execute(
            "INSERT INTO records(record_uid, record_type, station_id, occurred_at, "
            "lifecycle, current_rev, created_at, scope, group_label) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (record["record_uid"], envelope["record_type"], station_id,
             envelope["occurred_at"], record["lifecycle"], record["rev"], now,
             scope, group_label),
        )
        self.conn.execute(
            "INSERT INTO record_versions(record_uid, rev, fields_json, digest, op, created_at) "
            "VALUES(?,?,?,?,?,?)",
            (record["record_uid"], record["rev"], _dump(fields), record["digest"],
             "create", now),
        )
        self.conn.execute(
            "INSERT OR REPLACE INTO dedupe_index"
            "(record_uid, station_id, occurred_day, test_kind, dc_system_id) VALUES(?,?,?,?,?)",
            (record["record_uid"], station_id, wall_day(envelope["occurred_at"]),
             fields.get("test_kind"), fields.get("dc_system_id")),
        )
        self.audit(actor or envelope.get("submitted_by") or "", "create",
                   record["record_uid"], {"items": len(fields.get("items") or [])})
        self.conn.commit()

    def save_confirm(self, envelope: dict, result: dict, *, actor: str = "") -> None:
        record = result["record"]
        uid = record["record_uid"]
        now = iso_now()
        self.conn.execute(
            "UPDATE records SET lifecycle=?, confirmed_at=? WHERE record_uid=?",
            (record["lifecycle"], now, uid),
        )
        for slot in record.get("signature_slots") or []:
            if slot.get("state") == "signed":
                self.conn.execute(
                    "INSERT OR REPLACE INTO confirmations"
                    "(record_uid, rev, slot, by_who, at) VALUES(?,?,?,?,?)",
                    (uid, record["rev"], slot.get("slot"), slot.get("by"), slot.get("at")),
                )
        self.audit(actor or "auto-confirm", "confirm", uid,
                   {"lifecycle": record["lifecycle"]})
        self.conn.commit()

    def save_correct(self, envelope: dict, result: dict, *, actor: str = "") -> None:
        record = result["record"]
        fields = record["fields"]
        uid = record["record_uid"]
        now = iso_now()
        self.conn.execute(
            "INSERT INTO record_versions(record_uid, rev, fields_json, digest, op, created_at) "
            "VALUES(?,?,?,?,?,?)",
            (uid, record["rev"], _dump(fields), record["digest"], "correct", now),
        )
        self.conn.execute(
            "UPDATE records SET current_rev=?, lifecycle=? WHERE record_uid=?",
            (record["rev"], record["lifecycle"], uid),
        )
        self.conn.execute(
            "UPDATE dedupe_index SET occurred_day=?, test_kind=?, dc_system_id=? "
            "WHERE record_uid=?",
            (wall_day(envelope["occurred_at"]), fields.get("test_kind"),
             fields.get("dc_system_id"), uid),
        )
        self.audit(actor or envelope.get("submitted_by") or "", "correct", uid,
                   {"rev": record["rev"]})
        self.conn.commit()

    def save_void(self, envelope: dict, result: dict, *, actor: str = "") -> None:
        record = result["record"]
        uid = record["record_uid"]
        self.conn.execute(
            "UPDATE records SET lifecycle=?, voided_at=? WHERE record_uid=?",
            (record["lifecycle"], iso_now(), uid),
        )
        self.audit(actor or envelope.get("voided_by") or "", "void", uid,
                   {"reason": envelope.get("void_reason")})
        self.conn.commit()

    def update_alarm(self, station_id: str, record_type: str,
                     alarm_state: dict | None, occurred_at: str) -> None:
        fingerprint = (alarm_state or {}).get("fingerprint")
        if not fingerprint:
            return
        now = iso_now()
        state = (alarm_state or {}).get("state")
        row = self.conn.execute(
            "SELECT occur_count FROM alarms WHERE fingerprint=?", (fingerprint,)
        ).fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO alarms(fingerprint, station_id, record_type, first_seen_at, "
                "occur_count, last_status, escalated_count, updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (fingerprint, station_id, record_type, occurred_at, 1, state,
                 1 if state == "escalated" else 0, now),
            )
        else:
            self.conn.execute(
                "UPDATE alarms SET occur_count=occur_count+1, last_status=?, "
                "escalated_count=escalated_count+?, updated_at=? WHERE fingerprint=?",
                (state, 1 if state == "escalated" else 0, now, fingerprint),
            )
        self.conn.commit()

    def audit(self, actor: str, action: str, target: str, detail) -> None:
        self.conn.execute(
            "INSERT INTO ops_audit(at, actor, action, target, detail_json) VALUES(?,?,?,?,?)",
            (iso_now(), actor, action, target, _dump(detail)),
        )
        self.conn.commit()

    # ── 提交回执（幂等） ─────────────────────────────────────────────

    def get_receipt(self, client_submission_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT result_json FROM intake_receipts WHERE client_submission_id=?",
            (client_submission_id,),
        ).fetchone()
        return json.loads(row["result_json"]) if row else None

    def put_receipt(self, client_submission_id: str, station_id: str,
                    submitted_at: str, result: dict) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO intake_receipts"
            "(client_submission_id, station_id, submitted_at, result_json, created_at) "
            "VALUES(?,?,?,?,?)",
            (client_submission_id, station_id, submitted_at, _dump(result), iso_now()),
        )
        self.conn.commit()

    # ── 读侧：物化引擎视图 ───────────────────────────────────────────

    def build_ledger_view(self, station_id: str, record_type: str) -> dict:
        """账本视图：同站同类型全量记录（含墓碑）+ 已定稿摘要 + 联动记录（P1 空）。"""
        rows: list[dict] = []
        for rec in self.conn.execute(
            "SELECT * FROM records WHERE station_id=? AND record_type=? "
            "ORDER BY occurred_at, record_uid",
            (station_id, record_type),
        ):
            version = self.conn.execute(
                "SELECT fields_json, digest FROM record_versions "
                "WHERE record_uid=? AND rev=?",
                (rec["record_uid"], rec["current_rev"]),
            ).fetchone()
            fields = json.loads(version["fields_json"])
            rows.append({
                "record_uid": rec["record_uid"],
                "lifecycle": rec["lifecycle"],
                "rev": rec["current_rev"],
                "occurred_at": rec["occurred_at"],
                "digest": version["digest"],
                "fields": fields,
                "dedupe_key_values": {
                    "station": station_id,
                    "occurred_day": wall_day(rec["occurred_at"]),
                    "test_kind": fields.get("test_kind"),
                    "dc_system_id": fields.get("dc_system_id"),
                },
            })
        digests = [
            row["digest"]
            for row in self.conn.execute(
                "SELECT v.digest AS digest FROM record_versions v "
                "JOIN records r ON r.record_uid = v.record_uid "
                "WHERE r.station_id=? AND r.record_type=? "
                "AND r.lifecycle IN ('confirmed', 'archived')",
                (station_id, record_type),
            )
        ]
        return {
            "same_type_records": rows,
            "confirmed_digests": digests,
            "linked_records": [],
        }

    def build_history(self, station_id: str, record_type: str) -> list[dict]:
        """趋势历史：仅 archived 行入（§5.1 口径）；按 occurred_at 严格递增。"""
        rows: list[dict] = []
        for rec in self.conn.execute(
            "SELECT * FROM records WHERE station_id=? AND record_type=? "
            "AND lifecycle='archived' ORDER BY occurred_at, record_uid",
            (station_id, record_type),
        ):
            version = self.conn.execute(
                "SELECT fields_json, digest FROM record_versions "
                "WHERE record_uid=? AND rev=?",
                (rec["record_uid"], rec["current_rev"]),
            ).fetchone()
            rows.append({
                "occurred_at": rec["occurred_at"],
                "digest": version["digest"],
                "lifecycle": "archived",
                "fields": json.loads(version["fields_json"]),
            })
        return rows

    def build_alarm_history(self, station_id: str, record_type: str) -> list[dict]:
        return [
            {
                "fingerprint": row["fingerprint"],
                "first_seen_at": row["first_seen_at"],
                "occur_count": row["occur_count"],
                "last_status": row["last_status"],
                "escalated_count": row["escalated_count"],
            }
            for row in self.conn.execute(
                "SELECT * FROM alarms WHERE station_id=? AND record_type=? "
                "ORDER BY first_seen_at, fingerprint",
                (station_id, record_type),
            )
        ]

    # ── 概览 ────────────────────────────────────────────────────────

    def counts(self) -> dict:
        return {
            table: self.conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            for table in _TABLES
        }
