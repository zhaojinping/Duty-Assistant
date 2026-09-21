"""群消息接入口：解析「蓄电池电压测量数据」降级文本 → 提交核心（P3 群通道改道）。

应用现存格式（textFull()）+ 本改道扩展行：

    # 蓄电池电压测量数据
    # 提交时间: 2026-09-21 11:45

    [组别] 3号组(12只)
    [温度] 23
    [数量] 12/12　[合格区间] 13.20~13.80V
    [异常] 无
    [数据] 1:13.36,2:13.38,…
      ← 扩展（应用增强后 / 人工补录时提供）：
    [直流系统] DC-003
    [浮充电压] 13.50
    [测试性质] 定期

- [数量]/[合格区间]/[异常] 为应用侧展示行，核心不采用（阈值以核心配置为准）。
- 缺 3 个必填字段（直流系统编号/浮充电压/测试性质）的消息会被核心拒收，
  群内回执提示补齐方式（不落账、不写表）；补齐（扩展行）后重发即可。
- 幂等：client_submission_id = 钉钉 messageId（回执机制天然去重，重拉不重记）。
"""

from __future__ import annotations

import json
import re

from da_core import dws_cli
from da_core.intake import IntakeError
from da_core.service import submit_submission

HEADER = "蓄电池电压测量数据"  # 判定标记（钉钉会把 "# " 行转成 **加粗**，不能依赖 #）
CURSOR_PARAM = "group_intake"
DEFAULT_GROUP = "APM测试"
_REQUIRED_HINT = "直流系统编号/浮充电压/测试性质"

_SUBMIT_TIME_RE = re.compile(r"提交时间[:：]\s*(\d{4}-\d{2}-\d{2})[\sT]+(\d{2}:\d{2})")
_BLOCK_RE = re.compile(r"\[组别\]\s*")


def _clean(text: str) -> str:
    """归一化钉钉 Markdown 规整后的文本：去加粗星号、全角空格转半角。"""
    return (text or "").replace("**", "").replace("\u3000", " ")


def parse_group_message(text: str) -> dict:
    """解析降级文本（兼容「原样换行」与「钉钉 Markdown 规整」两种形态）。

    返回 ``{"submitted_at": str|None, "groups":[{group, env_temp?, dc_system_id?,
    float_voltage?, test_kind?, items:[{no, volt}]}]}``
    """
    cleaned = _clean(text)
    if HEADER not in cleaned:
        raise IntakeError("不是蓄电池电压测量数据消息")

    submitted_at = None
    match = _SUBMIT_TIME_RE.search(cleaned)
    if match:
        submitted_at = f"{match.group(1)}T{match.group(2)}:00+08:00"

    parts = _BLOCK_RE.split(cleaned)
    groups: list[dict] = []
    for block in parts[1:]:
        group_name = block.split("[", 1)[0].strip()
        if not group_name or "[" not in block:
            continue
        entry: dict = {"group": group_name, "items": []}

        match = re.search(r"\[温度\]\s*([^\s\[]+)", block)
        if match and match.group(1) != "未填":
            try:
                entry["env_temp"] = float(match.group(1))
            except ValueError:
                pass
        match = re.search(r"\[直流系统\]\s*([^\s\[]+)", block)
        if match and match.group(1) != "未填":
            entry["dc_system_id"] = match.group(1)
        match = re.search(r"\[浮充电压\]\s*([\d.]+)", block)
        if match:
            try:
                entry["float_voltage"] = float(match.group(1))
            except ValueError:
                pass
        match = re.search(r"\[测试性质\]\s*([^\s\[]+)", block)
        if match and match.group(1) != "未填":
            entry["test_kind"] = match.group(1)
        match = re.search(r"\[数据\]\s*(.*)", block, flags=re.S)
        if match:
            data_part = match.group(1)
            cut = data_part.find("[")
            if cut != -1:
                data_part = data_part[:cut]
            for pair in re.split(r"[,\s]+", data_part.strip()):
                if ":" not in pair:
                    continue
                number, value = pair.split(":", 1)
                try:
                    entry["items"].append({"no": int(number.strip()),
                                           "volt": float(value.strip())})
                except ValueError:
                    continue
        # [数量]/[合格区间]/[异常] 展示行：忽略（核心口径为准）
        groups.append(entry)

    if not groups:
        raise IntakeError("消息里没有解析到任何组数据")
    return {"submitted_at": submitted_at, "groups": groups}


