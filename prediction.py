# -*- coding: utf-8 -*-
"""每日复盘预测模块：结合东方财富快讯 + 技术面，生成次日回调/冲高卖出价。

每晚 19:00 由 cron 调用：
    python prediction.py

输出：data/predictions.json
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import requests

from config import DATA_DIR, load_params, load_watchlist
from datafeed import get_daily
from signals import compute_frame

# ── 东方财富快讯 ──────────────────────────────────────────────

KUAIXUN_BASE = (
    "https://np-listapi.eastmoney.com/comm/web/getFastNewsList"
    "?req_trace={trace}&client=web&biz=web_724&fastColumn=102"
    "&sortEnd=&pageSize=50&type=0"
)

POSITIVE_KW = {
    "利好", "增长", "突破", "上修", "涨停", "大涨", "新高", "盈利",
    "超预期", "回购", "增持", "分红", "中标", "签约", "获批", "创新高",
    "强势", "上涨", "反弹", "复苏", "需求旺盛", "订单", "扩产",
}
NEGATIVE_KW = {
    "利空", "亏损", "下修", "跌停", "大跌", "新低", "减持", "违规",
    "处罚", "退市", "风险", "下降", "下滑", "暴雷", "违约", "诉讼",
    "负面", "警告", "缩量", "破位", "下跌", "疲软", "产能过剩",
}


def fetch_kuaixun() -> list[dict]:
    """从东方财富快讯抓取最新 50 条财经快讯。"""
    import random
    trace = str(int(time.time() * 1000)) + str(random.randint(100, 999))
    url = KUAIXUN_BASE.format(trace=trace)
    try:
        r = requests.get(url, timeout=15,
                         headers={"User-Agent": "Mozilla/5.0",
                                  "Referer": "https://kuaixun.eastmoney.com/"})
        r.raise_for_status()
        data = r.json()
        items = data.get("data", {}).get("fastNewsList", [])
        result = []
        for it in items:
            title = it.get("title", "") or ""
            content = it.get("summary", "") or ""
            ts = it.get("showTime", "") or ""
            if not title and not content:
                continue
            result.append({"title": title, "content": content, "time": ts})
        return result
    except Exception as e:
        print(f"[WARN] 快讯抓取失败: {e}")
        return []

def match_stock_news(name: str, news_list: list[dict]) -> list[dict]:
    """从快讯列表中筛选与指定股票相关的新闻。"""
    matched = []
    # 提取股票名称关键词（去掉常见后缀如"股份""集团"等，提高匹配率）
    keywords = [name]
    for suffix in ("股份", "集团", "科技", "电子", "电气", "医药", "生物", "新材"):
        if name.endswith(suffix) and len(name) > len(suffix):
            keywords.append(name[: -len(suffix)])
    for nw in news_list:
        text = (nw.get("title", "") + " " + nw.get("content", "")).strip()
        if any(kw in text for kw in keywords):
            matched.append(nw)
    return matched


def simple_sentiment(text: str) -> float:
    """简单的关键词情绪分析，返回 [-1, 1]，正=利好，负=利空。"""
    text = text.lower()
    pos = sum(1 for kw in POSITIVE_KW if kw in text)
    neg = sum(1 for kw in NEGATIVE_KW if kw in text)
    total = pos + neg
    if total == 0:
        return 0.0
    return (pos - neg) / total


def stock_sentiment(name: str, news_list: list[dict]) -> tuple[float, str]:
    """计算单只股票的新闻情绪，返回 (score, summary)。"""
    matched = match_stock_news(name, news_list)
    if not matched:
        return 0.0, ""
    scores = [simple_sentiment(n.get("title", "") + " " + n.get("content", ""))
              for n in matched]
    avg = sum(scores) / len(scores)
    # 取前 3 条相关新闻标题作为摘要
    summary = "；".join(n.get("title", "")[:30] for n in matched[:3])
    return avg, summary


# ── 技术面预测 ────────────────────────────────────────────────

def predict_prices(frame: pd.DataFrame, sentiment: float = 0.0) -> dict:
    """基于技术面 + 新闻情绪，计算次日回调买入价和冲高卖出价。

    回调买入价：次日可能回调到的支撑位（逢低买入机会）
    冲高卖出价：次日可能冲高到的阻力位（冲高卖出止盈价格）

    返回 dict: {pullback, surge, pullback_reason, surge_reason}
    """
    last = frame.iloc[-1]
    prev = frame.iloc[-2] if len(frame) >= 2 else last
    close = float(last["close"])
    high = float(last["high"])
    low = float(last["low"])
    ma20 = float(last["ma_fast"]) if pd.notna(last["ma_fast"]) else close
    ma60 = float(last["ma_slow"]) if pd.notna(last["ma_slow"]) else close
    atr_val = float(last.get("atr_pct", 0.05)) * close  # atr_pct * close = ATR

    # 最近 5 日低点/高点作为短期支撑/阻力
    recent = frame.tail(5)
    recent_low = float(recent["low"].min())
    recent_high = float(recent["high"].max())

    # ── 回调买入价 ──
    # 核心逻辑：寻找下方最近的支撑位
    supports = []
    pullback_reasons = []

    # 1. MA20 支撑（若当前在 MA20 上方）
    if close > ma20:
        supports.append(ma20)
        pullback_reasons.append(f"MA20={ma20:.2f}")

    # 2. MA60 支撑（若当前在 MA60 上方）
    if close > ma60:
        supports.append(ma60)
        pullback_reasons.append(f"MA60={ma60:.2f}")

    # 3. 前日低点支撑
    prev_low = float(prev["low"])
    if prev_low < close:
        supports.append(prev_low)
        pullback_reasons.append(f"前低={prev_low:.2f}")

    # 4. 近 5 日最低点
    if recent_low < close:
        supports.append(recent_low)
        pullback_reasons.append(f"5日低={recent_low:.2f}")

    # 取最高的支撑位作为回调目标（最近的可回调点）
    if supports:
        pullback = max(s for s in supports if s < close)
    else:
        # 无明显支撑，用 ATR 估算回调
        pullback = close - 0.5 * atr_val
        pullback_reasons.append(f"ATR回调={pullback:.2f}")

    # 情绪调整：利空时回调更深
    if sentiment < -0.3:
        pullback -= 0.3 * atr_val
        pullback_reasons.append("利空加深")
    elif sentiment > 0.3:
        pullback += 0.2 * atr_val
        pullback_reasons.append("利好支撑")

    # 确保不超过收盘价
    pullback = min(pullback, close * 0.99)

    # ── 冲高卖出价 ──
    # 核心逻辑：寻找上方最近的阻力位 / 突破目标
    resistances = []
    surge_reasons = []

    # 1. 前日高点突破
    prev_high = float(prev["high"])
    if prev_high > close:
        resistances.append(prev_high)
        surge_reasons.append(f"前高={prev_high:.2f}")
    else:
        # 已经突破前高
        surge_target = prev_high + 0.3 * atr_val
        resistances.append(surge_target)
        surge_reasons.append(f"破前高+={surge_target:.2f}")

    # 2. 近 5 日最高点
    if recent_high > close:
        resistances.append(recent_high)
        surge_reasons.append(f"5日高={recent_high:.2f}")

    # 3. 吊灯线位置（如有）
    chandelier = float(last["chandelier"]) if pd.notna(last.get("chandelier")) else 0
    if chandelier > close:
        resistances.append(chandelier)
        surge_reasons.append(f"吊灯={chandelier:.2f}")

    # 取最低的阻力位作为冲高目标（最容易到达的突破点）
    if resistances:
        surge = min(r for r in resistances if r > close) if any(r > close for r in resistances) \
            else close + 0.5 * atr_val
    else:
        surge = close + 0.5 * atr_val
        surge_reasons.append(f"ATR冲高={surge:.2f}")

    # 情绪调整：利好时冲更高
    if sentiment > 0.3:
        surge += 0.3 * atr_val
        surge_reasons.append("利好加速")
    elif sentiment < -0.3:
        surge -= 0.2 * atr_val
        surge_reasons.append("利空压制")

    # 确保不低于收盘价
    surge = max(surge, close * 1.005)

    return {
        "pullback": round(pullback, 2),
        "surge": round(surge, 2),
        "pullback_reason": "；".join(pullback_reasons[:3]),
        "surge_reason": "；".join(surge_reasons[:3]),
    }


# ── 主流程 ────────────────────────────────────────────────────

PREDICTION_FILE = DATA_DIR / "predictions.json"
ACCURACY_FILE = DATA_DIR / "prediction_accuracy.json"


def load_predictions() -> dict:
    """加载预测结果。返回 {"date": ..., "stocks": {...}} 或空。"""
    if PREDICTION_FILE.exists():
        try:
            return json.loads(PREDICTION_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def load_accuracy() -> dict:
    """加载预测准确率数据。返回 {code: {buy_correct, buy_fail, sell_correct, sell_fail, last_check}, ...} 或空。"""
    if ACCURACY_FILE.exists():
        try:
            data = json.loads(ACCURACY_FILE.read_text(encoding="utf-8"))
            # 迁移旧格式（correct/fail → buy_correct/buy_fail/sell_correct/sell_fail）
            migrated = False
            for code, v in data.items():
                if "correct" in v and "buy_correct" not in v:
                    v["buy_correct"] = 0
                    v["buy_fail"] = 0
                    v["sell_correct"] = 0
                    v["sell_fail"] = 0
                    v.pop("correct", None)
                    v.pop("fail", None)
                    migrated = True
            if migrated:
                save_accuracy(data)
            return data
        except Exception:
            pass
    return {}


def save_accuracy(accuracy: dict) -> None:
    """保存预测准确率数据。"""
    ACCURACY_FILE.write_text(json.dumps(accuracy, ensure_ascii=False, indent=2),
                             encoding="utf-8")


def check_accuracy() -> dict:
    """检查昨日预测与今日实际数据的匹配情况，更新准确率。

    判定标准（买入和卖出分别独立计算）：
      预测回调买入价 vs 当日最低价 误差 ≤ 1.5% → 买入正确，否则 → 买入错误
      预测冲高卖出价 vs 当日最高价 误差 ≤ 1.5% → 卖出正确，否则 → 卖出错误
    """
    accuracy = load_accuracy()
    prev_preds = load_predictions()

    if not prev_preds or not prev_preds.get("stocks"):
        return accuracy

    today = datetime.now().strftime("%Y-%m-%d")
    pred_date = prev_preds.get("date", "")

    # 预测是今天生成的，还没过夜，无法验证
    if pred_date == today:
        return accuracy

    # 检查是否今天已验证过（避免重复计数）
    already = any(v.get("last_check") == today for v in accuracy.values())
    if already:
        return accuracy

    wl = load_watchlist()

    for code, pred in prev_preds["stocks"].items():
        if code not in wl:
            continue
        try:
            df = get_daily(code, refresh=False)
            if df.empty or len(df) < 2:
                continue

            last = df.iloc[-1]
            actual_low = float(last["low"])
            actual_high = float(last["high"])
            pb = pred.get("pullback")
            sg = pred.get("surge")
            if pb is None or sg is None:
                continue

            pb_err = abs(actual_low - pb) / pb
            sg_err = abs(actual_high - sg) / sg

            if code not in accuracy:
                accuracy[code] = {"buy_correct": 0, "buy_fail": 0,
                                  "sell_correct": 0, "sell_fail": 0,
                                  "last_check": ""}

            # 买入（回调价 vs 最低价）独立判定
            if pb_err <= 0.015:
                accuracy[code]["buy_correct"] += 1
                buy_tag = "买对"
            else:
                accuracy[code]["buy_fail"] += 1
                buy_tag = "买错"

            # 卖出（冲高价 vs 最高价）独立判定
            if sg_err <= 0.015:
                accuracy[code]["sell_correct"] += 1
                sell_tag = "卖对"
            else:
                accuracy[code]["sell_fail"] += 1
                sell_tag = "卖错"

            accuracy[code]["last_check"] = today

            name = pred.get("name", code)
            print(f"  [验证] {code} {name}: "
                  f"低={actual_low:.2f}(预{pb:.2f} {pb_err:.1%}) → {buy_tag}  "
                  f"高={actual_high:.2f}(预{sg:.2f} {sg_err:.1%}) → {sell_tag}")
        except Exception as e:
            print(f"  [验证] {code}: 失败 - {e}")

    save_accuracy(accuracy)
    return accuracy


def run_prediction() -> dict:
    """对自选股列表的所有股票执行复盘预测。"""
    print(f"[预测] 开始复盘预测 {datetime.now().strftime('%Y-%m-%d %H:%M')}")

    # 0. 验证昨日预测准确率
    check_accuracy()

    # 1. 抓取快讯
    news_list = fetch_kuaixun()
    print(f"[预测] 快讯: {len(news_list)} 条")

    # 2. 遍历自选股
    wl = load_watchlist()
    p = load_params()
    results = {}

    for code, name in wl.items():
        try:
            df = get_daily(code, refresh=False)
            if df.empty or len(df) < 5:
                continue
            frame = compute_frame(df, p)
            if frame.empty:
                continue

            # 新闻情绪
            sent, news_summary = stock_sentiment(name, news_list)

            # 技术面预测
            pred = predict_prices(frame, sent)
            pred["name"] = name
            pred["close"] = round(float(frame.iloc[-1]["close"]), 2)
            pred["sentiment"] = round(sent, 2)
            pred["news_summary"] = news_summary
            pred["date"] = str(frame.index[-1].date()) if hasattr(frame.index[-1], "date") \
                else str(frame.iloc[-1].get("date", ""))
            results[code] = pred

            print(f"  {code} {name}: 回调={pred['pullback']} 冲高={pred['surge']} 情绪={sent:.1f}")
        except Exception as e:
            print(f"  {code} {name}: 预测失败 - {e}")

    # 3. 保存
    out = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "time": datetime.now().strftime("%H:%M"),
        "news_count": len(news_list),
        "stocks": results,
    }
    PREDICTION_FILE.write_text(json.dumps(out, ensure_ascii=False, indent=2),
                               encoding="utf-8")
    print(f"[预测] 完成，{len(results)} 只股票 → {PREDICTION_FILE}")
    return out


if __name__ == "__main__":
    run_prediction()
