# -*- coding: utf-8 -*-
"""多模型回测对比：找出最优分层仓位参数组合。

对比维度：
  - 总收益 / 年化 / 最大回撤 / 交易腿数 / 胜率 / 平均持仓 / 加仓卖出次数
  - K线标记噪音 = base+add+sell_add+exit 事件总数（越少越清爽）

变体：
  BH       买入持有基准
  Old      旧模型（满仓进出，ST|MA60|chandelier 退出）
  TierA    当前分层（base_ratio=0.5, 退出=ST翻空|跌破MA60, 卖加仓=跌破MA20或触吊灯）
  TierB    MA60-only退出（退出=仅跌破MA60，不看ST翻空，减少噪音）
  TierC    严格卖加仓（卖加仓=跌破MA20且MA20下行 或 触吊灯，减少假信号）
  TierD    高门槛+严格（加仓门槛10%，严格卖加仓，MA60-only退出）
  TierE    满仓+半仓止盈（base_ratio=1.0满仓建底仓，加仓=标记不追加资金，
           卖加仓=卖出50%持仓，清仓=卖出剩余——贴合"先满仓再减半再清"思路）
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from config import load_params, load_watchlist
from datafeed import get_daily
from indicators import atr, chandelier_long_stop, sma, supertrend
from signals import compute_frame


def _perf(eq, dates):
    eq = np.asarray(eq, dtype=float)
    total = eq[-1] / eq[0] - 1.0
    days = max((dates[-1] - dates[0]).days, 1)
    cagr = (eq[-1] / eq[0]) ** (365.0 / days) - 1.0
    peak = np.maximum.accumulate(eq)
    mdd = float((eq / peak - 1.0).min())
    return total, cagr, mdd


def _tier_events(f, p, exit_mode, sell_add_mode, add_gain):
    """计算分层事件序列（可配置退出/卖加仓模式）。"""
    n = len(f)
    close = f["close"].to_numpy(float)
    mf = f["ma_fast"].to_numpy(float)
    ms = f["ma_slow"].to_numpy(float)
    sd = f["st_dir"].to_numpy(float)
    ch = f["chandelier"].to_numpy(float)
    fd = f["ma_fast"].diff().to_numpy(float)

    state = np.zeros(n, dtype=int)  # 0=空, 1=底仓, 2=底仓+加仓
    ev = {k: np.zeros(n, dtype=bool) for k in ("base", "add", "sell_add", "exit")}
    st, base_px, add_px = 0, np.nan, np.nan

    for i in range(n):
        if np.isnan(ms[i]) or np.isnan(mf[i]) or np.isnan(ch[i]):
            continue
        up = sd[i] == 1 and close[i] > ms[i]
        if sell_add_mode == "strict":
            weak = (close[i] < mf[i] and fd[i] <= 0) or close[i] < ch[i]
        else:
            weak = close[i] < mf[i] or close[i] < ch[i]
        if exit_mode == "ma60_only":
            full = close[i] < ms[i]
        else:
            full = sd[i] == -1 or close[i] < ms[i]
        fast_up = fd[i] > 0 if not np.isnan(fd[i]) else False

        if st == 0:
            if up:
                st, base_px, add_px = 1, close[i], np.nan
                ev["base"][i] = True
        elif st == 1:
            if full:
                st, base_px, add_px = 0, np.nan, np.nan
                ev["exit"][i] = True
            else:
                ref = base_px * (1.0 + add_gain)
                if not np.isnan(add_px):
                    ref = max(ref, add_px)
                if close[i] > mf[i] and fast_up and close[i] > ref and not weak:
                    st, add_px = 2, close[i]
                    ev["add"][i] = True
        else:
            if full:
                st, base_px, add_px = 0, np.nan, np.nan
                ev["exit"][i] = True
            elif weak:
                st = 1
                ev["sell_add"][i] = True
        state[i] = st

    return ev, state


def _backtest_tier(f, p, exit_mode, sell_add_mode, add_gain, base_ratio):
    """分层仓位回测（可配置）。"""
    ev, state = _tier_events(f, p, exit_mode, sell_add_mode, add_gain)
    fees = float(p["fees"])
    opens, closes, dates = f["open"].values, f["close"].values, f.index

    cash = 1.0
    base_sh = add_sh = 0.0
    equity = np.ones(len(f))
    n_base = n_add = n_sell = n_exit = 0
    first_buy = None
    base_leg = add_leg = None

    for i in range(1, len(f)):
        k = i - 1
        if ev["base"][k]:
            spend = cash * base_ratio
            sh = spend / (opens[i] * (1 + fees))
            cash -= spend; base_sh = sh
            base_leg = True; n_base += 1
            if first_buy is None: first_buy = i
        elif ev["add"][k] and base_leg:
            spend = cash
            sh = spend / (opens[i] * (1 + fees))
            cash = 0.0; add_sh = sh; add_leg = True; n_add += 1
        elif ev["sell_add"][k] and add_leg:
            cash += add_sh * opens[i] * (1 - fees)
            add_sh = 0.0; add_leg = False; n_sell += 1
        elif ev["exit"][k]:
            if add_leg:
                cash += add_sh * opens[i] * (1 - fees)
                add_sh = 0.0; add_leg = False
            if base_leg:
                cash += base_sh * opens[i] * (1 - fees)
                base_sh = 0.0; base_leg = False
            n_exit += 1
        equity[i] = cash + (base_sh + add_sh) * closes[i]

    strat = _perf(equity if first_buy is not None else np.array([1.0]),
                  dates if first_buy is not None else dates[:1])
    return strat, n_base + n_add + n_sell + n_exit, n_sell, n_base, first_buy


def _backtest_tier_full_half(f, p, exit_mode, sell_add_mode, add_gain):
    """满仓+半仓止盈模式：base_ratio=1.0满仓建底仓，加仓=标记，卖加仓=卖出50%持仓。"""
    ev, state = _tier_events(f, p, exit_mode, sell_add_mode, add_gain)
    fees = float(p["fees"])
    opens, closes, dates = f["open"].values, f["close"].values, f.index

    cash = 1.0
    shares = 0.0
    half_sold = False
    equity = np.ones(len(f))
    n_events = n_sell = 0
    first_buy = None

    for i in range(1, len(f)):
        k = i - 1
        if ev["base"][k] and shares == 0:
            shares = cash / (opens[i] * (1 + fees))
            cash = 0.0; half_sold = False; n_events += 1
            if first_buy is None: first_buy = i
        elif ev["add"][k] and shares > 0 and not half_sold:
            pass  # 加仓=标记，不追加资金
        elif ev["sell_add"][k] and shares > 0 and not half_sold:
            sell = shares * 0.5
            cash += sell * opens[i] * (1 - fees)
            shares -= sell; half_sold = True; n_sell += 1; n_events += 1
        elif ev["exit"][k] and shares > 0:
            cash += shares * opens[i] * (1 - fees)
            shares = 0.0; half_sold = False; n_events += 1
        equity[i] = cash + shares * closes[i]

    strat = _perf(equity if first_buy is not None else np.array([1.0]),
                  dates if first_buy is not None else dates[:1])
    return strat, n_events, n_sell, 0, first_buy


def _backtest_old(f, p):
    """旧模型：满仓进出。"""
    fees = float(p["fees"])
    opens, closes, dates = f["open"].values, f["close"].values, f.index
    sd = f["st_dir"].to_numpy(float)
    ms = f["ma_slow"].to_numpy(float)
    ch = f["chandelier"].to_numpy(float)

    close = f["close"].to_numpy(float)
    cash = 1.0; shares = 0.0
    equity = np.ones(len(f))
    n_trades = 0; first_buy = None; hold = False

    for i in range(1, len(f)):
        k = i - 1
        if np.isnan(ms[k]):
            entry = False; exit_ = False
        else:
            entry = sd[k] == 1 and close[k] > ms[k]
            exit_ = sd[k] == -1 or close[k] < ms[k] or close[k] < ch[k]

        if not hold and entry and not exit_:
            shares = cash / (opens[i] * (1 + fees)); cash = 0.0; hold = True; n_trades += 1
            if first_buy is None: first_buy = i
        elif hold and exit_:
            cash += shares * opens[i] * (1 - fees); shares = 0.0; hold = False; n_trades += 1
        equity[i] = cash + shares * closes[i]

    strat = _perf(equity if first_buy is not None else np.array([1.0]),
                  dates if first_buy is not None else dates[:1])
    return strat, n_trades, 0, 0, first_buy


def main():
    p = load_params()
    print("=" * 120)
    print("多模型回测对比（2018~2026-08，前复权，单边费率0.1%）")
    print(f"参数: ST×{p['st_multiplier']} 吊灯×{p['chandelier_atr_mult']} MA{p['ma_fast']}/{p['ma_slow']}")
    print("=" * 120)

    variants = [
        ("BH",         "买入持有",           None),
        ("Old",        "旧模型(满仓进出)",    None),
        ("TierA",      "当前分层(0.5/5%/宽退)", ("st_or_ma60", "aggressive", 0.05, 0.5)),
        ("TierB",      "MA60退出(0.5/5%)",    ("ma60_only",  "aggressive", 0.05, 0.5)),
        ("TierC",      "严格卖加仓(0.5/5%)",  ("ma60_only",  "strict",     0.05, 0.5)),
        ("TierD",      "高门槛(0.5/10%/严)",  ("ma60_only",  "strict",     0.10, 0.5)),
        ("TierE",      "满仓+半止盈(1.0/5%)", ("ma60_only",  "aggressive", 0.05, "full_half")),
    ]

    results = {}
    for code, name in load_watchlist().items():
        df = get_daily(code, refresh=False)
        f = compute_frame(df, p)
        closes = f["close"].to_numpy(float)
        dates = f.index

        print(f"\n{'─'*120}")
        print(f"  {code} {name}  (数据: {dates[0].strftime('%Y-%m-%d')} ~ {dates[-1].strftime('%Y-%m-%d')}, {len(f)}根)")
        print(f"{'─'*120}")
        header = f"{'模型':<22} {'总收益':>10} {'年化':>8} {'最大回撤':>10} {'腿数':>6} {'卖加仓':>6} {'事件总数':>8} {'vsBH':>8}"
        print(header)
        print(f"{'─'*86}")

        stock_res = {}
        for vid, vlabel, vcfg in variants:
            if vid == "BH":
                eq = np.ones(len(f))
                if len(f) > 0:
                    eq[:] = closes / closes[0]
                total, cagr, mdd = _perf(eq, dates)
                stock_res[vid] = (total, cagr, mdd, 0, 0, 0)
            elif vid == "Old":
                strat, n_ev, n_sell, n_base, fb = _backtest_old(f, p)
                stock_res[vid] = (strat[0], strat[1], strat[2], n_ev, n_sell, n_ev)
            elif vcfg[3] == "full_half":
                strat, n_ev, n_sell, n_base, fb = _backtest_tier_full_half(f, p, vcfg[0], vcfg[1], vcfg[2])
                stock_res[vid] = (strat[0], strat[1], strat[2], n_ev, n_sell, n_ev)
            else:
                strat, n_ev, n_sell, n_base, fb = _backtest_tier(f, p, vcfg[0], vcfg[1], vcfg[2], vcfg[3])
                stock_res[vid] = (strat[0], strat[1], strat[2], n_ev, n_sell, n_ev)

            r = stock_res[vid]
            bh_total = stock_res.get("BH", (0,))[0]
            vs_bh = r[0] / bh_total - 1.0 if bh_total != 0 else 0
            print(f"  {vlabel:<20} {r[0]*100:>+9.1f}% {r[1]*100:>+7.1f}% {r[2]*100:>9.1f}% "
                  f"{r[3]:>6} {r[4]:>6} {r[5]:>8} {vs_bh*100:>+7.1f}%")

        results[code] = stock_res

    # 汇总
    print(f"\n{'='*120}")
    print("  三票平均汇总")
    print(f"{'='*120}")
    print(f"{'模型':<22} {'均总收益':>10} {'均年化':>8} {'均回撤':>10} {'均腿数':>6} {'均卖加仓':>8} {'均vsBH':>8}")
    print(f"{'─'*80}")
    for vid, vlabel, _ in variants:
        rs = [results[c].get(vid, (0,0,0,0,0,0)) for c in results]
        avg_total = sum(r[0] for r in rs) / len(rs)
        avg_cagr = sum(r[1] for r in rs) / len(rs)
        avg_mdd = sum(r[2] for r in rs) / len(rs)
        avg_n = sum(r[3] for r in rs) / len(rs)
        avg_sell = sum(r[4] for r in rs) / len(rs)
        bh_avg = sum(results[c]["BH"][0] for c in results) / len(results)
        vs_bh = avg_total / bh_avg - 1.0 if bh_avg != 0 else 0
        print(f"  {vlabel:<20} {avg_total*100:>+9.1f}% {avg_cagr*100:>+7.1f}% {avg_mdd*100:>9.1f}% "
              f"{avg_n:>6.0f} {avg_sell:>8.0f} {vs_bh*100:>+7.1f}%")


if __name__ == "__main__":
    main()
