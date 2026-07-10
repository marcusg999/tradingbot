"""Entry point and main trading loop.

Design:
  * One poll every ``poll_interval_sec`` (default 60s) drives *both* the fast
    path (kill switch + trailing-stop maintenance) and, when a new hourly
    candle has closed, the strategy path (entries/exits).
  * Alpaca is the source of truth. On boot we reconcile local state against
    live positions/orders before trading.
  * Stops live server-side at Alpaca, so a crash or redeploy never leaves a
    position unprotected. SIGTERM persists state and exits WITHOUT closing
    positions.
"""
from __future__ import annotations

import logging
import signal
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from alpaca.trading.enums import OrderSide

import config as config_module
from broker import Broker, Candle, PositionInfo, normalize_symbol
from logger import Notifier, get_logger, log_event
from risk import RiskManager
from state import PositionRecord, State
from strategy import Signal, generate_signal


class Engine:
    def __init__(self) -> None:
        self.c = config_module.load_config()
        self.log = get_logger("momentum-bot", self.c.log_level, self.c.log_json)
        self.notifier = Notifier(self.c, self.log)
        self.risk = RiskManager(self.c)
        self._shutdown = False
        self._ready = False
        # Last closed-candle timestamp we have acted on, per symbol.
        self._last_candle_ts: Dict[str, Optional[datetime]] = {
            s: None for s in self.c.symbols}
        self._log_startup_banner()

        # Credentials are required to construct the Alpaca clients. Fail clean
        # with a helpful message rather than a raw SDK traceback.
        if not self.c.api_key or not self.c.api_secret:
            self.log.error("ALPACA_API_KEY / ALPACA_API_SECRET not set. "
                           "Copy .env.example to .env and add your paper keys. "
                           "Exiting.")
            return
        self.state = State(self.c.state_db_path, self.c.trades_csv_path)
        self.broker = Broker(self.c)
        self._ready = True

    # ------------------------------------------------------------------
    def _log_startup_banner(self) -> None:
        if self.c.live:
            self.log.warning("=== LIVE TRADING ENABLED — REAL MONEY AT RISK ===")
        else:
            reason = ""
            if self.c.trading_mode_raw.lower() == "live":
                reason = " (TRADING_MODE=live but I_UNDERSTAND_REAL_MONEY!=yes)"
            self.log.warning("Running in PAPER mode%s. No real money at risk.",
                             reason)
        log_event(self.log, logging.INFO, "config loaded",
                  symbols=self.c.symbols, mode=self.c.mode_label,
                  ema_fast=self.c.ema_fast, ema_slow=self.c.ema_slow,
                  rsi=(self.c.rsi_low, self.c.rsi_high),
                  risk_per_trade=self.c.risk_per_trade)

    # ------------------------------------------------------------------
    # Signals & lifecycle
    # ------------------------------------------------------------------
    def install_signal_handlers(self) -> None:
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

    def _handle_signal(self, signum, _frame) -> None:
        self.log.warning("received signal %s — shutting down gracefully "
                         "(positions stay open, stops live at Alpaca)", signum)
        self._shutdown = True

    # ------------------------------------------------------------------
    # Bars
    # ------------------------------------------------------------------
    def _lookback_start(self) -> datetime:
        span = max(self.c.ema_slow, self.c.rsi_period) + 60
        hours = span * self.c.timeframe_hours
        return datetime.now(timezone.utc) - timedelta(hours=hours)

    def _split_bars(self, bars: List[Candle]) -> Tuple[List[Candle], Optional[Candle]]:
        """Split into (closed_candles, latest_bar).

        The last bar may still be forming; it is excluded from signal input
        but returned separately for current-price / staleness use.
        """
        if not bars:
            return [], None
        latest = bars[-1]
        now = datetime.now(timezone.utc)
        interval = timedelta(hours=self.c.timeframe_hours)
        ts = latest.timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts + interval > now:  # still forming
            return bars[:-1], latest
        return bars, latest

    # ------------------------------------------------------------------
    # Reconciliation (idempotent restart)
    # ------------------------------------------------------------------
    def reconcile(self) -> None:
        self.log.info("reconciling local state against Alpaca...")
        live = self.broker.list_positions()
        tracked = self.state.all_positions()

        # Positions Alpaca has that we forgot → adopt them and ensure a stop.
        for sym, pos in live.items():
            if sym not in tracked:
                stop = self.risk.hard_stop(pos.avg_entry_price)
                rec = PositionRecord(
                    symbol=sym, qty=pos.qty, entry_price=pos.avg_entry_price,
                    stop_price=stop, stop_order_id=None,
                    high_water_mark=max(pos.avg_entry_price, pos.current_price),
                    trail_active=False,
                    opened_at=datetime.now(timezone.utc).isoformat())
                self.state.upsert_position(rec)
                self.log.warning("adopted untracked position %s qty=%s", sym,
                                 pos.qty)
            self._ensure_stop_order(sym)

        # Positions we tracked that Alpaca no longer has → closed while we were
        # down (almost certainly a stop fill). Book the exit and forget them.
        for sym, rec in tracked.items():
            if sym not in live:
                self.log.warning("tracked position %s gone at Alpaca — booking "
                                 "closed", sym)
                self.state.record_trade(
                    symbol=sym, side="SELL", qty=rec.qty,
                    entry=rec.entry_price, exit=rec.stop_price,
                    stop=rec.stop_price, pnl=None,
                    reason_entry="", reason_exit="closed_while_down")
                self.state.delete_position(sym)
        self.log.info("reconcile complete: %d live position(s)", len(live))

    def _ensure_stop_order(self, symbol: str) -> None:
        """Guarantee a live server-side stop order backs the position."""
        rec = self.state.get_position(symbol)
        if rec is None:
            return
        open_orders = self.broker.list_open_orders(symbol)
        has_stop = any(
            getattr(o, "side", None) == OrderSide.SELL and
            getattr(o, "stop_price", None) is not None
            for o in open_orders)
        if has_stop:
            return
        try:
            order = self.broker.submit_stop_sell(symbol, rec.qty, rec.stop_price)
            rec.stop_order_id = getattr(order, "id", None)
            self.state.upsert_position(rec)
            self.log.warning("re-armed missing stop for %s at %.2f", symbol,
                             rec.stop_price)
        except Exception as exc:  # pragma: no cover
            self.log.error("failed to arm stop for %s: %s", symbol, exc)

    # ------------------------------------------------------------------
    # Kill switch
    # ------------------------------------------------------------------
    def check_kill_switch(self, day_start_equity: float, equity: float) -> bool:
        if self.state.is_kill_switch_active():
            return True
        if self.risk.kill_switch(day_start_equity, equity):
            drop = (day_start_equity - equity) / day_start_equity
            msg = (f"KILL SWITCH: equity {equity:.2f} is {drop:.1%} below day "
                   f"start {day_start_equity:.2f}. Closing all, halting.")
            self.log.critical(msg)
            try:
                self.broker.cancel_all_orders()
                self.broker.close_all_positions()
            except Exception as exc:  # pragma: no cover
                self.log.error("kill-switch teardown error: %s", exc)
            for sym in list(self.state.all_positions().keys()):
                self.state.delete_position(sym)
            self.state.set_kill_switch(True, msg)
            self.notifier.send("🚨 KILL SWITCH TRIGGERED", msg)
            return True
        return False

    # ------------------------------------------------------------------
    # Position management
    # ------------------------------------------------------------------
    def manage_trailing_stop(self, symbol: str, current_price: float) -> None:
        rec = self.state.get_position(symbol)
        if rec is None or current_price <= 0:
            return
        new_stop, changed = self.risk.update_trailing_stop(
            rec.entry_price, rec.high_water_mark, current_price, rec.stop_price)
        rec.high_water_mark = max(rec.high_water_mark, current_price)
        if changed:
            # Replace the server-side stop: cancel old, submit new.
            if rec.stop_order_id:
                self.broker.cancel_order(rec.stop_order_id)
            try:
                order = self.broker.submit_stop_sell(symbol, rec.qty, new_stop)
                rec.stop_order_id = getattr(order, "id", None)
                rec.stop_price = new_stop
                rec.trail_active = True
                log_event(self.log, logging.INFO, "trailing stop raised",
                          symbol=symbol, new_stop=round(new_stop, 2),
                          price=current_price)
                self.notifier.send("Trailing stop raised",
                                   f"{symbol} → {new_stop:.2f} (px {current_price:.2f})")
            except Exception as exc:  # pragma: no cover
                self.log.error("failed to raise trailing stop %s: %s", symbol, exc)
        self.state.upsert_position(rec)

    def _current_exposure(self, positions: Dict[str, PositionInfo]) -> float:
        return sum(abs(p.market_value) for p in positions.values())

    def enter(self, symbol: str, price: float, equity: float,
              exposure: float, data_age: float, reason: str) -> None:
        plan = self.risk.plan_entry(equity, price, exposure, data_age)
        if not plan.accepted:
            log_event(self.log, logging.INFO, "entry rejected", symbol=symbol,
                      reason=plan.reason, price=price, equity=equity)
            return
        self.log.info("ENTER %s qty=%.8f @~%.2f stop=%.2f (%s)", symbol,
                      plan.qty, price, plan.stop_price, reason)
        try:
            order = self.broker.submit_market_buy(symbol, plan.qty)
        except Exception as exc:
            self.log.error("market buy failed %s: %s", symbol, exc)
            return
        fill_price = self._wait_fill(order) or price
        # Recompute the stop off the actual fill.
        stop = self.risk.hard_stop(fill_price)
        stop_order_id = None
        try:
            stop_order = self.broker.submit_stop_sell(symbol, plan.qty, stop)
            stop_order_id = getattr(stop_order, "id", None)
        except Exception as exc:  # pragma: no cover
            self.log.error("stop submit failed %s: %s — closing to stay flat",
                           symbol, exc)
            try:
                self.broker.close_position(symbol)
            except Exception:
                pass
            return
        rec = PositionRecord(
            symbol=symbol, qty=plan.qty, entry_price=fill_price,
            stop_price=stop, stop_order_id=stop_order_id,
            high_water_mark=fill_price, trail_active=False,
            opened_at=datetime.now(timezone.utc).isoformat())
        self.state.upsert_position(rec)
        self.state.record_trade(
            symbol=symbol, side="BUY", qty=plan.qty, entry=fill_price,
            exit=None, stop=stop, pnl=None, reason_entry=reason, reason_exit="")
        self.notifier.send("🟢 Opened position",
                           f"{symbol} {plan.qty:.6f} @ {fill_price:.2f}\n"
                           f"stop {stop:.2f} | {reason}")

    def exit(self, symbol: str, price: float, reason: str) -> None:
        rec = self.state.get_position(symbol)
        if rec is None:
            return
        self.log.info("EXIT %s (%s)", symbol, reason)
        if rec.stop_order_id:
            self.broker.cancel_order(rec.stop_order_id)
        try:
            self.broker.close_position(symbol)
        except Exception as exc:
            self.log.error("close_position failed %s: %s", symbol, exc)
            return
        exit_price = price
        pnl = (exit_price - rec.entry_price) * rec.qty
        self.state.record_trade(
            symbol=symbol, side="SELL", qty=rec.qty, entry=rec.entry_price,
            exit=exit_price, stop=rec.stop_price, pnl=pnl,
            reason_entry="", reason_exit=reason)
        self.state.delete_position(symbol)
        emoji = "🟢" if pnl >= 0 else "🔴"
        self.notifier.send(f"{emoji} Closed position",
                           f"{symbol} @ {exit_price:.2f} | P&L {pnl:+.2f} | {reason}")

    def _wait_fill(self, order, attempts: int = 5, delay: float = 1.0) -> Optional[float]:
        """Poll an order for its average fill price. Best-effort."""
        oid = getattr(order, "id", None)
        if oid is None:
            return None
        for _ in range(attempts):
            try:
                fresh = self.broker.trading.get_order_by_id(oid)
            except Exception:  # pragma: no cover
                break
            avg = getattr(fresh, "filled_avg_price", None)
            if avg:
                return float(avg)
            time.sleep(delay)
        return None

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------
    def run_once(self) -> None:
        equity = self.broker.get_equity()
        day_start = self.state.roll_day_if_needed(equity)

        if self.check_kill_switch(day_start, equity):
            self.log.critical("TRADING HALTED (kill switch). Reason: %s. "
                              "Manual restart required.",
                              self.state.kill_switch_reason())
            return

        positions = self.broker.list_positions()
        exposure = self._current_exposure(positions)
        unrealized = sum(p.unrealized_pl for p in positions.values())

        for symbol in self.c.symbols:
            try:
                self._process_symbol(symbol, equity, positions, exposure)
            except Exception as exc:  # pragma: no cover - never kill the loop
                self.log.exception("error processing %s: %s", symbol, exc)

        log_event(self.log, logging.INFO, "cycle",
                  equity=round(equity, 2), day_start=round(day_start, 2),
                  exposure=round(exposure, 2),
                  unrealized_pl=round(unrealized, 2),
                  open_positions=list(positions.keys()))

    def _process_symbol(self, symbol: str, equity: float,
                        positions: Dict[str, PositionInfo],
                        exposure: float) -> None:
        bars = self.broker.get_bars(symbol, self._lookback_start())
        closed, latest = self._split_bars(bars)
        if latest is None or not closed:
            self.log.info("%s: insufficient bar data", symbol)
            return

        current_price = latest.close
        data_age = self.broker.candle_age_seconds(latest)
        norm = normalize_symbol(symbol)
        have_position = norm in positions

        # Fast path: keep the trailing stop current every cycle.
        if have_position:
            self.manage_trailing_stop(norm, current_price)

        # Strategy path: only on a freshly closed candle.
        last_closed_ts = closed[-1].timestamp
        if self._last_candle_ts.get(symbol) == last_closed_ts:
            return
        self._last_candle_ts[symbol] = last_closed_ts

        candle_dicts = [c.as_dict() for c in closed]
        result = generate_signal(
            candle_dicts, in_position=have_position,
            fast_period=self.c.ema_fast, slow_period=self.c.ema_slow,
            rsi_period=self.c.rsi_period, rsi_low=self.c.rsi_low,
            rsi_high=self.c.rsi_high)

        log_event(self.log, logging.INFO, "signal", symbol=symbol,
                  signal=result.signal.value, reason=result.reason,
                  price=result.price,
                  fast_ema=None if result.fast_ema is None else round(result.fast_ema, 2),
                  slow_ema=None if result.slow_ema is None else round(result.slow_ema, 2),
                  rsi=None if result.rsi is None else round(result.rsi, 1))

        if result.signal == Signal.ENTER_LONG and not have_position:
            if data_age > self.c.max_data_staleness_sec:
                self.log.warning("%s: skip entry, stale data %.0fs", symbol,
                                 data_age)
                return
            self.enter(symbol, current_price, equity, exposure, data_age,
                       result.reason)
        elif result.signal == Signal.EXIT_LONG and have_position:
            self.exit(norm, current_price, result.reason)

    def run(self) -> None:
        if not self._ready:
            return
        self.install_signal_handlers()
        try:
            self.reconcile()
        except Exception as exc:
            self.log.exception("reconcile failed: %s", exc)

        self.log.info("entering main loop (poll every %ds)",
                      self.c.poll_interval_sec)
        while not self._shutdown:
            start = time.monotonic()
            try:
                self.run_once()
            except Exception as exc:  # pragma: no cover
                self.log.exception("cycle error: %s", exc)
            elapsed = time.monotonic() - start
            sleep_for = max(1.0, self.c.poll_interval_sec - elapsed)
            # Sleep in small slices so SIGTERM is honored promptly.
            slept = 0.0
            while slept < sleep_for and not self._shutdown:
                time.sleep(min(1.0, sleep_for - slept))
                slept += 1.0

        self.state.close()
        self.log.info("shutdown complete. positions remain open; stops live at "
                      "Alpaca.")


def main() -> None:
    Engine().run()


if __name__ == "__main__":
    main()
