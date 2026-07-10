"""Backtest the *exact* live strategy over historical Alpaca candles.

Reuses ``strategy.generate_signal`` and ``risk`` sizing/stop functions so the
simulation and the live bot share one implementation. Reports total return,
max drawdown, win rate, profit factor and trade count, and prints an honest
side-by-side against buy-and-hold.

Usage:
    python backtest.py --symbol BTC/USD --days 180
"""
from __future__ import annotations

import argparse
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from alpaca.data.historical import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

import config as config_module
from risk import (hard_stop_price, position_size, should_activate_trail,
                  trailing_stop_price)
from strategy import Signal, generate_signal


@dataclass
class BTCandle:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float

    def as_dict(self) -> dict:
        return {"timestamp": self.timestamp, "open": self.open,
                "high": self.high, "low": self.low, "close": self.close,
                "volume": self.volume}


@dataclass
class Trade:
    symbol: str
    entry_time: datetime
    entry_price: float
    qty: float
    exit_time: Optional[datetime] = None
    exit_price: Optional[float] = None
    reason_exit: str = ""

    @property
    def pnl(self) -> float:
        if self.exit_price is None:
            return 0.0
        return (self.exit_price - self.entry_price) * self.qty

    @property
    def return_pct(self) -> float:
        if self.exit_price is None or self.entry_price == 0:
            return 0.0
        return (self.exit_price - self.entry_price) / self.entry_price


def fetch_candles(symbol: str, days: int, timeframe_hours: int,
                  api_key: str = "", api_secret: str = "") -> List[BTCandle]:
    client = (CryptoHistoricalDataClient(api_key, api_secret)
              if api_key and api_secret else CryptoHistoricalDataClient())
    start = datetime.now(timezone.utc) - timedelta(days=days)
    req = CryptoBarsRequest(
        symbol_or_symbols=[symbol],
        timeframe=TimeFrame(timeframe_hours, TimeFrameUnit.Hour),
        start=start)
    bars = client.get_crypto_bars(req)
    raw = bars.data.get(symbol, []) if hasattr(bars, "data") else []
    return [BTCandle(b.timestamp, float(b.open), float(b.high), float(b.low),
                     float(b.close), float(b.volume)) for b in raw]


@dataclass
class BacktestResult:
    symbol: str
    days: int
    initial_equity: float
    final_equity: float
    trades: List[Trade] = field(default_factory=list)
    equity_curve: List[float] = field(default_factory=list)
    buy_hold_return: float = 0.0

    @property
    def total_return(self) -> float:
        if self.initial_equity == 0:
            return 0.0
        return (self.final_equity - self.initial_equity) / self.initial_equity

    @property
    def num_trades(self) -> int:
        return len([t for t in self.trades if t.exit_price is not None])

    @property
    def wins(self) -> List[Trade]:
        return [t for t in self.trades if t.exit_price is not None and t.pnl > 0]

    @property
    def losses(self) -> List[Trade]:
        return [t for t in self.trades if t.exit_price is not None and t.pnl <= 0]

    @property
    def win_rate(self) -> float:
        n = self.num_trades
        return len(self.wins) / n if n else 0.0

    @property
    def profit_factor(self) -> float:
        gross_win = sum(t.pnl for t in self.wins)
        gross_loss = -sum(t.pnl for t in self.losses)
        if gross_loss == 0:
            return float("inf") if gross_win > 0 else 0.0
        return gross_win / gross_loss

    @property
    def max_drawdown(self) -> float:
        peak = float("-inf")
        max_dd = 0.0
        for eq in self.equity_curve:
            peak = max(peak, eq)
            if peak > 0:
                dd = (peak - eq) / peak
                max_dd = max(max_dd, dd)
        return max_dd


