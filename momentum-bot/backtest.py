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
                 initial_equity: float, fee_bps: float = 0.0) -> BacktestResult:
    """Bar-by-bar simulation mirroring the live decision flow.

    Each closed candle: if flat, evaluate the entry signal on close; if in a
    position, first check the stop as it stood at the END of the PREVIOUS bar
    (a ratchet from this bar's high must not rescue this bar's low — the
    intrabar sequence is unknown), then ratchet the trail, then evaluate the
    signal exit on close. ``fee_bps`` is charged per side on notional.
    """
    result = BacktestResult(symbol=symbol, days=0,
                            initial_equity=initial_equity,
                            final_equity=initial_equity)
    fee = fee_bps / 10_000.0
    cash = initial_equity
    open_trade: Optional[Trade] = None
    stop_price = 0.0
    high_water = 0.0

    # Warm-up: need slow EMA + a prior bar before any signal is valid.
    warmup = cfg.ema_slow + 1

    # A signal detected on a bar's close can only be acted on afterwards, so
    # it executes at the NEXT bar's open — never at the signaling bar's own
    # close (which would be look-ahead: you can't transact at a price you only
    # know once the bar has closed). ``pending`` carries the deferred action.
    pending: Optional[str] = None  # "enter" | "exit" | None

    for i in range(len(candles)):
        bar = candles[i]

        # 1) Execute a pending signal at THIS bar's open.
        if pending == "enter" and open_trade is None:
            equity = cash
            entry = bar.open
            stop_price = hard_stop_price(entry, cfg.hard_stop_pct)
            qty = position_size(equity, entry, stop_price, cfg.risk_per_trade)
            max_qty = (equity * min(cfg.max_order_pct, cfg.max_exposure_pct)) / entry
            qty = min(qty, max_qty)
            if qty > 0:
                high_water = entry
                cash -= qty * entry * (1.0 + fee)
                open_trade = Trade(symbol=symbol, entry_time=bar.timestamp,
                                   entry_price=entry, qty=qty)
            pending = None
        elif pending == "exit" and open_trade is not None:
            open_trade.exit_time = bar.timestamp
            open_trade.exit_price = bar.open
            open_trade.reason_exit = "ema_cross_down_next_open"
            cash += open_trade.qty * bar.open * (1.0 - fee)
            result.trades.append(open_trade)
            open_trade = None
            pending = None

        equity = cash + (open_trade.qty * bar.close if open_trade else 0.0)
        if i < warmup:
            result.equity_curve.append(equity)
            continue

        window = [c.as_dict() for c in candles[: i + 1]]

        if open_trade is not None:
            # 2) Stop hit intrabar? Checked against the stop as it stood BEFORE
            #    this bar's high ratchets it (no intrabar look-ahead). Stops are
            #    price-triggered, so they DO fire within the bar (not deferred).
            if bar.low <= stop_price:
                open_trade.exit_time = bar.timestamp
                open_trade.exit_price = stop_price
                open_trade.reason_exit = "stop_hit"
                cash += open_trade.qty * stop_price * (1.0 - fee)
                result.trades.append(open_trade)
                open_trade = None
                result.equity_curve.append(cash)
                continue

            # 3) Trailing-stop ratchet using this bar's high — effective next bar.
            high_water = max(high_water, bar.high)
            if should_activate_trail(open_trade.entry_price, high_water,
                                     cfg.trail_activate_pct):
                trail = trailing_stop_price(high_water, cfg.trail_pct)
                stop_price = max(stop_price, trail)

            # 4) Signal exit → defer to next bar's open.
            sig = generate_signal(
                window, in_position=True, fast_period=cfg.ema_fast,
                slow_period=cfg.ema_slow, rsi_period=cfg.rsi_period,
                rsi_low=cfg.rsi_low, rsi_high=cfg.rsi_high)
            if sig.signal == Signal.EXIT_LONG:
                pending = "exit"
        else:
            # Flat: entry signal → defer to next bar's open.
            sig = generate_signal(
                window, in_position=False, fast_period=cfg.ema_fast,
                slow_period=cfg.ema_slow, rsi_period=cfg.rsi_period,
                rsi_low=cfg.rsi_low, rsi_high=cfg.rsi_high)
            if sig.signal == Signal.ENTER_LONG:
                pending = "enter"

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


def print_report(result: BacktestResult, days: int,
                 fee_bps: float = 0.0) -> None:
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
    if fee_bps > 0:
        print(f"  Fees modeled at {fee_bps:.0f} bps per side. Slippage is NOT")
        print("  modeled; real results will be somewhat worse.")
    else:
        print("  Fees/slippage NOT modeled (use --fee-bps 25 for Alpaca's")
        print("  taker fee); real results will be worse.")
    print(f"{line}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest the momentum strategy")
    parser.add_argument("--symbol", default="BTC/USD")
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--cash", type=float, default=10_000.0)
    parser.add_argument("--fee-bps", type=float, default=0.0,
                        help="fee per side in basis points (Alpaca taker ~25)")
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
    result = run_backtest(candles, cfg, args.symbol, args.cash,
                          fee_bps=args.fee_bps)
    result.days = args.days
    print_report(result, args.days, fee_bps=args.fee_bps)


if __name__ == "__main__":
    main()
