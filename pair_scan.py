# -*- coding: utf-8 -*-
"""对子数扫描器：全市场扫描对子底/对子顶。

对子数：股价中出现重复数字的模式，如 155.55、177.88、255.55，
是主力精准控盘的特征信号。

对子底：当日最低价出现对子数 → 主力在底部精准控价
对子顶：当日最高价出现对子数 → 主力在顶部精准出货

数据源：东方财富 push2delay API（全市场实时行情）
"""
from __future__ import annotations

import json
import time
import subprocess
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from config import DATA_DIR, tx_symbol

warnings.filterwarnings("ignore")

PAIR_SCAN_FILE = DATA_DIR / "pair_scan.json"

_EM_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
_EM_REFERER = "https://data.eastmoney.com/"

# 全市场 A 股列表 API（沪深主板 + 创业板 + 科创板）
# 全市场 A 股列表 API 基础 URL（沪深主板 + 创业板 + 科创板）
EM_ALL_STOCKS_BASE = (
    "https://push2delay.eastmoney.com/api/qt/clist/get"
    "?fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
    "&fields=f2,f3,f4,f5,f6,f7,f8,f12,f14,f15,f16,f17,f18"
    "&po=1&fid=f3&np=1"
    "&ut=bd1d9ddb04089700d2b3f41e3c84c2d4&_="
)

# 北京交易所（北交所）
EM_BSE_STOCKS_BASE = (
    "https://push2delay.eastmoney.com/api/qt/clist/get"
    "?fs=m:0+t:81"
    "&fields=f2,f3,f4,f5,f6,f7,f8,f12,f14,f15,f16,f17,f18"
    "&po=1&fid=f3&np=1"
    "&ut=bd1d9ddb04089700d2b3f41e3c84c2d4&_="
)

def _curl_em(url: str, retries: int = 3, timeout: int = 20) -> dict:
    """用 curl 拉取东方财富数据 API（带 Referer 防封）。"""
    last_err = None
    for i in range(retries):
        try:
            r = subprocess.run(
                ["curl", "-sS", "--noproxy", "*", "-m", str(timeout),
                 "-H", f"User-Agent: {_EM_UA}",
                 "-H", f"Referer: {_EM_REFERER}", url],
                capture_output=True, text=True, encoding="utf-8", timeout=timeout + 5,
            )
            if r.returncode == 0 and r.stdout.strip():
                return json.loads(r.stdout)
            last_err = f"curl rc={r.returncode} stderr={r.stderr[:200]}"
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
        time.sleep(1.5 * (i + 1))
    raise ConnectionError(f"fetch failed: {url[:80]}... ({last_err})")


def _sf(v, default=None) -> float | None:
    """安全浮点转换。"""
    try:
        val = float(v)
        if val != val:  # NaN
            return default
        return val
    except (ValueError, TypeError):
        return default


def _em_price(v, default=None) -> float | None:
    """东方财富 push2 价格解析：原始值 / 100（API 以 0.01 为单位）。"""
    val = _sf(v)
    if val is None:
        return default
    return round(val / 100, 3)


