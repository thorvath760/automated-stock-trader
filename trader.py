from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import (
    GetOrdersRequest,
    MarketOrderRequest,
    StopLossRequest,
    TakeProfitRequest,
)

from strategy import add_signals, order_levels, position_quantity

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "trades.db"
STATE_PATH = ROOT / "runtime_state.json"


@dataclass
class Clients:
    trading: TradingClient
    data: StockHistoricalDataClient


def load_config() -> dict:
    path = ROOT / "config.json"
    if not path.exists():
        raise FileNotFoundError("Copy config.example.json to config.json first.")
    return json.loads(path.read_text(encoding="utf-8"))


def clients() -> Clients:
    key = os.getenv("ALPACA_API_KEY", "").strip()
    secret = os.getenv("ALPACA_SECRET_KEY", "").strip()
    if not key or not secret:
        raise RuntimeError("Missing ALPACA_API_KEY or ALPACA_SECRET_KEY.")
    if os.getenv("ALPACA_PAPER", "true").lower() != "true":
        raise RuntimeError("This build requires ALPACA_PAPER=true.")
    return Clients(TradingClient(key, secret, paper=True), StockHistoricalDataClient(key, secret))


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS events (
            timestamp TEXT, symbol TEXT, event TEXT, side TEXT, qty INTEGER,
            entry REAL, stop REAL, target REAL, details TEXT)""")


def log_event(symbol: str, event: str, side: str = "", qty: int = 0,
              entry: float = 0, stop: float = 0, target: float = 0, details: str = "") -> None:
    init_db()
    with sqlite3.connect(DB_PATH) as db:
        db.execute("INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (
            datetime.now(timezone.utc).isoformat(), symbol, event, side, qty,
            entry, stop, target, details,
        ))


def fetch_bars(data_client: StockHistoricalDataClient, symbol: str, cfg: dict) -> pd.DataFrame:
    request = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame(int(cfg["timeframe_minutes"]), TimeFrameUnit.Minute),
        start=datetime.now(timezone.utc) - timedelta(days=int(cfg["lookback_days"])),
    )
    bars = data_client.get_stock_bars(request).df
    if bars.empty:
        return bars
    if isinstance(bars.index, pd.MultiIndex):
        bars = bars.xs(symbol, level="symbol")
    return bars.sort_index()

def diagnose_signal(row: pd.Series, cfg: dict) -> str:
    """Explain why the newest bar did not qualify."""

    required = ("prior_low", "prior_high", "avg_volume", "ema")
    if any(pd.isna(row.get(name)) for name in required):
        return "not enough bars to calculate indicators"

    swept_low = float(row["low"]) < float(row["prior_low"])
    swept_high = float(row["high"]) > float(row["prior_high"])

    volume_ratio = float(row["volume"]) / float(row["avg_volume"])
    volume_ok = volume_ratio >= float(cfg["volume_multiplier"])

    if swept_low:
        if float(row["close"]) <= float(row["prior_low"]):
            return "bullish sweep occurred, but price did not reclaim the prior low"
        if float(row["close"]) <= float(row["open"]):
            return "bullish sweep occurred, but the candle did not close bullish"
        if not volume_ok:
            return f"bullish sweep passed, but volume was only {volume_ratio:.2f}x average"
        if (
            bool(cfg.get("require_trend", True))
            and float(row["close"]) <= float(row["ema"])
        ):
            return "bullish sweep and volume passed, but price was below the EMA"

    if swept_high:
        if float(row["close"]) >= float(row["prior_high"]):
            return "bearish sweep occurred, but price did not reclaim the prior high"
        if float(row["close"]) >= float(row["open"]):
            return "bearish sweep occurred, but the candle did not close bearish"
        if not volume_ok:
            return f"bearish sweep passed, but volume was only {volume_ratio:.2f}x average"
        if (
            bool(cfg.get("require_trend", True))
            and float(row["close"]) >= float(row["ema"])
        ):
            return "bearish sweep and volume passed, but price was above the EMA"
        if not bool(cfg.get("allow_shorts", False)):
            return "eligible bearish signal detected, but short selling is disabled"

    if not swept_low and not swept_high:
        return "no liquidity sweep on the newest bar"

    return "signal conditions passed"
def _daily_state(equity: float) -> dict:
    today = datetime.now(timezone.utc).date().isoformat()
    state = {}
    if STATE_PATH.exists():
        try:
            state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            state = {}
    if state.get("date") != today:
        state = {"date": today, "starting_equity": equity, "orders": 0}
        STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return state


def scan_once(cfg: dict, api: Clients) -> list[str]:
    messages: list[str] = []
    account = api.trading.get_account()
    equity = float(account.equity)
    state = _daily_state(equity)
    drawdown_pct = (float(state["starting_equity"]) - equity) / float(state["starting_equity"]) * 100
    if drawdown_pct >= float(cfg["max_daily_loss_pct"]):
        return [f"Daily loss guard active ({drawdown_pct:.2f}%)."]
    if int(state["orders"]) >= int(cfg["max_trades_per_day"]):
        return ["Daily trade limit reached."]
    if not api.trading.get_clock().is_open:
        return ["Market is closed."]

    positions = {p.symbol for p in api.trading.get_all_positions()}
    orders = api.trading.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN))
    occupied = positions | {o.symbol for o in orders}

    for symbol in cfg["symbols"]:
        symbol = symbol.upper().strip()
        if symbol in occupied:
            messages.append(f"{symbol}: skipped; position or open order exists.")
            continue
        bars = fetch_bars(api.data, symbol, cfg)
        if bars.empty:
            messages.append(f"{symbol}: no bars returned.")
            continue
        signals = add_signals(bars, cfg)
        row = signals.iloc[-1]
        side = "buy" if bool(row["long_signal"]) else "sell" if bool(row["short_signal"]) else ""
        if not side or (side == "sell" and not bool(cfg.get("allow_shorts", False))):
            messages.append(f"{symbol}: no eligible signal.")
            continue

        entry, stop, target = order_levels(row, side, cfg)
        qty = position_quantity(equity, entry, stop, cfg)
        if qty < 1:
            messages.append(f"{symbol}: signal found but calculated quantity is zero.")
            continue
        order = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
            order_class=OrderClass.BRACKET,
            take_profit=TakeProfitRequest(limit_price=round(target, 2)),
            stop_loss=StopLossRequest(stop_price=round(stop, 2)),
            client_order_id=f"ls-{symbol}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
        )
        result = api.trading.submit_order(order_data=order)
        state["orders"] = int(state["orders"]) + 1
        STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")
        log_event(symbol, "paper_order_submitted", side, qty, entry, stop, target, str(result.id))
        messages.append(f"{symbol}: {diagnose_signal(row, cfg)}.")
        if int(state["orders"]) >= int(cfg["max_trades_per_day"]):
            break
    return messages

