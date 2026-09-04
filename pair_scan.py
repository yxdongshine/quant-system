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
import threading
import time
import subprocess
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from config import DATA_DIR, tx_symbol

import datafeed

warnings.filterwarnings("ignore")

PAIR_SCAN_FILE = DATA_DIR / "pair_scan.json"

# API 不可用时自动跳过网络请求（线程安全）
_api_down = threading.Event()

# 仅保留未破天数 >= 此值的对子（排除当日新出现的对子，要求收盘形成且支撑有效）
MIN_UNBROKEN_DAYS = 3
# 对子数出现时间不要超过此天数（保持新鲜度）
MAX_UNBROKEN_DAYS = 5

_EM_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
_EM_REFERER = "https://data.eastmoney.com/"

# 沪深主板 + 创业板（不含科创板/北交所/三板）
# m:0+t:6 沪市主板, m:0+t:80 创业板, m:1+t:2 深市主板
EM_ALL_STOCKS_BASE = (
    "https://push2delay.eastmoney.com/api/qt/clist/get"
    "?fs=m:0+t:6,m:0+t:80,m:1+t:2"
    "&fields=f2,f3,f4,f5,f6,f7,f8,f12,f14,f15,f16,f17,f18"
    "&po=1&fid=f3&np=1"
    "&ut=bd1d9ddb04089700d2b3f41e3c84c2d4&_="
)

def _probe_api() -> bool:
    """快速检测数据 API 是否可达（5 秒超时，单次尝试）。
    用于扫描开始前判断是否需要网络请求，避免 API 被限流时全线程卡在重试。
    返回 True 仅当 API 返回可解析的 JSON 数据（排除 WAF 拦截页等）。
    """
    test_url = "https://ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh600519,day,2024-01-01,,640,qfq"
    try:
        r = subprocess.run(
            ["curl", "-sS", "--noproxy", "*", "-m", "5", test_url],
            capture_output=True, text=True, encoding="utf-8", timeout=8,
        )
        if r.returncode == 0 and r.stdout.strip():
            # 验证返回的是有效 JSON，而非 WAF 拦截页（HTML）
            data = json.loads(r.stdout)
            return isinstance(data, dict) and "data" in data
    except (json.JSONDecodeError, Exception):
        pass
    return False


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
    """获取股票近 N 个交易日日线数据（优先本地 CSV，缺失时调用外部 API）。
    
    当 API 被限流/不可用时，_api_down 事件被设置，后续请求自动跳过网络调用，
    仅使用本地 CSV 缓存，避免全市场扫描卡在超时重试上。
    """
    cache_csv = DATA_DIR / f"{code}.csv"
    df = pd.DataFrame()
    if cache_csv.exists():
        try:
            df = pd.read_csv(cache_csv, index_col=0, parse_dates=True)
            if not df.empty and len(df) >= days:
                df = df.sort_index().tail(days)
                for col in ["open", "close", "high", "low", "volume"]:
                    if col not in df.columns:
                        df[col] = float("nan")
                return df
        except Exception:
            df = pd.DataFrame()
    # 本地缺失或不足，尝试外部 API
    if _api_down.is_set():
        return pd.DataFrame()
    try:
        df = datafeed.get_daily(code, refresh=True)
        if df.empty or len(df) < days:
            return pd.DataFrame()
        df = df.sort_index().tail(days)
        for col in ["open", "close", "high", "low", "volume"]:
            if col not in df.columns:
                df[col] = float("nan")
        return df
    except Exception:
        _api_down.set()  # 标记 API 不可用，后续跳过网络请求
        return pd.DataFrame()


