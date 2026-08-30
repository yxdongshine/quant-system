# -*- coding: utf-8 -*-
"""指标引擎（纯 pandas/numpy 实现，公式与 TA-Lib 等价）。

说明：本机 Python 3.14 暂无 TA-Lib/vectorbt 预编译包，
以下手写实现覆盖本系统所需的全部指标：
  - SMA / ATR(Wilder) / SuperTrend / Donchian / Chandelier Exit
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def atr(df: pd.DataFrame, n: int) -> pd.Series:
    """Wilder ATR（等价 TA-Lib ATR）。df 需含 high/low/close。"""
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [df["high"] - df["low"],
         (df["high"] - prev_close).abs(),
         (df["low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def supertrend(df: pd.DataFrame, n: int = 10, mult: float = 3.0) -> pd.DataFrame:
    """经典 SuperTrend。返回 DataFrame(st, dir)，dir=1 多头 / -1 空头。"""
    a = atr(df, n)
    mid = (df["high"] + df["low"]) / 2
    ub = mid + mult * a          # basic upper band
    lb = mid - mult * a          # basic lower band

    close = df["close"].values
    ub_v, lb_v = ub.values, lb.values
    n_bar = len(df)
    f_ub = np.full(n_bar, np.nan)   # final upper band
    f_lb = np.full(n_bar, np.nan)   # final lower band
    st = np.full(n_bar, np.nan)
    direction = np.ones(n_bar, dtype=int)  # 1 多 / -1 空

    for i in range(1, n_bar):
        # final band 递推
        f_ub[i] = min(ub_v[i], f_ub[i - 1]) if (close[i - 1] <= f_ub[i - 1] or np.isnan(f_ub[i - 1])) else ub_v[i]
        f_lb[i] = max(lb_v[i], f_lb[i - 1]) if (close[i - 1] >= f_lb[i - 1] or np.isnan(f_lb[i - 1])) else lb_v[i]

        if np.isnan(f_ub[i]):
            f_ub[i] = ub_v[i]
        if np.isnan(f_lb[i]):
            f_lb[i] = lb_v[i]

        prev_dir = direction[i - 1]
        if prev_dir == 1:
            direction[i] = -1 if close[i] < f_lb[i] else 1
        else:
            direction[i] = 1 if close[i] > f_ub[i] else -1

        st[i] = f_lb[i] if direction[i] == 1 else f_ub[i]

    return pd.DataFrame({"st": st, "dir": direction}, index=df.index)


def donchian(df: pd.DataFrame, n: int) -> pd.DataFrame:
    """唐奇安通道：upper=max(high,n), lower=min(low,n)。"""
    return pd.DataFrame(
        {"upper": df["high"].rolling(n, min_periods=n).max(),
         "lower": df["low"].rolling(n, min_periods=n).min()},
        index=df.index,
    )


def chandelier_long_stop(df: pd.DataFrame, n: int = 22, mult: float = 3.0) -> pd.Series:
    """吊灯止盈线（多头持仓）：rolling_max(high,n) - mult*ATR(n)。

    经典 Chandelier Exit 定义。滚动窗口本身实现跟踪语义：
    创新高则线跟随上移，长期回落后窗口滑出旧高点、线自然回落。
    （切勿加 cummax：会把线永远钉在历史最高点，导致永远空仓。）
    """
    a = atr(df, n)
    return df["high"].rolling(n, min_periods=n).max() - mult * a
