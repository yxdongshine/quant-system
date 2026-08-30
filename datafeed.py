# -*- coding: utf-8 -*-
"""A股日线数据层。

设计要点：
1. 主源=腾讯 fqkline（稳定、对脚本宽容），备源=东财 kline；
   两者均通过 subprocess 调用系统 curl 拉取 —— 本机实测 Python
   requests 的 TLS 指纹会被风控间歇性 RST，curl(Schannel) 可用。
2. 前复权数据在除权后整条历史会被重算，因此不做增量 append，
   每次全量刷新整段历史重写 CSV，避免复权口径混杂。
3. 腾讯单次最多返回 640 根，翻页策略：以已取得的最早日期为
   新的 end 往回翻，直到覆盖 history_start。
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from datetime import time as dtime
from pathlib import Path
from urllib.parse import quote

import pandas as pd

from config import DATA_DIR, tx_symbol, load_params

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def _curl_json(url: str, retries: int = 3, timeout: int = 20) -> dict:
    """用系统 curl 拉取 JSON，带重试退避。"""
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
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
        time.sleep(1.5 * (i + 1))
    raise ConnectionError(f"fetch failed: {url[:80]}... ({last_err})")


# ---------------- 腾讯主源 ----------------

def _tx_page(sym: str, start: str, end: str) -> pd.DataFrame:
    url = (f"https://ifzq.gtimg.cn/appstock/app/fqkline/get"
           f"?param={sym},day,{start},{end},640,qfq")
    d = _curl_json(url)
    node = d["data"][sym]
    rows = node.get("qfqday") or node.get("day") or []
    if not rows:
        return pd.DataFrame()
    # 腾讯列序: date, open, close, high, low, volume [, ...]
    df = pd.DataFrame([r[:6] for r in rows],
                      columns=["date", "open", "close", "high", "low", "volume"])
    df["date"] = pd.to_datetime(df["date"])
    df = df.astype({c: float for c in ["open", "close", "high", "low", "volume"]})
    return df.set_index("date").sort_index()


def fetch_tencent(code: str, start: str) -> pd.DataFrame:
    """翻页拉取腾讯前复权日线，覆盖 [start, 最新]。

    腾讯接口实测口径（2026-08-25）：
    - 只要显式指定 end（哪怕传明天），响应就**不含"今天"的 K 线**；
      传 end=今天 会导致永远丢当天数据（页面停滞在前一交易日）；
    - end 留空才返回当日根（盘中=实时未收盘根，收盘后=当日收盘根），
      且当日根不计入 count 上限、start 只做下限过滤。
    因此首页 end 留空；翻页回补历史用显式 prev_end（历史日期无此问题）。
    """
    sym = tx_symbol(code)
    frames = [_tx_page(sym, start, "")]
    for _ in range(30):  # 最多翻 30 页（约 19 年）
        earliest = frames[-1].index.min()
        if earliest <= pd.Timestamp(start):
            break
        prev_end = (earliest - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        page = _tx_page(sym, "1990-01-01", prev_end)
        if page.empty:
            break
        frames.append(page)
        time.sleep(0.6)  # 温和限速
    df = pd.concat(frames)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df.loc[pd.Timestamp(start):]


# ---------------- 东财备源 ----------------

def fetch_eastmoney(code: str, start: str, end: str) -> pd.DataFrame:
    secid = ("1." if code.startswith(("6", "5", "9")) else "0.") + code
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get"
           f"?secid={secid}&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56"
           f"&klt=101&fqt=1&beg={start.replace('-', '')}&end={end.replace('-', '')}&lmt=1000000")
    d = _curl_json(url)
    klines = (d.get("data") or {}).get("klines") or []
    if not klines:
        return pd.DataFrame()
    # 东财列序: date, open, close, high, low, volume
    df = pd.DataFrame([x.split(",")[:6] for x in klines],
                      columns=["date", "open", "close", "high", "low", "volume"])
    df["date"] = pd.to_datetime(df["date"])
    df = df.astype({c: float for c in ["open", "close", "high", "low", "volume"]})
    return df.set_index("date").sort_index()


# ---------------- 对外接口：带本地缓存 ----------------

def get_daily(code: str, refresh: bool = True) -> pd.DataFrame:
    """获取日线（前复权）。refresh=True 时全量拉取并重写缓存。

    两个关键口径（2026-08-25 修复）：
    腾讯 fqkline 显式指定 end 就不返回当日 K 线（传"明天"也不行），
      因此主源首页 end 留空以拿到当日根；
    2. 盘中（15:05 收盘前）拉取时丢弃当天未走完的半根 K 线，避免用
       实时价算出误导性信号（网页白天新增自选股同样受此保护）。
    """
    params = load_params()
    start = params["history_start"]
    end = (pd.Timestamp.today() + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    cache = DATA_DIR / f"{code}.csv"

    if not refresh and cache.exists():
        return pd.read_csv(cache, index_col=0, parse_dates=True)

    try:
        df = fetch_tencent(code, start)
        if df.empty:
            raise ValueError("tencent returned empty")
    except Exception:  # noqa: BLE001
        df = fetch_eastmoney(code, start, end)

    if df.empty:
        raise RuntimeError(f"no data for {code}")
    now = pd.Timestamp.now()
    if df.index.max().date() == now.date() and now.time() < dtime(15, 5):
        df = df.iloc[:-1]  # 丢弃今天的盘中半根 K 线
        if df.empty:
            raise RuntimeError(f"no completed bars for {code}")
    df.to_csv(cache, encoding="utf-8")
    return df


# ---------------- 股票搜索（腾讯 smartbox，支持中文名/代码/拼音） ----------------

def search_stock(q: str) -> list[dict]:
    """模糊搜索 A 股（支持中文/代码/拼音），返回 [{code,name,market}]。

    smartbox 响应格式（纯 ASCII，中文以 \\u 转义）：
      v_hint="market~code~名称~拼音~类型^market~code~...^..."
    类型以 GP-A 开头且市场为 sh/sz 的才是沪深 A 股。
    """
    q = q.strip()
    if not q:
        return []
    url = f"https://smartbox.gtimg.cn/s3/?v=2&q={quote(q)}&t=all"
    r = subprocess.run(
        ["curl", "-sS", "--noproxy", "*", "-m", "15",
         "-H", f"User-Agent: {UA}", url],
        capture_output=True, timeout=20,
    )
    txt = r.stdout.decode("utf-8", errors="ignore")
    if "=" not in txt:
        return []
    try:
        # 等号右侧是一个 JSON 字符串，json.loads 顺带完成 \u 中文反转义
        blob = json.loads(txt.split("=", 1)[1].strip().rstrip(";"))
    except json.JSONDecodeError:
        return []
    out: list[dict] = []
    for it in blob.split("^"):
        p = it.split("~")
        if (len(p) >= 5 and p[0] in ("sh", "sz")
                and p[4].startswith("GP-A")
                and re.fullmatch(r"\d{6}", p[1])):
            out.append({"code": p[1], "name": p[2], "market": p[0]})
    return out[:10]


# ---------------- 实时行情（盘中快照，仅页面展示用） ----------------

def get_quotes(codes: list[str]) -> dict[str, dict]:
    """批量拉取腾讯实时行情 qt.gtimg.cn，返回 {code: 行情dict}。

    仅用于网页展示最新价/涨跌幅；信号计算始终基于已收盘 K 线——
    两者刻意分离，避免盘中价格触发误导性信号。
    响应为 GBK 编码：v_sz300750="51~名称~代码~现价~昨收~今开~...~时间~涨跌~涨跌%"；
    涨跌幅自行按 现价/昨收 计算，不依赖字段位序。
    """
    if not codes:
        return {}
    url = "https://qt.gtimg.cn/q=" + ",".join(tx_symbol(c) for c in codes)
    r = subprocess.run(
        ["curl", "-sS", "--noproxy", "*", "-m", "10",
         "-H", f"User-Agent: {UA}", url],
        capture_output=True, timeout=15,
    )
    out: dict[str, dict] = {}
    for line in r.stdout.decode("gbk", errors="ignore").splitlines():
        if "=" not in line:
            continue
        left, right = line.split("=", 1)
        code = left.strip().split("_")[-1][-6:]      # v_sz300750 -> 300750
        p = right.strip().strip(";").strip('"').split("~")
        if len(p) < 5:
            continue
        try:
            price, prev = float(p[3]), float(p[4])
        except ValueError:
            continue
        if price <= 0 or prev <= 0:                   # 停牌/异常快照
            continue
        t = p[30] if len(p) > 30 and len(p[30]) == 14 and p[30].isdigit() else ""
        out[code] = {
            "price": price,
            "prev_close": prev,
            "change": round(price - prev, 2),
            "change_pct": round((price / prev - 1) * 100, 2),
            "time_str": f"{t[8:10]}:{t[10:12]}:{t[12:14]}" if t else "",
        }
    return out


if __name__ == "__main__":
    from config import WATCHLIST, ensure_dirs

    ensure_dirs()
    for code, name in WATCHLIST.items():
        df = get_daily(code)
        print(f"{code} {name}: {len(df)} bars, "
              f"{df.index.min().date()} ~ {df.index.max().date()}, "
              f"last_close={df['close'].iloc[-1]:.2f}")
