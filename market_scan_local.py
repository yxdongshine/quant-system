# -*- coding: utf-8 -*-
"""基于本地已下载数据的全市场打分：读取 data/ 目录所有 CSV，计算信号和分数，取 TOP 5。"""
from __future__ import annotations
import warnings
import pandas as pd
from pathlib import Path
from config import DATA_DIR, load_params
from signals import compute_frame, current_signal
import akshare as ak

warnings.filterwarnings("ignore")
TOP_N = 5


def get_stock_names() -> dict:
    """尝试从 akshare 获取代码→名称映射，失败则返回空。"""
    try:
        df = ak.stock_info_a_code_name()
        return dict(zip(df["code"], df["name"]))
    except Exception:
        return {}


def main():
    params = load_params()
    name_map = get_stock_names()
    csv_dir = Path(DATA_DIR)
    csvs = sorted(csv_dir.glob("*.csv"))
    print(f"本地数据: {len(csvs)} 只股票 CSV")

    scored = []
    for i, csv_path in enumerate(csvs):
        code = csv_path.stem
        name = name_map.get(code, code)
        try:
            df = pd.read_csv(csv_path, index_col=0, parse_dates=True)
            if len(df) < 120:
                continue
            frame = compute_frame(df, params)
            sig = current_signal(frame, params)
            sig["code"] = code
            sig["name"] = name
            scored.append(sig)
        except Exception:
            pass
        if (i + 1) % 30 == 0:
            print(f"  已处理 {i+1}/{len(csvs)}...")

    scored.sort(key=lambda x: x.get("score", 0), reverse=True)
    top = scored[:TOP_N]

    print(f"\n{'='*60}")
    print(f"  今日趋势确认模型 TOP {TOP_N}（基于本地数据，按分数降序）")
    print(f"{'='*60}")
    for i, s in enumerate(top):
        close_s = f"{s['close']:.2f}" if isinstance(s.get('close'), (int, float)) else str(s.get('close','-'))
        print(f"\n  #{i+1}  {s['code']} {s['name']}")
        print(f"       信号: {s['action']}  分数: {s.get('score', 0)}")
        print(f"       收盘: {close_s}  仓位: {s.get('state_text', '—')}")
        print(f"       依据: {s.get('reason', '')}")

    print(f"\n{'='*60}")
    print(f"完整排行（前20 / 共{len(scored)}只）：")
    print(f"{'排名':>4} | {'分数':>4} | {'代码':>6} | {'名称':>8} | {'信号':>4} | {'收盘':>8} | 依据")
    print("-" * 90)
    for i, s in enumerate(scored[:20]):
        act = s.get("action", "?")
        close_s = f"{s['close']:.2f}" if isinstance(s.get('close'), (int, float)) else str(s.get('close','-'))
        print(f"{i+1:>4} | {s.get('score',0):>4} | {s['code']:>6} | {s['name']:>8} | {act:>4} | {close_s:>8} | {s.get('reason', '')}")

    print(f"\n推荐添加到自选的代码：")
    print(", ".join(f"{s['code']}({s['name']})" for s in top))


if __name__ == "__main__":
    main()