def detect_pair(price: float | None) -> dict:
    """检测价格是否为对子数（主力精准控盘特征信号）。

    对子数类型（按强度排序）：
      AAA（abb.bb 全对）: 整数末位 = 小数末两位
        如 155.55, 255.55, 83.33, 5.55
      AB  （ab.ab 镜像对）: 整数最后两位 = 小数两位
        如 15.15, 23.23, 56.56
      AA+ （abb.cc 双对+）: 整数末两位重复 + 小数末两位重复（不同数字）
        如 177.88, 22.55, 33.99
      AA  （a.bb 尾对）: 仅小数末两位相同
        如 12.33, 5.88, 8.44

    排除:
      - .00 结尾（整除价，非主力控盘信号）

    返回:
        {is_pair, pair_level, pair_type, pair_digit, pair_desc}
    """
    empty = {"is_pair": False, "pair_level": "", "pair_type": "", "pair_digit": "", "pair_desc": ""}
    if price is None or price <= 0:
        return empty

    price_str = f"{price:.2f}"
    parts = price_str.split(".")
    int_part = parts[0]
    dec_part = parts[1] if len(parts) > 1 else ""

    if len(dec_part) < 2:
        return empty

    # 排除 .00（整除价）
    if dec_part == "00":
        return empty

    dec_last1 = dec_part[-1]
    dec_last2 = dec_part[-2]
    dec_is_pair = dec_last1 == dec_last2      # 小数末两位相同

    # 整数部分信息
    int_last1 = int_part[-1] if int_part else ""
    int_last2 = int_part[-2:] if len(int_part) >= 2 else int_part.zfill(2)[-2:]
    int_is_pair = len(int_part) >= 2 and int_last2[0] == int_last2[1]  # 整数末两位相同

    # 1) AAA (abb.bb): 整数末位 = 小数末两位（如 155.55）
    if dec_is_pair and int_last1 == dec_last1:
        return {
            "is_pair": True, "pair_level": "AAA", "pair_type": "abb.bb",
            "pair_digit": dec_last1,
            "pair_desc": f"{price_str}=全对{dec_last1}",
        }

    # 2) AB (ab.ab): 整数最后两位 = 小数两位（如 15.15）
    if not dec_is_pair and len(int_part) >= 2 and int_last2 == dec_part:
        return {
            "is_pair": True, "pair_level": "AB", "pair_type": "ab.ab",
            "pair_digit": dec_part,
            "pair_desc": f"{price_str}=镜像{dec_part}",
        }

    # 3) AA+ (abb.cc / aa.bb): 整数末两位重复 + 小数末两位重复（不同数字）
    #    如 177.88 (77+88), 22.55 (22+55)
    if int_is_pair and dec_is_pair and int_last2[0] != dec_last1:
        return {
            "is_pair": True, "pair_level": "AA+", "pair_type": "abb.cc",
            "pair_digit": dec_last1,
            "pair_desc": f"{price_str}=双对{int_last2[0]}{dec_last1}",
        }

    # 4) AA (a.bb): 仅小数末两位相同（如 12.33）
    if dec_is_pair:
        return {
            "is_pair": True, "pair_level": "AA", "pair_type": "a.bb",
            "pair_digit": dec_last1,
            "pair_desc": f"{price_str}=尾对{dec_last1}",
        }

    return empty


def _fetch_kline(code: str, days: int = 30) -> pd.DataFrame:
    """获取股票近 N 个交易日日线数据（腾讯前复权，用于对子数历史验证）。

    使用腾讯接口（ifzq.gtimg.cn），避免 push2his.eastmoney.com 在服务器被封锁。
    """
    sym = tx_symbol(code)
    start = (datetime.now() - timedelta(days=int(days * 1.8))).strftime("%Y-%m-%d")
    url = (f"https://ifzq.gtimg.cn/appstock/app/fqkline/get"
           f"?param={sym},day,{start},,{days},qfq")
    try:
        data = _curl_em(url, retries=1, timeout=10)
        node = (data.get("data") or {}).get(sym) or {}
        rows = node.get("qfqday") or node.get("day") or []
        if not rows:
            return pd.DataFrame()
        # 腾讯列序: date, open, close, high, low, volume [, ...]
        df = pd.DataFrame([r[:6] for r in rows],
                          columns=["date", "open", "close", "high", "low", "volume"])
        df["date"] = pd.to_datetime(df["date"])
        df = df.astype({c: float for c in ["open", "close", "high", "low", "volume"]})
        return df.set_index("date").sort_index()
    except Exception:
        return pd.DataFrame()


def _check_unbroken_pair(code: str, pair_price: float, lookback: int = 30) -> dict:
    """检查对子价位是否未被跌破（主力强支撑信号）。

    对子底出现后，后续交易日最低价均未低于对子价 → 支撑有效。

    返回: {is_unbroken, days_held, first_pair_date}
    """
    df = _fetch_kline(code, lookback)
    if df.empty or len(df) < 2:
        # K线数据获取失败 → 无法验证，视为新出现（不因数据缺失拒绝）
        return {"is_unbroken": True, "days_held": 1, "first_pair_date": "今日"}

    # 查找对子价出现的最近一天（±0.02 容差，应对数据精度差异）
    pair_rows = df[(df["low"].round(2) - pair_price).abs() < 0.02]
    if pair_rows.empty:
        # 对子价仅今天出现，历史数据中未见 → 视为新出现
        return {"is_unbroken": True, "days_held": 1, "first_pair_date": "今日"}

    first_date = pair_rows.index[-1]  # 最近出现的对子日

    # 检查之后是否有跌破（不含当日自身）
    after = df.loc[first_date:].iloc[1:]
    if after.empty:
        return {"is_unbroken": True, "days_held": 1, "first_pair_date": str(first_date.date())}

    broken = after[after["low"].round(2) < pair_price - 0.02]
    if not broken.empty:
        return {"is_unbroken": False, "days_held": 0, "first_pair_date": ""}

    days_held = len(after) + 1
    return {"is_unbroken": True, "days_held": days_held, "first_pair_date": str(first_date.date())}


