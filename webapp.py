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
from pair_scan import (load_pair_result, scan_pair_numbers,
                       load_compass_result, scan_compass_stocks,
                       load_xinda_result, scan_xinda_stocks,
                       load_bull_hunter_result, scan_bull_hunter_stocks)

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


@app.get("/api/pair_scan")
def api_pair_scan() -> dict:
    """对子数扫描：返回上次扫描结果，refresh=1 时重新扫描。"""
    if request.args.get("refresh") == "1":
        return scan_pair_numbers()
    return load_pair_result()


@app.get("/api/compass_scan")
def api_compass_scan() -> dict:
    """指南针模式（空中加油/高位整理蓄势）扫描。"""
    if request.args.get("refresh") == "1":
        return scan_compass_stocks()
    return load_compass_result()


@app.get("/api/xinda_scan")
def api_xinda_scan() -> dict:
    """信达模式（突破加速/量价齐升）扫描。"""
    if request.args.get("refresh") == "1":
        return scan_xinda_stocks()
    return load_xinda_result()



@app.get("/api/bull_hunter_scan")
def api_bull_hunter_scan() -> dict:
    """猎牛选股（多因子综合评分）扫描。"""
    if request.args.get("refresh") == "1":
        return scan_bull_hunter_stocks()
    return load_bull_hunter_result()


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
  <div class="tab" onclick="switchTab('compass',event)">指南针模式 <span class="badge" id="compass-count"></span></div>
  <div class="tab" onclick="switchTab('xinda',event)">信达模式 <span class="badge" id="xinda-count"></span></div>
  <div class="tab" onclick="switchTab('bull',event)">猎牛选股 <span class="badge" id="bull-count"></span></div>
  <div class="tab" onclick="switchTab('watchlist',event)">自选股 <span class="badge" id="wl-count"></span></div>
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
  <h2 style="color:#f0b90b">★ 强支撑对子 <span style="font-size:13px;color:#6b7280;font-weight:400">（收盘形成且未破≥3天 → 主力强支撑，重点信号）</span></h2>
  <div id="tbl-strong-pairs"><div class="loading">点击标签加载数据...</div></div>
</div>
<div id="tab-compass" style="display:none">
  <div class="scan-bar">
    <span class="info" id="compass-info">点击「指南针模式」标签加载数据</span>
    <button onclick="loadCompassScan(true)">重新扫描</button>
  </div>
  <div style="font-size:11px;color:#8a93a6;margin:6px 0 10px;display:flex;gap:16px;flex-wrap:wrap">
    <span><b style="color:#f0b90b">●</b> 长期趋势向上（>MA60）</span>
    <span><b style="color:#00d4aa">●</b> 近20日振幅≥8%</span>
    <span><b style="color:#6ea8fe">●</b> 高位整理+长影线密集</span>
  </div>
  <h2 style="color:#f0b90b">★ 指南针模式（空中加油） <span style="font-size:13px;color:#6b7280;font-weight:400">（以300803指南针K线形态为模型，高位整理蓄势）</span></h2>
  <div id="tbl-compass-stocks"><div class="loading">点击标签加载数据...</div></div>
</div>
<div id="tab-xinda" style="display:none">
  <div class="scan-bar">
    <span class="info" id="xinda-info">点击「信达模式」标签加载数据</span>
    <button onclick="loadXindaScan(true)">重新扫描</button>
  </div>
  <div style="font-size:11px;color:#8a93a6;margin:6px 0 10px;display:flex;gap:16px;flex-wrap:wrap">
    <span><b style="color:#f0b90b">●</b> 多头排列（Close>MA5>MA10>MA20）</span>
    <span><b style="color:#00d4aa">●</b> 近5日累计涨幅≥10%</span>
    <span><b style="color:#6ea8fe">●</b> 放量上涨（5日量≥1.5倍20日量）</span>
  </div>
  <h2 style="color:#f0b90b">★ 信达模式（突破加速） <span style="font-size:13px;color:#6b7280;font-weight:400">（以600657信达地产K线形态为模型，平台突破后量价齐升）</span></h2>
  <div id="tbl-xinda-stocks"><div class="loading">点击标签加载数据...</div></div>
