"""入口卡投放：X-APM 机器人身份经开放平台投模板互动卡（createAndDeliver）。

- 用途：「到期提醒随发」（升级链 level 1，一个到期周期只发 1 张）与
  「月报随发」（da_daily 收口推送，见 reporting.push_monthly_report）。
- 凭据：appKey/appSecret 运行时经 ``dws devapp +credentials-get`` 读取，
  仅进程内流转；accessToken 即取即用，绝不留存、绝不打印。
- 卡面：模板 ``33f4dfb8``（🔋蓄电池电压测量，[点击录入][查看记录] 双按钮）。
- 按钮跳转地址是**模板变量**：必须随卡携带候选参数集（2026-09-22 实弹命中
  “批量候选键”方案），否则按钮是死键。未知键被模板静默忽略，无害。
- 幂等：按「到期周期」去重（config_params.entry_card_last_due = 任务 due_at）。
- ``runner`` = dws 子进程注入；``poster`` = HTTP 注入（测试/演练用假发送器）。
"""

from __future__ import annotations

import json
import time
import urllib.request

from da_core import dws_cli

UNIFIED_APP_ID = "bd6170ca-05b4-4fec-b8e0-d31384f90131"
ROBOT_CODE = "dingwc9bjbnkt9im6xyr"
TEMPLATE_ID = "33f4dfb8-1bbc-4df1-bfe6-51eec93fab00.schema"
GROUP_CID = "cidv+NrVM1LiAtv8FG85KEqMw=="
ENTRY_URL = "https://battery-voltage-entry.app.workbuddy.host/"
LEDGER_URL = "https://alidocs.dingtalk.com/i/nodes/np9zOoBVBYALR6aeuenZZglmW1DK0g6l"

# 按钮 1「点击录入」候选变量名（28 个）
_BTN1_NAMES = ["url1", "url_1", "URL1", "Url1", "link1", "link_1", "jumpUrl1", "jump_url1",
               "openUrl1", "open_url1", "btn1Url", "btn1_url", "btn1url",
               "button1Url", "button1_url", "button1url", "action1Url", "action1_url",
               "entryUrl", "entry_url", "fillUrl", "fill_url", "inputUrl", "input_url",
               "luruUrl", "luru_url", "createUrl", "create_url"]
# 按钮 2「查看记录」候选变量名（28 个）
_BTN2_NAMES = ["url2", "url_2", "URL2", "Url2", "link2", "link_2", "jumpUrl2", "jump_url2",
               "openUrl2", "open_url2", "btn2Url", "btn2_url", "btn2url",
               "button2Url", "button2_url", "button2url", "action2Url", "action2_url",
               "viewUrl", "view_url", "recordUrl", "record_url",
               "ledgerUrl", "ledger_url", "chakanUrl", "chakan_url", "queryUrl", "query_url"]
# 单变量兜底（不确定指向哪个按钮，取录入入口）
_SOLO_NAMES = ["url", "link", "jumpUrl", "openUrl", "actionUrl"]

# 接口地址常量（换 token 的登录端点与投卡端点；非凭据，凭据运行时取）
_ACCESS_URL = "https://api.dingtalk.com/v1.0/oauth2/accessToken"
_DELIVER_URL = "https://api.dingtalk.com/v1.0/card/instances/createAndDeliver"


def build_card_params(entry_url: str = ENTRY_URL, ledger_url: str = LEDGER_URL) -> dict:
    """按钮跳转参数集（候选批量；命中即生效、未知键被忽略）。"""
    params = {name: entry_url for name in _BTN1_NAMES}
    params.update({name: ledger_url for name in _BTN2_NAMES})
    params.update({name: entry_url for name in _SOLO_NAMES})
    return params


def build_payload(*, target: str = "group", user_id: str | None = None,
                  out_track_id: str | None = None, group_cid: str = GROUP_CID,
                  entry_url: str = ENTRY_URL, ledger_url: str = LEDGER_URL,
                  robot_code: str = ROBOT_CODE, template_id: str = TEMPLATE_ID) -> dict:
    """createAndDeliver 请求体。target=group 投群；target=dm 投机器人单聊。"""
    if target == "group":
        space = f"dtv1.card//IM_GROUP.{group_cid}"
        deliver_model = {"imGroupOpenDeliverModel": {"robotCode": robot_code}}
        space_model = {"imGroupOpenSpaceModel": {"supportForward": True}}
        suffix = "group"
    elif target == "dm":
        if not user_id:
            raise ValueError("dm 投放需要 user_id")
        space = f"dtv1.card//IM_ROBOT.{user_id}"
        deliver_model = {"imRobotOpenDeliverModel": {"robotCode": robot_code}}
        space_model = {"imRobotOpenSpaceModel": {"supportForward": True}}
        suffix = "dm"
    else:
        raise ValueError(f"未知投放目标：{target!r}")
    out_track = out_track_id or f"dutyplus-entrycard-{suffix}-{int(time.time())}"
    payload = {
        "userIdType": 1,
        "cardTemplateId": template_id,
        "outTrackId": out_track,
        "openSpaceId": space,
        "cardData": {"cardParamMap": build_card_params(entry_url, ledger_url)},
        "callbackType": "STREAM",
    }
    payload.update(space_model)
    payload.update(deliver_model)
    return payload


