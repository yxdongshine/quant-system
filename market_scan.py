# -*- coding: utf-8 -*-
"""全市场扫描：从A股今日收盘数据中按趋势确认模型打分，取前 N 名。

策略：
1. 用 curl 调东财 API 获取全A股今日行情（避免 Python requests TLS 风控）
2. 预筛：涨幅>0、成交额>1亿、非ST/新股/退市
3. 逐只拉取日线数据，运行 compute_frame + current_signal 计算分数
4. 按分数降序取 top_n
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import warnings

import pandas as pd

from config import load_params
from datafeed import get_daily
from signals import compute_frame, current_signal

warnings.filterwarnings("ignore")

TOP_N = 5
MIN_AMOUNT = 1e8
MIN_CHANGE_PCT = 0.0
MIN_HISTORY_DAYS = 120
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def curl_json(url: str, retries: int = 3, timeout: int = 20) -> dict:
    last_err = None
    for i in range(retries):
        try:
            r = subprocess.run(
                ["curl", "-sS", "--noproxy", "*", "-m", str(timeout),
                 "-H", f"User-Agent: {UA}", url],
                capture_output=True, text=True, encoding="utf-8", timeout=timeout + 5,
            )
            if r.returncode == 0 and r.stdout.strip():
                return json.loads(r.stdout)
            last_err = f"curl rc={r.returncode} stderr={r.stderr[:200]}"
        except Exception as e:
            last_err = str(e)
        time.sleep(1.5 * (i + 1))
    raise ConnectionError(f"fetch failed: {url[:80]}... ({last_err})")


def fetch_market_snapshot() -> list[dict]:
    """用 curl 调东财 API 获取全A股今日行情（分页拉取，按涨幅降序）。"""
    print("  获取全市场行情快照(curl)...", end=" ", flush=True)
    all_items = []
    for page in range(1, 60):
        url = (
            "https://push2.eastmoney.com/api/qt/clist/get"
            f"?pn={page}&pz=100&po=1&np=1&fltt=2&invt=2&fid=f3"
            "&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
            "&fields=f2,f3,f4,f5,f6,f7,f12,f14"
        )
        try:
            d = curl_json(url)
            items = (d.get("data") or {}).get("diff") or []
            if not items:
                break
            all_items.extend(items)
        except Exception:
            break
        time.sleep(0.5)
    print(f"OK ({len(all_items)} 只)")
    return all_items


def prescreen(items: list[dict]) -> list[dict]:
    out = []
    for r in items:
        code = str(r.get("f12", ""))
        name = str(r.get("f14", ""))
        change_pct = r.get("f3", 0) or 0
        amount = r.get("f6", 0) or 0

        if len(code) != 6:
            continue
        if "ST" in name or "退" in name:
            continue
        if change_pct < MIN_CHANGE_PCT:
            continue
        if amount < MIN_AMOUNT:
            continue
        if code.startswith(("8", "9")):
            continue

        out.append({"code": code, "name": name,
                     "change_pct": change_pct, "amount": amount})

    out.sort(key=lambda x: x["change_pct"], reverse=True)
    return out


def score_stock(code: str, name: str, params: dict) -> dict | None:
    try:
        df = get_daily(code, refresh=True)
        if len(df) < MIN_HISTORY_DAYS:
            return None
        frame = compute_frame(df, params)
        sig = current_signal(frame, params)
        sig["code"] = code
        sig["name"] = name
        return sig
    except Exception:
        return None


def main():
    print("=== 全市场趋势确认模型扫描 ===")
    params = load_params()

    items = fetch_market_snapshot()
    candidates = prescreen(items)
    print(f"  预筛后候选: {len(candidates)} 只（涨幅>0, 成交额>1亿, 非ST）")

    pool = candidates[:150]
    print(f"  取涨幅前 {len(pool)} 只做详细打分...")

    scored = []
    for i, c in enumerate(pool):
        code, name = c["code"], c["name"]
        sig = score_stock(code, name, params)
        if sig:
            scored.append(sig)
        if (i + 1) % 20 == 0:
            print(f"    已处理 {i+1}/{len(pool)}...")
        time.sleep(0.3)

    scored.sort(key=lambda x: x.get("score", 0), reverse=True)
    top = scored[:TOP_N]

    print(f"\n{'='*60}")
    print(f"  今日趋势确认模型 TOP {TOP_N}（按分数降序）")
    print(f"{'='*60}")
    for i, s in enumerate(top):
        print(f"\n  #{i+1}  {s['code']} {s['name']}")
        print(f"       信号: {s['action']}  分数: {s.get('score', 0)}")
        close_s = f"{s['close']:.2f}" if isinstance(s.get('close'), (int, float)) else str(s.get('close','-'))
        print(f"       收盘: {close_s}  仓位: {s.get('state_text', '—')}")
        print(f"       依据: {s.get('reason', '')}")

    print(f"\n{'='*60}")
    print("完整排行（前20）：")
    print(f"{'排名':>4} | {'分数':>4} | {'代码':>6} | {'名称':>8} | {'信号':>4} | {'收盘':>8} | 依据")
    print("-" * 90)
    for i, s in enumerate(scored[:20]):
        act = s.get("action", "?")
        close_s = f"{s['close']:.2f}" if isinstance(s.get('close'), (int, float)) else str(s.get('close','-'))
        print(f"{i+1:>4} | {s.get('score',0):>4} | {s['code']:>6} | {s['name']:>8} | {act:>4} | {close_s:>8} | {s.get('reason', '')}")

    print(f"\n推荐添加到自选的代码：")
    print(", ".join(f"{s['code']}({s['name']})" for s in top))


if __name__ == "__main__":
    main()
