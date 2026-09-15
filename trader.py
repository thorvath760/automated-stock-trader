from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import (
    GetOrdersRequest,
    LimitOrderRequest,
    MarketOrderRequest,
    StopLossRequest,
    TakeProfitRequest,
)

from strategy import add_signals, order_levels, position_quantity

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "trades.db"
STATE_PATH = ROOT / "runtime_state.json"
EASTERN = ZoneInfo("America/New_York")


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


def _daily_state(equity: float) -> dict:
    today = datetime.now(EASTERN).date().isoformat()
    state = {}
    if STATE_PATH.exists():
        try:
            state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            state = {}
    if state.get("date") != today:
        state = {"date": today, "starting_equity": equity, "orders": 0,
                 "extended_pending": state.get("extended_pending", {}),
                 "extended_managed": state.get("extended_managed", {})}
        STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")
    state.setdefault("extended_pending", {})
    state.setdefault("extended_managed", {})
    return state


def _save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def trading_session(clock_open: bool, now: datetime | None = None) -> str:
    if clock_open:
        return "regular"
    current = (now or datetime.now(timezone.utc)).astimezone(EASTERN)
    if current.weekday() >= 5:
        return "closed"
    current_time = current.time().replace(tzinfo=None)
    if time(4) <= current_time < time(9, 30):
        return "premarket"
    if time(16) <= current_time < time(20):
        return "afterhours"
    return "closed"


def _extended_enabled(session: str, cfg: dict) -> bool:
    return ((session == "premarket" and bool(cfg.get("allow_premarket", False))) or
            (session == "afterhours" and bool(cfg.get("allow_after_hours", False))))


def _limit_price(reference: float, side: str, offset_pct: float) -> float:
    multiplier = 1 + offset_pct / 100 if side == "buy" else 1 - offset_pct / 100
    return round(reference * multiplier, 2)


def _manage_extended_orders(cfg: dict, api: Clients, state: dict) -> list[str]:
    messages: list[str] = []
    now = datetime.now(timezone.utc)
    timeout = max(1, int(cfg.get("extended_hours_order_timeout_minutes", 5)))
    for symbol, item in list(state["extended_pending"].items()):
        try:
            order = api.trading.get_order_by_id(item["order_id"])
            status = str(order.status).lower().split(".")[-1]
            if status == "filled":
                item["qty"] = int(float(order.filled_qty or item["qty"]))
                state["extended_managed"][symbol] = item
                del state["extended_pending"][symbol]
                messages.append(f"{symbol}: extended-hours entry filled; software exit monitoring active.")
            elif status in {"canceled", "expired", "rejected", "replaced"}:
                del state["extended_pending"][symbol]
                messages.append(f"{symbol}: extended-hours entry {status}.")
            elif now - datetime.fromisoformat(item["submitted_at"]) >= timedelta(minutes=timeout):
                api.trading.cancel_order_by_id(item["order_id"])
                del state["extended_pending"][symbol]
                messages.append(f"{symbol}: stale extended-hours entry canceled.")
        except Exception as exc:
            messages.append(f"{symbol}: could not check extended-hours entry: {exc}")

    positions = {p.symbol: p for p in api.trading.get_all_positions()}
    offset = float(cfg.get("extended_hours_limit_offset_pct", 0.1))
    for symbol, item in list(state["extended_managed"].items()):
        if symbol not in positions:
            del state["extended_managed"][symbol]
            continue
        if item.get("exit_order_id"):
            try:
                exit_order = api.trading.get_order_by_id(item["exit_order_id"])
                status = str(exit_order.status).lower().split(".")[-1]
                if status == "filled":
                    del state["extended_managed"][symbol]
                    messages.append(f"{symbol}: extended-hours exit filled.")
                elif status in {"canceled", "expired", "rejected"}:
                    item.pop("exit_order_id", None)
                    messages.append(f"{symbol}: extended-hours exit {status}; monitoring resumed.")
            except Exception as exc:
                messages.append(f"{symbol}: could not check extended-hours exit: {exc}")
            continue
        bars = fetch_bars(api.data, symbol, cfg)
        if bars.empty:
            continue
        price = float(bars.iloc[-1].close)
        side = item["side"]
        triggered = ((price <= float(item["stop"]) or price >= float(item["target"])) if side == "buy"
                     else (price >= float(item["stop"]) or price <= float(item["target"])))
        if not triggered:
            continue
        close_side = "sell" if side == "buy" else "buy"
        qty = min(int(item["qty"]), int(abs(float(positions[symbol].qty))))
        result = api.trading.submit_order(order_data=LimitOrderRequest(
            symbol=symbol, qty=qty,
            side=OrderSide.SELL if close_side == "sell" else OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
            limit_price=_limit_price(price, close_side, offset), extended_hours=True,
            client_order_id=f"lsx-exit-{symbol}-{now.strftime('%Y%m%d%H%M%S')}",
        ))
        item["exit_order_id"] = str(result.id)
        messages.append(f"{symbol}: software stop/target triggered; extended-hours limit exit submitted.")
    _save_state(state)
    return messages


