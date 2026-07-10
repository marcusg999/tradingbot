"""Risk management: position sizing, stops, kill switch, exposure caps.

The free functions are pure (numbers in, numbers out) so they can be unit
tested exhaustively. ``RiskManager`` wraps them with a ``Config`` for use by
the live loop and backtester.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------
def hard_stop_price(entry_price: float, hard_stop_pct: float) -> float:
    """Absolute price of the hard stop (a fixed % below entry)."""
    return entry_price * (1.0 - hard_stop_pct)


def trailing_stop_price(high_water_mark: float, trail_pct: float) -> float:
    """Trailing stop price given the highest price seen since entry."""
    return high_water_mark * (1.0 - trail_pct)


def should_activate_trail(entry_price: float, current_price: float,
                          trail_activate_pct: float) -> bool:
    """Trailing stop arms only after price gains at least the activation %."""
    if entry_price <= 0:
        return False
    return current_price >= entry_price * (1.0 + trail_activate_pct)


def position_size(equity: float, entry_price: float, stop_price: float,
                  risk_per_trade: float) -> float:
    """Quantity such that a stop-out loses ``risk_per_trade`` of equity.

    qty = (equity * risk_per_trade) / (entry_price - stop_price)

    Returns 0.0 if inputs are degenerate (non-positive risk distance, etc.).
    """
    if equity <= 0 or entry_price <= 0:
        return 0.0
    risk_distance = entry_price - stop_price
    if risk_distance <= 0:
        return 0.0
    risk_amount = equity * risk_per_trade
    return risk_amount / risk_distance


def kill_switch_triggered(day_start_equity: float, current_equity: float,
                          daily_kill_pct: float) -> bool:
    """True once equity has dropped by ``daily_kill_pct`` from day start."""
    if day_start_equity <= 0:
        return False
    drawdown = (day_start_equity - current_equity) / day_start_equity
    return drawdown >= daily_kill_pct


def exposure_ok(current_exposure_value: float, new_order_value: float,
                equity: float, max_exposure_pct: float) -> bool:
    """True if adding ``new_order_value`` keeps total exposure within the cap."""
    if equity <= 0:
        return False
    projected = current_exposure_value + new_order_value
    return projected <= equity * max_exposure_pct + 1e-9


@dataclass(frozen=True)
class SanityResult:
    ok: bool
    reason: str


def sanity_check_order(order_value: float, equity: float, max_order_pct: float,
                       data_age_sec: float, max_staleness_sec: float,
                       min_notional: float = 0.0) -> SanityResult:
    """Pre-flight checks run before *every* order submission."""
    if equity <= 0:
        return SanityResult(False, "non_positive_equity")
    if order_value <= 0:
        return SanityResult(False, "non_positive_order_value")
    if order_value < min_notional:
        return SanityResult(False,
                            f"below_min_notional({order_value:.2f}<{min_notional})")
    if order_value > equity * max_order_pct + 1e-9:
        return SanityResult(False,
                            f"order_exceeds_{max_order_pct:.0%}_of_equity")
    if data_age_sec > max_staleness_sec:
        return SanityResult(False,
                            f"stale_data({data_age_sec:.0f}s>{max_staleness_sec}s)")
    return SanityResult(True, "ok")


# --------------------------------------------------------------------------
# Manager
# --------------------------------------------------------------------------
@dataclass
class SizedOrder:
    qty: float
    entry_price: float
    stop_price: float
    order_value: float
    accepted: bool
    reason: str


class RiskManager:
    """Config-bound convenience layer over the pure functions above."""

    def __init__(self, config) -> None:
        self.c = config

    def hard_stop(self, entry_price: float) -> float:
        return hard_stop_price(entry_price, self.c.hard_stop_pct)

    def kill_switch(self, day_start_equity: float, equity: float) -> bool:
        return kill_switch_triggered(day_start_equity, equity,
                                     self.c.daily_kill_pct)

    def plan_entry(self, equity: float, entry_price: float,
                   current_exposure_value: float, data_age_sec: float) -> SizedOrder:
        """Full entry-sizing pipeline with every risk cap applied.

        Order of operations:
          1. Size from the 2%-risk stop distance.
          2. Clamp so notional never exceeds ``max_order_pct`` of equity.
          3. Clamp so total exposure never exceeds ``max_exposure_pct``.
          4. Run the sanity checks; reject if any fail.
        """
        stop = self.hard_stop(entry_price)
        qty = position_size(equity, entry_price, stop, self.c.risk_per_trade)
        if qty <= 0:
            return SizedOrder(0, entry_price, stop, 0, False, "zero_size")

        # Cap 1: single-order notional.
        max_order_value = equity * self.c.max_order_pct
        if qty * entry_price > max_order_value:
            qty = max_order_value / entry_price

        # Cap 2: aggregate exposure headroom.
        exposure_headroom = equity * self.c.max_exposure_pct - current_exposure_value
        if exposure_headroom <= 0:
            return SizedOrder(0, entry_price, stop, 0, False, "exposure_cap_full")
        if qty * entry_price > exposure_headroom:
            qty = exposure_headroom / entry_price

        order_value = qty * entry_price
        sanity = sanity_check_order(
            order_value, equity, self.c.max_order_pct,
            data_age_sec, self.c.max_data_staleness_sec, self.c.min_notional,
        )
        if not sanity.ok:
            return SizedOrder(qty, entry_price, stop, order_value, False,
                              sanity.reason)
        return SizedOrder(qty, entry_price, stop, order_value, True, "ok")

    def update_trailing_stop(self, entry_price: float, high_water_mark: float,
                             current_price: float,
                             current_stop: float) -> Tuple[float, bool]:
        """Return (new_stop_price, changed).

        Once price is up ``trail_activate_pct`` from entry, the stop ratchets
        up to ``trail_pct`` below the high-water mark, but never moves down.
        """
        hwm = max(high_water_mark, current_price)
        if not should_activate_trail(entry_price, hwm, self.c.trail_activate_pct):
            return current_stop, False
        candidate = trailing_stop_price(hwm, self.c.trail_pct)
        if candidate > current_stop:
            return candidate, True
        return current_stop, False
