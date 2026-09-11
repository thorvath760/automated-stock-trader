from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from dotenv import load_dotenv

from strategy import add_signals, order_levels
from trader import DB_PATH, clients, fetch_bars, init_db, load_config, scan_once

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"

st.set_page_config(page_title="Liquidity Sweep Trader", page_icon="📈", layout="wide")
load_dotenv()


def ensure_config() -> dict:
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text((ROOT / "config.example.json").read_text(encoding="utf-8"), encoding="utf-8")
    return load_config()


cfg = ensure_config()
st.title("Liquidity Sweep Paper Trader")
st.caption("Signal research, backtesting, risk controls, and Alpaca paper-order execution")

with st.sidebar:
    st.header("Strategy settings")
    symbols = st.text_input("Symbols", ", ".join(cfg["symbols"]))
    cfg["timeframe_minutes"] = st.selectbox("Bar timeframe", [1, 5, 15, 30, 60], index=[1, 5, 15, 30, 60].index(cfg["timeframe_minutes"]))
    cfg["sweep_lookback"] = st.number_input("Sweep lookback bars", 3, 100, cfg["sweep_lookback"])
    cfg["volume_multiplier"] = st.number_input("Volume multiplier", 1.0, 5.0, float(cfg["volume_multiplier"]), 0.1)
    cfg["require_trend"] = st.toggle("Require EMA trend", cfg["require_trend"])
    st.header("Risk controls")
    cfg["risk_per_trade_pct"] = st.number_input("Risk per trade (%)", 0.1, 2.0, float(cfg["risk_per_trade_pct"]), 0.1)
    cfg["reward_to_risk"] = st.number_input("Reward/risk", 1.0, 5.0, float(cfg["reward_to_risk"]), 0.25)
    cfg["max_position_pct"] = st.number_input("Max position (%)", 1.0, 25.0, float(cfg["max_position_pct"]), 1.0)
    cfg["max_daily_loss_pct"] = st.number_input("Daily loss cutoff (%)", 0.5, 5.0, float(cfg["max_daily_loss_pct"]), 0.25)
    cfg["max_trades_per_day"] = st.number_input("Max trades/day", 1, 20, int(cfg["max_trades_per_day"]))
    cfg["symbols"] = [x.strip().upper() for x in symbols.split(",") if x.strip()]
    if st.button("Save settings", use_container_width=True):
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        st.success("Settings saved.")

tab_overview, tab_chart, tab_backtest, tab_log = st.tabs(["Account", "Signals", "Backtest", "Activity log"])

try:
    api = clients()
except Exception as exc:
    api = None
    st.warning(f"Add paper API credentials to .env to connect: {exc}")

with tab_overview:
    if api:
        account = api.trading.get_account()
        c1, c2, c3 = st.columns(3)
        c1.metric("Paper equity", f"${float(account.equity):,.2f}")
        c2.metric("Buying power", f"${float(account.buying_power):,.2f}")
        c3.metric("Market", "Open" if api.trading.get_clock().is_open else "Closed")
        if st.button("Run one paper scan", type="primary"):
            with st.spinner("Scanning..."):
                for msg in scan_once(cfg, api):
                    st.write(msg)
    st.info("Continuous automation runs separately with: python bot.py")

with tab_chart:
    selected = st.selectbox("Symbol", cfg["symbols"] or ["SPY"])
    if api and st.button("Load latest signal chart"):
        bars = add_signals(fetch_bars(api.data, selected, cfg), cfg)
        fig = go.Figure(go.Candlestick(x=bars.index, open=bars.open, high=bars.high, low=bars.low, close=bars.close, name=selected))
        fig.add_trace(go.Scatter(x=bars.index, y=bars.ema, name="EMA"))
        longs = bars[bars.long_signal]
        shorts = bars[bars.short_signal]
        fig.add_trace(go.Scatter(x=longs.index, y=longs.low, mode="markers", marker_symbol="triangle-up", marker_size=12, name="Bull sweep"))
        fig.add_trace(go.Scatter(x=shorts.index, y=shorts.high, mode="markers", marker_symbol="triangle-down", marker_size=12, name="Bear sweep"))
        fig.update_layout(height=650, xaxis_rangeslider_visible=False)
        st.plotly_chart(fig, use_container_width=True)

with tab_backtest:
    st.write("The backtest enters on the next bar open and resolves stops before targets when both occur in one bar (conservative assumption).")
    if api and st.button("Run backtest"):
        bars = add_signals(fetch_bars(api.data, selected, cfg), cfg)
        trades = []
        i = 0
        while i < len(bars) - 1:
            row = bars.iloc[i]
            side = "buy" if row.long_signal else "sell" if row.short_signal and cfg.get("allow_shorts") else ""
            if not side:
                i += 1
                continue
            entry = float(bars.iloc[i + 1].open)
            _, signal_stop, _ = order_levels(row, side, cfg)
            risk = abs(entry - signal_stop)
            if risk <= 0:
                i += 1
                continue
            target = entry + risk * cfg["reward_to_risk"] if side == "buy" else entry - risk * cfg["reward_to_risk"]
            exit_price, outcome, j = float(bars.iloc[-1].close), "open", len(bars) - 1
            for j in range(i + 1, len(bars)):
                b = bars.iloc[j]
                stopped = b.low <= signal_stop if side == "buy" else b.high >= signal_stop
                won = b.high >= target if side == "buy" else b.low <= target
                if stopped:
                    exit_price, outcome = signal_stop, "loss"
                    break
                if won:
                    exit_price, outcome = target, "win"
                    break
            r_multiple = (exit_price - entry) / risk if side == "buy" else (entry - exit_price) / risk
            trades.append({"entry_time": bars.index[i + 1], "side": side, "entry": entry, "exit": exit_price, "outcome": outcome, "R": r_multiple})
            i = j + 1
        results = pd.DataFrame(trades)
        if results.empty:
            st.warning("No qualifying trades in this sample.")
        else:
            a, b, c = st.columns(3)
            a.metric("Trades", len(results))
            b.metric("Win rate", f"{(results.outcome == 'win').mean() * 100:.1f}%")
            c.metric("Total R", f"{results.R.sum():.2f}")
            st.line_chart(results.R.cumsum())
            st.dataframe(results, use_container_width=True)

with tab_log:
    init_db()
    with sqlite3.connect(DB_PATH) as db:
        events = pd.read_sql_query("SELECT * FROM events ORDER BY timestamp DESC LIMIT 500", db)
    st.dataframe(events, use_container_width=True, hide_index=True)