def _fetch_with_pages(base_url: str, label: str, page_size: int = 5000) -> list[dict]:
    """分页拉取东方财富行情数据，确保获取全部股票。"""
    all_rows: list[dict] = []
    page = 1
    while True:
        url = f"{base_url}{int(time.time()*1000)}&pn={page}&pz={page_size}"
        data = _curl_em(url)
        d = data.get("data", {})
        total = d.get("total", 0)
        rows = d.get("diff", [])
        all_rows.extend(rows)
        print(f"[pair_scan] {label} 第{page}页: {len(rows)} 只 (total={total})")
        if not rows or len(all_rows) >= total:
            break
        page += 1
    return all_rows


def _fetch_all_stocks() -> list[dict]:
    """获取全市场 A 股（含北交所）实时行情。"""
    all_stocks = []
    seen_codes: set[str] = set()

    def _add_unique(rows: list[dict]) -> None:
        for r in rows:
            code = r.get("f12", "")
            if code and code not in seen_codes:
                seen_codes.add(code)
                all_stocks.append(r)

    # 主板 + 创业板 + 科创板
    try:
        rows = _fetch_with_pages(EM_ALL_STOCKS_BASE, "沪深创科")
        _add_unique(rows)
    except Exception as e:
        print(f"[pair_scan] 沪深创科获取失败: {e}")

    # 北交所
    try:
        rows = _fetch_with_pages(EM_BSE_STOCKS_BASE, "北交所")
        _add_unique(rows)
    except Exception as e:
        print(f"[pair_scan] 北交所获取失败: {e}")

    print(f"[pair_scan] 去重后总股票数: {len(all_stocks)}")
    return all_stocks


def scan_pair_numbers() -> dict:
    """全市场扫描对子底和对子顶。

    返回:
        {
            scan_time: str,
            total_stocks: int,
            pair_bottoms: [...],
            pair_tops: [...],
        }
    """
    print("[pair_scan] 开始全市场对子数扫描...")
    t0 = time.time()

    stocks = _fetch_all_stocks()
    if not stocks:
        print("[pair_scan] 未获取到股票数据")
        return _empty_result()

    pair_bottoms = []
    pair_tops = []

    for row in stocks:
        code = row.get("f12", "")
        name = row.get("f14", "")
        if not code or not name:
            continue

        # f2=最新价, f3=涨跌幅, f5=成交量(手), f6=成交额, f8=换手率
        # f15=最高, f16=最低, f17=今开, f18=昨收
        low = _em_price(row.get("f16"))
        high = _em_price(row.get("f15"))
        close = _em_price(row.get("f2"))
        change_pct = _em_price(row.get("f3"))
        turnover = _em_price(row.get("f8"))  # 换手率
        amount = _em_price(row.get("f6"))  # 成交额（元）

        if low is None or high is None or close is None:
            continue

        # 排除 ST、退市股
        if "ST" in name or "退" in name:
            continue

        # 排除新上市股（价格可能异常）
        if close < 1 or close > 5000:
            continue

        # 检查最低价是否为对子数 → 对子底
        low_pair = detect_pair(low)
        if low_pair["is_pair"]:
            pair_bottoms.append({
                "code": code,
                "name": name,
                "price": close,
                "low": low,
                "high": high,
                "change_pct": change_pct,
                "turnover": turnover,
                "amount": amount,
                "pair_price": low,
                "pair_level": low_pair["pair_level"],
                "pair_type": low_pair["pair_type"],
                "pair_digit": low_pair["pair_digit"],
                "pair_desc": low_pair["pair_desc"],
            })

        # 检查最高价是否为对子数 → 对子顶
        high_pair = detect_pair(high)
        if high_pair["is_pair"]:
            pair_tops.append({
                "code": code,
                "name": name,
                "price": close,
                "low": low,
                "high": high,
                "change_pct": change_pct,
                "turnover": turnover,
                "amount": amount,
                "pair_price": high,
                "pair_level": high_pair["pair_level"],
                "pair_type": high_pair["pair_type"],
                "pair_digit": high_pair["pair_digit"],
                "pair_desc": high_pair["pair_desc"],
            })

    # 只保留强对子（AAA 全对 + AB 镜像 + AA+ 双对），弱对子(AA)不需要
    STRONG_LEVELS = {"AAA", "AB", "AA+"}
    pair_bottoms = [s for s in pair_bottoms if s["pair_level"] in STRONG_LEVELS]
    pair_tops = [s for s in pair_tops if s["pair_level"] in STRONG_LEVELS]

    # 排序：全对(AAA) > 双对(AA+) > 镜像(AB)，同级别按成交额降序
    level_order = {"AAA": 0, "AA+": 1, "AB": 2}
    pair_bottoms.sort(key=lambda x: (
        level_order.get(x["pair_level"], 9),
        -(x.get("amount") or 0),
    ))
    pair_tops.sort(key=lambda x: (
        level_order.get(x["pair_level"], 9),
        -(x.get("amount") or 0),
    ))

    # 历史验证：检查强对子底是否未被跌破（核心信号，并发加速）
    strong_pairs = []
    n_bottoms = len(pair_bottoms)
    print(f"[pair_scan] 开始历史验证 {n_bottoms} 只强对子底（并发8线程）...")

    def _verify_one(args):
        idx, stock = args
        chk = _check_unbroken_pair(stock["code"], stock["pair_price"])
        return idx, chk

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(_verify_one, (i, s)) for i, s in enumerate(pair_bottoms)]
        done_n = 0
        for fut in as_completed(futures):
            idx, chk = fut.result()
            stock = pair_bottoms[idx]
            stock["unbroken_days"] = chk["days_held"]
            stock["first_pair_date"] = chk["first_pair_date"]
            stock["is_unbroken"] = chk["is_unbroken"]
            if chk["is_unbroken"]:
                strong_pairs.append(dict(stock))
            done_n += 1
            if done_n % 50 == 0:
                print(f"[pair_scan] 历史验证进度: {done_n}/{n_bottoms}")

    # 强支撑对子按未破天数降序 → 成交额降序
    strong_pairs.sort(key=lambda x: (
        -(x.get("unbroken_days") or 0),
        -(x.get("amount") or 0),
    ))

    elapsed = time.time() - t0
    result = {
        "scan_time": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "total_stocks": len(stocks),
        "pair_bottom_count": len(pair_bottoms),
        "pair_top_count": len(pair_tops),
        "strong_pair_count": len(strong_pairs),
        "elapsed_sec": round(elapsed, 1),
        "pair_bottoms": pair_bottoms[:80],
        "pair_tops": pair_tops[:80],
        "strong_pairs": strong_pairs[:80],
    }

    save_pair_result(result)
    print(f"[pair_scan] 扫描完成，耗时 {elapsed:.1f}s")
    print(f"[pair_scan] 强对子底: {len(pair_bottoms)} 只, 强对子顶: {len(pair_tops)} 只, 未破支撑: {len(strong_pairs)} 只")
    return result


