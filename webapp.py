# -*- coding: utf-8 -*-
"""持仓纪律系统 v3 - Web 端（Flask 单文件 + ECharts CDN）。

趋势确认模型（FastStrict）：趋势确认进出，MA20 确认出场 + 板块联动补涨。
  BUY   买入 —— close > MA20 AND close > MA60
  SELL  卖出 —— close < MA20 AND MA20 下行
  LOCK  锁利 —— close < 吊灯止盈线（提示卖半仓）
  PIVOT 补涨 —— 同板块≥50%已持仓，空仓股提前关注入场
  HOLD  持有 —— 什么都不做
  WAIT  等待 —— 空仓中

页面：
  /               总览：信号列表（含板块联动）+ 实时行情 + 全市场扫描
  /stock/<code>   个股：K线 + MA20/60 + 吊灯线 + 买卖点标记
"""
from __future__ import annotations

import json
import re
import time

from flask import Flask, abort, jsonify, render_template_string, request

import pandas as pd

from backtest import run
from config import (DATA_DIR, SECTORS, ensure_dirs, load_params, load_watchlist,
                    save_watchlist)
from datafeed import get_daily, get_quotes, search_stock
from signals import compute_frame, current_signal, sector_boost
from prediction import load_predictions, load_accuracy
from sector_rotation import load_sector_result
from pair_scan import load_pair_result, scan_pair_numbers

app = Flask(__name__)


@app.after_request
def _no_cache(resp):
    """防止浏览器缓存 HTML 页面（API 数据无需缓存）。"""
    if resp.content_type and "text/html" in resp.content_type:
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    return resp


_cache: dict[str, dict] = {}

ACTION_STYLE = {
    "SELL": ("卖出", "#e74c3c", "🔴"),
    "LOCK": ("锁利", "#f39c12", "🟠"),
    "BUY":  ("买入", "#2980b9", "🔵"),
    "PIVOT":("补涨", "#e67e22", "🟡"),
    "HOLD": ("持有", "#27ae60", "🟢"),
    "WAIT": ("等待", "#6b7280", "⚪"),
}


def get_stock(code: str) -> dict:
    if code not in load_watchlist():
        abort(404)
    mtime = (DATA_DIR / f"{code}.csv").stat().st_mtime
    hit = _cache.get(code)
    if hit and hit["mtime"] == mtime:
        return hit
    p = load_params()
    df = get_daily(code, refresh=False)
    frame = compute_frame(df, p)
    item = {
        "mtime": mtime,
        "frame": frame,
        "sig": current_signal(frame, p),
        "bt": run(df, p),
    }
    _cache[code] = item
    return item


def _f2(v) -> float | None:
    return round(float(v), 2) if pd.notna(v) else None


# ---------------- API ----------------

@app.get("/api/overview")
def api_overview() -> dict:
    p = load_params()
    rows, bt_map = [], {}
    for code, name in load_watchlist().items():
        s = get_stock(code)
        bt = s["bt"]
        sig = dict(s["sig"])           # copy（避免污染缓存）
        sig["code"], sig["name"] = code, name
        rows.append(sig)
        bt_map[code] = bt
    # 板块联动加分
    rows = sector_boost(rows, SECTORS)
    preds = load_predictions()
    pred_stocks = preds.get("stocks", {}) if preds else {}
    acc_data = load_accuracy()
    stocks = []
    for r in rows:
        code = r["code"]
        bt = bt_map[code]
        stocks.append({
            "code": code, "name": r["name"], "sig": r,
            "eq_dates": bt["eq_dates"], "equity": bt["equity"],
            "bh_equity": bt["bh_equity"],
            "pred": pred_stocks.get(code, {}),
            "acc": acc_data.get(code, {"buy_correct": 0, "buy_fail": 0,
                                       "sell_correct": 0, "sell_fail": 0}),
        })
    return {"params": p, "stocks": stocks,
            "pred_date": preds.get("date", "") if preds else ""}


@app.get("/api/kline/<code>")
def api_kline(code: str, bars: int = 480):
    s = get_stock(code)
    f = s["frame"].tail(bars)
    dates = [d.strftime("%Y-%m-%d") for d in f.index]

    def col(c: str):
        return [None if pd.isna(v) else _f2(v) for v in f[c]]

    return jsonify({
        "code": code, "name": load_watchlist().get(code, code), "dates": dates,
        "ohlc": [[_f2(r.open), _f2(r.close), _f2(r.low), _f2(r.high)]
                 for r in f.itertuples()],
        "ma_fast": col("ma_fast"), "ma_slow": col("ma_slow"),
        "st": col("st"), "chandelier": col("chandelier"),
        "marks": s["bt"]["marks"], "sig": s["sig"], "bt": {
            "total": s["bt"]["strat"]["total"], "mdd": s["bt"]["strat"]["mdd"],
            "bh_total": s["bt"]["bh"]["total"], "bh_mdd": s["bt"]["bh"]["mdd"],
            "n_trades": s["bt"]["n_trades"], "win_rate": s["bt"]["win_rate"],
        }, "trades": s["bt"]["trades"][-15:][::-1],
    })


# ---------------- 自选池管理 ----------------

@app.get("/api/search")
def api_search() -> dict:
    q = (request.args.get("q") or "").strip()
    try:
        return jsonify({"results": search_stock(q)})
    except Exception as e:
        return jsonify({"results": [], "msg": str(e)[:120]})


@app.get("/api/quote")
def api_quote() -> dict:
    try:
        return jsonify({"quotes": get_quotes(list(load_watchlist().keys()))})
    except Exception as e:
        return jsonify({"quotes": {}, "msg": str(e)[:120]})


@app.post("/api/watchlist")
def api_add_stock() -> dict:
    d = request.get_json(force=True, silent=True) or {}
    code = str(d.get("code", "")).strip()
    name = str(d.get("name", "")).strip()[:16]
    if not re.fullmatch(r"\d{6}", code):
        return jsonify({"ok": False, "msg": "代码须为6位数字"})
    wl = load_watchlist()
    if code in wl:
        return jsonify({"ok": False, "msg": "已在自选中"})
    try:
        df = get_daily(code)
    except Exception as e:
        return jsonify({"ok": False, "msg": f"数据拉取失败: {str(e)[:80]}"})
    if len(df) < 80:
        return jsonify({"ok": False, "msg": "上市时间过短（历史不足80根K线）"})
    wl[code] = name or code
    save_watchlist(wl)
    _cache.pop(code, None)
    return jsonify({"ok": True, "code": code, "name": wl[code]})


@app.delete("/api/watchlist/<code>")
def api_del_stock(code: str) -> dict:
    wl = load_watchlist()
    if code not in wl:
        return jsonify({"ok": False, "msg": "不在自选中"})
    del wl[code]
    save_watchlist(wl)
    _cache.pop(code, None)
    return jsonify({"ok": True})


# ---------------- 全市场扫描 ----------------

_name_cache: dict = {}