def _default_poster(url: str, payload: dict, headers: dict) -> dict:
    """真实 HTTP 投递（urllib）。"""
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def credentials_from_payload(body: dict) -> dict:
    """兼容凭据放在 data 或 result。dws 1.0.59 实测放在 result。"""
    if not isinstance(body, dict):
        return {}
    for key in ("data", "result"):
        section = body.get(key)
        if not isinstance(section, dict):
            continue
        if section.get("appKey") or section.get("appSecret"):
            return section
        nested = section.get("data")
        if isinstance(nested, dict) and (nested.get("appKey") or nested.get("appSecret")):
            return nested
    return {}


def send_entry_card(*, target: str = "group", user_id: str | None = None,
                    out_track_id: str | None = None, runner=None,
                    poster=None, group_cid: str = GROUP_CID,
                    entry_url: str = ENTRY_URL, ledger_url: str = LEDGER_URL) -> dict:
    """投放一张入口卡；返回 ``{sent, detail, outTrackId?}``（不抛异常给调用方）。"""
    post = poster or _default_poster
    run = runner or dws_cli.run_dws
    rc, out, err = run(["devapp", "+credentials-get",
                        "--unified-app-id", UNIFIED_APP_ID, "-f", "json"])
    if rc != 0:
        return {"sent": False, "detail": f"credentials-get rc={rc}: {err.strip()[:200]}"}
    try:
        data = credentials_from_payload(json.loads(out))
    except (TypeError, ValueError):
        return {"sent": False, "detail": "credentials-get 输出无法解析"}
    if not data.get("appKey") or not data.get("appSecret"):
        return {"sent": False, "detail": "credentials 缺少 appKey/appSecret"}

    token_resp = post(_ACCESS_URL,
                      {"appKey": data["appKey"], "appSecret": data["appSecret"]},
                      {"Content-Type": "application/json"})
    token = (token_resp or {}).get("accessToken")
    if not token:
        return {"sent": False, "detail": "accessToken 获取失败"}

    payload = build_payload(target=target, user_id=user_id, out_track_id=out_track_id,
                            group_cid=group_cid, entry_url=entry_url, ledger_url=ledger_url)
    resp = post(_DELIVER_URL, payload,
                {"Content-Type": "application/json",
                 "x-acs-dingtalk-access-token": token})
    ok = bool((resp or {}).get("success"))
    return {"sent": ok, "outTrackId": payload["outTrackId"],
            "detail": json.dumps(resp, ensure_ascii=False)[:300]}


def deliver_entry_card(ledger, task: dict, *, runner=None, poster=None,
                       dry_run: bool = False, settings=None) -> dict:
    """按周期幂等地随发入口卡（供升级链调用；失败不阻断文字触达）。"""
    due = (task or {}).get("due_at") or ""
    if due and ledger.get_config_param("entry_card_last_due") == due:
        return {"sent": False, "reason": "already-sent-cycle", "due": due}
    if dry_run:
        return {"sent": False, "reason": "dry-run", "due": due}
    card_kwargs = {}
    if settings is not None and settings.group_cid is not None:
        if not settings.group_cid or not settings.entry_url:
            return {"sent": False, "reason": "card-not-configured", "due": due}
        card_kwargs = {
            "group_cid": settings.group_cid,
            "entry_url": settings.entry_url,
            "ledger_url": settings.ledger_url or settings.entry_url,
        }
    try:
        result = send_entry_card(target="group", runner=runner, poster=poster, **card_kwargs)
    except Exception as exc:  # noqa: BLE001 — 卡片失败不得阻断升级链
        result = {"sent": False, "detail": f"error: {exc}"}
    if result.get("sent") and due:
        ledger.set_config_param("entry_card_last_due", due, updated_by="escalation")
    return {"sent": bool(result.get("sent")), "detail": result.get("detail"),
            "due": due}
