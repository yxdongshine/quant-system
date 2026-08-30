# -*- coding: utf-8 -*-
"""全局配置：标的池与策略参数。

params.json 由 daily_scan 每日刷新（数据驱动的自适应入口），
config.py 里的值仅作为 params.json 不存在时的初始默认值。
"""
from pathlib import Path
import json

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
REPORT_DIR = BASE_DIR / "reports"
LOG_DIR = BASE_DIR / "logs"
PARAMS_FILE = BASE_DIR / "params.json"
WEBHOOK_FILE = BASE_DIR / "webhook.txt"   # 企业微信群机器人 Webhook（单行 URL）
WATCHLIST_FILE = BASE_DIR / "watchlist.json"   # 运行时自选池（网页可增删）
SECTOR_ROTATION_FILE = DATA_DIR / "sector_rotation.json"  # 板块轮动扫描结果

# 标的池（用户中线波段持仓风格）
WATCHLIST = {
    "300750": "宁德时代",
    "300308": "中际旭创",
    "300502": "新易盛",
}

# 板块分组（同板块股票联动：低位补涨信号）
SECTORS = {
    "光通信": ["300308", "300502", "601869", "002281", "600487", "300394", "300476"],
    "新能源电池": ["300750"],
    "覆铜板": ["600183", "002384"],
    "电子元件": ["300408", "000636"],
    "半导体设备": ["300604"],
    "存储": ["300857", "001309"],
    "医药CRO": ["603259"],
    "化工": ["002165"],
    "房地产": ["600657"],
}

# 市场标识：腾讯接口前缀（sz=深证, sh=上证）
def tx_symbol(code: str) -> str:
    return ("sh" if code.startswith(("6", "5", "9")) else "sz") + code

# ---- 策略默认参数（可被 params.json 覆盖）----
DEFAULT_PARAMS = {
    "atr_period": 14,            # ATR 周期
    "st_multiplier": 2.5,        # SuperTrend 乘数
    "ma_fast": 20,               # 短期均线（买入/卖出信号线）
    "ma_slow": 60,               # 中线多空分界线
    "chandelier_period": 22,     # 吊灯止盈回看窗口
    "chandelier_atr_mult": 4.0,  # 吊灯 ATR 乘数
    "fees": 0.001,               # 单边综合费率（佣金+印花税+滑点，保守）
    "history_start": "2018-01-01",
}


def load_params() -> dict:
    """读取 params.json；不存在则用默认值并落盘。"""
    if PARAMS_FILE.exists():
        with open(PARAMS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    params = dict(DEFAULT_PARAMS)
    save_params(params)
    return params


def save_params(params: dict) -> None:
    PARAMS_FILE.write_text(
        json.dumps(params, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_watchlist() -> dict[str, str]:
    """运行时自选池；watchlist.json 不存在时以 config 内置池初始化。"""
    if WATCHLIST_FILE.exists():
        with open(WATCHLIST_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    wl = dict(WATCHLIST)
    save_watchlist(wl)
    return wl


def save_watchlist(wl: dict[str, str]) -> None:
    WATCHLIST_FILE.write_text(
        json.dumps(wl, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_webhook() -> str:
    """企业微信群机器人 Webhook；未配置返回空串（推送自动跳过）。"""
    if WEBHOOK_FILE.exists():
        return WEBHOOK_FILE.read_text(encoding="utf-8").strip()
    return ""


def ensure_dirs() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    REPORT_DIR.mkdir(exist_ok=True)