def _load_names() -> dict:
    """加载股票代码→名称映射（缓存到 data/names.json）。"""
    global _name_cache
    if _name_cache:
        return _name_cache
    nf = DATA_DIR / "names.json"
    if nf.exists():
        try:
            _name_cache = json.loads(nf.read_text("utf-8"))
            return _name_cache
        except Exception:
            pass
    try:
        import akshare as ak
        df = ak.stock_info_a_code_name()
        _name_cache = dict(zip(df["code"], df["name"]))
        nf.write_text(json.dumps(_name_cache, ensure_ascii=False), "utf-8")
    except Exception:
        _name_cache = {}
    return _name_cache


_scan_cache: dict = {}


SCAN_TOP_N = 5   # 全市场扫描只返回 TOP N

@app.get("/api/market_scan")
def api_market_scan() -> dict:
    """全市场扫描：读取 data/ 下所有 CSV，计算信号并按分数排序，只返回 TOP N。"""
    refresh = request.args.get("refresh", "0") == "1"
    csvs = sorted(DATA_DIR.glob("*.csv"))
    mtime_key = "|".join(f"{c.stem}:{int(c.stat().st_mtime)}" for c in csvs)
    if not refresh and _scan_cache.get("key") == mtime_key:
        wl = load_watchlist()
        for s in _scan_cache["result"]:
            s["in_wl"] = s["code"] in wl
        return {"total": len(_scan_cache["result"]),
                "stocks": _scan_cache["result"],
                "scan_time": _scan_cache.get("time", "")}

    p = load_params()
    wl = load_watchlist()
    names = _load_names()
    preds = load_predictions()
    pred_stocks = preds.get("stocks", {}) if preds else {}
    acc_data = load_accuracy()
    scored = []
    for csv_path in csvs:
        code = csv_path.stem
        try:
            df = pd.read_csv(csv_path, index_col=0, parse_dates=True)
            if len(df) < 120:
                continue
            frame = compute_frame(df, p)
            sig = dict(current_signal(frame, p))
            sig["code"] = code
            sig["name"] = names.get(code, code)
            sig["in_wl"] = code in wl
            # 精细排序分：主排序=整数分数，副排序=距MA20百分比
            close = sig.get("close") or 0
            ma_fast = sig.get("ma_fast")
            pct = ((close / ma_fast) - 1) * 100 if ma_fast and ma_fast > 0 else 0
            sig["pct_from_ma20"] = round(pct, 2)
            sig["sort_score"] = sig.get("score", 0) + pct * 0.001
            pred_info = pred_stocks.get(code, {})
            sig["pullback"] = pred_info.get("pullback")
            sig["surge"] = pred_info.get("surge")
            acc = acc_data.get(code, {})
            sig["buy_correct"] = acc.get("buy_correct", 0)
            sig["buy_fail"] = acc.get("buy_fail", 0)
            sig["sell_correct"] = acc.get("sell_correct", 0)
            sig["sell_fail"] = acc.get("sell_fail", 0)
            scored.append(sig)
        except Exception:
            pass
    scored.sort(key=lambda x: x.get("sort_score", 0), reverse=True)
    top = scored[:SCAN_TOP_N]
    scan_time = time.strftime("%Y-%m-%d %H:%M")
    _scan_cache.update({"key": mtime_key, "result": top, "time": scan_time})
    return {"total": len(top), "stocks": top, "scan_time": scan_time}


@app.get("/api/pair_scan")
def api_pair_scan() -> dict:
    """对子数扫描：返回上次扫描结果，refresh=1 时重新扫描。"""
    if request.args.get("refresh") == "1":
        return scan_pair_numbers()
    return load_pair_result()


@app.get("/api/sector_rotation")
def api_sector_rotation() -> dict:
    """板块轮动：返回上次扫描的推荐结果（冷门+低估值+龙头）+ 动态收益。"""
    data = load_sector_result()
    # 动态计算每只龙头的当前收益（基于最新 CSV 收盘价 vs entry_price）
    for rec in data.get("recommendations", []):
        for l in rec.get("leaders", []):
            ep = l.get("entry_price")
            if ep and ep > 0:
                code = l.get("code", "")
                csv_path = DATA_DIR / f"{code}.csv"
                try:
                    if csv_path.exists():
                        df = pd.read_csv(csv_path, index_col=0, parse_dates=True)
                        if len(df) > 0:
                            cur_close = float(df["close"].iloc[-1])
                            l["current_price"] = round(cur_close, 3)
                            l["hold_return"] = round((cur_close / ep - 1) * 100, 2)
                except Exception:
                    pass
    return data


# ---------------- 页面 ----------------

BASE_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: #14161f; color: #d8dce6; font-family: "Microsoft YaHei",
       "PingFang SC", sans-serif; padding: 20px; }