</div>
<div id="tab-bull" style="display:none">
  <div class="scan-bar">
    <span class="info" id="bull-info">点击「猎牛选股」标签加载数据</span>
    <button onclick="loadBullHunter(true)">重新扫描</button>
  </div>
  <div style="font-size:11px;color:#8a93a6;margin:6px 0 10px;display:flex;gap:16px;flex-wrap:wrap">
    <span><b style="color:#f0b90b">●</b> 趋势25分（MA排列+均线斜率）</span>
    <span><b style="color:#00d4aa">●</b> 动量30分（5/10日涨幅+当日涨）</span>
    <span><b style="color:#6ea8fe">●</b> 量能20分（量比+量价配合）</span>
    <span><b style="color:#e879f9">●</b> 位置18分（距高点+底部距离+振幅）</span>
    <span><b style="color:#fb923c">●</b> 质量15分（价格+流动性+成交额）</span>
  </div>
  <h2 style="color:#f0b90b">★ 猎牛选股（多因子综合评分） <span style="font-size:13px;color:#6b7280;font-weight:400">（满分108，≥55分入选，按总分降序）</span></h2>
  <div id="tbl-bull-stocks"><div class="loading">点击标签加载数据...</div></div>
</div>
<div id="tab-watchlist" style="display:none">
  <h2>信号列表（按分数降序）</h2>
  <div id="tbl-sig"></div>
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
  const info = document.getElementById('pair-info');
  if(force){{
    document.getElementById('tbl-strong-pairs').innerHTML = '<div class="loading">扫描全市场+历史验证中（约1~3分钟）...</div>';
    info.textContent = '扫描中...';
  }}
  try{{
    const url = '/api/pair_scan' + (force?'?refresh=1':'');
    const d = await fetch(url).then(x=>x.json());
    PAIR_DATA = d;
    PAIR_LOADED = true;
    const sc = d.strong_pair_count||0;
    document.getElementById('pair-count').textContent = sc;
    info.textContent = '★强支撑(未破≥3天) ' + sc + ' 只 · 扫描 ' + (d.total_stocks||0) + ' 只 · ' + (d.scan_time||'—');
    renderPairScan();
  }}catch(e){{
    document.getElementById('tbl-strong-pairs').innerHTML = '<div class="loading">加载失败: '+e.message+'</div>';
    info.textContent = '加载失败';
  }}
}}

function renderPairScan(){{
  if(!PAIR_DATA){{
    document.getElementById('tbl-strong-pairs').innerHTML = '<div class="loading">暂无数据</div>';
    return;
  }}
  const strongs = PAIR_DATA.strong_pairs || [];
  const lvMap = {{
    'AAA': {{color:'#f0b90b', bg:'background:rgba(240,185,11,0.15)', label:'全对'}},
    'AA+': {{color:'#00d4aa', bg:'background:rgba(0,212,170,0.12)', label:'双对'}},
    'AB':  {{color:'#6ea8fe', bg:'background:rgba(110,168,254,0.12)', label:'镜像'}},
  }};

  // ── 强支撑对子（重点信号） ──
  // ── 强支撑对子（重点信号） ──
  const strongHeader = `<tr><th>#</th><th>代码</th><th>名称</th><th style="color:#f0b90b">分数</th><th>支撑价</th><th>类型</th><th style="color:#f0b90b">未破天数</th><th>首次出现</th>
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
    const score = s.score!=null ? s.score : 0;
    const scoreColor = score>=200 ? '#f0b90b' : score>=150 ? '#e67e22' : score>=100 ? '#6ea8fe' : '#8a93a6';
    return `<tr>
      <td style="color:#6b7280;font-weight:700">${{i+1}}</td>
      <td><a href="/stock/${{s.code}}" style="color:#6ea8fe">${{s.code}}</a></td>
      <td style="font-weight:600">${{s.name}}</td>
      <td style="font-weight:700;color:${{scoreColor}};font-size:14px;text-align:center">${{score}}</td>
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
      `<tr><td colspan="13" style="color:#6b7280;text-align:center;padding:20px">暂无强支撑对子</td></tr>`) +
    `</table>`;
  document.getElementById('tbl-strong-pairs').innerHTML = htmlS;
}}

let COMPASS_DATA = null;
let COMPASS_LOADED = false;

