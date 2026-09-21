"""拉取式输入源：核心主动从应用侧只读接口拉取提交（内网零暴露）。

对接实况（WorkBuddy 侧 §2，2026-09-21）::

    GET {base}/api/submissions?since={游标}&limit=100
    X-Submissions-Token: ＜token＞     # 注意：不是 Authorization（对方网关会注入/覆写该头）
    → {"items": [{"seq": 1, "received_at": "...", "payload": {提交报文}}],
       "next_cursor": "...", "has_more": false}

语义：

- 提交报文 ``payload`` = 与页面提交同源同构（契约 §4）；条目亦兼容裸提交对象形态；
- 幂等键 = ``payload.client_submission_id``（与其它通道共享 intake_receipts 判重；重扫安全）；
- 游标初始 ``"0"``，此后用 ``next_cursor``；只前进、存 ``config_params``；硬失败不推进；
- 每轮最多翻 10 页；服务端保留 ≥30 天。
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request

from da_core.clock import iso_now
from da_core.intake import IntakeError
from da_core.ledger import Ledger
from da_core.service import submit_submission
from da_core.settings import Settings

CURSOR_PARAM = "pull_intake"
TOKEN_HEADER = "X-Submissions-Token"  # 对接方网关会覆写 Authorization，勿改
_ITEM_KEYS = ("client_submission_id", "operator", "submitted_at", "groups")
_MAX_PAGES = 10


def default_fetcher(url: str, token: str | None, timeout: int = 30) -> dict:
    """默认取数器：仅接受 http(s)；鉴权头按对接规约。"""
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError(f"仅支持 http(s) 地址：{url!r}")
    headers = {"Accept": "application/json"}
    if token:
        headers[TOKEN_HEADER] = token
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8"))


def build_url(base_url: str, cursor: str | None, limit: int) -> str:
    query = {"limit": str(limit), "since": cursor or "0"}
    return base_url.rstrip("/") + "/api/submissions?" + urllib.parse.urlencode(query)


def extract_payload(item: dict) -> dict:
    """兼容两种条目形态：包裹体 ``{seq, received_at, payload}`` 或裸提交对象。"""
    payload = item.get("payload")
    return payload if isinstance(payload, dict) else item


def pull_remote(ledger: Ledger, settings: Settings, *, base_url: str | None = None,
                token: str | None = None, fetcher=None, limit: int = 100,
                dispatch: bool = True, dry_run: bool = False,
                runner=None, notify_group: bool = False) -> dict:
    """拉取并处理新提交；``dry_run`` 只取不落账、不推进游标。"""
    if not base_url:
        raise IntakeError("未配置拉取接口地址（--base-url 或 DA_PULL_URL）")
    ledger.seed_config(settings)
    stored = ledger.get_param(CURSOR_PARAM) or {}
    start_cursor = stored.get("cursor")
    cursor = start_cursor

    processed: list[dict] = []
    fresh_summaries: list[dict] = []  # 新入库（非重放）的提交，用于群内回执
    fetched = 0
    hard_fail = False
    pages = 0
    while True:
        url = build_url(base_url, cursor, limit)
        try:
            payload = (fetcher or default_fetcher)(url, token)
        except Exception as exc:  # noqa: BLE001 — 网络/协议失败统一转运维信号
            raise RuntimeError(f"拉取失败：{exc!r}") from None
        if not isinstance(payload, dict):
            raise RuntimeError("拉取返回不是 JSON 对象")

        items = payload.get("items") or []
        fetched += len(items)
        for item in items:
            if not isinstance(item, dict):
                hard_fail = True
                processed.append({"id": None, "ok": False, "error": "非对象条目"})
                continue
            submission = extract_payload(item)
            submission_id = submission.get("client_submission_id")
            if dry_run:
                processed.append({
                    "id": submission_id, "would_submit": True,
                    "groups": [g.get("group") for g in (submission.get("groups") or [])
                               if isinstance(g, dict)],
                })
                continue
            wire = {key: submission.get(key) for key in _ITEM_KEYS}
            try:
                summary = submit_submission(wire, settings=settings, ledger=ledger,
                                            dispatch=dispatch)
            except IntakeError as exc:
                hard_fail = True  # 结构性问题：保留游标，下轮重试
                processed.append({"id": submission_id, "ok": False, "error": str(exc)})
                continue
            replayed = bool(summary.get("replayed", False))
            if summary.get("ok") and not replayed:
                fresh_summaries.append(summary)
            processed.append({
                "id": submission_id,
                "ok": summary.get("ok"),
                "replayed": replayed,
                "records": [g.get("record_uid") for g in summary.get("groups") or []],
            })

        next_cursor = payload.get("next_cursor") or cursor
        has_more = bool(payload.get("has_more"))
        cursor = next_cursor
        pages += 1
        if not has_more or hard_fail or pages >= _MAX_PAGES or not items:
            break

    if not dry_run and cursor and cursor != start_cursor and not hard_fail:
        ledger.set_param(CURSOR_PARAM,
                         {"cursor": cursor, "updated_at": iso_now()},
                         updated_by="puller")

    notify_errors: list[str] = []
    if notify_group and fresh_summaries:
        station_id = (settings.station or {}).get("station_id")
        contacts = ledger.get_contacts(station_id) if station_id else {}
        target = (contacts or {}).get("reminder_group")
        if target:
            from da_core.group_intake import _receipt_text
            from da_core.outbox import send_group
            for summary in fresh_summaries:
                text = _receipt_text(summary)
                if not text:
                    continue
                try:
                    send_group(target, text, runner=runner)
                except Exception as exc:  # noqa: BLE001 — 回执失败不影响入库事实
                    notify_errors.append(repr(exc)[:200])
    return {"fetched": fetched, "pages": pages, "processed": processed,
            "cursor": cursor, "hard_fail": hard_fail, "notify_errors": notify_errors}