def run_backtest(candles: List[BTCandle], cfg, symbol: str,
                 initial_equity: float) -> BacktestResult:
    """Bar-by-bar simulation mirroring the live decision flow.

    Each closed candle: if flat, evaluate the entry signal on close; if in a
    position, first check whether the intrabar low pierced the (possibly
    trailed) stop, then evaluate the exit signal on close.
    """
    result = BacktestResult(symbol=symbol, days=0,
                            initial_equity=initial_equity,
                            final_equity=initial_equity)
    cash = initial_equity
    open_trade: Optional[Trade] = None
    stop_price = 0.0
    high_water = 0.0

    # Warm-up: need slow EMA + a prior bar before any signal is valid.
    warmup = cfg.ema_slow + 1

    for i in range(len(candles)):
        bar = candles[i]
        equity = cash + (open_trade.qty * bar.close if open_trade else 0.0)

        if i < warmup:
            result.equity_curve.append(equity)
            continue

        window = [c.as_dict() for c in candles[: i + 1]]

        if open_trade is not None:
            # 1) Trailing-stop ratchet using this bar's high.
            high_water = max(high_water, bar.high)
            if should_activate_trail(open_trade.entry_price, high_water,
                                     cfg.trail_activate_pct):
                trail = trailing_stop_price(high_water, cfg.trail_pct)
                stop_price = max(stop_price, trail)

            # 2) Stop hit intrabar?
            if bar.low <= stop_price:
                open_trade.exit_time = bar.timestamp
                open_trade.exit_price = stop_price
                open_trade.reason_exit = "stop_hit"
                cash += open_trade.qty * stop_price
                result.trades.append(open_trade)
                open_trade = None
                result.equity_curve.append(cash)
                continue

            # 3) Signal-based exit on close.
            sig = generate_signal(
                window, in_position=True, fast_period=cfg.ema_fast,
                slow_period=cfg.ema_slow, rsi_period=cfg.rsi_period,
                rsi_low=cfg.rsi_low, rsi_high=cfg.rsi_high)
            if sig.signal == Signal.EXIT_LONG:
                open_trade.exit_time = bar.timestamp
                open_trade.exit_price = bar.close
                open_trade.reason_exit = sig.reason
                cash += open_trade.qty * bar.close
                result.trades.append(open_trade)
                open_trade = None
            result.equity_curve.append(
                cash + (open_trade.qty * bar.close if open_trade else 0.0))
            continue

        # Flat: look for an entry.
        sig = generate_signal(
            window, in_position=False, fast_period=cfg.ema_fast,
            slow_period=cfg.ema_slow, rsi_period=cfg.rsi_period,
            rsi_low=cfg.rsi_low, rsi_high=cfg.rsi_high)
        if sig.signal == Signal.ENTER_LONG:
            entry = bar.close
            stop_price = hard_stop_price(entry, cfg.hard_stop_pct)
            qty = position_size(equity, entry, stop_price, cfg.risk_per_trade)
            # Apply the same single-order / exposure caps as live.
            max_qty = (equity * min(cfg.max_order_pct, cfg.max_exposure_pct)) / entry
            qty = min(qty, max_qty)
            if qty > 0:
                high_water = entry
                cash -= qty * entry
                open_trade = Trade(symbol=symbol, entry_time=bar.timestamp,
                                   entry_price=entry, qty=qty)
        result.equity_curve.append(
            cash + (open_trade.qty * bar.close if open_trade else 0.0))

    # Mark-to-market any position still open at the end.
    if open_trade is not None:
        last = candles[-1]
        final_equity = cash + open_trade.qty * last.close
    else:
        final_equity = cash
    result.final_equity = final_equity

    if candles:
        first_close = candles[warmup].close if len(candles) > warmup else candles[0].close
        last_close = candles[-1].close
        result.buy_hold_return = (last_close - first_close) / first_close
    return result


def print_report(result: BacktestResult, days: int) -> None:
    r = result
    bh = r.buy_hold_return
    strat_ret = r.total_return
    line = "=" * 60
    print(f"\n{line}")
    print(f"  BACKTEST RESULTS — {r.symbol}  ({days} days, 1h candles)")
    print(line)
    rows = [
        ("Initial equity", f"${r.initial_equity:,.2f}"),
        ("Final equity", f"${r.final_equity:,.2f}"),
        ("Total return", f"{strat_ret:+.2%}"),
        ("Max drawdown", f"{r.max_drawdown:.2%}"),
        ("Win rate", f"{r.win_rate:.1%}  ({len(r.wins)}/{r.num_trades})"),
        ("Profit factor", f"{r.profit_factor:.2f}"),
        ("Number of trades", f"{r.num_trades}"),
    ]
    for label, val in rows:
        print(f"  {label:<20} {val:>18}")
    print(line)
    print("  BENCHMARK — buy & hold")
    print(f"  {'Buy & hold return':<20} {bh:>+18.2%}")
    edge = strat_ret - bh
    verdict = "OUTPERFORMED" if edge > 0 else "UNDERPERFORMED"
    print(f"  {'Strategy vs B&H':<20} {edge:>+17.2%}  ({verdict})")
    print(line)
    if strat_ret < bh:
        print("  NOTE: the strategy did NOT beat simply holding over this")
        print("  window. Momentum systems lag in trending/choppy regimes.")
    print(f"{line}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest the momentum strategy")
    parser.add_argument("--symbol", default="BTC/USD")
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--cash", type=float, default=10_000.0)
    args = parser.parse_args()

    cfg = config_module.load_config()
    candles = fetch_candles(args.symbol, args.days, cfg.timeframe_hours,
                            cfg.api_key, cfg.api_secret)
    if len(candles) < cfg.ema_slow + 2:
        print(f"Not enough candles ({len(candles)}) for {args.symbol}. "
              f"Need > {cfg.ema_slow + 2}. Check symbol / date range / keys.")
        return
    print(f"Fetched {len(candles)} candles for {args.symbol} "
          f"({candles[0].timestamp:%Y-%m-%d} → {candles[-1].timestamp:%Y-%m-%d})")
    result = run_backtest(candles, cfg, args.symbol, args.cash)
    result.days = args.days
    print_report(result, args.days)


if __name__ == "__main__":
    main()