async function loadCompassScan(force){{
  const info = document.getElementById('compass-info');
  if(force){{
    document.getElementById('tbl-compass-stocks').innerHTML = '<div class="loading">扫描全市场中（约3~10分钟）...</div>';
    info.textContent = '扫描中...';
  }}
  try{{
    const url = '/api/compass_scan' + (force?'?refresh=1':'');
    const d = await fetch(url).then(x=>x.json());
    COMPASS_DATA = d;
    COMPASS_LOADED = true;
    const cnt = d.count||0;
    document.getElementById('compass-count').textContent = cnt;
    info.textContent = '指南针模式 ' + cnt + ' 只 · 扫描 ' + (d.total_stocks||0) + ' 只 · ' + (d.scan_time||'—');
    renderCompassScan();
  }}catch(e){{
    document.getElementById('tbl-compass-stocks').innerHTML = '<div class="loading">加载失败: '+e.message+'</div>';
    info.textContent = '加载失败';
  }}
}}

function renderCompassScan(){{
  if(!COMPASS_DATA){{
    document.getElementById('tbl-compass-stocks').innerHTML = '<div class="loading">暂无数据</div>';
    return;
  }}
  const stocks = COMPASS_DATA.stocks || [];
  const header = `<tr><th>#</th><th>代码</th><th>名称</th><th style="color:#f0b90b">评分</th><th>现价</th><th>涨跌幅</th><th>成交额(亿)</th>
    <th>MA5</th><th>MA10</th><th>MA20</th><th>MA60</th><th>20日振幅</th><th>长影线日</th><th>操作</th></tr>`;
  function rowHtml(s, i){{
    const close = s.price!=null ? s.price.toFixed(2) : '—';
    const chg = s.change_pct!=null ? (s.change_pct>=0?'+':'')+s.change_pct.toFixed(2)+'%' : '—';
    const chgColor = s.change_pct!=null && s.change_pct>=0 ? '#e74c3c' : '#27ae60';
    const amt = s.amount!=null ? (s.amount/1e8).toFixed(2) : '—';
    return `<tr>
      <td style="color:#6b7280;font-weight:700">${{i+1}}</td>
      <td><a href="/stock/${{s.code}}" style="color:#6ea8fe">${{s.code}}</a></td>
      <td style="font-weight:600">${{s.name}}</td>
      <td style="color:#f0b90b;font-weight:700;font-size:14px">${{s.score!=null?s.score:'—'}}</td>
      <td>${{close}}</td>
      <td style="color:${{chgColor}}">${{chg}}</td>
      <td style="color:#8a93a6">${{amt}}</td>
      <td style="color:#f0b90b">${{s.sma5!=null?s.sma5.toFixed(2):'—'}}</td>
      <td style="color:#00d4aa">${{s.sma10!=null?s.sma10.toFixed(2):'—'}}</td>
      <td style="color:#6ea8fe">${{s.sma20!=null?s.sma20.toFixed(2):'—'}}</td>
      <td style="color:#8a93a6">${{s.sma60!=null?s.sma60.toFixed(2):'—'}}</td>
      <td style="color:#f0b90b;font-weight:700">${{s.amplitude_20d!=null?s.amplitude_20d.toFixed(2):'—'}}%</td>
      <td style="color:#00d4aa">${{s.long_shadow_days!=null?s.long_shadow_days:'—'}}</td>
      <td><button class="scan-add" onclick="quickAdd('${{s.code}}','${{s.name}}')">+自选</button></td>
    </tr>`;
  }}
  let html = `<table style="font-size:12px">${{header}}` +
    (stocks.length ? stocks.map((s,i)=>rowHtml(s,i)).join('') :
      `<tr><td colspan="15" style="color:#6b7280;text-align:center;padding:20px">暂无符合条件的股票</td></tr>`) +
    `</table>`;
  document.getElementById('tbl-compass-stocks').innerHTML = html;
}}

