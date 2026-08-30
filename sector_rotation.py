# -*- coding: utf-8 -*-
"""板块轮动模型：全市场冷门低估值龙头扫描。

目标：在全市场 A 股中，自动发现「冷门 + 低估值 + 龙头股」的板块组合，
为长线分板块持仓提供决策依据。

评分模型 = 4 维度综合打分：
  冷度评分(30%)  — 近期跌幅大、换手率低 → 逆向布局
  估值低度(35%)  — PE/PB 低、股息率高 → 安全边际
  龙头确定性(15%) — 市值大、ROE 高 → 确定性溢价
  技术安全(20%)  — MA120 支撑、量能萎缩 → 避免接飞刀

数据源：东方财富 push2delay API（延时15分钟，板块轮动周扫描足够）+ 本地 CSV 日线
运行频率：每周一次（板块冷度/估值变化缓慢）
"""
from __future__ import annotations

import json
import time
import warnings
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from config import DATA_DIR, SECTOR_ROTATION_FILE, load_params
from datafeed import get_daily
from signals import compute_frame, current_signal
from prediction import load_predictions, load_accuracy, predict_prices

warnings.filterwarnings("ignore")

# ── 专用 curl 函数（东方财富 push2 API 需要特殊头） ──
import subprocess

_EM_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
_EM_REFERER = "https://data.eastmoney.com/"


def _curl_em(url: str, retries: int = 3, timeout: int = 20) -> dict:
    """用 curl 拉取东方财富数据 API（带 Referer 防封）。"""
    last_err = None
    for i in range(retries):
        try:
            r = subprocess.run(
                ["curl", "-sS", "--noproxy", "*", "-m", str(timeout),
                 "-H", f"User-Agent: {_EM_UA}",
                 "-H", f"Referer: {_EM_REFERER}",
                 "-H", "Accept: */*",
                 url],
                capture_output=True, text=True, encoding="utf-8",
                timeout=timeout + 5,
            )
            if r.returncode == 0 and r.stdout.strip():
                data = json.loads(r.stdout)
                # 检查 API 层面的错误码
                if isinstance(data, dict) and data.get("rc") == 0:
                    return data
                elif isinstance(data, dict) and data.get("data") is not None:
                    return data
                last_err = f"API rc={data.get('rc')}, msg={data.get('dsc', '')[:100]}"
            else:
                last_err = f"curl rc={r.returncode} stderr={r.stderr[:200]}"
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
        time.sleep(2 * (i + 1))
    raise ConnectionError(f"eastmoney fetch failed: {url[:80]}... ({last_err})")


# ── 评分权重 ──
W_COLD = 0.30       # 冷度评分权重
W_VALUE = 0.35      # 估值低度权重
W_LEADER = 0.15     # 龙头确定性权重
W_TECH = 0.20       # 技术安全权重

# ── 筛选阈值 ──
COLD_THRESHOLD = 50       # 冷度 ≥ 50
VALUE_THRESHOLD = 50      # 估值低度 ≥ 50
TECH_THRESHOLD = 40       # 技术安全 ≥ 40
TOP_SECTORS = 10          # 输出 TOP 10 板块
LEADERS_PER_SECTOR = 1    # 每板块只推荐 1 只龙头

# ── 缓存文件 ──
SECTOR_LIST_CACHE = DATA_DIR / "sector_list.json"
SECTOR_CACHE_TTL = 86400  # 板块数据缓存 1 天（秒）

