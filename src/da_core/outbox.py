"""触达发件箱：催办消息 / 待办 的唯一出口（渠道层）。

- 渠道：钉钉待办（``dws todo +create``）、群消息（``dws chat +send-to-group``）；
  一条提醒「双轨」发送：待办常驻 + 群消息触达；末级 DING 由升级链（T4）启用。
- 回执：每次投递在 ``task_events`` 落一行（attempt 时间 / result / retry_count / detail）；
  幂等键 = (task_id, level, channel)——同层同渠道成功后不重发；失败按调用方节奏重试。
- 发送器可注入（runner）——测试与演练使用假发送器，零真实消息。
- 无人值守语义：催办触达属已授权运行流程（「无人值守、不等来问」），
  dws 的交互确认门禁在非交互环境用 ``--yes`` 表达该既有授权；每次发送均留痕可审计。
"""

from __future__ import annotations

from da_core import dws_cli

# config_contacts 角色约定（催办触达目标）
ROLE_GROUP = "reminder_group"        # 群名称或 openConversationId
ROLE_ASSIGNEE = "reminder_assignee"  # 责任人 userId（待办执行人）
ROLE_ESCALATE = "reminder_escalate"  # 升级对象姓名（DM 用；班长/管理）


def render_cycle_message(*, group: str, due: str, last_done: str | None = None,
                         days_to_due: int | None = None, overdue_days: int = 0) -> dict:
    """周期提醒文案（群消息用正文；待办用标题）。"""
    if overdue_days > 0:
        title = f"【超期{overdue_days}天】{group} 蓄电池测量未完成"
        lead = f"⚠️ {group} 蓄电池定期测量已超期 {overdue_days} 天（应于 {due} 前完成）"
    elif days_to_due is not None and 0 <= days_to_due <= 3:
        title = f"【倒计时{days_to_due}天】{group} 蓄电池测量"
        lead = f"⏰ {group} 蓄电池定期测量应于 {due} 前完成（剩余 {days_to_due} 天）"
    else:
        title = f"{group} 蓄电池测量待办"
        lead = f"{group} 蓄电池定期测量待办（应于 {due} 前完成）"
    lines = [lead]
    if last_done:
        lines.append(f"上次完成：{last_done[:10]}")
    lines.append("请按规程完成测量并录入。")
    lines.append("——AI助手")
    return {"title": title, "text": "\n".join(lines)}


def send_group(group: str, text: str, *, runner=None, confirm: bool = True) -> dict:
    """群消息触达（group 为群名称或 openConversationId）。"""
    args = ["chat", "+send-to-group", "--group", group, "--content", text,
            "--format", "json"]
    if confirm:
        args.append("--yes")
    rc, out, err = (runner or dws_cli.run_dws)(args)
    return {"ok": rc == 0, "rc": rc, "detail": (out or err).strip()[:300]}


def send_todo(assignee: str, *, title: str, due: str | None = None,
              priority: int = 30, runner=None, confirm: bool = True) -> dict:
    """待办触达（待办常驻）。assignee 为真实 userId。"""
    args = ["todo", "+create", "--title", title, "--executors", assignee,
            "--priority", str(priority), "--format", "json"]
    if due:
        args += ["--due", due]
    if confirm:
        args.append("--yes")
    rc, out, err = (runner or dws_cli.run_dws)(args)
    return {"ok": rc == 0, "rc": rc, "detail": (out or err).strip()[:300]}


def send_dm(name: str, text: str, *, runner=None, confirm: bool = True) -> dict:
    """单人 DM 触达（升级对象用；name 为姓名，唯一解析）。"""
    args = ["chat", "+dm", "--to", name, "--content", text, "--format", "json"]
    if confirm:
        args.append("--yes")
    rc, out, err = (runner or dws_cli.run_dws)(args)
    return {"ok": rc == 0, "rc": rc, "detail": (out or err).strip()[:300]}


_RETRY_CAP = 3  # 同一 (task, level, channel) 的投递重试上限（含失败留痕）


def deliver(ledger, spec: dict, *, runner=None, dry_run: bool = False) -> dict:
    """单次投递 + 回执落账（幂等拦截：同 (task, level, channel) 已成功后不重发）。

    spec: ``{task_id, level, channel: 'group'|'todo'|'dm', target, text, title?, due?}``
    """
    channel = spec["channel"]
    if channel not in ("group", "todo", "dm"):
        raise ValueError(f"未知渠道：{channel!r}")
    prior = ledger.find_task_event(spec["task_id"], spec["level"], channel)
    if prior and prior["result"] == "sent":
        return {"sent": False, "reason": "already-sent", "event_id": prior["event_id"]}
    if prior and prior["retry_count"] >= _RETRY_CAP:
        return {"sent": False, "reason": "retry-exhausted",
                "retry_count": prior["retry_count"]}
    if dry_run:
        return {"sent": False, "reason": "dry-run", "would": dict(spec)}

    if channel == "group":
        result = send_group(spec["target"], spec["text"], runner=runner)
    elif channel == "dm":
        result = send_dm(spec["target"], spec["text"], runner=runner)
    else:
        result = send_todo(spec["target"], title=spec.get("title") or "蓄电池测量提醒",
                           due=spec.get("due"), runner=runner)
    retry_count = (prior["retry_count"] + 1) if prior else 0
    event = ledger.record_task_event(
        task_id=spec["task_id"], level=spec["level"], channel=channel,
        target=spec["target"], result="sent" if result["ok"] else "failed",
        detail=result.get("detail"), retry_count=retry_count)
    return {"sent": result["ok"], "retry_count": retry_count, "event": event,
            "detail": result.get("detail")}


def deliver_cycle(ledger, task: dict, item: dict, *, contacts: dict, level: int,
                  runner=None, dry_run: bool = False) -> list[dict]:
    """按任务当前状态双轨触达（群 + 待办）；未配置目标如实回 no-target。"""
    group_label = item.get("group") or task.get("task_id", "").split("|")[1]
    message = render_cycle_message(
        group=group_label, due=task["due_at"], last_done=item.get("last_done_at"),
        days_to_due=item.get("days_to_due"), overdue_days=item.get("overdue_days") or 0)
    results: list[dict] = []

    group_target = contacts.get(ROLE_GROUP)
    if group_target:
        results.append({"channel": "group", **deliver(ledger, {
            "task_id": task["task_id"], "level": level, "channel": "group",
            "target": group_target, "text": message["text"]}, runner=runner,
            dry_run=dry_run)})
    else:
        results.append({"channel": "group", "sent": False, "reason": "no-target"})

    assignee = contacts.get(ROLE_ASSIGNEE)
    if assignee:
        results.append({"channel": "todo", **deliver(ledger, {
            "task_id": task["task_id"], "level": level, "channel": "todo",
            "target": assignee, "title": message["title"],
            "due": task["due_at"] + "T23:59:59+08:00"}, runner=runner, dry_run=dry_run)})
    else:
        results.append({"channel": "todo", "sent": False, "reason": "no-target"})
    return results
