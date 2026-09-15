from __future__ import annotations

from itertools import product
from typing import Callable

import pandas as pd

from strategy import add_signals, order_levels


def backtest_r_values(bars: pd.DataFrame, cfg: dict) -> list[float]:
    if bars.empty:
        return []
    signals = add_signals(bars.copy(), cfg)
    trades: list[float] = []
    i = 0
    while i < len(signals) - 1:
        row = signals.iloc[i]
        side = "buy" if bool(row.long_signal) else "sell" if bool(row.short_signal) and cfg.get("allow_shorts", False) else ""
        if not side:
            i += 1
            continue
        entry = float(signals.iloc[i + 1].open)
        _, stop, _ = order_levels(row, side, cfg)
        risk = abs(entry - stop)
        if risk <= 0:
            i += 1
            continue
        target = entry + risk * cfg["reward_to_risk"] if side == "buy" else entry - risk * cfg["reward_to_risk"]
        exit_price, j = float(signals.iloc[-1].close), len(signals) - 1
        for j in range(i + 1, len(signals)):
            bar = signals.iloc[j]
            stopped = bar.low <= stop if side == "buy" else bar.high >= stop
            won = bar.high >= target if side == "buy" else bar.low <= target
            if stopped:
                exit_price = stop
                break
            if won:
                exit_price = target
                break
        trades.append((exit_price - entry) / risk if side == "buy" else (entry - exit_price) / risk)
        i = j + 1
    return trades


def metrics(values: list[float]) -> dict:
    if not values:
        return {"trades": 0, "total_r": 0.0, "expectancy_r": 0.0, "win_rate": 0.0,
                "profit_factor": 0.0, "max_drawdown_r": 0.0}
    series = pd.Series(values, dtype=float)
    curve = pd.concat([pd.Series([0.0]), series.cumsum()], ignore_index=True)
    drawdown = curve.cummax() - curve
    gains = float(series[series > 0].sum())
    losses = abs(float(series[series < 0].sum()))
    return {
        "trades": len(values), "total_r": round(float(series.sum()), 3),
        "expectancy_r": round(float(series.mean()), 3),
        "win_rate": round(float((series > 0).mean() * 100), 1),
        "profit_factor": round(gains / losses, 3) if losses else (999.0 if gains else 0.0),
        "max_drawdown_r": round(float(drawdown.max()), 3),
    }


def recommend_settings(base_cfg: dict, fetch: Callable[[str, dict], pd.DataFrame]) -> dict:
    training_days = int(base_cfg.get("optimizer_training_days", 60))
    validation_days = int(base_cfg.get("optimizer_validation_days", 20))
    minimum_trades = int(base_cfg.get("optimizer_minimum_validation_trades", 20))
    minimum_total_r = float(base_cfg.get("optimizer_minimum_validation_total_r", 1.0))
    max_drawdown = float(base_cfg.get("optimizer_maximum_validation_drawdown_r", 6.0))
    minimum_pf = float(base_cfg.get("optimizer_minimum_profit_factor", 1.1))
    symbols = list(base_cfg.get("symbols", []))
    candidates = list(product([5, 15, 30], [5, 10, 20], [1.2, 1.5, 2.0], [False, True], [1.5, 2.0]))
    cache: dict[tuple[str, int], pd.DataFrame] = {}
    for timeframe in {item[0] for item in candidates}:
        request_cfg = dict(base_cfg, timeframe_minutes=timeframe,
                           lookback_days=training_days + validation_days + 5)
        for symbol in symbols:
            cache[(symbol, timeframe)] = fetch(symbol, request_cfg)

    scored = []
    for timeframe, sweep, volume, trend, reward in candidates:
        candidate = dict(base_cfg, timeframe_minutes=timeframe, sweep_lookback=sweep,
                         volume_multiplier=volume, require_trend=trend, reward_to_risk=reward)
        train_r: list[float] = []
        validation_r: list[float] = []
        for symbol in symbols:
            bars = cache[(symbol, timeframe)]
            if bars.empty:
                continue
            cutoff = bars.index.max() - pd.Timedelta(days=validation_days)
            training_start = cutoff - pd.Timedelta(days=training_days)
            train_r.extend(backtest_r_values(bars[(bars.index >= training_start) & (bars.index < cutoff)], candidate))
            validation_r.extend(backtest_r_values(bars[bars.index >= cutoff], candidate))
        train = metrics(train_r)
        validation = metrics(validation_r)
        scored.append({"settings": {"timeframe_minutes": timeframe, "sweep_lookback": sweep,
                                     "volume_multiplier": volume, "require_trend": trend,
                                     "reward_to_risk": reward},
                       "training": train, "validation": validation})

    scored.sort(key=lambda item: (item["training"]["expectancy_r"], item["training"]["total_r"]), reverse=True)
    finalists = scored[:10]
    qualified = [item for item in finalists if
                 item["validation"]["trades"] >= minimum_trades and
                 item["validation"]["total_r"] >= minimum_total_r and
                 item["validation"]["max_drawdown_r"] <= max_drawdown and
                 item["validation"]["profit_factor"] >= minimum_pf]
    qualified.sort(key=lambda item: (item["validation"]["expectancy_r"], item["validation"]["total_r"]), reverse=True)
    return {"recommendation": qualified[0] if qualified else None, "finalists": finalists,
            "tested": len(scored), "minimum_validation_trades": minimum_trades}
