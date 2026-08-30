# -*- coding: utf-8 -*-
"""轻量事件回测 v3：趋势确认模型（满仓进出）。

执行模型（贴近 A 股实际）：
  - 信号 T 日收盘生成，T+1 日开盘成交；
  - 满仓进出：BUY 全仓买入，SELL 全仓卖出；
  - LOCK（吊灯线）仅作提示不参与回测；
  - 单边综合费率 fees（佣金+印花税+滑点，默认 0.1%）；
  - 对比基准：买入持有（同一天上车，持有到期末）。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from config import REPORT_DIR, ensure_dirs, load_params, load_watchlist
from datafeed import get_daily
from signals import compute_frame, signal_series


def _perf(eq: np.ndarray, dates: pd.DatetimeIndex) -> dict:
    """净值序列 -> 总收益/年化/最大回撤。"""
    eq = np.asarray(eq, dtype=float)
    total = eq[-1] / eq[0] - 1.0
    days = max((dates[-1] - dates[0]).days, 1)
    cagr = (eq[-1] / eq[0]) ** (365.0 / days) - 1.0
    peak = np.maximum.accumulate(eq)
    mdd = float((eq / peak - 1.0).min())
    return {"total": total, "cagr": cagr, "mdd": mdd}


def run(df: pd.DataFrame, p: dict) -> dict:
    """趋势确认模型回测：满仓买/卖，返回统计与交易明细。"""
    f = compute_frame(df, p)
    sig = signal_series(f, p)
    fees = float(p["fees"])
    opens, closes, dates = f["open"].values, f["close"].values, f.index

    cash = 1.0
    shares = 0.0
    equity = np.ones(len(f))
    trades: list[dict] = []
    hold = False
    entry_i: int | None = None
    entry_px = 0.0
    first_buy: int | None = None

    for i in range(1, len(f)):
        k = i - 1  # 信号来自前一根 K 线

        if sig["buy"].iloc[k] and not hold:
            shares = cash / (opens[i] * (1 + fees))
            cash = 0.0
            hold = True
            entry_i = i
            entry_px = float(opens[i])
            if first_buy is None:
                first_buy = i

        elif sig["sell"].iloc[k] and hold:
            proceeds = shares * opens[i] * (1 - fees)
            trades.append({
                "entry_date": dates[entry_i].strftime("%Y-%m-%d"),
                "exit_date": dates[i].strftime("%Y-%m-%d"),
                "entry": round(float(opens[entry_i]), 2),
                "exit": round(float(opens[i]), 2),
                "ret_pct": round((proceeds / (shares * opens[entry_i] * (1 + fees)) - 1.0) * 100, 2),
                "hold_days": int((dates[i] - dates[entry_i]).days),
            })
            cash = proceeds
            shares = 0.0
            hold = False
            entry_i = None

        equity[i] = cash + shares * closes[i]

    # 期末仍持仓：虚拟平仓
    if hold and entry_i is not None:
        trades.append({
            "entry_date": dates[entry_i].strftime("%Y-%m-%d"),
            "exit_date": dates[-1].strftime("%Y-%m-%d") + "(未平)",
            "entry": round(float(opens[entry_i]), 2),
            "exit": round(float(closes[-1]), 2),
            "ret_pct": round(
                (shares * closes[-1] * (1 - fees)
                 / (shares * opens[entry_i] * (1 + fees)) - 1.0) * 100, 2),
            "hold_days": int((dates[-1] - dates[entry_i]).days),
        })

    strat = _perf(equity, dates)

    # 买入持有基准
    if first_buy is not None:
        base = opens[first_buy] * (1 + fees)
        bh_eq = np.full(len(f), np.nan)
        bh_eq[first_buy:] = closes[first_buy:] * (1 - fees) / base
        bh = _perf(bh_eq[first_buy:], dates[first_buy:])
    else:
        bh = {"total": 0.0, "cagr": 0.0, "mdd": 0.0}

    closed = [t for t in trades if "(未平)" not in t["exit_date"]]
    wins = [t for t in closed if t["ret_pct"] > 0]

    # 净值序列（网页图表用）
    bh_eq_list = (bh_eq[first_buy:].tolist()
                  if first_buy is not None else [1.0])
    eq_dates = ([d.strftime("%Y-%m-%d") for d in dates[first_buy:]]
                if first_buy is not None
                else [dates[-1].strftime("%Y-%m-%d")])

    # K线标记
    marks = []
    for i in range(len(f)):
        if sig["buy"].iloc[i]:
            marks.append({
                "coord": [f.index[i].strftime("%Y-%m-%d"),
                          round(float(f["close"].iloc[i]), 2)],
                "value": "买", "itemStyle": {"color": "#2980b9"},
            })
        if sig["sell"].iloc[i]:
            marks.append({
                "coord": [f.index[i].strftime("%Y-%m-%d"),
                          round(float(f["close"].iloc[i]), 2)],
                "value": "卖", "itemStyle": {"color": "#e74c3c"},
            })
        if sig["lock"].iloc[i] and sig["state"].iloc[i] == 1:
            marks.append({
                "coord": [f.index[i].strftime("%Y-%m-%d"),
                          round(float(f["close"].iloc[i]), 2)],
                "value": "锁", "itemStyle": {"color": "#f39c12"},
            })

    return {
        "n_bars": len(f),
        "start": dates[first_buy if first_buy is not None else 0].strftime("%Y-%m-%d"),
        "end": dates[-1].strftime("%Y-%m-%d"),
        "strat": strat,
        "bh": bh,
        "equity": equity[first_buy:].tolist() if first_buy is not None else [1.0],
        "bh_equity": bh_eq_list,
        "eq_dates": eq_dates,
        "n_trades": len(trades),
        "win_rate": (len(wins) / len(closed)) if closed else 0.0,
        "avg_hold": (float(np.mean([t["hold_days"] for t in closed]))
                     if closed else 0.0),
        "avg_ret": (float(np.mean([t["ret_pct"] for t in trades]))
                    if trades else 0.0),
        "trades": trades,
        "marks": marks,
    }


def main() -> None:
    ensure_dirs()
    p = load_params()

    lines: list[str] = [
        "# 回测报告：趋势确认模型 vs 买入持有",
        "",
        f"- 参数：ATR={p['atr_period']} ST乘数={p['st_multiplier']} "
        f"MA{p['ma_fast']}/{p['ma_slow']} 吊灯={p['chandelier_period']}x{p['chandelier_atr_mult']}",
        f"- 模型：close>MA20 AND >MA60 买入；close<MA20 AND MA20下行 卖出",
        f"- 费率：单边 {p['fees']*100:.2f}%；信号收盘生成、次日开盘执行",
        "",
        "| 代码 | 名称 | 区间 | BH总收益 | 策略总收益 | BH年化 | 策略年化 |"
        " BH最大回撤 | 策略最大回撤 | 腿数 | 胜率 | 平均持仓 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    console = [""]

    for code, name in load_watchlist().items():
        df = get_daily(code, refresh=False)
        r = run(df, p)
        row = (f"| {code} | {name} | {r['start']}~{r['end']} "
               f"| {r['bh']['total']*100:+.1f}% | {r['strat']['total']*100:+.1f}% "
               f"| {r['bh']['cagr']*100:+.1f}% | {r['strat']['cagr']*100:+.1f}% "
               f"| {r['bh']['mdd']*100:.1f}% | {r['strat']['mdd']*100:.1f}% "
               f"| {r['n_trades']} | {r['win_rate']*100:.0f}% | {r['avg_hold']:.0f}天 |")
        lines.append(row)
        console.append(
            f"{code} {name}: BH {r['bh']['total']*100:+.1f}% vs 策略 {r['strat']['total']*100:+.1f}%"
            f" | 回撤 BH {r['bh']['mdd']*100:.1f}% -> 策略 {r['strat']['mdd']*100:.1f}%"
            f" | {r['n_trades']}笔 胜率{r['win_rate']*100:.0f}%"
        )

        lines += ["", f"## {code} {name} 交易明细", "",
                  "| 入场日 | 出场日 | 买价 | 卖价 | 收益% | 持仓天 |",
                  "|---|---|---|---|---|---|"]
        for t in r["trades"][-15:]:
            lines.append(
                f"| {t['entry_date']} | {t['exit_date']} | {t['entry']} "
                f"| {t['exit']} | {t['ret_pct']:+.2f} | {t['hold_days']} |")

    out = REPORT_DIR / f"backtest_{pd.Timestamp.today():%Y-%m-%d}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    console.append(f"[saved] {out}")
    print("\n".join(console))


if __name__ == "__main__":
    main()