// ── 信达模式扫描 ──
let XINDA_DATA = null, XINDA_LOADED = false;
async function loadXindaScan(force){{
  const info = document.getElementById('xinda-info');
  if(force){{
    document.getElementById('tbl-xinda-stocks').innerHTML = '<div class="loading">扫描全市场中（约3~10分钟）...</div>';
    info.textContent = '扫描中...';
  }}
  try{{
    const url = '/api/xinda_scan' + (force?'?refresh=1':'');
    const d = await fetch(url).then(x=>x.json());
    XINDA_DATA = d;
    XINDA_LOADED = true;
    const cnt = d.count||0;
    document.getElementById('xinda-count').textContent = cnt;
    info.textContent = '信达模式 ' + cnt + ' 只 · 扫描 ' + (d.total_stocks||0) + ' 只 · ' + (d.scan_time||'—');
    renderXindaScan();
  }}catch(e){{
    document.getElementById('tbl-xinda-stocks').innerHTML = '<div class="loading">加载失败: '+e.message+'</div>';
    info.textContent = '加载失败';
  }}
}}
function renderXindaScan(){{
  if(!XINDA_DATA){{
    document.getElementById('tbl-xinda-stocks').innerHTML = '<div class="loading">暂无数据</div>';
    return;
  }}
  let stocks = XINDA_DATA.stocks||[];
  const header = `<tr style="color:#8a93a6;font-size:11px">
    <th>#</th><th>代码</th><th>名称</th><th style="color:#f0b90b">评分</th><th>现价</th><th>涨跌%</th><th>成交额(亿)</th>
    <th>MA5</th><th>MA10</th><th>MA20</th><th>5日涨幅%</th><th>阳线天</th>
    <th>20日振幅%</th><th>量比</th><th>操作</th></tr>`;
  function rowHtml(s,i){{
    const chg = s.change_pct!=null?s.change_pct.toFixed(2):'—';
    const amt = s.amount!=null?(s.amount/1e8).toFixed(2):'—';
    return `<tr>
      <td>${{i+1}}</td>
      <td><a href="/stock/${{s.code}}" style="color:#6ea8fe">${{s.code}}</a></td>
      <td>${{s.name}}</td>
      <td style="color:#f0b90b;font-weight:700;font-size:14px">${{s.score!=null?s.score:'—'}}</td>
      <td style="color:#f0b90b">${{s.price!=null?s.price.toFixed(2):'—'}}</td>
      <td style="color:${{chg>=0?'#00d4aa':'#f6465d'}}">${{chg}}%</td>
      <td>${{amt}}</td>
      <td style="color:#f0b90b">${{s.sma5!=null?s.sma5.toFixed(2):'—'}}</td>
      <td style="color:#00d4aa">${{s.sma10!=null?s.sma10.toFixed(2):'—'}}</td>
      <td style="color:#6ea8fe">${{s.sma20!=null?s.sma20.toFixed(2):'—'}}</td>
      <td style="color:#f0b90b;font-weight:700">${{s.gain_5d!=null?s.gain_5d.toFixed(2):'—'}}%</td>
      <td style="color:#00d4aa">${{s.up_days!=null?s.up_days:'—'}}/5</td>
      <td style="color:#6ea8fe">${{s.amplitude_20d!=null?s.amplitude_20d.toFixed(2):'—'}}%</td>
      <td style="color:#f0b90b">${{s.vol_ratio!=null?s.vol_ratio.toFixed(2):'—'}}x</td>
      <td><button class="scan-add" onclick="quickAdd('${{s.code}}','${{s.name}}')">+自选</button></td>
    </tr>`;
  }}
  let html = `<table style="font-size:12px">${{header}}` +
    (stocks.length ? stocks.map((s,i)=>rowHtml(s,i)).join('') :
      `<tr><td colspan="14" style="color:#6b7280;text-align:center;padding:20px">暂无符合条件的股票</td></tr>`) +
    `</table>`;
  document.getElementById('tbl-xinda-stocks').innerHTML = html;
}}
// ── 猎牛选股扫描 ──
let BULL_DATA = null, BULL_LOADED = false;
async function loadBullHunter(force){{
  const info = document.getElementById('bull-info');
  if(force){{
    document.getElementById('tbl-bull-stocks').innerHTML = '<div class="loading">扫描全市场中（约3~10分钟）...</div>';
    info.textContent = '扫描中...';
  }}
  try{{
    const url = '/api/bull_hunter_scan' + (force?'?refresh=1':'');
    const d = await fetch(url).then(x=>x.json());
    BULL_DATA = d; BULL_LOADED = true;
    const cnt = d.count||0;
    document.getElementById('bull-count').textContent = cnt;
    info.textContent = '猎牛选股 ' + cnt + ' 只 · 扫描 ' + (d.total_stocks||0) + ' 只 · ' + (d.scan_time||'—');
    renderBullHunter();
  }}catch(e){{
    document.getElementById('tbl-bull-stocks').innerHTML = '<div class="loading">加载失败: '+e.message+'</div>';
    info.textContent = '加载失败';
  }}
}}
function renderBullHunter(){{
  if(!BULL_DATA){{
    document.getElementById('tbl-bull-stocks').innerHTML = '<div class="loading">暂无数据</div>';
    return;
  }}
  const stocks = BULL_DATA.stocks || [];
  let header = `<tr style="color:#6b7280;border-bottom:1px solid #23272f">
    <th>#</th><th>代码</th><th>名称</th><th>总分</th>
    <th style="color:#f0b90b">趋势</th><th style="color:#00d4aa">动量</th><th style="color:#6ea8fe">量能</th><th style="color:#e879f9">位置</th><th style="color:#fb923c">质量</th>
    <th>5日涨%</th><th>10日涨%</th><th>量比</th><th>距高%</th><th>振幅%</th><th>操作</th></tr>`;
  function rowHtml(s,i){{
    const sc = s.score||0;
    const scoreColor = sc>=80?'#f0b90b':sc>=70?'#00d4aa':sc>=60?'#6ea8fe':'#8a93a6';
    return `<tr style="border-bottom:1px solid #1a1d27">
      <td style="color:#6b7280">${{i+1}}</td>
      <td><a href="/stock/${{s.code}}" style="color:#6ea8fe">${{s.code}}</a></td>
      <td style="font-weight:600">${{s.name||'—'}}</td>
      <td style="color:${{scoreColor}};font-weight:700;font-size:14px">${{sc}}</td>
      <td style="color:#f0b90b">${{s.trend!=null?s.trend:'—'}}</td>
      <td style="color:#00d4aa">${{s.momentum!=null?s.momentum:'—'}}</td>
      <td style="color:#6ea8fe">${{s.volume!=null?s.volume:'—'}}</td>
      <td style="color:#e879f9">${{s.position!=null?s.position:'—'}}</td>
      <td style="color:#fb923c">${{s.trade!=null?s.trade:'—'}}</td>
      <td style="color:#f0b90b;font-weight:700">${{s.gain_5d!=null?s.gain_5d.toFixed(1):'—'}}%</td>
      <td>${{s.gain_10d!=null?s.gain_10d.toFixed(1):'—'}}%</td>
      <td>${{s.vol_ratio!=null?s.vol_ratio.toFixed(2):'—'}}x</td>
      <td>${{s.dist_high!=null?s.dist_high.toFixed(1):'—'}}%</td>
      <td>${{s.amplitude_20d!=null?s.amplitude_20d.toFixed(1):'—'}}%</td>
      <td><button class="scan-add" onclick="quickAdd('${{s.code}}','${{s.name}}')">+自选</button></td>
    </tr>`;
  }}
  let html = `<table style="font-size:12px">${{header}}` +
    (stocks.length ? stocks.map((s,i)=>rowHtml(s,i)).join('') :
      `<tr><td colspan="15" style="color:#6b7280;text-align:center;padding:20px">暂无符合条件的股票</td></tr>`) +
    `</table>`;
  document.getElementById('tbl-bull-stocks').innerHTML = html;
}}

