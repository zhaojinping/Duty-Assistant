"""拉取式输入源：核心主动从应用侧只读接口拉取提交（内网零暴露）。

契约见仓内 ``docs/input-channel-v2.md`` §2（即《输入通道 v2 · 需求》）::

    GET {base}/api/submissions?since={游标}&limit=100
    Authorization: Bearer <token>
    → {"items": [{client_submission_id, operator, submitted_at, groups: [...]}],
       "next_cursor": "...", "has_more": false}

语义：

- 幂等键 = ``client_submission_id``（与其它通道共享 intake_receipts 判重；重扫安全）；
- 游标只前进、存 ``config_params``；硬失败（解析不了的结构）不推进，下轮重试；
- 拉取失败抛 ``RuntimeError``（由守候任务告警）；服务端保留 ≥30 天，不丢数据。
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
_ITEM_KEYS = ("client_submission_id", "operator", "submitted_at", "groups")


def default_fetcher(url: str, token: str | None, timeout: int = 30) -> dict:
    """默认取数器：仅接受 http(s)，显式携带 Bearer token。"""
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError(f"仅支持 http(s) 地址：{url!r}")
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8"))


def build_url(base_url: str, cursor: str | None, limit: int) -> str:
    query = {"limit": str(limit)}
    if cursor:
        query["since"] = cursor
    return base_url.rstrip("/") + "/api/submissions?" + urllib.parse.urlencode(query)


def pull_remote(ledger: Ledger, settings: Settings, *, base_url: str | None = None,
                token: str | None = None, fetcher=None, limit: int = 100,
                dispatch: bool = True, dry_run: bool = False) -> dict:
    """拉取并处理新提交；``dry_run`` 只取不落账、不推进游标。"""
    if not base_url:
        raise IntakeError("未配置拉取接口地址（--base-url 或 DA_PULL_URL）")
    ledger.seed_config(settings)
    stored = ledger.get_param(CURSOR_PARAM) or {}
    cursor = stored.get("cursor")
    url = build_url(base_url, cursor, limit)

    try:
        payload = (fetcher or default_fetcher)(url, token)
    except Exception as exc:  # noqa: BLE001 — 网络/协议失败统一转运维信号
        raise RuntimeError(f"拉取失败：{exc!r}") from None
    if not isinstance(payload, dict):
        raise RuntimeError("拉取返回不是 JSON 对象")

    items = payload.get("items") or []
    processed: list[dict] = []
    hard_fail = False
    for item in items:
        if not isinstance(item, dict):
            hard_fail = True
            processed.append({"id": None, "ok": False, "error": "非对象条目"})
            continue
        submission_id = item.get("client_submission_id")
        submission = {key: item.get(key) for key in _ITEM_KEYS}
        if dry_run:
            processed.append({
                "id": submission_id, "would_submit": True,
                "groups": [g.get("group") for g in (submission.get("groups") or [])
                           if isinstance(g, dict)],
            })
            continue
        try:
            summary = submit_submission(submission, settings=settings, ledger=ledger,
                                        dispatch=dispatch)
        except IntakeError as exc:
            hard_fail = True  # 结构性问题：保留游标，下轮重试
            processed.append({"id": submission_id, "ok": False, "error": str(exc)})
            continue
        processed.append({
            "id": submission_id,
            "ok": summary.get("ok"),
            "replayed": summary.get("replayed", False),
            "records": [g.get("record_uid") for g in summary.get("groups") or []],
        })

    next_cursor = payload.get("next_cursor")
    if not dry_run and next_cursor and next_cursor != cursor and not hard_fail:
        ledger.set_param(CURSOR_PARAM,
                         {"cursor": next_cursor, "updated_at": iso_now()},
                         updated_by="puller")
    return {"fetched": len(items), "processed": processed,
            "cursor": next_cursor or cursor, "hard_fail": hard_fail}