def scan_once(cfg: dict, api: Clients) -> list[str]:
    messages: list[str] = []
    account = api.trading.get_account()
    equity = float(account.equity)
    state = _daily_state(equity)
    messages.extend(_manage_extended_orders(cfg, api, state))
    drawdown_pct = (float(state["starting_equity"]) - equity) / float(state["starting_equity"]) * 100
    if drawdown_pct >= float(cfg["max_daily_loss_pct"]):
        return [f"Daily loss guard active ({drawdown_pct:.2f}%)."]
    if int(state["orders"]) >= int(cfg["max_trades_per_day"]):
        return ["Daily trade limit reached."]
    clock = api.trading.get_clock()
    session = trading_session(bool(clock.is_open))
    if session == "closed" or (session != "regular" and not _extended_enabled(session, cfg)):
        return messages + [f"Trading session is {session}; scanning is disabled."]

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
        sizing_cfg = cfg
        if session != "regular":
            sizing_cfg = dict(cfg)
            sizing_cfg["risk_per_trade_pct"] = float(cfg["risk_per_trade_pct"]) * float(cfg.get("extended_hours_risk_multiplier", 0.5))
        qty = position_quantity(equity, entry, stop, sizing_cfg)
        if qty < 1:
            messages.append(f"{symbol}: signal found but calculated quantity is zero.")
            continue
        if session == "regular":
            order = MarketOrderRequest(
                symbol=symbol, qty=qty, side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
                time_in_force=TimeInForce.DAY, order_class=OrderClass.BRACKET,
                take_profit=TakeProfitRequest(limit_price=round(target, 2)),
                stop_loss=StopLossRequest(stop_price=round(stop, 2)),
                client_order_id=f"ls-{symbol}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}")
        else:
            if side == "sell" and not bool(cfg.get("extended_hours_shorting", False)):
                messages.append(f"{symbol}: bearish signal detected; extended-hours shorting disabled.")
                continue
            order = LimitOrderRequest(
                symbol=symbol, qty=qty, side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
                limit_price=_limit_price(float(row.close), side, float(cfg.get("extended_hours_limit_offset_pct", 0.1))),
                extended_hours=True,
                client_order_id=f"lsx-{symbol}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}")
        result = api.trading.submit_order(order_data=order)
        state["orders"] = int(state["orders"]) + 1
        if session != "regular":
            state["extended_pending"][symbol] = {
                "order_id": str(result.id), "submitted_at": datetime.now(timezone.utc).isoformat(),
                "side": side, "qty": qty, "stop": round(stop, 2), "target": round(target, 2)}
        _save_state(state)
        log_event(symbol, "paper_order_submitted", side, qty, entry, stop, target, str(result.id))
        messages.append(f"{symbol}: {side} {qty} submitted in {session} (paper).")
        if int(state["orders"]) >= int(cfg["max_trades_per_day"]):
            break
    return messages