// ── Tab 切换 ──
function switchTab(tab,evt){{
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  evt.currentTarget.classList.add('active');
  document.getElementById('tab-pair').style.display = tab==='pair'?'block':'none';
  document.getElementById('tab-compass').style.display = tab==='compass'?'block':'none';
  document.getElementById('tab-xinda').style.display = tab==='xinda'?'block':'none';
  document.getElementById('tab-bull').style.display = tab==='bull'?'block':'none';
  document.getElementById('tab-watchlist').style.display = tab==='watchlist'?'block':'none';
  if(tab==='pair' && !PAIR_LOADED) loadPairScan(false);
  if(tab==='compass' && !COMPASS_LOADED) loadCompassScan(false);
  if(tab==='xinda' && !XINDA_LOADED) loadXindaScan(false);
  if(tab==='bull' && !BULL_LOADED) loadBullHunter(false);
}}
async function quickAdd(code, name){{
  if(!confirm('添加 ' + code + ' ' + name + ' 到自选？')) return;
  try{{
    const r = await fetch('/api/watchlist', {{method:'POST',
      headers:{{'Content-Type':'application/json'}},
      body: JSON.stringify({{code, name}})}}).then(x=>x.json());
    if(r.ok){{
      alert('已添加到自选');
    }} else {{
      alert('失败：' + r.msg);
    }}
  }}catch(e){{
    alert('网络错误：' + e.message);
  }}
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
