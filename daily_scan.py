# -*- coding: utf-8 -*-
"""每日收盘扫描：刷新数据 -> 计算信号 -> 板块联动 -> 控制台输出 + 落盘报告。

用法：
  python daily_scan.py            # 联网刷新数据后扫描
  python daily_scan.py --offline  # 断网时用本地缓存扫描

输出：reports/signal_YYYY-MM-DD.md（按分数排序，含板块联动加分）
"""
from __future__ import annotations

import sys

import pandas as pd

from config import REPORT_DIR, SECTORS, ensure_dirs, load_params, load_watchlist
from datafeed import get_daily
from signals import SEVERITY, compute_frame, current_signal, sector_boost

LABEL = {"SELL": "卖出", "LOCK": "锁利", "BUY": "买入", "PIVOT": "补涨", "HOLD": "持有", "WAIT": "等待"}


def scan(refresh: bool = True) -> list[dict]:
    ensure_dirs()
    p = load_params()
    rows: list[dict] = []

    for code, name in load_watchlist().items():
        try:
            try:
                df = get_daily(code, refresh=refresh)
            except Exception:
                df = get_daily(code, refresh=False)
            sig = current_signal(compute_frame(df, p), p)
            sig["code"], sig["name"] = code, name
            rows.append(sig)
        except Exception as e:
            rows.append({"code": code, "name": name, "action": "ERROR",
                         "reason": f"数据不可用: {str(e)[:100]}", "date": "-",
                         "close": "-", "st_dir": 0, "ma_fast": None,
                         "ma_slow": None, "chandelier": None, "atr_pct": None,
                         "state": -1, "state_text": "未知", "score": 0})

    # 板块联动加分
    rows = sector_boost(rows, SECTORS)
    return rows


def render(rows: list[dict]) -> str:
    today = pd.Timestamp.today().strftime("%Y-%m-%d %H:%M")
    head = [
        f"# 持仓纪律扫描 · {today}",
        "",
        "> 趋势确认模型：**BUY** 站上20+60日线买入持仓；"
        "**SELL** 跌破20日线且均线下行清仓；**LOCK** 触吊灯线锁利提示；"
        "**PIVOT** 同板块≥50%已持仓→低位补涨；"
        "**WAIT** 空仓等待入场信号。均次日开盘执行。",
        "",
        "| 分数 | 代码 | 名称 | 板块 | 收盘价 | 仓位 | 信号 | 依据 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        act = r.get("action", "ERROR")
        tag = LABEL.get(act, "数据异常")
        sev = SEVERITY.get(act, 9)
        close = r.get("close", "-")
        close_s = f"{close:.2f}" if isinstance(close, float) else str(close)
        st_t = r.get("state_text", "—")
        sc = r.get("score", 0)
        sec = r.get("sector", "") or "—"
        head.append(f"| {sc} | {r['code']} | {r['name']} | {sec} | {close_s} "
                    f"| {st_t} | {tag} | {r.get('reason', '')} |")

    head += ["", "## 指标快照", ""]
    for r in rows:
        ma20 = r.get("ma_fast")
        ma60 = r.get("ma_slow")
        chan = r.get("chandelier")
        atrp = r.get("atr_pct")
        st = "多头" if r.get("st_dir") == 1 else ("空头" if r.get("st_dir") == -1 else "-")
        head.append(
            f"- **{r['code']} {r['name']}**（{r.get('date', '-')}）："
            f"ST={st}"
            + (f" | 20日线={ma20}" if ma20 else "")
            + (f" | 60日线={ma60}" if ma60 else "")
            + (f" | 吊灯线={chan}" if chan else "")
            + (f" | ATR占比={atrp}%" if atrp is not None else "")
        )
    return "\n".join(head) + "\n"


if __name__ == "__main__":
    refresh = "--offline" not in sys.argv
    rows = scan(refresh)
    text = render(rows)
    print(text)
    out = REPORT_DIR / f"signal_{pd.Timestamp.today():%Y-%m-%d}.md"
    out.write_text(text, encoding="utf-8")
    print(f"[saved] {out}")
    try:
        from notify import push_daily
        push_daily(rows)
    except Exception as e:
        print(f"[notify] 推送失败: {e}")