def ingest_group_message(text: str, *, message_id: str, settings, ledger,
                         actor: str = "群消息", dispatch: bool = True,
                         dry_run: bool = False) -> dict:
    """解析并提交（幂等键=messageId）；返回 submit 摘要（含 rejected 明细）。

    结构性问题（如缺必填字段）不抛出，转为 rejected 摘要（回执据此提示补齐）。
    """
    parsed = parse_group_message(text)
    data = {
        "client_submission_id": f"dingtalk:{message_id}",
        "operator": actor,
        "groups": parsed["groups"],
    }
    if parsed.get("submitted_at"):
        data["submitted_at"] = parsed["submitted_at"]
    try:
        return submit_submission(data, settings=settings, ledger=ledger,
                                 dispatch=dispatch, dry_run_dispatch=dry_run)
    except IntakeError as exc:
        message = str(exc)
        code = "E_REQUIRED" if "缺字段" in message else "E_INTAKE"
        return {
            "ok": False,
            "submitted_at": parsed.get("submitted_at"),
            "groups": [
                {"group": group.get("group"), "status": "rejected",
                 "validation": {"ok": False, "errors": [
                     {"code": code, "path": "payload", "message": message}]}}
                for group in parsed["groups"]
            ],
            "counts": {"groups": len(parsed["groups"]), "ok": 0, "cells": 0},
        }


def fetch_recent_messages(group: str, *, runner=None, limit: int = 50) -> list[dict]:
    """拉取群最近消息（升序返回）。"""
    rc, out, err = (runner or dws_cli.run_dws)([
        "chat", "+chat-messages", "--group", group, "--limit", str(limit),
        "--no-reactions", "--format", "json"])
    if rc != 0:
        raise RuntimeError(f"读群消息失败：{(err or out).strip()[:200]}")
    try:
        messages = json.loads(out).get("messages") or []
    except (TypeError, ValueError):
        raise RuntimeError("读群消息返回不是 JSON") from None
    return sorted(messages, key=lambda m: m.get("createTime") or "")


def _receipt_text(summary: dict) -> str:
    if summary.get("replayed"):
        return ""  # 重放不回执（防刷屏）
    if not summary.get("ok"):
        reasons = []
        need_fix = False
        for group in summary.get("groups") or []:
            if group.get("status") != "rejected":
                continue
            errors = (group.get("validation") or {}).get("errors") or []
            codes = {error.get("code") for error in errors}
            if "E_REQUIRED" in codes:
                need_fix = True
                reasons.append(f"{group['group']}：缺必填字段（{_REQUIRED_HINT}）")
            elif "E_DUP_KEY" in codes:
                reasons.append(f"{group['group']}：同日同组已有记录，无需重发")
            else:
                first = (errors[:1] or [{}])[0]
                message = first.get("message") or first.get("code") or "校验未通过"
                reasons.append(f"{group['group']}：{message}")
        text = "⚠️ 未入库：" + "；".join(reasons)
        if need_fix:
            text += "\n请补 [直流系统] [浮充电压] [测试性质] 行后重发。"
        return text + "\n——AI助手"
    parts = []
    for group in summary.get("groups") or []:
        if group.get("status") != "ok":
            continue
        item_count = len(((group.get("_payload") or {}).get("items")) or [])
        violations = [e for e in (group.get("rules") or [])
                      if e.get("verdict") == "violation"]
        parts.append(f"{group['group']} {item_count} 只（异常 {len(violations)}）")
    return "✅ 已入库：" + "；".join(parts) + "\n——AI助手"


def poll_group(ledger, settings, *, actor: str = "群消息", runner=None,
               reply: bool = True, dispatch: bool = True, dry_run: bool = False,
               limit: int = 50) -> dict:
    """拉取群消息 → 处理新的测量提交 →（可选）回执；游标存 config_params。"""
    cursor = ledger.get_param(CURSOR_PARAM) or {}
    group = cursor.get("group") or DEFAULT_GROUP
    last_time = cursor.get("last_time")

    messages = fetch_recent_messages(group, runner=runner, limit=limit)
    processed: list[dict] = []
    for message in messages:
        created = message.get("createTime") or ""
        if last_time and created <= last_time:
            continue
        text = message.get("text") or ""
        if HEADER not in text:
            continue
        message_id = message.get("messageId") or ""

        if dry_run:  # 解析演练：不提交、不落账、不回执、不推进游标
            try:
                parsed = parse_group_message(text)
                processed.append({
                    "messageId": message_id, "would_submit": True,
                    "groups": [g.get("group") for g in parsed["groups"]]})
            except IntakeError as exc:
                processed.append({"messageId": message_id, "error": str(exc)})
            continue

        try:
            summary = ingest_group_message(text, message_id=message_id,
                                           settings=settings, ledger=ledger,
                                           actor=actor, dispatch=dispatch)
        except IntakeError as exc:
            processed.append({"messageId": message_id, "ok": False,
                              "error": str(exc)})
            ledger.set_param(CURSOR_PARAM,
                             {"group": group, "last_time": created,
                              "last_msg": message_id}, updated_by="poller")
            continue

        reply_text = _receipt_text(summary)
        if reply and reply_text:
            from da_core.outbox import send_group
            send_group(group, reply_text, runner=runner)
        processed.append({"messageId": message_id, "ok": summary.get("ok"),
                          "replayed": summary.get("replayed", False),
                          "reply": reply_text or None})
        ledger.set_param(CURSOR_PARAM,
                         {"group": group, "last_time": created,
                          "last_msg": message_id}, updated_by="poller")

    return {"group": group, "scanned": len(messages),
            "processed": processed,
            "cursor": ledger.get_param(CURSOR_PARAM)}
