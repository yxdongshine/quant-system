# -*- coding: utf-8 -*-
"""FastExit 精细调优：在收益最高的 FastExit 基础上减少噪音。

FastExit 核心：close > MA20 AND close > MA60 入；close < MA20 出
问题：MA20 出场太敏感 → 大量假信号（价格 MA20 附近反复穿越）

解决方案：3 种降噪策略
  1) FastStrict:  exit = close<MA20 AND MA20下行（需 MA20 确认下跌趋势）
  2) FastChan:    exit = close<chandelier（用吊灯线替代 MA20 作跟踪止盈）
  3) FastHybrid:  exit = close<chandelier OR (close<MA20 AND close<MA60)

另外测试不同 chandelier ATR 乘数（2x/3x/4x）对 FastChan 的影响。
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
    if eq[0] <= 0 or eq[-1] <= 0:
        return 0.0, 0.0, 0.0
    total = eq[-1] / eq[0] - 1.0
    days = max((dates[-1] - dates[0]).days, 1)
    cagr = (eq[-1] / eq[0]) ** (365.0 / days) - 1.0
    peak = np.maximum.accumulate(eq)
    mdd = float((eq / peak - 1.0).min())
    return total, cagr, mdd


def _backtest_full(close, opens, dates, fees, entry, exit_):
    n = len(close)
    cash, shares = 1.0, 0.0
    equity = np.ones(n)
    hold = False
    first_buy = None
    n_trades = 0

    for i in range(1, n):
        k = i - 1
        if not hold and entry[k] and not exit_[k]:
            shares = cash / (opens[i] * (1 + fees))
            cash = 0.0; hold = True; n_trades += 1
            if first_buy is None: first_buy = i
        elif hold and exit_[k]:
            cash += shares * opens[i] * (1 - fees)
            shares = 0.0; hold = False; n_trades += 1
        equity[i] = cash + shares * close[i]

    strat = _perf(equity[first_buy:] if first_buy else np.array([1.0]),
                  dates[first_buy:] if first_buy else dates[:1])
    return strat, n_trades


def main():
    p = load_params()
    print("=" * 130)
    print("FastExit 精细调优（2018~2026-08，前复权，单边费率0.1%）")
    print("=" * 130)

    # 模型定义
    variants = [
        ("FastExit",     "快进快出(基准)"),
        ("FastStrict",   "MA20下行确认"),
        ("FastChan3",    "吊灯3xATR止盈"),
        ("FastChan4",    "吊灯4xATR止盈"),
        ("FastChan2",    "吊灯2xATR(紧)"),
        ("FastHybrid",   "吊灯+MA20双保险"),
        ("MAArray",      "均线阵列(对比)"),
        ("Chandelier",   "纯吊灯+ST入场(对比)"),
    ]

    all_results = {}

    for code, name in load_watchlist().items():
        df = get_daily(code, refresh=False)
        f = compute_frame(df, p)
        n = len(f)
        close  = f["close"].to_numpy(float)
        opens  = f["open"].to_numpy(float)
        mf     = f["ma_fast"].to_numpy(float)
        ms     = f["ma_slow"].to_numpy(float)
        sd     = f["st_dir"].to_numpy(float)
        ch     = f["chandelier"].to_numpy(float)
        mfd    = f["ma_fast"].diff().to_numpy(float)
        dates  = f.index
        fees   = float(p["fees"])

        # 紧吊灯 2x/3x ATR
        ch2 = chandelier_long_stop(f, 22, 2.0).to_numpy(float)
        ch3 = chandelier_long_stop(f, 22, 3.0).to_numpy(float)

        valid = ~(np.isnan(ms) | np.isnan(mf) | np.isnan(ch))
        ma20_falling = np.zeros(n, dtype=bool)
        for i in range(1, n):
            if not np.isnan(mfd[i]):
                ma20_falling[i] = mfd[i] <= 0

        # 入场（所有 FastExit 系列共用）
        entry = valid & (close > mf) & (close > ms)

        # 各模型出场条件
        exits = {
            "FastExit":   valid & (close < mf),
            "FastStrict": valid & (close < mf) & ma20_falling,
            "FastChan3":  valid & ~np.isnan(ch3) & (close < ch3),
            "FastChan4":  valid & (close < ch),
            "FastChan2":  valid & ~np.isnan(ch2) & (close < ch2),
            "FastHybrid": valid & ((close < ch) | ((close < mf) & (close < ms))),
            "MAArray":    valid & ((close < mf) | (mf < ms)),
            "Chandelier": valid & (close < ch),
        }
        # Chandelier 的入场条件不同
        entry_chan = valid & (close > mf) & (sd == 1)

        print(f"\n{'─'*130}")
        print(f"  {code} {name}  ({dates[0].strftime('%Y-%m-%d')} ~ {dates[-1].strftime('%Y-%m-%d')}, {n}根)")
        print(f"{'─'*130}")
        print(f"  {'模型':<24} {'总收益':>10} {'年化':>8} {'最大回撤':>10} {'交易次数':>8} {'信号总数':>8} {'vsBH':>8}")
        print(f"  {'─'*80}")

        # BH
        bh_eq = close / close[0]
        bh_eq = np.where(np.isfinite(bh_eq), bh_eq, 1.0)
        bh = _perf(bh_eq, dates)
        stock_res = {"BH": bh}
        print(f"  {'买入持有':<22} {bh[0]*100:>+9.1f}% {bh[1]*100:>+7.1f}% {bh[2]*100:>9.1f}% {'—':>8} {'—':>8} {'—':>8}")

        for vid, vlabel in variants:
            if vid == "Chandelier":
                ent = entry_chan
            else:
                ent = entry
            ext = exits[vid]
            strat, n_tr = _backtest_full(close, opens, dates, fees, ent, ext)
            n_sig = int(ent.sum()) + int(ext.sum())
            stock_res[vid] = (strat[0], strat[1], strat[2], n_tr, n_sig)
            r = stock_res[vid]
            vs_bh = r[0] / bh[0] - 1.0 if bh[0] != 0 else 0
            print(f"  {vlabel:<22} {r[0]*100:>+9.1f}% {r[1]*100:>+7.1f}% {r[2]*100:>9.1f}% "
                  f"{r[3]:>8} {r[4]:>8} {vs_bh*100:>+7.1f}%")

        all_results[code] = stock_res

    # ── 汇总 ──
    print(f"\n{'='*130}")
    print("  三票平均汇总")
    print(f"{'='*130}")
    print(f"  {'模型':<24} {'均总收益':>10} {'均年化':>8} {'均回撤':>10} {'均交易':>8} {'均信号':>8} {'均vsBH':>8}")
    print(f"  {'─'*80}")

    bh_avg = sum(all_results[c]["BH"][0] for c in all_results) / len(all_results)
    for vid, vlabel in variants:
        rs = [all_results[c].get(vid, (0,0,0,0,0)) for c in all_results]
        avg_t = sum(r[0] for r in rs) / len(rs)
        avg_c = sum(r[1] for r in rs) / len(rs)
        avg_m = sum(r[2] for r in rs) / len(rs)
        avg_n = sum(r[3] for r in rs) / len(rs)
        avg_s = sum(r[4] for r in rs) / len(rs)
        vs_bh = avg_t / bh_avg - 1.0 if bh_avg != 0 else 0
        flag = "★" if vs_bh > -0.3 else " "  # 标记接近BH的模型
        print(f"{flag} {vlabel:<22} {avg_t*100:>+9.1f}% {avg_c*100:>+7.1f}% {avg_m*100:>9.1f}% "
              f"{avg_n:>8.0f} {avg_s:>8.0f} {vs_bh*100:>+7.1f}%")

    print(f"  {'买入持有':<22} {bh_avg*100:>+9.1f}%")

    # ── 逐股最优 ──
    print(f"\n{'='*130}")
    print("  逐股最优模型")
    print(f"{'='*130}")
    for code, name in load_watchlist().items():
        bh_r = all_results[code]["BH"][0]
        best_vid = max(variants, key=lambda v: all_results[code].get(v[0], (0,))[0])
        best_r = all_results[code].get(best_vid[0], (0,))[0]
        best_n = all_results[code].get(best_vid[0], (0,0,0,0,0))[3]
        best_mdd = all_results[code].get(best_vid[0], (0,0,0,0,0))[2]
        gap_pct = (best_r / bh_r - 1) * 100 if bh_r != 0 else 0
        print(f"  {code} {name}: BH={bh_r*100:+.0f}% | 最优={best_vid[1]} "
              f"{best_r*100:+.0f}% (差{gap_pct:+.0f}%, 回撤{best_mdd*100:.1f}%, {best_n}次交易)")


if __name__ == "__main__":
    main()
