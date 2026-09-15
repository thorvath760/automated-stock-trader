from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from dotenv import load_dotenv

from client_portal import DISCLOSURE_TEXT, load_user_data, require_client_access, save_user_data
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


RISK_PROFILES = {
    "Conservative": {
        "risk_per_trade_pct": 0.10,
        "max_position_pct": 3.0,
        "max_daily_loss_pct": 0.50,
        "max_trades_per_day": 2,
        "reward_to_risk": 1.50,
        "symbols": ["SPY", "QQQ"],
    },
    "Moderate": {
        "risk_per_trade_pct": 0.25,
        "max_position_pct": 5.0,
        "max_daily_loss_pct": 1.00,
        "max_trades_per_day": 3,
        "reward_to_risk": 1.50,
        "symbols": ["SPY", "QQQ", "AAPL", "MSFT"],
    },
    "Aggressive": {
        "risk_per_trade_pct": 0.50,
        "max_position_pct": 10.0,
        "max_daily_loss_pct": 2.00,
        "max_trades_per_day": 4,
        "reward_to_risk": 2.00,
        "symbols": ["SPY", "QQQ", "AAPL", "MSFT"],
    },
}


def risk_profile(willingness: int, capacity: int) -> str:
    """Score willingness while preventing it from exceeding financial capacity."""
    total = willingness + capacity
    profile = "Conservative" if total <= 5 else "Moderate" if total <= 11 else "Aggressive"
    if capacity <= 3:
        return "Conservative"
    if capacity <= 6 and profile == "Aggressive":
        return "Moderate"
    return profile


client = require_client_access()
cfg = ensure_config()
stored_client_data = load_user_data(client["user_id"]) if client["enabled"] else {}
if stored_client_data.get("strategy_config"):
    cfg.update(stored_client_data["strategy_config"])
st.title("Liquidity Sweep Paper Trader")
st.caption("Signal research, backtesting, risk controls, and Alpaca paper-order execution")

with st.sidebar:
    if client["enabled"]:
        st.write(f"Signed in as **{client['name']}**")
        st.caption(client["email"])
        if st.button("Sign out", use_container_width=True):
            st.logout()
        st.divider()
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
        if client["enabled"]:
            save_user_data(client["user_id"], strategy_config=cfg)
        else:
            CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        st.success("Settings saved.")

tab_overview, tab_risk, tab_chart, tab_backtest, tab_log = st.tabs(
    ["Account", "Risk assessment", "Signals", "Backtest", "Activity log"]
)

if client["admin"]:
    try:
        api = clients()
    except Exception as exc:
        api = None
        st.warning(f"Add paper API credentials to .env to connect: {exc}")
else:
    api = None

with tab_overview:
    if client["enabled"] and not client["admin"]:
        st.subheader("Your Alpaca paper account")
        st.warning("Not connected. Individual Alpaca OAuth connections are disabled until the app is registered with Alpaca.")
        st.info("Do not enter or send an Alpaca password, API key, or secret. The future Connect Alpaca button will use Alpaca's authorization page.")
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
    if client["admin"]:
        st.info("Continuous automation runs separately with: python bot.py")

with tab_risk:
    st.subheader("Educational paper-trading risk assessment")
    st.write(
        "This questionnaire does not collect a name, account number, or brokerage credentials. "
        "It estimates a paper-trading profile from risk willingness and financial capacity."
    )
    with st.form("risk_assessment"):
        st.markdown("#### Risk willingness")
        loss_reaction = st.radio(
            "If a paper portfolio fell 15% in one month, what would you most likely do?",
            [0, 1, 2],
            format_func=lambda x: ["Exit to prevent further losses", "Hold and review", "Hold or add if the plan remains valid"][x],
        )
        volatility = st.radio(
            "Which paper-return pattern would you prefer?",
            [0, 1, 2],
            format_func=lambda x: ["Small fluctuations and lower return potential", "Moderate fluctuations", "Large fluctuations and higher return potential"][x],
        )
        experience = st.radio(
            "How much experience do you have with stocks or ETFs?",
            [0, 1, 2],
            format_func=lambda x: ["Little or none", "Some", "Substantial"][x],
        )
        drawdown = st.radio(
            "What maximum temporary paper loss could you tolerate without abandoning the plan?",
            [0, 1, 2],
            format_func=lambda x: ["Less than 10%", "10% to 20%", "More than 20%"][x],
        )

        st.markdown("#### Risk capacity")
        horizon = st.radio(
            "How long before these funds would be needed?",
            [0, 1, 2],
            format_func=lambda x: ["Less than 2 years", "2 to 5 years", "More than 5 years"][x],
        )
        emergency = st.radio(
            "Is a separate emergency fund available?",
            [0, 1, 2],
            format_func=lambda x: ["No", "Partially funded", "Yes, adequately funded"][x],
        )
        income = st.radio(
            "How stable is ongoing income?",
            [0, 1, 2],
            format_func=lambda x: ["Uncertain", "Generally stable", "Very stable with surplus cash flow"][x],
        )
        withdrawals = st.radio(
            "How likely are withdrawals during the next two years?",
            [0, 1, 2],
            format_func=lambda x: ["Likely", "Possible", "Unlikely"][x],
        )
        assessed = st.form_submit_button("Calculate paper profile", type="primary")

    if assessed:
        willingness_score = loss_reaction + volatility + experience + drawdown
        capacity_score = horizon + emergency + income + withdrawals
        st.session_state["risk_result"] = {
            "profile": risk_profile(willingness_score, capacity_score),
            "willingness": willingness_score,
            "capacity": capacity_score,
        }

    result = st.session_state.get("risk_result")
    if not result and stored_client_data.get("risk_result"):
        result = stored_client_data["risk_result"]
    if result:
        profile = result["profile"]
        proposed = RISK_PROFILES[profile]
        st.success(f"Suggested educational paper profile: {profile}")
        c1, c2 = st.columns(2)
        c1.metric("Risk willingness", f"{result['willingness']} / 8")
        c2.metric("Risk capacity", f"{result['capacity']} / 8")
        st.dataframe(
            pd.DataFrame(
                {
                    "Setting": ["Risk per trade", "Maximum position", "Daily loss cutoff", "Maximum trades/day", "Reward/risk"],
                    "Suggested value": [
                        f"{proposed['risk_per_trade_pct']:.2f}%",
                        f"{proposed['max_position_pct']:.0f}%",
                        f"{proposed['max_daily_loss_pct']:.2f}%",
                        proposed["max_trades_per_day"],
                        f"{proposed['reward_to_risk']:.2f}",
                    ],
                }
            ),
            use_container_width=True,
            hide_index=True,
        )
        st.caption(
            "Risk capacity limits the result when it is lower than risk willingness. "
            "This output is educational, is not investment advice, and applies only to paper trading."
        )
        if st.button("Apply this paper profile"):
            cfg.update(proposed)
            cfg["allow_shorts"] = False
            cfg["paper_only"] = True
            if client["enabled"]:
                save_user_data(client["user_id"], strategy_config=cfg, risk_result=result)
            else:
                CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
            st.session_state.clear()
            st.rerun()

    if assessed and client["enabled"]:
        save_user_data(client["user_id"], risk_result=st.session_state["risk_result"])

    with st.expander("Important disclosure"):
        st.write(DISCLOSURE_TEXT)

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