def load_pair_cache() -> dict[str, dict]:
    """加载持久化对子缓存（用于跨交易日跟踪对子底未破天数）。"""
    if PAIR_CACHE_FILE.exists():
        try:
            with open(PAIR_CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_pair_cache(cache: dict[str, dict]) -> None:
    """保存对子缓存。"""
    PAIR_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(PAIR_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


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
        data = _curl_em(url) or {}
        d = data.get("data") or {}
        total = d.get("total", 0)
        rows = d.get("diff", [])
        all_rows.extend(rows)
        print(f"[pair_scan] {label} 第{page}页: {len(rows)} 只 (total={total})")
        if not rows or len(all_rows) >= total:
            break
        page += 1
    return all_rows


# 科创板/北交所/三板代码前缀
_EXCLUDED_PREFIXES = ("68", "69", "8", "4", "43", "83", "87", "88")


def _is_excluded_market(code: str) -> bool:
    """判断是否属于科创板、北交所或三板。"""
    return code.startswith(_EXCLUDED_PREFIXES)


def _fetch_all_stocks() -> list[dict]:
    """获取沪深主板+创业板实时行情（排除科创板/北交所/三板）。"""
    all_stocks = []
    seen_codes: set[str] = set()

    def _add_unique(rows: list[dict]) -> None:
        for r in rows:
            code = r.get("f12", "")
            if not code:
                continue
            if code in seen_codes:
                continue
            # 二次过滤：排除科创板/北交所/三板
            if _is_excluded_market(code):
                continue
            seen_codes.add(code)
            all_stocks.append(r)

    try:
        rows = _fetch_with_pages(EM_ALL_STOCKS_BASE, "沪深主板+创业板")
        _add_unique(rows)
    except Exception as e:
        print(f"[pair_scan] 沪深主板+创业板获取失败: {e}")

    print(f"[pair_scan] 去重后总股票数: {len(all_stocks)}")
    return all_stocks


STRONG_LEVELS = {"AAA", "AB", "AA+"}


def _compute_score(stock: dict) -> int:
    """对子底综合评分（分数越高 = 对子越强 + 支撑越久 + 成交越活跃）。

    评分维度：
      - 对子强度: AAA=100, AA+=70, AB=40
      - 未破天数: 每天 +3 分
      - 成交额:   每 1 亿 +5 分，上限 50 分
      - 涨幅空间: 现价较对子价涨幅 5%~30% 加 20 分（说明有上涨但仍有空间）
    """
    level_scores = {"AAA": 100, "AA+": 70, "AB": 40}
    score = level_scores.get(stock.get("pair_level"), 0)

    # 未破天数
    score += (stock.get("unbroken_days") or 0) * 3

    # 成交额（亿元）
    amount = (stock.get("amount") or 0) / 1e8
    score += min(amount * 5, 50)

    # 涨幅空间
    pair_price = stock.get("pair_price") or 0
    price = stock.get("price") or 0
    if pair_price and pair_price > 0:
        gain_pct = (price - pair_price) / pair_price * 100
        if 5 <= gain_pct <= 30:
            score += 20
        elif 30 < gain_pct <= 50:
            score += 10
        elif gain_pct > 50:
            score += 0
        else:
            score += 5

    return int(score)

def _is_falling_from_local_high(df: pd.DataFrame, price: float, today_high: float | None = None) -> bool:
    """排除近3天出现局部高点且未突破、处于下跌中的股票。

    逻辑：在最近5个K线日内（含今天），如果局部高点出现在今天之前，
    且现价未突破该高点，则视为短期见顶下跌，予以排除。
    """
    if df.empty or len(df) < 4:
        return False
    prev = df.tail(4)  # 今天之前的最近4天
    max_high = prev["high"].max()
    # 如果今天的高点更高或持平（距离3%以内），说明今天突破了近期高点，不排除
    if today_high is not None and today_high >= max_high * 0.97:
        return False
    # 局部高点出现在prev中，且现价未突破（距离3%以上）
    if price < max_high * 0.97:
        return True
    return False


def _scan_one_stock(args: tuple[int, dict, dict[str, dict]]) -> dict | None:
    """单只股票扫描：优先读本地 CSV 缓存找历史对子，无缓存则看当天最低价。"""
    idx, stock, stock_dict = args
    code = stock.get("f12", "")
    name = stock.get("f14", "")
    if not code or not name:
        return None
    if "ST" in name or "退" in name:
        return None

    pair_price = None
    pair_info = None
    pair_date = None
    unbroken_days = 0

    # 1) 优先尝试本地 CSV 缓存 / 外部 K线 API
    df = _fetch_kline(code, days=20)
    if not df.empty and len(df) >= 2:
        # 计算整个观察期内的实际最低低点
        actual_min = df["low"].min()
        # 从历史 K线（近→远）查找最近一个强对子底
        for row_idx in range(len(df) - 1, -1, -1):
            date = df.index[row_idx]
            low = df.iloc[row_idx]["low"]
            if pd.isna(low):
                continue
            pair = detect_pair(low)
            if pair["is_pair"] and pair["pair_level"] in STRONG_LEVELS:
                # 验证该对子价必须就是（或极接近）整个观察期的实际最低点
                # 允许 0.5% 的误差容忍，避免前复权小数点漂移导致漏掉
                if low > actual_min * 1.005:
                    continue  # 不是真实底部，继续往前找
                pair_date = date
                pair_price = low
                pair_info = pair
                break

        if pair_price is not None and pair_date is not None:
            # 验证是否被跌破（对子日之后不含当日）
            after_df = df.loc[pair_date:]
            after_excl = after_df.iloc[1:]
            if not after_excl.empty:
                broken = after_excl[after_excl["low"].round(2) < pair_price - 0.02]
                if not broken.empty:
                    return None  # 已被跌破
            unbroken_days = len(after_df)

    # 2) 无 K线数据 或 K线中未找到对子 → 检查当天最低价
    if pair_price is None:
        cur = stock_dict.get(code, {})
        low_today = _em_price(cur.get("f16"))
        if low_today is not None:
            pair = detect_pair(low_today)
            if pair["is_pair"] and pair["pair_level"] in STRONG_LEVELS:
                pair_price = low_today
                pair_info = pair
                pair_date = datetime.now().date()
                unbroken_days = 1

    if pair_price is None or pair_info is None:
        return None

    if unbroken_days < MIN_UNBROKEN_DAYS:
        return None  # 太新，暂不输出（但会在缓存中跟踪）
    if unbroken_days > MAX_UNBROKEN_DAYS:
        return None  # 对子出现时间太久，不输出

    # 3) 补充实时行情字段
    cur = stock_dict.get(code, {})
    close = _em_price(cur.get("f2"))
    change_pct = _em_price(cur.get("f3"))
    turnover = _em_price(cur.get("f8"))
    amount = _em_price(cur.get("f6"))
    high = _em_price(cur.get("f15"))
    low_today = _em_price(cur.get("f16"))

    if close is None or close < 1 or close > 5000:
        return None

    # 排除近3天出现局部高点且未突破的股票
    if not df.empty and len(df) >= 4:
        if _is_falling_from_local_high(df, close, today_high=high):
            return None

    return {
        "code": code,
        "name": name,
        "price": close,
        "low": low_today,
        "high": high,
        "change_pct": change_pct,
        "turnover": turnover,
        "amount": amount,
        "pair_price": pair_price,
        "pair_level": pair_info["pair_level"],
        "pair_type": pair_info["pair_type"],
        "pair_digit": pair_info["pair_digit"],
        "pair_desc": pair_info["pair_desc"],
        "unbroken_days": unbroken_days,
        "first_pair_date": str(pair_date.date()) if hasattr(pair_date, "date") else str(pair_date),
        "is_unbroken": True,
    }


def scan_pair_numbers() -> dict:
    """全市场扫描对子底（本地 CSV 缓存 + 实时行情双源，解决历史对子遗漏）。

    核心改进：
      - 优先读取本地 CSV 缓存（data/{code}.csv）扫描历史 K线中的对子底
      - 无缓存的股票回退到当天实时最低价检测
      - 通过 pair_cache.json 持久化缓存跨交易日跟踪对子底
    """
    print("[pair_scan] 开始全市场对子数扫描（CSV缓存+实时行情双源模式）...")
    t0 = time.time()

    stocks = _fetch_all_stocks()
    if not stocks:
        print("[pair_scan] 未获取到股票数据")
        return _empty_result()

    stock_dict = {s.get("f12"): s for s in stocks if s.get("f12")}
    total = len(stocks)
    print(f"[pair_scan] 共 {total} 只股票，开始并发扫描（8线程）...")

    strong_pairs: list[dict] = []
    done_n = 0

    def _on_done(fut):
        nonlocal done_n
        done_n += 1
        if done_n % 500 == 0:
            print(f"[pair_scan] 扫描进度: {done_n}/{total}")
        try:
            res = fut.result()
            if res is not None:
                strong_pairs.append(res)
        except Exception as e:
            print(f"[pair_scan] 单只股票扫描异常: {e}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = []
        for i, s in enumerate(stocks):
            f = pool.submit(_scan_one_stock, (i, s, stock_dict))
            f.add_done_callback(_on_done)
            futures.append(f)
        for f in futures:
            f.result()

    # 计算分数并排序：分数降序 → 未破天数降序 → 成交额降序
    for sp in strong_pairs:
        sp["score"] = _compute_score(sp)

    strong_pairs.sort(key=lambda x: (
        -(x.get("score") or 0),
        -(x.get("unbroken_days") or 0),
        -(x.get("amount") or 0),
    ))

    elapsed = time.time() - t0
    result = {
        "scan_time": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "total_stocks": len(stocks),
        "pair_bottom_count": 0,
        "pair_top_count": 0,
        "strong_pair_count": len(strong_pairs),
        "elapsed_sec": round(elapsed, 1),
        "pair_bottoms": [],
        "pair_tops": [],
        "strong_pairs": strong_pairs[:80],
    }

    save_pair_result(result)
    print(f"[pair_scan] 扫描完成，耗时 {elapsed:.1f}s")
    print(f"[pair_scan] 未破{MIN_UNBROKEN_DAYS}~{MAX_UNBROKEN_DAYS}天强支撑对子底: {len(strong_pairs)} 只")
    return result


# ── MA 极度粘合+多头上拐扫描 ──

def _check_ma_alignment(df: pd.DataFrame) -> dict | None:
    """检查股票是否满足 MA 粘合+多头上拐+周线调整充分条件。

    条件：
      1. 收盘价站上 5/10/20/60 日线
      2. 4 条均线极度粘合（最高与最低均线间距 < 3% 收盘价）
      3. 均线整体拐头向上：60日线必须上升，且 5/10/20 中至少 2 条上升
      4. 周线调整充分：近20日波动率适中(>=5%)，且近5日未创10日新低
    """
    if len(df) < 65:
        return None
    # 计算均线
    df = df.copy()
    df["sma5"] = df["close"].rolling(5).mean()
    df["sma10"] = df["close"].rolling(10).mean()
    df["sma20"] = df["close"].rolling(20).mean()
    df["sma60"] = df["close"].rolling(60).mean()
    # 取最近两天数据
    cur = df.iloc[-1]
    prev = df.iloc[-2]
    if pd.isna(cur["sma60"]) or pd.isna(prev["sma60"]):
        return None
    close = cur["close"]
    smas = [cur["sma5"], cur["sma10"], cur["sma20"], cur["sma60"]]
    # 1) 站上全部均线
    if not all(close > ma for ma in smas):
        return None
    # 2) 极度粘合（放宽到 3%，包容更多正在粘合标的）
    spread = max(smas) - min(smas)
    if spread / close > 0.03:
        return None
    # 3) 均线整体拐头向上：60日线必须升，5/10/20 中至少 2 条升
    rising = [
        cur["sma5"] > prev["sma5"],
        cur["sma10"] > prev["sma10"],
        cur["sma20"] > prev["sma20"],
    ]
    if not (cur["sma60"] > prev["sma60"] and sum(rising) >= 2):
        return None
    # 4) 周线调整充分：近20日振幅 >=5%，且近5日未创10日新低
    recent20 = df.tail(20)
    if recent20["high"].max() / recent20["low"].min() - 1 < 0.05:
        return None
    recent10_low = df.tail(10)["low"].min()
    if df.tail(5)["low"].min() <= recent10_low:
        return None
    return {
        "sma5": round(cur["sma5"], 2),
        "sma10": round(cur["sma10"], 2),
        "sma20": round(cur["sma20"], 2),
        "sma60": round(cur["sma60"], 2),
        "spread": round(spread, 3),
        "spread_pct": round(spread / close * 100, 2),
    }


def _scan_one_ma_stock(args: tuple[int, dict, dict[str, dict]]) -> dict | None:
    """单只股票 MA 扫描。"""
    idx, stock, stock_dict = args
    code = stock.get("f12", "")
    name = stock.get("f14", "")
    if not code or not name:
        return None
    if "ST" in name or "退" in name:
        return None
    df = _fetch_kline(code, days=65)
    if df.empty or len(df) < 65:
        return None
    ma_info = _check_ma_alignment(df)
    if ma_info is None:
        return None
    cur = stock_dict.get(code, {})
    close = _em_price(cur.get("f2"))
    change_pct = _em_price(cur.get("f3"))
    amount = _em_price(cur.get("f6"))
    if close is None or close < 1 or close > 5000:
        return None
    return {
        "code": code,
        "name": name,
        "price": close,
        "change_pct": change_pct,
        "amount": amount,
        **ma_info,
    }


def scan_ma_stocks() -> dict:
    """全市场扫描 MA 极度粘合+多头上拐股票。"""
    _api_down.clear()
    if not _probe_api():
        _api_down.set()
        print("[ma_scan] ⚠ 数据 API 不可用，仅使用本地缓存")
    print("[ma_scan] 开始全市场 MA 扫描...")
    t0 = time.time()
    stocks = _fetch_all_stocks()
    stock_dict = {s["f12"]: s for s in stocks}
    results = []
    done_n = 0

    def _on_done(fut):
        nonlocal done_n
        done_n += 1
        if done_n % 500 == 0:
            print(f"[ma_scan] 扫描进度: {done_n}/{len(stocks)}")
        try:
            res = fut.result()
            if res:
                results.append(res)
        except Exception as e:
            print(f"[ma_scan] 单只股票扫描异常: {e}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = []
        for idx, stock in enumerate(stocks):
            fut = pool.submit(_scan_one_ma_stock, (idx, stock, stock_dict))
            fut.add_done_callback(_on_done)
            futures.append(fut)
        for fut in futures:
            fut.result()

    elapsed = time.time() - t0
    result = {
        "scan_time": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "total_stocks": len(stocks),
        "count": len(results),
        "elapsed_sec": round(elapsed, 1),
        "stocks": results,
    }
    save_ma_result(result)
    print(f"[ma_scan] 扫描完成，耗时 {elapsed:.1f}s，符合条件的股票: {len(results)} 只")
    return result


MA_SCAN_FILE = DATA_DIR / "ma_scan.json"


def save_ma_result(data: dict) -> None:
    MA_SCAN_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(MA_SCAN_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_ma_result() -> dict:
    if MA_SCAN_FILE.exists():
        try:
            with open(MA_SCAN_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"scan_time": "", "total_stocks": 0, "count": 0, "elapsed_sec": 0, "stocks": []}


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


# ---------------- 指南针模式（空中加油/高位整理蓄势） ----------------

COMPASS_SCAN_FILE = DATA_DIR / "compass_scan.json"


def _check_compass_pattern(df: pd.DataFrame) -> dict | None:
    """指南针模式（空中加油/高位整理蓄势）检测。

    基于 300803 指南针 K 线特征抽象出的模型（已适当放宽）：
      1. 收盘价 > sma60 且 sma60 上升（长期上升趋势）
      2. 近20日振幅 >= 6%（有充分震荡/洗盘）
      3. 收盘价距20日高点 < 5%（强势，位于区间上半部）
      4. 近5日中至少1日出现长影线（上下影线合计 > 实体 * 1.5）
      5. 近5日最低 >= 近10日最低（未创短期新低）
      6. 近5日收盘价变异系数 < 4%（窄幅整理）
    """
    if len(df) < 65:
        return None
    df = df.copy()
    df["sma5"] = df["close"].rolling(5).mean()
    df["sma10"] = df["close"].rolling(10).mean()
    df["sma20"] = df["close"].rolling(20).mean()
    df["sma60"] = df["close"].rolling(60).mean()

    cur = df.iloc[-1]
    prev = df.iloc[-2]
    if pd.isna(cur["sma60"]) or pd.isna(prev["sma60"]):
        return None
    close = cur["close"]

    # 1) 收盘价 > sma60 且 sma60 上升
    if not (close > cur["sma60"] and cur["sma60"] > prev["sma60"]):
        return None

    # 2) 近20日振幅 >= 6%
    recent20 = df.tail(20)
    amplitude = recent20["high"].max() / recent20["low"].min() - 1
    if amplitude < 0.06:
        return None

    # 3) 收盘价距20日高点 < 5%
    if close / recent20["high"].max() < 0.95:
        return None

    # 4) 近5日中至少1日出现长影线
    recent5 = df.tail(5).copy()
    recent5["body"] = (recent5["close"] - recent5["open"]).abs()
    recent5["upper_shadow"] = recent5["high"] - recent5[["open", "close"]].max(axis=1)
    recent5["lower_shadow"] = recent5[["open", "close"]].min(axis=1) - recent5["low"]
    recent5["shadow"] = recent5["upper_shadow"] + recent5["lower_shadow"]
    recent5["is_long_shadow"] = recent5["shadow"] > recent5["body"] * 1.5
    if recent5["is_long_shadow"].sum() < 1:
        return None

    # 5) 近5日最低 >= 近10日最低
    if df.tail(5)["low"].min() < df.tail(10)["low"].min() - 1e-9:
        return None

    # 6) 近5日收盘价变异系数 < 4%
    recent5_close = df.tail(5)["close"]
    cv = recent5_close.std() / recent5_close.mean()
    if cv >= 0.04:
        return None

    return {
        "sma5": round(cur["sma5"], 2),
        "sma10": round(cur["sma10"], 2),
        "sma20": round(cur["sma20"], 2),
        "sma60": round(cur["sma60"], 2),
        "amplitude_20d": round(amplitude * 100, 2),
        "cv_5d": round(cv * 100, 2),
        "long_shadow_days": int(recent5["is_long_shadow"].sum()),
    }


def _scan_one_compass_stock(args: tuple[int, dict, dict[str, dict]]) -> dict | None:
    """单只股票指南针模式扫描。"""
    idx, stock, stock_dict = args
    code = stock.get("f12", "")
    name = stock.get("f14", "")
    if not code or not name:
        return None
    if "ST" in name or "退" in name:
        return None
    df = _fetch_kline(code, days=65)
    if df.empty or len(df) < 65:
        return None
    pattern_info = _check_compass_pattern(df)
    if pattern_info is None:
        return None
    cur = stock_dict.get(code, {})
    close = _em_price(cur.get("f2"))
    change_pct = _em_price(cur.get("f3"))
    amount = _em_price(cur.get("f6"))
    if close is None or close < 1 or close > 5000:
        return None
    return {
        "code": code,
        "name": name,
        "price": close,
        "change_pct": change_pct,
        "amount": amount,
        **pattern_info,
    }


def scan_compass_stocks() -> dict:
    """全市场扫描指南针模式（空中加油/高位整理蓄势）股票。"""
    _api_down.clear()
    if not _probe_api():
        _api_down.set()
        print("[compass_scan] ⚠ 数据 API 不可用，仅使用本地缓存")
    print("[compass_scan] 开始全市场指南针模式扫描...")
    t0 = time.time()
    stocks = _fetch_all_stocks()
    stock_dict = {s["f12"]: s for s in stocks}
    results = []
    done_n = 0

    def _on_done(fut):
        nonlocal done_n
        done_n += 1
        if done_n % 500 == 0:
            print(f"[compass_scan] 扫描进度: {done_n}/{len(stocks)}")
        try:
            res = fut.result()
            if res:
                results.append(res)
        except Exception as e:
            print(f"[compass_scan] 单只股票扫描异常: {e}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = []
        for idx, stock in enumerate(stocks):
            fut = pool.submit(_scan_one_compass_stock, (idx, stock, stock_dict))
            fut.add_done_callback(_on_done)
            futures.append(fut)
        for fut in futures:
            fut.result()

    elapsed = time.time() - t0
    result = {
        "scan_time": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "total_stocks": len(stocks),
        "count": len(results),
        "elapsed_sec": round(elapsed, 1),
        "stocks": results,
    }
    save_compass_result(result)
    print(f"[compass_scan] 扫描完成，耗时 {elapsed:.1f}s，符合条件的股票: {len(results)} 只")
    return result


def save_compass_result(data: dict) -> None:
    COMPASS_SCAN_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(COMPASS_SCAN_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_compass_result() -> dict:
    if COMPASS_SCAN_FILE.exists():
        try:
            with open(COMPASS_SCAN_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"scan_time": "", "total_stocks": 0, "count": 0, "elapsed_sec": 0, "stocks": []}


# ---------------- 信达模式（突破加速/量价齐升） ----------------

XINDA_SCAN_FILE = DATA_DIR / "xinda_scan.json"


def _check_xinda_pattern(df: pd.DataFrame) -> dict | None:
    """信达地产模式（突破加速/量价齐升）检测。

    基于 600657 信达地产 K 线特征抽象出的模型：
      1. 收盘价 > sma5 > sma10 > sma20（多头排列）
      2. sma5、sma10、sma20 均上升（短期/中期趋势向上）
      3. 近5日累计涨幅 >= 10%（短期强势）
      4. 近5日中至少4天收阳（买盘强劲）
      5. 收盘价距20日高点 < 8%（位于区间上沿，未大幅回落）
      6. 近5日均量 >= 近20日均量的 1.5 倍（放量上涨）
      7. 20日振幅 >= 18%（有充分波动，非死水）
    """
    if len(df) < 25:
        return None
    df = df.copy()
    df["sma5"] = df["close"].rolling(5).mean()
    df["sma10"] = df["close"].rolling(10).mean()
    df["sma20"] = df["close"].rolling(20).mean()

    cur = df.iloc[-1]
    prev = df.iloc[-2]
    if pd.isna(cur["sma5"]) or pd.isna(cur["sma10"]) or pd.isna(cur["sma20"]):
        return None
    close = cur["close"]

    # 1) 收盘价 > sma5 > sma10 > sma20
    if not (close > cur["sma5"] > cur["sma10"] > cur["sma20"]):
        return None

    # 2) sma5、sma10、sma20 均上升
    if not (cur["sma5"] > prev["sma5"] and cur["sma10"] > prev["sma10"] and cur["sma20"] > prev["sma20"]):
        return None

    recent20 = df.tail(20)
    recent5 = df.tail(5)

    # 3) 近5日累计涨幅 >= 10%
    gain_5d = close / recent5["close"].iloc[0] - 1
    if gain_5d < 0.10:
        return None

    # 4) 近5日中至少4天收阳
    up_days = (recent5["close"] > recent5["open"]).sum()
    if up_days < 4:
        return None

    # 5) 收盘价距20日高点 < 8%
    if close / recent20["high"].max() < 0.92:
        return None

    # 6) 近5日均量 >= 近20日均量的 1.5 倍
    vol_5d = recent5["volume"].mean()
    vol_20d = recent20["volume"].mean()
    if vol_20d <= 0 or vol_5d / vol_20d < 1.5:
        return None

    # 7) 20日振幅 >= 18%
    amplitude = recent20["high"].max() / recent20["low"].min() - 1
    if amplitude < 0.18:
        return None

    return {
        "sma5": round(cur["sma5"], 2),
        "sma10": round(cur["sma10"], 2),
        "sma20": round(cur["sma20"], 2),
        "gain_5d": round(gain_5d * 100, 2),
        "up_days": int(up_days),
        "amplitude_20d": round(amplitude * 100, 2),
        "vol_ratio": round(vol_5d / vol_20d, 2),
    }


def _scan_one_xinda_stock(args: tuple[int, dict, dict[str, dict]]) -> dict | None:
    """单只股票信达模式扫描。"""
    idx, stock, stock_dict = args
    code = stock.get("f12", "")
    name = stock.get("f14", "")
    if not code or not name:
        return None
    if "ST" in name or "退" in name:
        return None
    df = _fetch_kline(code, days=25)
    if df.empty or len(df) < 25:
        return None
    pattern_info = _check_xinda_pattern(df)
    if pattern_info is None:
        return None
    cur = stock_dict.get(code, {})
    close = _em_price(cur.get("f2"))
    change_pct = _em_price(cur.get("f3"))
    amount = _em_price(cur.get("f6"))
    if close is None or close < 1 or close > 5000:
        return None
    return {
        "code": code,
        "name": name,
        "price": close,
        "change_pct": change_pct,
        "amount": amount,
        **pattern_info,
    }


def scan_xinda_stocks() -> dict:
    """全市场扫描信达模式（突破加速/量价齐升）股票。"""
    _api_down.clear()
    if not _probe_api():
        _api_down.set()
        print("[xinda_scan] ⚠ 数据 API 不可用，仅使用本地缓存")
    print("[xinda_scan] 开始全市场信达模式扫描...")
    t0 = time.time()
    stocks = _fetch_all_stocks()
    stock_dict = {s["f12"]: s for s in stocks}
    results = []
    done_n = 0

    def _on_done(fut):
        nonlocal done_n
        done_n += 1
        if done_n % 500 == 0:
            print(f"[xinda_scan] 扫描进度: {done_n}/{len(stocks)}")
        try:
            res = fut.result()
            if res:
                results.append(res)
        except Exception as e:
            print(f"[xinda_scan] 单只股票扫描异常: {e}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = []
        for idx, stock in enumerate(stocks):
            fut = pool.submit(_scan_one_xinda_stock, (idx, stock, stock_dict))
            fut.add_done_callback(_on_done)
            futures.append(fut)
        for fut in futures:
            fut.result()

    elapsed = time.time() - t0
    result = {
        "scan_time": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "total_stocks": len(stocks),
        "count": len(results),
        "elapsed_sec": round(elapsed, 1),
        "stocks": results,
    }
    save_xinda_result(result)
    print(f"[xinda_scan] 扫描完成，耗时 {elapsed:.1f}s，符合条件的股票: {len(results)} 只")
    return result


def save_xinda_result(data: dict) -> None:
    XINDA_SCAN_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(XINDA_SCAN_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_xinda_result() -> dict:
    if XINDA_SCAN_FILE.exists():
        try:
            with open(XINDA_SCAN_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"scan_time": "", "total_stocks": 0, "count": 0, "elapsed_sec": 0, "stocks": []}


if __name__ == "__main__":
    result = scan_pair_numbers()
    print(f"\n{'='*60}")
    print(f"  ★ 强支撑对子（未破{MIN_UNBROKEN_DAYS}~{MAX_UNBROKEN_DAYS}天）TOP 20")
    print(f"{'='*60}")
    for i, s in enumerate(result.get("strong_pairs", [])[:20]):
        ud = s.get("unbroken_days", 0)
        fd = s.get("first_pair_date", "")
        print(f"  {i+1:2d}. {s['code']} {s['name']:6s} 支撑:{s['pair_price']:8.2f} "
              f"未破{ud:2d}天({fd:10s}) 现价:{s['price']:8.2f} 涨跌幅:{s['change_pct']:+.2f}% 成交额:{(s.get('amount') or 0)/1e8:.2f}亿")
