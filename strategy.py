from __future__ import annotations

import pandas as pd


def add_signals(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Identify confirmed liquidity-sweep reversals without look-ahead data."""
    out = df.copy().sort_index()
    sweep_n = int(cfg["sweep_lookback"])
    volume_n = int(cfg["volume_lookback"])
    ema_n = int(cfg["ema_period"])

    out["prior_low"] = out["low"].shift(1).rolling(sweep_n).min()
    out["prior_high"] = out["high"].shift(1).rolling(sweep_n).max()
    out["avg_volume"] = out["volume"].shift(1).rolling(volume_n).mean()
    out["ema"] = out["close"].ewm(span=ema_n, adjust=False).mean()
    vol_ok = out["volume"] >= out["avg_volume"] * float(cfg["volume_multiplier"])

    bullish_reclaim = (
        (out["low"] < out["prior_low"])
        & (out["close"] > out["prior_low"])
        & (out["close"] > out["open"])
    )
    bearish_reclaim = (
        (out["high"] > out["prior_high"])
        & (out["close"] < out["prior_high"])
        & (out["close"] < out["open"])
    )
    if bool(cfg.get("require_trend", True)):
        bullish_reclaim &= out["close"] > out["ema"]
        bearish_reclaim &= out["close"] < out["ema"]

    out["long_signal"] = bullish_reclaim & vol_ok
    out["short_signal"] = bearish_reclaim & vol_ok
    return out


def order_levels(row: pd.Series, side: str, cfg: dict) -> tuple[float, float, float]:
    entry = float(row["close"])
    buffer_fraction = float(cfg["stop_buffer_pct"]) / 100
    if side == "buy":
        stop = float(row["low"]) * (1 - buffer_fraction)
        target = entry + (entry - stop) * float(cfg["reward_to_risk"])
    else:
        stop = float(row["high"]) * (1 + buffer_fraction)
        target = entry - (stop - entry) * float(cfg["reward_to_risk"])
    return entry, stop, target


def position_quantity(equity: float, entry: float, stop: float, cfg: dict) -> int:
    risk_dollars = equity * float(cfg["risk_per_trade_pct"]) / 100
    per_share_risk = abs(entry - stop)
    if per_share_risk <= 0 or entry <= 0:
        return 0
    by_risk = int(risk_dollars / per_share_risk)
    by_allocation = int((equity * float(cfg["max_position_pct"]) / 100) / entry)
    return max(0, min(by_risk, by_allocation))