a { color: #6ea8fe; text-decoration: none; }
.wrap { max-width: 1280px; margin: 0 auto; }
h1 { font-size: 22px; margin-bottom: 4px; }
h2 { font-size: 16px; margin: 26px 0 12px; color: #9aa3b5; }
.sub { color: #6b7280; font-size: 13px; margin-bottom: 18px; }
.cards { display: flex; gap: 16px; flex-wrap: wrap; }
.card { flex: 1 1 300px; background: #1d2130; border-radius: 12px; padding: 18px;
        border-top: 3px solid #3b4253; }
.card .t { font-size: 15px; color: #9aa3b5; }
.card .rt { font-size: 13px; margin-top: 8px; color: #8a93a6; }
.card .px { font-size: 26px; font-weight: 700; margin: 6px 0; }
.card .act { display: inline-block; padding: 3px 14px; border-radius: 6px;
             font-weight: 700; font-size: 15px; color: #fff; }
.card .why { margin-top: 10px; font-size: 13px; color: #8a93a6; line-height: 1.6; }
.card .st { font-size: 12px; color: #6b7280; margin-left: 6px; }
.chart { width: 100%; height: 320px; background: #1d2130; border-radius: 12px;
         padding: 6px; }
.grid3 { display: grid; grid-template-columns: repeat(auto-fit, minmax(340px, 1fr));
         gap: 16px; }
table { border-collapse: collapse; width: 100%; font-size: 13px;
        background: #1d2130; border-radius: 12px; overflow: hidden; }
th, td { padding: 9px 12px; text-align: right; border-bottom: 1px solid #262b3b; }
th { background: #232839; color: #9aa3b5; font-weight: 600; }
td:first-child, th:first-child { text-align: left; }
.good { color: #27ae60; } .bad { color: #e74c3c; } .warn { color: #f39c12; }
.kline { width: 100%; height: 560px; background: #1d2130; border-radius: 12px;
         padding: 6px; }
.pager { text-align: center; margin-top: 14px; }
.pager button { background: #2a3045; color: #d8dce6; border: 1px solid #3b4253;
  border-radius: 4px; padding: 5px 12px; margin: 0 3px; cursor: pointer; font-size: 13px; }
.pager button:hover:not(:disabled) { background: #3a4258; }
.pager button:disabled { opacity: .35; cursor: default; }
.pager button.cur { background: #2980b9; border-color: #2980b9; color: #fff; font-weight: 700; }
.pager .pg-info { margin-left: 10px; color: #6b7280; font-size: 13px; }
.tabs { display: flex; gap: 0; margin: 20px 0 16px; border-bottom: 2px solid #262b3b; }
.tab { padding: 10px 24px; cursor: pointer; font-size: 15px; color: #6b7280;
       border-bottom: 2px solid transparent; margin-bottom: -2px; transition: all .2s; }
.tab:hover { color: #d8dce6; }
.tab.active { color: #6ea8fe; border-bottom-color: #6ea8fe; font-weight: 600; }
.tab .badge { display: inline-block; background: #2a3045; color: #9aa3b5;
              border-radius: 10px; padding: 1px 8px; font-size: 12px; margin-left: 6px; }
.scan-bar { display: flex; align-items: center; justify-content: space-between;
            margin-bottom: 12px; }
.scan-bar .info { color: #6b7280; font-size: 13px; }
.scan-bar button { background: #2d5af5; color: #fff; border: 0; border-radius: 6px;
                   padding: 6px 16px; font-size: 13px; cursor: pointer; }
.scan-bar button:hover { background: #4a72f7; }
.scan-add { background: #2d5af5; color: #fff; border: 0; border-radius: 4px;
            padding: 3px 10px; font-size: 12px; cursor: pointer; white-space: nowrap; }
.scan-add:hover { background: #4a72f7; }
.scan-add.added { background: #27ae60; cursor: default; }
.loading { text-align: center; padding: 40px; color: #6b7280; font-size: 15px; }
.bar { display: flex; gap: 8px; position: relative; margin-bottom: 16px;
       max-width: 560px; }
.bar input { flex: 1; background: #1d2130; border: 1px solid #3b4253;
             border-radius: 8px; padding: 9px 12px; color: #d8dce6;
             font-size: 14px; outline: none; }
.bar input:focus { border-color: #6ea8fe; }
.bar button { background: #2d5af5; color: #fff; border: 0; border-radius: 8px;
              padding: 9px 18px; font-size: 14px; cursor: pointer; }
.bar button:hover { background: #4a72f7; }
#hint { position: absolute; top: 44px; left: 0; right: 0; z-index: 9;
        background: #232839; border: 1px solid #3b4253; border-radius: 8px;
        box-shadow: 0 8px 24px rgba(0,0,0,.45); display: none; }
#hint .it { padding: 9px 14px; font-size: 14px; cursor: pointer;
            border-bottom: 1px solid #2b3042; }
#hint .it:hover { background: #2b3350; }
#hint .mkt { color: #6b7280; font-size: 12px; margin-left: 8px; }
.del { float: right; color: #6b7280; cursor: pointer; margin-left: 10px;
       font-size: 15px; }
.del:hover { color: #e74c3c; }
.pred-pull { color: #2980b9; font-weight: 600; }
.pred-surge { color: #e67e22; font-weight: 600; }
.pred-none { color: #6b7280; font-size: 12px; }
"""

ECHARTS_JS = """
<script src="https://cdn.staticfile.net/echarts/5.4.3/echarts.min.js"
        onerror="var s=document.createElement('script');
                 s.src='https://cdn.jsdelivr.net/npm/echarts@5.4.3/dist/echarts.min.js';
                 document.head.appendChild(s)"></script>
"""

OVERVIEW_TPL = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>持仓纪律系统</title><style>{BASE_CSS}</style>
</head><body><div class="wrap">
<h1>持仓纪律系统 · 总览</h1>
<p class="sub">趋势确认模型：站上20日+60日线<b>买入持仓</b> → 跌破20日线<b>且均线下行</b>清仓 → 触吊灯线<b>锁利提示</b> → 空仓时<b>等待</b>。
信号基于收盘数据（截至 <b id="d"></b>）；实时价每30秒刷新 · 参数 <span id="prm"></span> · 预测 <span id="pd" style="color:#6b7280">—</span></p>
<div class="bar">
  <input id="q" placeholder="添加自选：名称 / 代码 / 拼音首字母"
         onkeydown="if(event.key==='Enter')doSearch()">
  <button onclick="doSearch()">搜索</button>
  <div id="hint"></div>
</div>
<div class="tabs">
  <div class="tab active" onclick="switchTab('pair',event)">对子数 <span class="badge" id="pair-count"></span></div>
  <div class="tab" onclick="switchTab('watchlist',event)">自选股 <span class="badge" id="wl-count"></span></div>
  <div class="tab" onclick="switchTab('scan',event)">全市场TOP5 <span class="badge" id="scan-count"></span></div>
  <div class="tab" onclick="switchTab('rotation',event)">板块轮动 <span class="badge" id="rot-count"></span></div>
</div>
<div id="tab-pair">
  <div class="scan-bar">
    <span class="info" id="pair-info">点击「对子数」标签加载数据</span>
    <button onclick="loadPairScan(true)">重新扫描</button>
  </div>
  <div style="font-size:11px;color:#8a93a6;margin:6px 0 10px;display:flex;gap:16px;flex-wrap:wrap">
    <span><b style="color:#f0b90b">●</b> AAA 全对 <small>abb.bb</small></span>
    <span><b style="color:#00d4aa">●</b> AA+ 双对 <small>abb.cc</small></span>
    <span><b style="color:#6ea8fe">●</b> AB 镜像 <small>ab.ab</small></span>
  </div>
  <h2 style="color:#f0b90b">★ 强支撑对子 <span style="font-size:13px;color:#6b7280;font-weight:400">（对子底出现后未跌破 → 主力强支撑，重点信号）</span></h2>
  <div id="tbl-strong-pairs"><div class="loading">点击标签加载数据...</div></div>
  <h2>今日对子底 <span style="font-size:13px;color:#6b7280">（最低价出现对子数 → 主力底部精准控价）</span></h2>
  <div id="tbl-pair-bottom"><div class="loading">点击标签加载数据...</div></div>
  <h2>今日对子顶 <span style="font-size:13px;color:#6b7280">（最高价出现对子数 → 主力顶部精准出货）</span></h2>
  <div id="tbl-pair-top"><div class="loading">点击标签加载数据...</div></div>
</div>
<div id="tab-watchlist" style="display:none">
  <h2>信号列表（按分数降序）</h2>
  <div id="tbl-sig"></div>
</div>
<div id="tab-scan" style="display:none">
  <div class="scan-bar">
    <span class="info" id="scan-info">点击「全市场TOP5」标签开始扫描</span>
    <button onclick="loadScan(true)">重新扫描</button>
  </div>
  <div id="tbl-scan"><div class="loading">点击标签加载数据...</div></div>
</div>
<div id="tab-rotation" style="display:none">
  <div class="scan-bar">
    <span class="info" id="rot-info">点击「板块轮动」标签加载推荐</span>
    <button onclick="loadRotation(true)">刷新数据</button>
  </div>
  <div id="tbl-rotation"><div class="loading">点击标签加载数据...</div></div>
</div>
<h2>操作守则</h2>
<p class="sub">
1) <b>买入</b>（持仓）：收盘站上 20 日线且站上 60 日线 → 次日开盘买入持仓。<br>
2) <b>卖出</b>（清仓）：收盘跌破 20 日线<b>且 20 日线下行</b> → 次日开盘全部卖出。<br>
3) <b>锁利提示</b>：触及吊灯止盈线 → 可考虑卖出半仓锁定利润，剩余待卖出信号。<br>
4) <b>补涨</b>（板块联动）：同板块 ≥ 50% 已持仓，空仓股可提前关注入场，不必等站上 60 日线追高。<br>
5) <b>等待</b>（空仓）：未满足买入条件时保持空仓，不追高、不抄底。<br>
6) 回调中 20 日线仍上行时<b>持有不动</b>——假突破不卖飞，趋势确认才离场。
</p>
</div>
<script>
const C = {{SELL:"#e74c3c", LOCK:"#f39c12", BUY:"#2980b9", PIVOT:"#e67e22", HOLD:"#27ae60", WAIT:"#6b7280"}};
const L = {{SELL:"卖出", LOCK:"锁利", BUY:"买入", PIVOT:"补涨", HOLD:"持有", WAIT:"等待"}};
function fmtPct(v){{ return (v>=0?"+":"") + (v*100).toFixed(1) + "%"; }}
async function loadQuotes(){{
  try{{
    const qs = (await fetch("/api/quote").then(x=>x.json())).quotes;
    for(const c in qs){{
      const q = qs[c], el = document.getElementById("rt-"+c);
      if(el) el.innerHTML = `实时 <b>${{q.price.toFixed(2)}}</b> `+
        `<span style="color:${{q.change_pct>=0?'#e74c3c':'#27ae60'}}">`+
        `${{q.change_pct>=0?'▲':'▼'}}${{Math.abs(q.change_pct).toFixed(2)}}%</span>`+
        ` <span style="font-size:11px;color:#5b6478">${{q.time_str}}</span>`;
    }}
  }}catch(e){{}}
}}
loadQuotes(); setInterval(loadQuotes, 30000);
async function doSearch(){{
  const q = document.getElementById("q").value.trim();
  const h = document.getElementById("hint");
  if(!q) return;
  h.style.display = "block"; h.innerHTML = "<div class='it'>搜索中...</div>";
  const r = await fetch("/api/search?q=" + encodeURIComponent(q)).then(x=>x.json());
  if(!r.results.length){{ h.innerHTML = "<div class='it'>无匹配的A股</div>"; return; }}
  h.innerHTML = r.results.map(s =>
    `<div class="it" onclick="addStock('${{s.code}}','${{s.name}}')">${{s.code}} ${{s.name}}<span class="mkt">${{s.market.toUpperCase()}}</span></div>`).join("");
}}
async function addStock(code, name){{
  const h = document.getElementById("hint");
  if(!confirm("添加 " + code + " " + name + " 到自选？")) return;
  h.innerHTML = "<div class='it'>拉取历史数据中（约5~10秒）...</div>";
  const r = await fetch("/api/watchlist", {{method:"POST",
    headers:{{"Content-Type":"application/json"}},
    body: JSON.stringify({{code, name}})}}).then(x=>x.json());
  if(r.ok) location.reload();
  else h.innerHTML = "<div class='it'>失败：" + r.msg + "</div>";
}}
async function delStock(code, name){{
  if(!confirm("从自选移除 " + code + " " + name + "？")) return;
  const r = await fetch("/api/watchlist/" + code, {{method:"DELETE"}}).then(x=>x.json());
  if(r.ok) location.reload(); else alert(r.msg);
}}
document.addEventListener("click", e=>{{
  if(!e.target.closest(".bar")) document.getElementById("hint").style.display = "none";
}});

// ── 自选股分页 ──
let ALL_STOCKS = [];
let CUR_PAGE = 1;
const PAGE_SIZE = 10;

function renderPage(page){{
  const total = ALL_STOCKS.length;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  CUR_PAGE = Math.max(1, Math.min(page, pages));
  const start = (CUR_PAGE - 1) * PAGE_SIZE;
  const slice = ALL_STOCKS.slice(start, start + PAGE_SIZE);
  let html = `<table>
    <tr><th>分数</th><th>代码</th><th>名称</th><th>板块</th><th>收盘</th><th>实时</th>
      <th>回调买入</th><th>冲高卖出</th><th>买对</th><th>买错</th><th>卖对</th><th>卖错</th>
      <th>动作</th><th>仓位</th><th>依据</th><th></th></tr>` +
  slice.map(s=>{{
    const a = s.sig.action, c = C[a]||"#888";
    const sc = s.sig.score||0, scColor = sc>=70?'#27ae60':sc>=40?'#f0b90b':'#6b7280';
    const sec = s.sig.sector || '—';
    const pull = s.pred && s.pred.pullback;
    const surge = s.pred && s.pred.surge;
    const acc = s.acc || {{}};
    const bc = acc.buy_correct || 0;
    const bf = acc.buy_fail || 0;
    const sc2 = acc.sell_correct || 0;
    const sf = acc.sell_fail || 0;
    return `<tr>
      <td style="font-weight:700;color:${{scColor}}">${{sc}}</td>
      <td><a href="/stock/${{s.code}}" style="color:#6ea8fe">${{s.code}}</a></td>
      <td>${{s.name}}</td>
      <td>${{sec}}</td>
      <td>${{s.sig.close}}</td>
      <td id="rt-${{s.code}}" style="font-size:12px;color:#6b7280">—</td>
      <td>${{pull!=null ? '<span class="pred-pull">'+pull.toFixed(2)+'</span>' : '<span class="pred-none">—</span>'}}</td>
      <td>${{surge!=null ? '<span class="pred-surge">'+surge.toFixed(2)+'</span>' : '<span class="pred-none">—</span>'}}</td>
      <td style="color:#27ae60;font-weight:700">${{bc}}</td>
      <td style="color:#e74c3c;font-weight:700">${{bf}}</td>
      <td style="color:#27ae60;font-weight:700">${{sc2}}</td>
      <td style="color:#e74c3c;font-weight:700">${{sf}}</td>
      <td><span class="act" style="background:${{c}}">${{L[a]||a}}</span></td>
      <td>${{s.sig.state_text || '—'}}</td>
      <td style="text-align:left;font-size:12px;color:#8a93a6;max-width:320px">${{s.sig.reason}}</td>
      <td><span class="del" onclick="delStock('${{s.code}}','${{s.name}}')">×</span></td>
    </tr>`;}}).join("") + `</table>`;
  if(pages > 1){{
    html += `<div class="pager"><button onclick="renderPage(CUR_PAGE-1)"` +
      (CUR_PAGE<=1?' disabled':'')+ `>上一页</button>`;
    for(let i=1;i<=pages;i++){{
      html += `<button onclick="renderPage(${{i}})"` +
        (i===CUR_PAGE?' class="cur"':'')+ `>${{i}}</button>`;
    }}
    html += `<button onclick="renderPage(CUR_PAGE+1)"` +
      (CUR_PAGE>=pages?' disabled':'')+ `>下一页</button>` +
      `<span class="pg-info">第 ${{CUR_PAGE}}/${{pages}} 页 · 共 ${{total}} 只</span></div>`;
  }}
  document.getElementById("tbl-sig").innerHTML = html;
  loadQuotes();
}}

// ── 对子数扫描 ──
let PAIR_DATA = null;
let PAIR_LOADED = false;

async function loadPairScan(force){{
  const elB = document.getElementById('tbl-pair-bottom');
  const elT = document.getElementById('tbl-pair-top');
  const info = document.getElementById('pair-info');
  if(force){{
    elB.innerHTML = '<div class="loading">扫描全市场+历史验证中（约1~3分钟）...</div>';
    elT.innerHTML = '';
    document.getElementById('tbl-strong-pairs').innerHTML = '';
    info.textContent = '扫描中...';
  }}
  try{{
    const url = '/api/pair_scan' + (force?'?refresh=1':'');
    const d = await fetch(url).then(x=>x.json());
    PAIR_DATA = d;
    PAIR_LOADED = true;
    const sc = d.strong_pair_count||0;
    const bc = d.pair_bottom_count||0;
    const tc = d.pair_top_count||0;
    document.getElementById('pair-count').textContent = sc || (bc + tc);
    info.textContent = '★强支撑 ' + sc + ' 只 · 对子底 ' + bc + ' 只 · 对子顶 ' + tc + ' 只 · 扫描 ' + (d.total_stocks||0) + ' 只 · ' + (d.scan_time||'—');
    renderPairScan();
  }}catch(e){{
    elB.innerHTML = '<div class="loading">加载失败: '+e.message+'</div>';
    info.textContent = '加载失败';
  }}
}}

function renderPairScan(){{
  if(!PAIR_DATA){{
    document.getElementById('tbl-strong-pairs').innerHTML = '<div class="loading">暂无数据</div>';
    document.getElementById('tbl-pair-bottom').innerHTML = '<div class="loading">暂无数据</div>';
    return;
  }}
  const strongs = PAIR_DATA.strong_pairs || [];
  const bottoms = PAIR_DATA.pair_bottoms || [];
  const tops = PAIR_DATA.pair_tops || [];
  const lvMap = {{
    'AAA': {{color:'#f0b90b', bg:'background:rgba(240,185,11,0.15)', label:'全对'}},
    'AA+': {{color:'#00d4aa', bg:'background:rgba(0,212,170,0.12)', label:'双对'}},
    'AB':  {{color:'#6ea8fe', bg:'background:rgba(110,168,254,0.12)', label:'镜像'}},
  }};

  // ── 强支撑对子（重点信号） ──
  const strongHeader = `<tr><th>#</th><th>代码</th><th>名称</th><th>支撑价</th><th>类型</th><th style="color:#f0b90b">未破天数</th><th>首次出现</th>
    <th>现价</th><th>涨跌幅</th><th>换手率</th><th>成交额(亿)</th><th>操作</th></tr>`;
  function strongRowHtml(s, i){{
    const lv = s.pair_level||'';
    const lvInfo = lvMap[lv] || {{color:'#8a93a6', bg:'', label:''}};
    const ud = s.unbroken_days||0;
    const udColor = ud>=5 ? '#f0b90b' : ud>=3 ? '#e67e22' : '#8a93a6';
    const udBg = ud>=5 ? 'background:rgba(240,185,11,0.12)' : '';
    const fd = s.first_pair_date||'—';
    const pp = s.pair_price!=null ? s.pair_price.toFixed(2) : '—';
    const close = s.price!=null ? s.price.toFixed(2) : '—';
    const chg = s.change_pct!=null ? (s.change_pct>=0?'+':'')+s.change_pct.toFixed(2)+'%' : '—';
    const chgColor = s.change_pct!=null && s.change_pct>=0 ? '#e74c3c' : '#27ae60';
    const turnover = s.turnover!=null ? s.turnover.toFixed(2)+'%' : '—';
    const amt = s.amount!=null ? (s.amount/1e8).toFixed(2) : '—';
    return `<tr>
      <td style="color:#6b7280;font-weight:700">${{i+1}}</td>
      <td><a href="/stock/${{s.code}}" style="color:#6ea8fe">${{s.code}}</a></td>
      <td style="font-weight:600">${{s.name}}</td>
      <td style="font-weight:700;color:${{lvInfo.color}};${{lvInfo.bg}};padding:3px 8px;border-radius:4px" title="${{s.pair_desc||''}}">${{pp}}</td>
      <td style="font-weight:700;color:${{lvInfo.color}}">${{lvInfo.label}}<span style="font-size:10px;opacity:0.7;margin-left:2px">${{s.pair_type||lv}}</span></td>
      <td style="font-weight:700;color:${{udColor}};${{udBg}};font-size:14px;padding:2px 6px;border-radius:3px;text-align:center">${{ud}}天</td>
      <td style="color:#8a93a6;font-size:11px">${{fd}}</td>
      <td>${{close}}</td>
      <td style="color:${{chgColor}}">${{chg}}</td>
      <td style="color:#8a93a6">${{turnover}}</td>
      <td style="color:#8a93a6">${{amt}}</td>
      <td><button class="scan-add" onclick="quickAdd('${{s.code}}','${{s.name}}')">+自选</button></td>
    </tr>`;
  }}
  let htmlS = `<table style="font-size:12px">${{strongHeader}}` +
    (strongs.length ? strongs.map((s,i)=>strongRowHtml(s,i)).join('') :
      `<tr><td colspan="12" style="color:#6b7280;text-align:center;padding:20px">暂无强支撑对子</td></tr>`) +
    `</table>`;

  // ── 对子底 / 对子顶 ──
  const header = `<tr><th>#</th><th>代码</th><th>名称</th><th>对子价</th><th>类型</th>
    <th>收盘</th><th>最低</th><th>最高</th><th>涨跌幅</th><th>换手率</th><th>成交额(亿)</th><th>操作</th></tr>`;

  function rowHtml(s, i){{
    const lv = s.pair_level||'';
    const lvInfo = lvMap[lv] || {{color:'#8a93a6', bg:'', label:''}};
    const pp = s.pair_price!=null ? s.pair_price.toFixed(2) : '—';
    const close = s.price!=null ? s.price.toFixed(2) : '—';
    const low = s.low!=null ? s.low.toFixed(2) : '—';
    const high = s.high!=null ? s.high.toFixed(2) : '—';
    const chg = s.change_pct!=null ? (s.change_pct>=0?'+':'')+s.change_pct.toFixed(2)+'%' : '—';
    const chgColor = s.change_pct!=null && s.change_pct>=0 ? '#e74c3c' : '#27ae60';
    const turnover = s.turnover!=null ? s.turnover.toFixed(2)+'%' : '—';
    const amt = s.amount!=null ? (s.amount/1e8).toFixed(2) : '—';
    return `<tr>
      <td style="color:#6b7280;font-weight:700">${{i+1}}</td>
      <td><a href="/stock/${{s.code}}" style="color:#6ea8fe">${{s.code}}</a></td>
      <td>${{s.name}}</td>
      <td style="font-weight:700;color:${{lvInfo.color}};${{lvInfo.bg}};padding:3px 8px;border-radius:4px" title="${{s.pair_desc||''}}">${{pp}}</td>
      <td style="font-weight:700;color:${{lvInfo.color}}">${{lvInfo.label}}<span style="font-size:10px;opacity:0.7;margin-left:2px">${{s.pair_type||lv}}</span></td>
      <td>${{close}}</td>
      <td style="color:#27ae60">${{low}}</td>
      <td style="color:#e74c3c">${{high}}</td>
      <td style="color:${{chgColor}}">${{chg}}</td>
      <td style="color:#8a93a6">${{turnover}}</td>
      <td style="color:#8a93a6">${{amt}}</td>
      <td><button class="scan-add" onclick="quickAdd('${{s.code}}','${{s.name}}')">+自选</button></td>
    </tr>`;
  }}

  let htmlB = `<table style="font-size:12px">${{header}}` +
    (bottoms.length ? bottoms.map((s,i)=>rowHtml(s,i)).join('') :
      `<tr><td colspan="12" style="color:#6b7280;text-align:center;padding:20px">暂无对子底股票</td></tr>`) +
    `</table>`;
  let htmlT = `<table style="font-size:12px">${{header}}` +
    (tops.length ? tops.map((s,i)=>rowHtml(s,i)).join('') :
      `<tr><td colspan="12" style="color:#6b7280;text-align:center;padding:20px">暂无对子顶股票</td></tr>`) +
    `</table>`;
  document.getElementById('tbl-strong-pairs').innerHTML = htmlS;
  document.getElementById('tbl-pair-bottom').innerHTML = htmlB;
  document.getElementById('tbl-pair-top').innerHTML = htmlT;
}}

// ── Tab 切换 ──
function switchTab(tab,evt){{
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  evt.currentTarget.classList.add('active');
  document.getElementById('tab-pair').style.display = tab==='pair'?'block':'none';
  document.getElementById('tab-watchlist').style.display = tab==='watchlist'?'block':'none';
  document.getElementById('tab-scan').style.display = tab==='scan'?'block':'none';
  document.getElementById('tab-rotation').style.display = tab==='rotation'?'block':'none';
  if(tab==='pair' && !PAIR_LOADED) loadPairScan(false);
  if(tab==='scan' && !SCAN_LOADED) loadScan(false);
  if(tab==='rotation' && !ROT_LOADED) loadRotation(false);
}}
// ── 全市场扫描（只展示 TOP 5）──
let ALL_SCAN = [];
let SCAN_LOADED = false;

async function loadScan(force){{
  const el = document.getElementById('tbl-scan');
  const info = document.getElementById('scan-info');
  el.innerHTML = '<div class="loading">扫描中，请稍候（约3~8秒）...</div>';
  info.textContent = '扫描中...';
  try{{
    const url = '/api/market_scan' + (force?'?refresh=1':'');
    const d = await fetch(url).then(x=>x.json());
    ALL_SCAN = d.stocks || [];
    SCAN_LOADED = true;
    document.getElementById('scan-count').textContent = d.total;
    info.textContent = 'TOP ' + d.total + ' · 扫描时间 ' + (d.scan_time||'—');
    renderScan();
  }}catch(e){{
    el.innerHTML = '<div class="loading">扫描失败: '+e.message+'</div>';
    info.textContent = '扫描失败';
  }}
}}

function renderScan(){{
  let html = `<table>
    <tr><th>#</th><th>分数</th><th>代码</th><th>名称</th><th>收盘</th>
        <th>距MA20</th><th>ATR%</th><th>回调买入</th><th>冲高卖出</th><th>买对</th><th>买错</th><th>卖对</th><th>卖错</th>
        <th>动作</th><th>仓位</th>
        <th style="text-align:left">依据</th><th>操作</th></tr>` +
    ALL_SCAN.map((s,i)=>{{
      const a = s.action, c = C[a]||'#888';
      const sc = s.score||0, scColor = sc>=70?'#27ae60':sc>=40?'#f0b90b':'#6b7280';
      const pct = s.pct_from_ma20!=null ? (s.pct_from_ma20>=0?'+':'')+s.pct_from_ma20.toFixed(1)+'%' : '—';
      const pctColor = s.pct_from_ma20!=null && s.pct_from_ma20>=0 ? '#e74c3c' : '#27ae60';
      const atr = s.atr_pct!=null ? s.atr_pct.toFixed(2)+'%' : '—';
      const pull = s.pullback;
      const surge = s.surge;
      const bc = s.buy_correct || 0;
      const bf = s.buy_fail || 0;
      const sc2 = s.sell_correct || 0;
      const sf = s.sell_fail || 0;
      const wl = s.in_wl;
      const addBtn = wl ? '<button class="scan-add added">已加</button>' :
        `<button class="scan-add" onclick="quickAdd('${{s.code}}','${{s.name}}')">+自选</button>`;
      return `<tr>
        <td style="color:#6b7280;font-weight:700">${{i+1}}</td>
        <td style="font-weight:700;color:${{scColor}}">${{sc}}</td>
        <td><a href="/stock/${{s.code}}" style="color:#6ea8fe">${{s.code}}</a></td>
        <td>${{s.name}}</td>
        <td>${{s.close}}</td>
        <td style="color:${{pctColor}}">${{pct}}</td>
        <td style="color:#8a93a6">${{atr}}</td>
        <td>${{pull!=null ? '<span class="pred-pull">'+pull.toFixed(2)+'</span>' : '<span class="pred-none">—</span>'}}</td>
        <td>${{surge!=null ? '<span class="pred-surge">'+surge.toFixed(2)+'</span>' : '<span class="pred-none">—</span>'}}</td>
        <td style="color:#27ae60;font-weight:700">${{bc}}</td>
        <td style="color:#e74c3c;font-weight:700">${{bf}}</td>
        <td style="color:#27ae60;font-weight:700">${{sc2}}</td>
        <td style="color:#e74c3c;font-weight:700">${{sf}}</td>
        <td><span class="act" style="background:${{c}}">${{L[a]||a}}</span></td>
        <td>${{s.state_text||'—'}}</td>
        <td style="text-align:left;font-size:12px;color:#8a93a6;max-width:280px">${{s.reason||''}}</td>
        <td>${{addBtn}}</td>
      </tr>`;}}).join('') + `</table>`;
  document.getElementById('tbl-scan').innerHTML = html;
}}

async function quickAdd(code, name){{
  if(!confirm('添加 ' + code + ' ' + name + ' 到自选？')) return;
  try{{
    const r = await fetch('/api/watchlist', {{method:'POST',
      headers:{{'Content-Type':'application/json'}},
      body: JSON.stringify({{code, name}})}}).then(x=>x.json());
    if(r.ok){{
      ALL_SCAN.forEach(s=>{{ if(s.code===code) s.in_wl=true; }});
      renderScan();
    }} else {{
      alert('失败：' + r.msg);
    }}
  }}catch(e){{
    alert('网络错误：' + e.message);
  }}
}}

// ── 板块轮动 ──
let ROT_DATA = null;
let ROT_LOADED = false;

async function loadRotation(force){{
  const el = document.getElementById('tbl-rotation');
  const info = document.getElementById('rot-info');
  el.innerHTML = '<div class="loading">加载板块轮动数据...</div>';
  info.textContent = '加载中...';
  try{{
    const d = await fetch('/api/sector_rotation').then(x=>x.json());
    ROT_DATA = d;
    ROT_LOADED = true;
    const recs = d.recommendations || [];
    document.getElementById('rot-count').textContent = recs.length;
    info.textContent = '推荐 ' + recs.length + ' 个板块 · 共扫描 ' + (d.total_sectors||0) +
      ' 个行业 · ' + (d.scan_time||'—');
    renderRotation();
  }}catch(e){{
    el.innerHTML = '<div class="loading">加载失败: '+e.message+'</div>';
    info.textContent = '加载失败';
  }}
}}

function renderRotation(){{
  if(!ROT_DATA || !ROT_DATA.recommendations || ROT_DATA.recommendations.length===0){{
    document.getElementById('tbl-rotation').innerHTML =
      '<div class="loading">暂无板块轮动推荐数据。请点击「刷新数据」或在服务器执行 sector_rotation.py 扫描。</div>';
    return;
  }}
  const recs = ROT_DATA.recommendations;
  const ACTION_MAP = {{'BUY':('买入','#2980b9'),'SELL':('卖出','#e74c3c'),'LOCK':('锁利','#f39c12'),'HOLD':('持有','#27ae60'),'WAIT':('等待','#6b7280'),'PIVOT':('补涨','#e67e22')}};
  let html = `<table style="font-size:12px">
    <tr><th>#</th><th>板块</th><th>综合分</th><th>冷度</th><th>估值</th>
        <th>PE</th><th>PB</th><th>涨跌幅</th>
        <th>龙头代码</th><th>龙头名称</th><th>龙头分</th><th>技术面</th><th>距MA120</th>
        <th>动作</th><th>仓位</th><th>依据</th><th>持有收益</th><th>操作</th></tr>` +
    recs.map((r,i)=>{{
      const ts = r.total_score||0, tsColor = ts>=70?'#27ae60':ts>=50?'#f0b90b':'#6b7280';
      const cs = r.cold_score||0, vs = r.value_score||0;
      const pe = r.pe!=null ? r.pe.toFixed(1) : '—';
      const pb = r.pb!=null ? r.pb.toFixed(2) : '—';
      const chg = r.change_pct!=null ? (r.change_pct>=0?'+':'')+r.change_pct.toFixed(2)+'%' : '—';
      const chgColor = r.change_pct!=null && r.change_pct>=0 ? '#e74c3c' : '#27ae60';
      const leaders = r.leaders || [];
      if(leaders.length===0){{
        return `<tr style="border-top:2px solid #262b3b"><td style="color:#6b7280">${{i+1}}</td>
          <td colspan="16" style="color:#6b7280">暂无龙头</td></tr>`;
      }}
      const l = leaders[0];
      const ls = l.leader_score||0, lsColor = ls>=70?'#27ae60':ls>=50?'#f0b90b':'#6b7280';
      const tech = l.tech_score||0, techColor = tech>=60?'#27ae60':tech>=40?'#f0b90b':'#e74c3c';
      const ma120 = l.pct_from_ma120!=null ? (l.pct_from_ma120>=0?'+':'')+l.pct_from_ma120.toFixed(1)+'%' : '—';
      const act = l.action||'—';
      const actMeta = ACTION_MAP[act] || ['—','#888'];
      const stateT = l.state_text||'—';
      const reason = l.reason ? l.reason.substring(0,30) : '—';
      const hr = l.hold_return!=null ? (l.hold_return>=0?'+':'')+l.hold_return.toFixed(2)+'%' : '待开始';
      const hrColor = l.hold_return!=null ? (l.hold_return>=0?'#e74c3c':'#27ae60') : '#f0b90b';
      const entryInfo = l.entry_price!=null ? '入场:'+l.entry_price : '下周一入场';
      const addBtn = `<button class="scan-add" onclick="quickAdd('${{l.code}}','${{l.name}}')">+自选</button>`;
      return `<tr style="border-top:2px solid #262b3b">
        <td style="color:#6b7280;font-weight:700">${{i+1}}</td>
        <td style="font-weight:700;color:#d8dce6">${{r.sector}}</td>
        <td style="font-weight:700;color:${{tsColor}}">${{ts.toFixed(1)}}</td>
        <td style="color:#8a93a6">${{cs.toFixed(1)}}</td>
        <td style="color:#8a93a6">${{vs.toFixed(1)}}</td>
        <td>${{pe}}</td>
        <td>${{pb}}</td>
        <td style="color:${{chgColor}}">${{chg}}</td>
        <td><a href="/stock/${{l.code}}" style="color:#6ea8fe">${{l.code}}</a></td>
        <td>${{l.name}}</td>
        <td style="font-weight:700;color:${{lsColor}}">${{ls.toFixed(1)}}</td>
        <td style="font-weight:700;color:${{techColor}}">${{tech.toFixed(1)}}</td>
        <td style="color:#8a93a6">${{ma120}}</td>
        <td style="font-weight:700;color:${{actMeta[1]}}">${{actMeta[0]}}</td>
        <td style="color:#8a93a6">${{stateT}}</td>
        <td style="text-align:left;color:#8a93a6;max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${{l.reason||''}}">${{reason}}</td>
        <td style="font-weight:700;color:${{hrColor}}" title="${{entryInfo}}">${{hr}}</td>
        <td>${{addBtn}}</td>
      </tr>`;
    }}).join('') + `</table>`;
  document.getElementById('tbl-rotation').innerHTML = html;
}}

// 默认加载对子数（首页签）
loadPairScan(false);

fetch("/api/overview").then(r=>r.json()).then(d=>{{
  document.getElementById("d").textContent = d.stocks[0].sig.date;
  document.getElementById("prm").textContent =
    "ST×" + d.params.st_multiplier + " 吊灯×" + d.params.chandelier_atr_mult;
  const pdEl = document.getElementById("pd");
  if(pdEl) pdEl.textContent = d.pred_date ? "更新于"+d.pred_date : "暂无";
  ALL_STOCKS = d.stocks.slice().sort((a,b)=>(b.sig.score||0)-(a.sig.score||0));
  document.getElementById("wl-count").textContent = ALL_STOCKS.length;
  renderPage(1);
}}).catch(e=>{{
  document.getElementById("tbl-sig").innerHTML =
    '<div class="loading">数据加载失败: '+e.message+'，请刷新重试</div>';
}});
</script></body></html>"""


@app.get("/")
def page_overview() -> str:
    return render_template_string(OVERVIEW_TPL)


STOCK_TPL = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>K线</title><style>{BASE_CSS}</style>{ECHARTS_JS}
</head><body><div class="wrap">
<h1 id="title"></h1><p class="sub"><span id="sub"></span><span id="rt-k"></span></p>
<div class="kline" id="k"></div>
<p class="sub">K线标记：买=买入 · 卖=卖出 · 锁=锁利提示（触吊灯线）</p>
<h2>回测摘要与最近交易</h2>
<p class="sub" id="btsum"></p>
<div id="tbl"></div>
<p class="sub"><a href="/">← 返回总览</a></p>
</div>
<script>
const C = {{SELL:"#e74c3c", LOCK:"#f39c12", BUY:"#2980b9", PIVOT:"#e67e22", HOLD:"#27ae60", WAIT:"#6b7280"}};
const L = {{SELL:"卖出", LOCK:"锁利", BUY:"买入", PIVOT:"补涨", HOLD:"持有", WAIT:"等待"}};
function fmtPct(v){{ return (v>=0?"+":"") + (v*100).toFixed(1) + "%"; }}
async function loadQuotes(){{
  try{{
    const qs = (await fetch("/api/quote").then(x=>x.json())).quotes;
    const code = location.pathname.split("/").pop();
    const q = qs[code];
    const k = document.getElementById("rt-k");
    if(q && k) k.innerHTML = ` · 实时 <b>${{q.price.toFixed(2)}}</b> `+
      `<span style="color:${{q.change_pct>=0?'#e74c3c':'#27ae60'}}">`+
      `${{q.change_pct>=0?'▲':'▼'}}${{Math.abs(q.change_pct).toFixed(2)}}%</span>`+
      ` <span style="color:#5b6478;font-size:11px">（${{q.time_str}}）</span>`;
  }}catch(e){{}}
}}
loadQuotes(); setInterval(loadQuotes, 30000);
fetch(location.pathname.replace("/stock/","/api/kline/")).then(r=>r.json()).then(d=>{{
  document.title = d.name + " · K线";
  document.getElementById("title").textContent = d.code + " " + d.name;
  const a = d.sig.action;
  document.getElementById("sub").innerHTML =
    `截至 ${{d.sig.date}} 收盘 <b>${{d.sig.close}}</b> —— ` +
    `<span style="color:${{C[a]||'#888'}};font-weight:700">${{L[a]||a}}</span>` +
    `［${{d.sig.state_text || '—'}}］` +
    `（${{d.sig.reason}}）`;
  const ch = echarts.init(document.getElementById("k"));
  ch.setOption({{
    animation:false,
    tooltip:{{trigger:"axis", backgroundColor:"#232839",
             textStyle:{{color:"#d8dce6"}}}},
    legend:{{top:6, textStyle:{{color:"#8a93a6"}},
            data:["MA20","MA60","SuperTrend","吊灯线"]}},
    grid:{{left:56, right:16, top:36, bottom:56}},
    xAxis:{{type:"category", data:d.dates, axisLabel:{{color:"#6b7280"}}}},
    yAxis:{{scale:true, axisLabel:{{color:"#6b7280"}},
           splitLine:{{lineStyle:{{color:"#262b3b"}}}}}},
    dataZoom:[{{type:"inside", start:55, end:100}},
              {{type:"slider", height:18, bottom:12, borderColor:"#262b3b",
                textStyle:{{color:"#6b7280"}}}}],
    series:[{{
      type:"candlestick", name:"K线", data:d.ohlc,
      itemStyle:{{color:"#ef232a", color0:"#14b143",
                 borderColor:"#ef232a", borderColor0:"#14b143"}},
      markPoint:{{symbolSize:26, label:{{fontSize:10, color:"#fff"}},
        data: d.marks}}
    }},
    {{name:"MA20", type:"line", data:d.ma_fast, showSymbol:false,
     lineStyle:{{width:1, color:"#f0b90b"}}, itemStyle:{{color:"#f0b90b"}}}},
    {{name:"MA60", type:"line", data:d.ma_slow, showSymbol:false,
     lineStyle:{{width:1, color:"#8e7cc3"}}, itemStyle:{{color:"#8e7cc3"}}}},
    {{name:"SuperTrend", type:"line", data:d.st, showSymbol:false,
     lineStyle:{{width:1.2, color:"#e67e22"}}, itemStyle:{{color:"#e67e22"}}}},
    {{name:"吊灯线", type:"line", data:d.chandelier, showSymbol:false,
     lineStyle:{{width:1, color:"#16a085", type:"dashed"}},
     itemStyle:{{color:"#16a085"}}}}]
  }});
  document.getElementById("btsum").innerHTML =
    `策略 <b class="${{d.bt.total>=0?'good':'bad'}}">${{fmtPct(d.bt.total)}}</b>` +
    ` / 回撤 <b class="warn">${{fmtPct(d.bt.mdd)}}</b>` +
    ` &nbsp;vs&nbsp; 持有 <b>${{fmtPct(d.bt.bh_total)}}</b>` +
    ` / 回撤 <b>${{fmtPct(d.bt.bh_mdd)}}</b>` +
    ` &nbsp;|&nbsp; ${{d.bt.n_trades}}笔 胜率${{(d.bt.win_rate*100).toFixed(0)}}%`;
  document.getElementById("tbl").innerHTML =
    `<table><tr><th>入场日</th><th>出场日</th><th>买价</th><th>卖价</th>
     <th>收益%</th><th>持仓天</th></tr>` +
    d.trades.map(t=>`<tr><td>${{t.entry_date}}</td><td>${{t.exit_date}}</td>
      <td>${{t.entry}}</td><td>${{t.exit}}</td>
      <td class="${{t.ret_pct>=0?'good':'bad'}}">${{t.ret_pct.toFixed(2)}}</td>
      <td>${{t.hold_days}}</td></tr>`).join("") + `</table>`;
}});
</script></body></html>"""


@app.get("/stock/<code>")
def page_stock(code: str) -> str:
    get_stock(code)
    return render_template_string(STOCK_TPL)


if __name__ == "__main__":
    ensure_dirs()
    print("Quant Web: http://0.0.0.0:8787")
    app.run(host="0.0.0.0", port=8787, debug=False)