# ── 东方财富 push2delay API ──
# 注: push2.eastmoney.com 的 /api/qt/clist/get 在部分服务器 IP 被封，
#     改用 push2delay.eastmoney.com（延时15分钟数据，板块轮动周扫描足够）。
# 行业板块列表: m:90+t:2 = 行业板块
# 字段: f2=最新价, f3=涨跌幅, f4=涨跌额, f8=换手率, f9=PE(TTM), f20=总市值, f23=PB, f104=上涨家数, f105=下跌家数
EM_SECTOR_URL = (
    "https://push2delay.eastmoney.com/api/qt/clist/get"
    "?fs=m:90+t:2&fields=f2,f3,f4,f8,f9,f12,f14,f20,f23,f104,f105"
    "&pn=1&pz=100&po=1&fid=f3&np=1&ut=bd1d9ddb04089700d2b3f41e3c84c2d4&_=0"
)
# 板块成分股: b:BK{code} = 指定板块
# 字段: f2=最新价, f3=涨跌幅, f8=换手率, f9=PE, f12=代码, f14=名称, f20=总市值, f23=PB
EM_STOCK_URL_TMPL = (
    "https://push2delay.eastmoney.com/api/qt/clist/get"
    "?fs=b:{board_code}&fields=f2,f3,f8,f9,f12,f14,f20,f23"
    "&pn=1&pz=100&po=1&fid=f20&np=1&ut=bd1d9ddb04089700d2b3f41e3c84c2d4&_=0"
)


# ====================================================================
#  数据获取层 — 东方财富 push2 API
# ====================================================================