def _empty_result() -> dict:
    return {
        "scan_time": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "total_stocks": 0,
        "pair_bottom_count": 0,
        "pair_top_count": 0,
        "strong_pair_count": 0,
        "elapsed_sec": 0,
        "pair_bottoms": [],
        "pair_tops": [],
        "strong_pairs": [],
    }


def load_pair_result() -> dict:
    """加载上次扫描结果。"""
    if PAIR_SCAN_FILE.exists():
        try:
            with open(PAIR_SCAN_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return _empty_result()


def save_pair_result(data: dict) -> None:
    """保存扫描结果。"""
    PAIR_SCAN_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(PAIR_SCAN_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    result = scan_pair_numbers()
    print(f"\n{'='*60}")
    print(f"  ★ 强支撑对子（未跌破）TOP 20")
    print(f"{'='*60}")
    for i, s in enumerate(result.get("strong_pairs", [])[:20]):
        ud = s.get("unbroken_days", 0)
        fd = s.get("first_pair_date", "")
        print(f"  {i+1:2d}. {s['code']} {s['name']:6s} 支撑:{s['pair_price']:8.2f} "
              f"未破{ud:2d}天({fd:10s}) 现价:{s['price']:8.2f} 涨跌幅:{s['change_pct']:+.2f}% 成交额:{(s.get('amount') or 0)/1e8:.2f}亿")

    print(f"\n{'='*60}")
    print(f"  对子底 TOP 15")
    print(f"{'='*60}")
    for i, s in enumerate(result["pair_bottoms"][:15]):
        print(f"  {i+1:2d}. {s['code']} {s['name']:6s} 低:{s['pair_price']:8.2f} "
              f"{s['pair_desc']:15s} 现价:{s['price']:8.2f} 涨跌幅:{s['change_pct']:+.2f}% 成交额:{(s.get('amount') or 0)/1e8:.2f}亿")

    print(f"\n{'='*60}")
    print(f"  对子顶 TOP 15")
    print(f"{'='*60}")
    for i, s in enumerate(result["pair_tops"][:15]):
        print(f"  {i+1:2d}. {s['code']} {s['name']:6s} 高:{s['pair_price']:8.2f} "
              f"{s['pair_desc']:15s} 现价:{s['price']:8.2f} 涨跌幅:{s['change_pct']:+.2f}% 成交额:{(s.get('amount') or 0)/1e8:.2f}亿")
