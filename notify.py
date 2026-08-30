# -*- coding: utf-8 -*-
"""企业微信群机器人推送（官方 Webhook，零封号风险）。

配置方法：
    1. 手机安装"企业微信"App（个人免费注册）；
    2. 随便建一个群 -> 群设置 -> 群机器人 -> 添加机器人 -> 复制 Webhook 地址；
    3. 把地址整行写入本目录 webhook.txt（文件不存在则本模块静默跳过推送）。
"""
from __future__ import annotations

import json
import urllib.request
from datetime import date

import pandas as pd

from config import LOG_DIR, load_webhook

WEB_URL = "http://124.223.111.59:8787"
LABEL = {"SELL": "卖出", "LOCK": "锁利", "BUY": "买入", "PIVOT": "补涨", "HOLD": "持有", "WAIT": "等待"}
COLOR = {"SELL": "warning", "LOCK": "warning", "BUY": "info", "PIVOT": "info", "HOLD": "info", "WAIT": "comment"}
ICON  = {"SELL": "🔴", "LOCK": "🟠", "BUY": "🔵", "PIVOT": "🟡", "HOLD": "🟢", "WAIT": "⚪"}


def _markdown(rows: list[dict]) -> str:
    ts = pd.Timestamp.now().strftime("%m-%d %H:%M")
    lines = [f"## 持仓纪律扫描 {ts}", "**明日开盘执行清单：**", ""]
    for r in rows:
        act = r.get("action", "ERROR")
        if act == "ERROR":
            lines.append(f"> ⚠️ {r.get('code')} {r.get('name')} 数据异常")
            continue
        close = r.get("close")
        close_s = f"{close:.2f}" if isinstance(close, float) else "-"
        st_t = r.get("state_text", "")
        reason = (r.get("reason") or "").replace(";", "；")
        if len(reason) > 42:
            reason = reason[:42] + "…"
        sc = r.get("score", 0)
        st_str = f"｜{st_t}" if st_t else ""
        lines.append(
            f"> {ICON.get(act, '⚪')} <font color=\"{COLOR.get(act, 'comment')}\">"
            f"**{LABEL.get(act, act)}**</font> {r['code']} {r['name']} {close_s}｜{sc}分{st_str}\n"
            f"> <font color=\"comment\">{reason}</font>"
        )
    lines += [
        "",
        "<font color=\"comment\">SELL清仓 / LOCK锁利提示 / PIVOT板块补涨 / HOLD持有 / WAIT等待 · "
        "基于收盘K线</font>",
        f"[打开网页版]({WEB_URL})",
    ]
    return "\n".join(lines)


def push_daily(rows: list[dict]) -> None:
    webhook = load_webhook()
    if not webhook.startswith("http"):
        print("[notify] 未配置 webhook.txt，跳过推送")
        return
    flag = LOG_DIR / "pushed.txt"
    today = date.today().isoformat()
    if flag.exists() and flag.read_text(encoding="utf-8").strip() == today:
        print("[notify] 今日已推送过，跳过（双 cron 去重）")
        return

    content = _markdown(rows).encode("utf-8")
    if len(content) > 4000:
        content = content[:3980]
    text = content.decode("utf-8", errors="ignore") + ("…" if len(content) >= 3980 else "")
    payload = json.dumps({"msgtype": "markdown", "markdown": {"content": text}})
    req = urllib.request.Request(
        webhook, data=payload.encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    if body.get("errcode") != 0:
        raise RuntimeError(f"wecom rejected: {body}")

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    flag.write_text(today, encoding="utf-8")
    print("[notify] 已推送到企业微信群")


if __name__ == "__main__":
    demo = [{"code": "300750", "name": "宁德时代", "action": "SELL",
             "close": 387.89, "reason": "收盘387.89跌破20日线390.12且均线下行",
             "state_text": "空仓"},
            {"code": "300308", "name": "中际旭创", "action": "HOLD",
             "close": 126.30, "reason": "持仓中,20日线上行趋势完好",
             "state_text": "持仓"}]
    print(_markdown(demo))
    push_daily(demo)
