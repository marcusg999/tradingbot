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

import argparse
import logging
import os
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
    def __init__(self, reset_kill_switch: bool = False) -> None:
        self._reset_kill_switch = reset_kill_switch
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

    def _sync_closed_positions(self, positions: Dict[str, PositionInfo]) -> None:
        """Book tracked positions that no longer exist at Alpaca.

        A server-side stop that fills between cycles closes the position
        without the bot's involvement; without this the exit never reaches
        the trade ledger. The 120s grace period avoids false positives from
        the positions endpoint lagging a just-filled entry.
        """
        for sym, rec in self.state.all_positions().items():
            if sym in positions:
                continue
            try:
                opened = datetime.fromisoformat(rec.opened_at)
                age = (datetime.now(timezone.utc) - opened).total_seconds()
                if age < 120:
                    continue
            except ValueError:
                pass
            self.log.warning("%s closed at Alpaca (stop fill) — booking exit "
                             "at ~%.6g", sym, rec.stop_price)
            pnl = (rec.stop_price - rec.entry_price) * rec.qty
            self.state.record_trade(
                symbol=sym, side="SELL", qty=rec.qty, entry=rec.entry_price,
                exit=rec.stop_price, stop=rec.stop_price, pnl=pnl,
                reason_entry="", reason_exit="stop_filled")
            self.state.delete_position(sym)
            emoji = "🟢" if pnl >= 0 else "🔴"
            self.notifier.send(f"{emoji} Stop filled",
                               f"{sym} @ ~{rec.stop_price:.6g} | "
                               f"P&L ~{pnl:+.2f}")

    def _check_stop_gap(self, symbol: str, current_price: float) -> None:
        """Force a market exit if price gapped through an unfilled stop-limit.

        A stop_limit protects against normal moves, but a violent drop can
        blow through the limit band and leave the order resting unfilled
        while the position keeps losing.
        """
        rec = self.state.get_position(symbol)
        if rec is None or current_price <= 0:
            return
        gap_floor = rec.stop_price * (1.0 - 2 * Broker.STOP_LIMIT_SLIPPAGE)
        if current_price >= gap_floor:
            return
        self.log.critical("%s price %.6g gapped below stop %.6g — forcing "
                          "market exit", symbol, current_price, rec.stop_price)
        self.exit(symbol, current_price, "stop_gap_forced_exit")

    def enter(self, symbol: str, price: float, equity: float,
              exposure: float, data_age: float, reason: str) -> float:
        """Attempt an entry. Returns the notional actually deployed (0.0 if
        rejected or failed) so the caller can keep its exposure total live."""
        plan = self.risk.plan_entry(equity, price, exposure, data_age)
        if not plan.accepted:
            log_event(self.log, logging.INFO, "entry rejected", symbol=symbol,
                      reason=plan.reason, price=price, equity=equity)
            return 0.0
        self.log.info("ENTER %s qty=%.8f @~%.2f stop=%.2f (%s)", symbol,
                      plan.qty, price, plan.stop_price, reason)
        try:
            order = self.broker.submit_market_buy(symbol, plan.qty)
        except Exception as exc:
            self.log.error("market buy failed %s: %s", symbol, exc)
            return 0.0
        avg_price, filled_qty = self._wait_fill(order)
        fill_price = avg_price or price
        qty = filled_qty or plan.qty
        # Recompute the stop off the actual fill.
        stop = self.risk.hard_stop(fill_price)

        # Persist the position BEFORE attempting the stop. If the stop submit
        # then fails, the position is already tracked, so the per-cycle
        # _ensure_stop_order will re-arm it — it never ends up untracked and
        # unprotected.
        rec = PositionRecord(
            symbol=symbol, qty=qty, entry_price=fill_price,
            stop_price=stop, stop_order_id=None,
            high_water_mark=fill_price, trail_active=False,
            opened_at=datetime.now(timezone.utc).isoformat())
        self.state.upsert_position(rec)
        self.state.record_trade(
            symbol=symbol, side="BUY", qty=qty, entry=fill_price,
            exit=None, stop=stop, pnl=None, reason_entry=reason, reason_exit="")

        try:
            stop_order = self.broker.submit_stop_sell(symbol, qty, stop)
            rec.stop_order_id = getattr(stop_order, "id", None)
            self.state.upsert_position(rec)
        except Exception as exc:  # pragma: no cover
            self.log.error("stop submit failed %s: %s — trying to close to "
                           "stay flat", symbol, exc)
            try:
                self.broker.close_position(symbol)
                # Close succeeded: book the immediate unwind and untrack.
                pnl = (fill_price - rec.entry_price) * qty
                self.state.record_trade(
                    symbol=symbol, side="SELL", qty=qty, entry=fill_price,
                    exit=fill_price, stop=stop, pnl=pnl, reason_entry="",
                    reason_exit="stop_submit_failed_closed")
                self.state.delete_position(symbol)
                return 0.0
            except Exception:
                # Both failed: position stays tracked and _ensure_stop_order
                # will re-arm the stop next cycle. Alert loudly meanwhile.
                self.log.critical("UNPROTECTED POSITION %s — stop submit and "
                                  "close both failed; will retry stop next "
                                  "cycle", symbol)
                self.notifier.send("⚠️ UNPROTECTED POSITION",
                                   f"{symbol}: stop failed on entry; retrying "
                                   f"each cycle. Intervene if it persists.")
                return qty * fill_price

        self.notifier.send("🟢 Opened position",
                           f"{symbol} {qty:.6f} @ {fill_price:.2f}\n"
                           f"stop {stop:.2f} | {reason}")
        return qty * fill_price

    def exit(self, symbol: str, price: float, reason: str) -> None:
        rec = self.state.get_position(symbol)
        if rec is None:
            return
        self.log.info("EXIT %s (%s)", symbol, reason)
        if rec.stop_order_id:
            self.broker.cancel_order(rec.stop_order_id)
        try:
            close_order = self.broker.close_position(symbol)
        except Exception as exc:
            # The stop was already cancelled; the position must not be left
            # naked. Re-arm the stop at its previous level before bailing.
            self.log.error("close_position failed %s: %s — re-arming stop",
                           symbol, exc)
            try:
                order = self.broker.submit_stop_sell(symbol, rec.qty,
                                                     rec.stop_price)
                rec.stop_order_id = getattr(order, "id", None)
                self.state.upsert_position(rec)
            except Exception:
                self.log.critical("UNPROTECTED POSITION %s — exit failed and "
                                  "stop re-arm failed; manual action required",
                                  symbol)
                self.notifier.send("⚠️ UNPROTECTED POSITION",
                                   f"{symbol}: exit failed and stop could not "
                                   f"be re-armed. Intervene manually.")
            return
        fill_price, _ = self._wait_fill(close_order)
        exit_price = fill_price or price
        pnl = (exit_price - rec.entry_price) * rec.qty
        self.state.record_trade(
            symbol=symbol, side="SELL", qty=rec.qty, entry=rec.entry_price,
            exit=exit_price, stop=rec.stop_price, pnl=pnl,
            reason_entry="", reason_exit=reason)
        self.state.delete_position(symbol)
        emoji = "🟢" if pnl >= 0 else "🔴"
        self.notifier.send(f"{emoji} Closed position",
                           f"{symbol} @ {exit_price:.2f} | P&L {pnl:+.2f} | {reason}")

    def _wait_fill(self, order, attempts: int = 5,
                   delay: float = 1.0) -> Tuple[Optional[float], Optional[float]]:
        """Poll an order for (avg_fill_price, filled_qty). Best-effort."""
        oid = getattr(order, "id", None)
        if oid is None:
            return None, None
        for _ in range(attempts):
            try:
                fresh = self.broker.trading.get_order_by_id(oid)
            except Exception:  # pragma: no cover
                break
            avg = getattr(fresh, "filled_avg_price", None)
            if avg:
                fq = getattr(fresh, "filled_qty", None)
                return float(avg), (float(fq) if fq else None)
            time.sleep(delay)
        return None, None

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------
    def run_once(self) -> None:
        equity = self.broker.get_equity()
        day_start = self.state.roll_day_if_needed(equity)
        self.state.record_equity(equity)  # time-series for the dashboard chart

        if self.check_kill_switch(day_start, equity):
            self.log.critical("TRADING HALTED (kill switch). Reason: %s. "
                              "Restart with --reset-kill-switch (or "
                              "RESET_KILL_SWITCH=yes) to resume.",
                              self.state.kill_switch_reason())
            return

        positions = self.broker.list_positions()
        self._sync_closed_positions(positions)
        exposure = self._current_exposure(positions)
        unrealized = sum(p.unrealized_pl for p in positions.values())

        for symbol in self.c.symbols:
            try:
                # Accumulate notional deployed this cycle so a later symbol's
                # entry is checked against exposure including earlier entries.
                exposure += self._process_symbol(symbol, equity, positions,
                                                 exposure)
            except Exception as exc:  # pragma: no cover - never kill the loop
                self.log.exception("error processing %s: %s", symbol, exc)

        log_event(self.log, logging.INFO, "cycle",
                  equity=round(equity, 2), day_start=round(day_start, 2),
                  exposure=round(exposure, 2),
                  unrealized_pl=round(unrealized, 2),
                  open_positions=list(positions.keys()))

    def _process_symbol(self, symbol: str, equity: float,
                        positions: Dict[str, PositionInfo],
                        exposure: float) -> float:
        """Returns the notional deployed by an entry this cycle (0.0 if none)."""
        bars = self.broker.get_bars(symbol, self._lookback_start())
        closed, latest = self._split_bars(bars)
        if latest is None or not closed:
            self.log.info("%s: insufficient bar data", symbol)
            return 0.0

        current_price = latest.close
        norm = normalize_symbol(symbol)
        have_position = norm in positions

        # Staleness = time since the newest CLOSED candle finished forming.
        # (Measuring from the forming bar's open would mark perfectly live
        # data "stale" from minute 5 of every hour.)
        interval = timedelta(hours=self.c.timeframe_hours)
        last_closed_ts = closed[-1].timestamp
        close_time = last_closed_ts
        if close_time.tzinfo is None:
            close_time = close_time.replace(tzinfo=timezone.utc)
        data_age = max(0.0, (datetime.now(timezone.utc) -
                             (close_time + interval)).total_seconds())

        # Fast path every cycle: trailing-stop ratchet, then verify a live
        # server-side stop actually exists (heals any failed cancel/replace),
        # then force out if price gapped through an unfilled stop-limit.
        if have_position:
            self.manage_trailing_stop(norm, current_price)
            self._ensure_stop_order(norm)
            self._check_stop_gap(norm, current_price)

        # Strategy path: only on a freshly closed candle.
        if self._last_candle_ts.get(symbol) == last_closed_ts:
            return 0.0
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
                return 0.0
            return self.enter(symbol, current_price, equity, exposure,
                              data_age, result.reason)
        elif result.signal == Signal.EXIT_LONG and have_position:
            self.exit(norm, current_price, result.reason)
        return 0.0

    def run(self) -> None:
        if not self._ready:
            return
        if self._reset_kill_switch and self.state.is_kill_switch_active():
            self.log.warning("KILL SWITCH MANUALLY RESET (was: %s). If you "
                             "set RESET_KILL_SWITCH=yes, unset it now so a "
                             "future halt isn't silently cleared on restart.",
                             self.state.kill_switch_reason())
            self.state.set_kill_switch(False, "manual_reset")
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
    parser = argparse.ArgumentParser(description="momentum-bot trading loop")
    parser.add_argument(
        "--reset-kill-switch", action="store_true",
        help="clear a persisted kill-switch halt, then start trading")
    args = parser.parse_args()
    reset = args.reset_kill_switch or (
        os.environ.get("RESET_KILL_SWITCH", "").strip().lower()
        in {"1", "true", "yes"})
    Engine(reset_kill_switch=reset).run()


if __name__ == "__main__":
    main()
