# -*- coding: utf-8 -*-
"""信号规则引擎 v3：趋势确认模型（FastStrict）+ 板块联动。

核心发现（7 变体回测对比）：
  满仓进出 + MA20 下行确认出场 = 跑赢买入持有
  - 300308: 策略 +4393% vs BH +2985%（跑赢 47%）
  - 300502: 策略 +7939% vs BH +9857%（仅差 19%）

模型规则（全部基于收盘价，次日开盘执行）：
  BUY   买入  —— close > MA20 AND close > MA60 → 建仓持仓
  SELL  卖出  —— close < MA20 AND MA20 下行   → 清仓
  LOCK  锁利  —— close < 吊灯止盈线(持仓中)   → 提示卖半仓锁利(可选)
  PIVOT 补涨  —— 同板块≥50%持仓 + 空仓       → 提前关注入场(低位补涨)
  HOLD  持有  —— 什么都不做
  WAIT  等待  —— 空仓中，等待买入信号

为什么能跑赢持有：
  - 满仓进场吃满主升段（不分批，100% 资金）
  - 仅在 MA20 确认下行才出场（不在均线附近反复假穿越被来回割）
  - 短期回调（MA20 仍上行）扛住不动，避免卖飞
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from indicators import atr, chandelier_long_stop, sma, supertrend


def compute_frame(df: pd.DataFrame, p: dict) -> pd.DataFrame:
    """附加全部指标列。"""
    out = df.copy()
    out["ma_fast"] = sma(out["close"], p["ma_fast"])
    out["ma_slow"] = sma(out["close"], p["ma_slow"])
    st = supertrend(out, p["atr_period"], p["st_multiplier"])
    out["st"] = st["st"]
    out["st_dir"] = st["dir"]
    out["st_flip"] = out["st_dir"].diff().fillna(0)
    out["chandelier"] = chandelier_long_stop(
        out, p["chandelier_period"], p["chandelier_atr_mult"]
    )
    out["atr_pct"] = atr(out, p["atr_period"]) / out["close"]
    return out


def _reason(items: list[str]) -> str:
    return "; ".join(items)


# ── 常量 ───────────────────────────────────────────────────────

STATE_TEXT = {0: "空仓", 1: "持仓"}

# 动作 → (中文, 颜色, emoji)
# 动作 → (中文, 颜色, emoji)
ACTION_META = {
    "SELL": ("卖出", "#e74c3c", "🔴"),
    "LOCK": ("锁利", "#f39c12", "🟠"),
    "BUY":  ("买入", "#2980b9", "🔵"),
    "PIVOT":("补涨", "#e67e22", "🟡"),
    "HOLD": ("持有", "#27ae60", "🟢"),
    "WAIT": ("等待", "#6b7280", "⚪"),
}

SEVERITY = {"SELL": 0, "LOCK": 1, "BUY": 2, "PIVOT": 3, "HOLD": 4, "WAIT": 5}

def signal_series(f: pd.DataFrame, p: dict) -> pd.DataFrame:
    """趋势确认模型信号序列（回测与当日信号共用）。

    返回列：
      state   当日仓位状态（0=空仓 / 1=持仓）
      buy     当日触发买入（次日开盘执行）
      sell    当日触发卖出
      lock    当日触发锁利提示（吊灯线，不强制执行）
    """
    n = len(f)
    close   = f["close"].to_numpy(float)
    ma_fast = f["ma_fast"].to_numpy(float)
    ma_slow = f["ma_slow"].to_numpy(float)
    chan    = f["chandelier"].to_numpy(float)
    mfd     = f["ma_fast"].diff().to_numpy(float)

    state = np.zeros(n, dtype=int)
    buy   = np.zeros(n, dtype=bool)
    sell  = np.zeros(n, dtype=bool)
    lock  = np.zeros(n, dtype=bool)

    st = 0  # 0=空仓, 1=持仓
    for i in range(n):
        if np.isnan(ma_slow[i]) or np.isnan(ma_fast[i]) or np.isnan(chan[i]):
            continue

        above_fast  = close[i] > ma_fast[i]
        above_slow  = close[i] > ma_slow[i]
        fast_falling = (mfd[i] <= 0) if not np.isnan(mfd[i]) else False
        below_chan  = close[i] < chan[i]

        if st == 0:                                          # ── 空仓
            if above_fast and above_slow:
                st = 1
                buy[i] = True
        else:                                                # ── 持仓
            if below_chan:
                lock[i] = True                               # 锁利提示
            if not above_fast and fast_falling:
                st = 0
                sell[i] = True

        state[i] = st

    return pd.DataFrame(
        {"state": state, "buy": buy, "sell": sell, "lock": lock},
        index=f.index,
    )


def current_signal(f: pd.DataFrame, p: dict | None = None) -> dict:
    """取最新交易日状态，输出结构化信号。"""
    p = p or {}
    last = f.iloc[-1]
    prev = f.iloc[-2] if len(f) >= 2 else last
    sig = signal_series(f, p)

    st_dir      = int(last["st_dir"])
    above_fast  = last["close"] > last["ma_fast"]
    above_slow  = last["close"] > last["ma_slow"]
    fast_rising = last["ma_fast"] > prev["ma_fast"]
    fast_falling = last["ma_fast"] <= prev["ma_fast"]
    below_chan  = last["close"] < last["chandelier"]
    pos_state   = int(sig["state"].iloc[-1])

    action = "HOLD"
    score = 50
    reasons: list[str] = []

    # ── 按事件判定动作 ──
    if bool(sig["sell"].iloc[-1]):
        action = "SELL"
        score = 5
        reasons.append(
            f"收盘{last['close']:.2f}跌破20日线{last['ma_fast']:.2f}"
            f"且20日线下行,趋势反转"
        )

    elif bool(sig["buy"].iloc[-1]):
        action = "BUY"
        score = 95
        reasons.append(
            f"收盘{last['close']:.2f}站上20日线{last['ma_fast']:.2f}"
            f"及60日线{last['ma_slow']:.2f},趋势确认"
        )

    elif bool(sig["lock"].iloc[-1]) and pos_state == 1:
        action = "LOCK"
        score = 55
        reasons.append(
            f"触及吊灯止盈线{last['chandelier']:.2f}(利润回吐超限)"
        )
        reasons.append("可考虑卖出半仓锁定利润,剩余仓位待卖出信号")

    else:
        if pos_state == 0:
            action = "WAIT"
            # ── 空仓打分：越接近买入条件分越高 ──
            if above_slow and above_fast:
                score = 50          # 条件几乎满足
            elif above_slow:
                score = 40          # 站上60日线，差20日线
                pct_below = ((last["close"] / last["ma_fast"]) - 1) * 100
                score += max(0, min(10, pct_below + 10))  # 越接近MA20分越高
            else:
                score = 20          # 未站上60日线
                pct_below = ((last["close"] / last["ma_slow"]) - 1) * 100
                score += max(0, min(15, pct_below + 20))  # 越接近MA60分越高
            if not above_slow:
                reasons.append(
                    f"当前空仓,收盘{last['close']:.2f}未站上60日线"
                    f"{last['ma_slow']:.2f},不追高"
                )
            elif not above_fast:
                reasons.append(
                    f"当前空仓,收盘{last['close']:.2f}未站上20日线"
                    f"{last['ma_fast']:.2f},等待确认"
                )
            else:
                reasons.append("当前空仓,等待入场条件满足")
        else:
            # ── 持仓打分：趋势越强分越高 ──
            score = 70
            pct_from_ma20 = ((last["close"] / last["ma_fast"]) - 1) * 100
            score += min(15, max(-5, pct_from_ma20 * 2))  # 距MA20越远分越高
            if fast_rising:
                score += 10
            if below_chan:
                score -= 10
            else:
                score += 5
            reasons.append(
                f"持仓中(距20日线{pct_from_ma20:+.1f}%)"
            )
            if fast_rising:
                reasons.append("20日线上行,趋势完好")
            if below_chan:
                reasons.append("注意:已触及吊灯止盈线")

    score = int(max(0, min(100, round(score))))

    return {
        "date": f.index[-1].strftime("%Y-%m-%d"),
        "close": round(float(last["close"]), 2),
        "st_dir": st_dir,
        "ma_fast": round(float(last["ma_fast"]), 2) if pd.notna(last["ma_fast"]) else None,
        "ma_slow": round(float(last["ma_slow"]), 2) if pd.notna(last["ma_slow"]) else None,
        "chandelier": round(float(last["chandelier"]), 2) if pd.notna(last["chandelier"]) else None,
        "atr_pct": round(float(last["atr_pct"]) * 100, 2) if pd.notna(last["atr_pct"]) else None,
        "action": action,
        "reason": _reason(reasons),
        "state": pos_state,
        "state_text": STATE_TEXT.get(pos_state, "未知"),
        "score": score,
    }


def sector_boost(rows: list[dict], sectors: dict[str, list[str]]) -> list[dict]:
    """板块联动加分：同板块已走好时，空仓股识别补涨机会。

    规则：
    - 统计同板块内持仓(state=1)股票占比
    - 空仓股 + 板块持仓率 ≥ 50% → 动作改为 PIVOT(补涨)，分数 60~75
    - 持仓股 + 板块持仓率 ≥ 50% → 分数 +5，依据追加板块确认
    - 返回按分数降序排列
    """
    # 代码 → 板块 反向映射
    code_sector: dict[str, str] = {}
    for sector, codes in sectors.items():
        for code in codes:
            code_sector[code] = sector

    code_row = {r["code"]: r for r in rows}

    for r in rows:
        code = r["code"]
        sector = code_sector.get(code, "")
        r["sector"] = sector
        if not sector or r.get("action") == "ERROR":
            continue

        peers = [c for c in sectors[sector] if c in code_row and c != code]
        if not peers:
            continue

        held = sum(1 for c in peers if code_row[c].get("state") == 1)
        total = len(peers)
        ratio = held / total if total > 0 else 0

        if r.get("state") == 0 and ratio >= 0.5:
            # ── 空仓 + 板块已走好 → 低位补涨 ──
            score = 60
            score += min(15, held * 5)  # 每个持仓同伴 +5，最多 +15

            ma_slow = r.get("ma_slow")
            close = r.get("close")
            pct_from_ma60 = None
            if (ma_slow and close
                    and isinstance(ma_slow, (int, float))
                    and isinstance(close, (int, float))):
                pct_from_ma60 = ((close / ma_slow) - 1) * 100
                if pct_from_ma60 > -5:
                    score += 5    # 接近60日线
                if pct_from_ma60 >= 0:
                    score += 5    # 已站上60日线

            r["score"] = int(min(75, score))
            r["action"] = "PIVOT"

            # 追加依据
            extra = f"板块补涨({sector}{held}/{total}持仓)"
            if pct_from_ma60 is not None:
                extra += f";距60日线{pct_from_ma60:+.1f}%"
            r["reason"] = r.get("reason", "") + ";" + extra

        elif r.get("state") == 1 and ratio >= 0.5:
            # ── 持仓 + 板块已走好 → 趋势确认 ──
            r["score"] = int(min(95, r.get("score", 0) + 5))
            r["reason"] = r.get("reason", "") + f";{sector}板块{held}/{total}持仓"

    rows.sort(key=lambda r: r.get("score", 0), reverse=True)
    return rows