def fetch_sector_list() -> list[dict]:
    """获取全市场行业板块列表 + 汇总指标。

    返回 [{name, code, change_pct, pe, pb, turnover, total_mv, up_count, down_count}, ...]
    """
    cache = SECTOR_LIST_CACHE
    if cache.exists():
        age = time.time() - cache.stat().st_mtime
        if age < SECTOR_CACHE_TTL:
            try:
                cached = json.loads(cache.read_text(encoding="utf-8"))
                if cached:  # 只在缓存有数据时才使用
                    return cached
            except Exception:
                pass

    print("[sector_rotation] 拉取行业板块列表...")
    try:
        data = _curl_em(EM_SECTOR_URL)
    except Exception as e:
        print(f"[sector_rotation] 获取板块列表失败: {e}")
        return []

    if not data or "data" not in data or not data["data"]:
        print("[sector_rotation] 板块列表响应为空")
        return []
    items = data["data"].get("diff") or data["data"].get("list") or []
    sectors = []
    for item in items:
        try:
            code = str(item.get("f12", "")).strip()
            name = str(item.get("f14", "")).strip()
            if not code or not name:
                continue
            # 东方财富 push2 API: f2/f3/f4/f8/f9/f23 以 0.01 为单位，需 /100
            sec = {
                "name": name,
                "code": code,
                "change_pct": _em_val(item.get("f3")),       # 涨跌幅 %
                "turnover": _em_val(item.get("f8")),          # 换手率 %
                "pe": _em_val(item.get("f9")),                # PE(TTM)
                "pb": _em_val(item.get("f23")),               # PB
                "total_mv": _safe_float(item.get("f20")),     # 总市值(元)
                "up_count": _safe_int(item.get("f104")),
                "down_count": _safe_int(item.get("f105")),
            }
            # PE/PB 为负或0表示亏损/无效，置为 None
            if sec["pe"] is not None and sec["pe"] <= 0:
                sec["pe"] = None
            if sec["pb"] is not None and sec["pb"] <= 0:
                sec["pb"] = None
            sec["stock_count"] = sec["up_count"] + sec["down_count"]
            sectors.append(sec)
        except Exception:
            continue

    # 只在有数据时才缓存
    if sectors:
        try:
            cache.write_text(json.dumps(sectors, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    print(f"[sector_rotation] 获取到 {len(sectors)} 个行业板块")
    return sectors


def fetch_sector_stocks(sector_code: str) -> list[dict]:
    """获取板块成分股 + 市值/估值。

    返回 [{code, name, total_mv, change_pct, pe, pb, turnover}, ...]
    """
    board_code = f"BK{sector_code}" if not sector_code.startswith("BK") else sector_code
    url = EM_STOCK_URL_TMPL.format(board_code=board_code)

    try:
        data = _curl_em(url, timeout=15)
    except Exception as e:
        print(f"[sector_rotation] 获取板块 '{sector_code}' 成分股失败: {e}")
        return []

    if not data or "data" not in data or not data["data"]:
        return []

    items = data["data"].get("diff") or data["data"].get("list") or []
    stocks = []
    for item in items:
        try:
            code = str(item.get("f12", "")).strip()
            name = str(item.get("f14", "")).strip()
            if not code or not name:
                continue
            # 排除 ST
            if "ST" in name or "st" in name:
                continue
            # 东方财富 push2 API: f2/f3/f8/f9/f23 以 0.01 为单位，需 /100
            pe = _em_val(item.get("f9"))
            pb = _em_val(item.get("f23"))
            stocks.append({
                "code": code,
                "name": name,
                "total_mv": _safe_float(item.get("f20")),     # 总市值(元)
                "change_pct": _em_val(item.get("f3")),        # 涨跌幅 %
                "turnover": _em_val(item.get("f8")),          # 换手率 %
                "pe": pe if pe and pe > 0 else None,
                "pb": pb if pb and pb > 0 else None,
            })
        except Exception:
            continue

    # 按市值降序
    stocks.sort(key=lambda x: x.get("total_mv", 0) or 0, reverse=True)
    return stocks


# ====================================================================
#  评分计算层
# ====================================================================

def _rank_pct(values: list[float], reverse: bool = False) -> list[float]:
    """计算列表中每个值在整体中的百分位排名（0-100）。

    reverse=False: 值越大排名越高
    reverse=True: 值越小排名越高（即反转排名）
    """
    arr = np.array(values, dtype=float)
    nan_mask = np.isnan(arr)
    if nan_mask.all():
        return [50.0] * len(values)
    median = np.nanmedian(arr)
    arr[nan_mask] = median

    n = len(arr)
    if n <= 1:
        return [50.0] * len(values)

    order = np.argsort(arr)
    ranks = np.empty_like(order, dtype=float)
    ranks[order] = np.arange(1, n + 1, dtype=float)
    pct = (ranks - 1) / (n - 1) * 100

    if reverse:
        pct = 100 - pct
    return pct.tolist()


def calc_cold_score(sector: dict, all_sectors: list[dict]) -> float:
    """板块冷度评分 [0-100]。越冷分越高。

    权重: 涨跌幅排名 40% + 换手率排名 30% + 下跌家数占比 30%
    """
    changes = [s.get("change_pct") for s in all_sectors if s.get("change_pct") is not None]
    turnovers = [s.get("turnover") for s in all_sectors if s.get("turnover") is not None]

    if not changes:
        return 50.0

    # 找到当前板块在 all_sectors 中的位置
    my_idx = 0
    for i, s in enumerate(all_sectors):
        if s.get("name") == sector.get("name"):
            my_idx = i
            break

    # 涨跌幅排名：跌幅越大分越高 → reverse=True
    chg_rank = _rank_pct(changes, reverse=True)
    chg_score = chg_rank[my_idx] if my_idx < len(chg_rank) else 50.0

    # 换手率排名：换手率越低分越高 → reverse=True
    if turnovers:
        to_rank = _rank_pct(turnovers, reverse=True)
        to_score = to_rank[my_idx] if my_idx < len(to_rank) else 50.0
    else:
        to_score = 50.0

    # 下跌家数占比：下跌家数越多分越高
    my_up = sector.get("up_count", 0) or 0
    my_down = sector.get("down_count", 0) or 0
    my_total = my_up + my_down
    if my_total > 0:
        down_ratio = my_down / my_total
        down_score = down_ratio * 100
    else:
        down_score = 50.0

    cold = chg_score * 0.40 + to_score * 0.30 + down_score * 0.30
    return round(min(100, max(0, cold)), 1)


def calc_value_score(sector: dict, all_sectors: list[dict]) -> float:
    """估值低度评分 [0-100]。估值越低分越高。

    权重: PE分位 50% + PB分位 50%
    """
    pe_list = [s.get("pe") for s in all_sectors if s.get("pe") is not None and s.get("pe") > 0]
    pb_list = [s.get("pb") for s in all_sectors if s.get("pb") is not None and s.get("pb") > 0]

    my_pe = sector.get("pe")
    my_pb = sector.get("pb")

    # PE 分位：PE 越低分越高
    if pe_list and my_pe and my_pe > 0:
        pe_rank = (1 - (my_pe - min(pe_list)) / max(max(pe_list) - min(pe_list), 0.1)) * 100
        pe_score = max(0, min(100, pe_rank))
    else:
        pe_score = 50.0

    # PB 分位：PB 越低分越高
    if pb_list and my_pb and my_pb > 0:
        pb_rank = (1 - (my_pb - min(pb_list)) / max(max(pb_list) - min(pb_list), 0.1)) * 100
        pb_score = max(0, min(100, pb_rank))
    else:
        pb_score = 50.0

    value = pe_score * 0.50 + pb_score * 0.50
    return round(min(100, max(0, value)), 1)


def calc_leader_score(stock: dict, sector_stocks: list[dict]) -> float:
    """龙头确定性评分 [0-100]。

    权重: 市值排名 40% + PE低度 30% + 涨跌幅(抗跌) 30%
    """
    # 市值排名：市值越大分越高
    mv_list = [s.get("total_mv", 0) or 0 for s in sector_stocks if s.get("total_mv")]
    my_mv = stock.get("total_mv", 0) or 0

    if mv_list and my_mv > 0:
        mv_rank = (my_mv / max(mv_list)) * 100
        mv_score = max(0, min(100, mv_rank))
    else:
        mv_score = 50.0

    # PE 低度：PE 越低分越高（龙头也要便宜）
    my_pe = stock.get("pe")
    pe_list = [s.get("pe") for s in sector_stocks if s.get("pe") and s.get("pe") > 0]
    if pe_list and my_pe and my_pe > 0:
        pe_rank = (1 - (my_pe - min(pe_list)) / max(max(pe_list) - min(pe_list), 0.1)) * 100
        pe_score = max(0, min(100, pe_rank))
    else:
        pe_score = 50.0

    # 涨跌幅（抗跌）：跌幅越小分越高
    chg_list = [s.get("change_pct", 0) or 0 for s in sector_stocks]
    my_chg = stock.get("change_pct", 0) or 0
    if chg_list:
        chg_rank = _rank_pct(chg_list, reverse=False)  # 值越大排名越高（抗跌）
        # 找到当前 stock 的位置
        my_idx = 0
        for i, s in enumerate(sector_stocks):
            if s.get("code") == stock.get("code"):
                my_idx = i
                break
        chg_score = chg_rank[my_idx] if my_idx < len(chg_rank) else 50.0
    else:
        chg_score = 50.0

    leader = mv_score * 0.40 + pe_score * 0.30 + chg_score * 0.30
    return round(min(100, max(0, leader)), 1)


def calc_tech_score(code: str) -> float:
    """技术安全评分 [0-100]。基于本地 CSV 日线数据。

    权重: MA120支撑 40% + 超跌程度 30% + 量能萎缩 30%
    """
    csv_path = DATA_DIR / f"{code}.csv"
    if not csv_path.exists():
        return 40.0  # 无数据给中等偏低分

    try:
        df = pd.read_csv(csv_path, index_col=0, parse_dates=True)
        if len(df) < 120:
            return 40.0
    except Exception:
        return 40.0

    close = df["close"].iloc[-1]
    vol = df.get("volume", pd.Series(dtype=float))

    # MA120 支撑
    ma120 = df["close"].rolling(120).mean().iloc[-1]
    if ma120 and ma120 > 0:
        pct_from_ma120 = (close / ma120 - 1) * 100
        if -5 <= pct_from_ma120 <= 5:
            ma_score = 80
        elif -10 <= pct_from_ma120 < -5:
            ma_score = 60
        elif -15 <= pct_from_ma120 < -10:
            ma_score = 40
        elif pct_from_ma120 > 5:
            ma_score = 30
        else:
            ma_score = 20
    else:
        ma_score = 50.0

    # 超跌程度
    high_60d = df["close"].tail(60).max()
    if high_60d and high_60d > 0:
        drawdown = (close / high_60d - 1) * 100
        if -20 <= drawdown <= -10:
            dd_score = 85
        elif -30 <= drawdown < -20:
            dd_score = 70
        elif -10 < drawdown <= 0:
            dd_score = 60
        elif drawdown < -30:
            dd_score = 40
        else:
            dd_score = 30
    else:
        dd_score = 50.0

    # 量能萎缩
    if len(vol) >= 20:
        vol_5 = vol.tail(5).mean()
        vol_20 = vol.tail(20).mean()
        if vol_20 and vol_20 > 0:
            vol_ratio = vol_5 / vol_20
            if vol_ratio < 0.5:
                vol_score = 90
            elif vol_ratio < 0.7:
                vol_score = 75
            elif vol_ratio < 1.0:
                vol_score = 55
            else:
                vol_score = 35
        else:
            vol_score = 50.0
    else:
        vol_score = 50.0

    tech = ma_score * 0.40 + dd_score * 0.30 + vol_score * 0.30
    return round(min(100, max(0, tech)), 1)


# ====================================================================
#  主扫描逻辑
# ====================================================================

def scan_sectors() -> dict:
    """全市场板块轮动扫描主入口。

    返回 {
        scan_time: str,
        total_sectors: int,
        filtered_sectors: int,
        recommendations: [{sector, cold_score, value_score, total_score,
                           pe, pb, change_pct, leaders: [{code, name, ...}]}]
    }
    """
    print("[sector_rotation] 开始扫描...")
    t0 = time.time()

    # 0) 加载预测、准确率、参数（用于信号增强）
    try:
        preds = load_predictions()
        pred_stocks = preds.get("stocks", {}) if preds else {}
    except Exception:
        pred_stocks = {}
    try:
        acc_data = load_accuracy()
    except Exception:
        acc_data = {}
    try:
        params = load_params()
    except Exception:
        params = {}

    # 1) 获取板块列表
    all_sectors = fetch_sector_list()
    if not all_sectors:
        print("[sector_rotation] 未获取到板块数据，跳过")
        return _empty_result()

    # 2) 计算冷度 + 估值
    for sec in all_sectors:
        sec["cold_score"] = calc_cold_score(sec, all_sectors)
        sec["value_score"] = calc_value_score(sec, all_sectors)

    # 3) 过滤：冷度 ≥ 50 且 估值低度 ≥ 50
    cold_and_value = [
        s for s in all_sectors
        if s.get("cold_score", 0) >= COLD_THRESHOLD
        and s.get("value_score", 0) >= VALUE_THRESHOLD
    ]
    print(f"[sector_rotation] 冷门+低估值板块: {len(cold_and_value)} / {len(all_sectors)}")

    # 4) 对每个候选板块，获取成分股并识别龙头
    recommendations = []
    for i, sec in enumerate(cold_and_value):
        sector_name = sec["name"]
        sector_code = sec["code"]
        print(f"[sector_rotation]   [{i+1}/{len(cold_and_value)}] 扫描板块: {sector_name} ({sector_code})")

        # 获取成分股
        stocks = fetch_sector_stocks(sector_code)
        if len(stocks) < 3:
            print(f"    成分股 {len(stocks)} 只，太少跳过")
            continue

        # 计算龙头评分并排序
        for stock in stocks:
            stock["leader_score"] = calc_leader_score(stock, stocks)

        # 选出龙头
        leaders = sorted(stocks, key=lambda x: x.get("leader_score", 0), reverse=True)
        top_leaders = []
        for lead in leaders[:LEADERS_PER_SECTOR]:
            # 确保有 CSV 日线数据（没有则自动下载）
            _ensure_csv(lead["code"])
            # 计算技术安全评分
            tech = calc_tech_score(lead["code"])
            lead["tech_score"] = tech
            if tech >= TECH_THRESHOLD:
                top_leaders.append(lead)

        if not top_leaders:
            print(f"    龙头技术面不达标，跳过")
            continue

        # 综合评分
        sec_cold = sec.get("cold_score", 0)
        sec_value = sec.get("value_score", 0)
        best_leader_score = top_leaders[0].get("leader_score", 0)
        best_tech_score = max(l.get("tech_score", 0) for l in top_leaders)

        total_score = (sec_cold * W_COLD + sec_value * W_VALUE
                       + best_leader_score * W_LEADER + best_tech_score * W_TECH)

        # 构建龙头数据 + 信号增强 + 入场价
        scan_now = datetime.now()
        next_mon = _next_monday(scan_now)
        leaders_data = []
        for l in top_leaders:
            ld = {
                "code": l["code"],
                "name": l["name"],
                "leader_score": l.get("leader_score", 0),
                "tech_score": l.get("tech_score", 0),
                "pe": l.get("pe"),
                "pb": l.get("pb"),
                "total_mv": l.get("total_mv"),
                "pct_from_ma120": _pct_from_ma120(l["code"]),
            }
            # 确保有 CSV 日线数据（没有则自动下载）
            _ensure_csv(l["code"])
            # 信号增强
            _enrich_leader(ld, pred_stocks, acc_data, params)
            # 入场价（下周一收盘）
            entry = _entry_price_on_date(l["code"], next_mon)
            ld["entry_price"] = entry.get("entry_price")
            ld["entry_date"] = entry.get("entry_date")
            leaders_data.append(ld)

        rec = {
            "sector": sector_name,
            "sector_code": sector_code,
            "cold_score": sec_cold,
            "value_score": sec_value,
            "total_score": round(total_score, 1),
            "pe": sec.get("pe"),
            "pb": sec.get("pb"),
            "change_pct": sec.get("change_pct"),
            "turnover": sec.get("turnover"),
            "stock_count": sec.get("stock_count", 0),
            "leaders": leaders_data,
        }
        recommendations.append(rec)
        print(f"    → 推荐 {len(top_leaders)} 只龙头，综合分 {total_score:.1f}")

        # 节流
        time.sleep(0.3)

    # 5) 按综合评分降序
    recommendations.sort(key=lambda x: x.get("total_score", 0), reverse=True)
    top_recs = recommendations[:TOP_SECTORS]

    elapsed = time.time() - t0
    result = {
        "scan_time": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "total_sectors": len(all_sectors),
        "filtered_sectors": len(cold_and_value),
        "elapsed_sec": round(elapsed, 1),
        "recommendations": top_recs,
    }

    # 保存
    save_sector_result(result)
    print(f"[sector_rotation] 扫描完成，耗时 {elapsed:.1f}s，推荐 {len(top_recs)} 个板块")
    return result


def _pct_from_ma120(code: str) -> float | None:
    """计算当前价距 MA120 百分比。"""
    csv_path = DATA_DIR / f"{code}.csv"
    if not csv_path.exists():
        return None
    try:
        df = pd.read_csv(csv_path, index_col=0, parse_dates=True)
        if len(df) < 120:
            return None
        close = df["close"].iloc[-1]
        ma120 = df["close"].rolling(120).mean().iloc[-1]
        if ma120 and ma120 > 0:
            return round((close / ma120 - 1) * 100, 2)
    except Exception:
        pass
    return None


def _ensure_csv(code: str) -> None:
    """确保本地有该股票的 CSV 日线数据，没有则自动下载。"""
    csv_path = DATA_DIR / f"{code}.csv"
    if csv_path.exists():
        return
    try:
        print(f"    [download] {code} CSV 不存在，自动下载...")
        get_daily(code)
        print(f"    [download] {code} 下载完成")
    except Exception as e:
        print(f"    [download] {code} 下载失败: {e}")


def _next_monday(scan_date: datetime | None = None) -> datetime:
    """计算扫描日期之后的下一个周一。"""
    d = scan_date or datetime.now()
    # 下周一 = 当前日期 + (7 - weekday) 天（weekday 0=周一）
    days = 7 - d.weekday()
    if days == 7:  # 今天就是周一，下周一 = 7天后
        days = 7
    return d + timedelta(days=days)


def _entry_price_on_date(code: str, target_date: datetime) -> dict:
    """计算指定日期（或之后首个交易日）的收盘价作为入场价。

    返回 {"entry_price": float, "entry_date": str} 或空 dict。
    """
    csv_path = DATA_DIR / f"{code}.csv"
    if not csv_path.exists():
        return {}
    try:
        df = pd.read_csv(csv_path, index_col=0, parse_dates=True)
        # 找到 >= target_date 的第一个交易日
        target_str = target_date.strftime("%Y-%m-%d")
        mask = df.index >= target_str
        if mask.any():
            row = df[mask].iloc[0]
            return {
                "entry_price": round(float(row["close"]), 3),
                "entry_date": row.name.strftime("%Y-%m-%d"),
            }
    except Exception:
        pass
    return {}


def _enrich_leader(leader: dict, pred_stocks: dict, acc_data: dict, params: dict) -> dict:
    """为龙头添加信号、预测、准确率字段。"""
    code = leader["code"]
    csv_path = DATA_DIR / f"{code}.csv"

    # 信号 + frame（一次性计算，复用于预测）
    sig_data = {}
    frame = None
    try:
        if csv_path.exists():
            df = pd.read_csv(csv_path, index_col=0, parse_dates=True)
            if len(df) >= 120:
                frame = compute_frame(df, params)
                sig = dict(current_signal(frame, params))
                sig_data = {
                    "action": sig.get("action", "WAIT"),
                    "state_text": sig.get("state_text", "空仓"),
                    "reason": sig.get("reason", ""),
                    "close": sig.get("close"),
                }
    except Exception:
        pass

    if not sig_data:
        sig_data = {"action": "—", "state_text": "—", "reason": "无数据", "close": None}

    # 预测：回调买入、冲高卖出
    # 优先使用 predictions.json（含新闻情绪调整），否则从技术面 frame 实时计算
    pred = pred_stocks.get(code, {})
    pullback = pred.get("pullback")
    surge = pred.get("surge")
    if pullback is None and frame is not None:
        try:
            pp = predict_prices(frame, sentiment=0.0)
            pullback = pp.get("pullback")
            surge = pp.get("surge")
        except Exception:
            pass

    # 准确率：买对/买错/卖对/卖错
    acc = acc_data.get(code, {})
    bc = acc.get("buy_correct", 0)
    bf = acc.get("buy_fail", 0)
    sc = acc.get("sell_correct", 0)
    sf = acc.get("sell_fail", 0)

    # 合并到 leader
    leader.update(sig_data)
    leader["pullback"] = pullback
    leader["surge"] = surge
    leader["buy_correct"] = bc
    leader["buy_fail"] = bf
    leader["sell_correct"] = sc
    leader["sell_fail"] = sf
    return leader


def _empty_result() -> dict:
    return {
        "scan_time": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "total_sectors": 0,
        "filtered_sectors": 0,
        "elapsed_sec": 0,
        "recommendations": [],
    }


# ====================================================================
#  结果读写
# ====================================================================

def load_sector_result() -> dict:
    """加载上次扫描结果。"""
    if SECTOR_ROTATION_FILE.exists():
        try:
            return json.loads(SECTOR_ROTATION_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return _empty_result()


def save_sector_result(data: dict) -> None:
    """保存扫描结果。"""
    SECTOR_ROTATION_FILE.parent.mkdir(exist_ok=True)
    SECTOR_ROTATION_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ====================================================================
#  工具函数
# ====================================================================

def _em_val(v, default=None) -> float | None:
    """东方财富 push2 API 数值解析：原始值 / 100（API 以 0.01 为单位）。"""
    try:
        val = float(v)
        if np.isnan(val):
            return default
        return round(val / 100, 4)
    except (ValueError, TypeError):
        return default


def _safe_float(v, default=None) -> float | None:
    try:
        val = float(v)
        return val if not np.isnan(val) else default
    except (ValueError, TypeError):
        return default


def _safe_int(v, default=0) -> int:
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return default


# ====================================================================
#  命令行入口
# ====================================================================

if __name__ == "__main__":
    from config import ensure_dirs
    ensure_dirs()
    result = scan_sectors()

    print(f"\n{'='*60}")
    print(f"  板块轮动模型 TOP {TOP_SECTORS}")
    print(f"{'='*60}")
    for i, rec in enumerate(result.get("recommendations", [])):
        print(f"\n  #{i+1}  {rec['sector']}  综合评分: {rec['total_score']}")
        print(f"       冷度: {rec['cold_score']}  估值: {rec['value_score']}  "
              f"PE: {rec.get('pe', '-')}  PB: {rec.get('pb', '-')}  "
              f"涨跌幅: {rec.get('change_pct', '-')}%")
        for l in rec.get("leaders", []):
            print(f"       龙头: {l['code']} {l['name']}  "
                  f"龙头评分: {l['leader_score']}  技术面: {l['tech_score']}  "
                  f"PE: {l.get('pe', '-')}  "
                  f"距MA120: {l.get('pct_from_ma120', '-')}%")
