# -*- coding: utf-8 -*-
"""参数寻优：网格搜索 ST乘数 x 吊灯乘数，按 Calmar(年化/回撤) 三票平均选优。

定位：粗调骨架参数，防过拟合 —— 只调 2 个最敏感参数、
步长取大，最终参数需在逻辑上可解释（"吊灯应比 ST 更宽松"）。

用法：python optimize.py
"""
from __future__ import annotations

import itertools

import pandas as pd

from backtest import run
from config import ensure_dirs, load_params, load_watchlist, save_params
from datafeed import get_daily

ST_MULTS = [2.5, 3.0, 3.5, 4.0]
CHAN_MULTS = [3.0, 4.0, 5.0]


def main() -> None:
    ensure_dirs()
    base = load_params()
    data = {c: get_daily(c, refresh=False) for c in load_watchlist()}

    rows = []
    for st_m, ch_m in itertools.product(ST_MULTS, CHAN_MULTS):
        p = dict(base, st_multiplier=st_m, chandelier_atr_mult=ch_m)
        per = [run(df, p) for df in data.values()]
        avg_cagr = sum(r["strat"]["cagr"] for r in per) / len(per)
        avg_mdd = sum(r["strat"]["mdd"] for r in per) / len(per)
        avg_n = sum(r["n_trades"] for r in per) / len(per)
        calmar = avg_cagr / abs(avg_mdd) if avg_mdd != 0 else 0.0
        rows.append({"st": st_m, "chan": ch_m, "cagr": avg_cagr,
                     "mdd": avg_mdd, "calmar": calmar, "n": avg_n})
        print(f"ST={st_m} 吊灯={ch_m} | 年化{avg_cagr*100:+.1f}% "
              f"回撤{avg_mdd*100:.1f}% Calmar={calmar:.2f} 均笔数{avg_n:.0f}")

    best = max(rows, key=lambda r: r["calmar"])
    print(f"\n最优: ST={best['st']} 吊灯={best['chan']} "
          f"(年化{best['cagr']*100:+.1f}%, 回撤{best['mdd']*100:.1f}%, "
          f"Calmar={best['calmar']:.2f})")
    save_params(dict(base, st_multiplier=best["st"],
                     chandelier_atr_mult=best["chan"]))
    print("[saved] params.json 已更新")


if __name__ == "__main__":
    main()
