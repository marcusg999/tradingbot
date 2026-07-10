"""Pure momentum-strategy logic.

Nothing in this module performs I/O. It takes candle data in and returns
signals out, so ``backtest.py`` and ``main.py`` (live) run *identical* logic.

A "candle" is a mapping with at least a ``close`` key (float). Helper
functions also accept plain sequences of floats for easy unit testing.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import List, Mapping, Optional, Sequence


class Signal(str, Enum):
    ENTER_LONG = "ENTER_LONG"
    EXIT_LONG = "EXIT_LONG"
    HOLD = "HOLD"


@dataclass(frozen=True)
class SignalResult:
    """Signal plus the indicator snapshot that produced it (for logging)."""
    signal: Signal
    price: Optional[float]
    fast_ema: Optional[float]
    slow_ema: Optional[float]
    prev_fast_ema: Optional[float]
    prev_slow_ema: Optional[float]
    rsi: Optional[float]
    reason: str


def ema(values: Sequence[float], period: int) -> List[Optional[float]]:
    """Exponential moving average.

    Returns a list the same length as ``values``. Entries before the EMA is
    seeded (index < period-1) are ``None``. The seed is a simple average of
    the first ``period`` values, then the standard EMA recurrence applies.
    """
    if period <= 0:
        raise ValueError("EMA period must be positive")
    n = len(values)
    out: List[Optional[float]] = [None] * n
    if n < period:
        return out

    multiplier = 2.0 / (period + 1.0)
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, n):
        prev = (values[i] - prev) * multiplier + prev
        out[i] = prev
    return out


def rsi(values: Sequence[float], period: int) -> List[Optional[float]]:
    """Wilder's RSI.

    Returns a list the same length as ``values`` with ``None`` until enough
    data exists (index < period). Uses Wilder's smoothing of average gains
    and losses. A zero average loss yields RSI 100.
    """
    if period <= 0:
        raise ValueError("RSI period must be positive")
    n = len(values)
    out: List[Optional[float]] = [None] * n
    if n <= period:
        return out

    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        change = values[i] - values[i - 1]
        if change >= 0:
            gains += change
        else:
            losses -= change
    avg_gain = gains / period
    avg_loss = losses / period
    out[period] = _rsi_from_avgs(avg_gain, avg_loss)

    for i in range(period + 1, n):
        change = values[i] - values[i - 1]
        gain = change if change > 0 else 0.0
        loss = -change if change < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out[i] = _rsi_from_avgs(avg_gain, avg_loss)
    return out


def _rsi_from_avgs(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def crossed_above(prev_fast: float, prev_slow: float,
                  curr_fast: float, curr_slow: float) -> bool:
    """True when the fast line crosses from at-or-below to strictly above.

    The exact-touch case (prev_fast == prev_slow, then curr_fast > curr_slow)
    counts as a fresh cross up.
    """
    return prev_fast <= prev_slow and curr_fast > curr_slow


def crossed_below(prev_fast: float, prev_slow: float,
                  curr_fast: float, curr_slow: float) -> bool:
    """True when the fast line crosses from at-or-above to strictly below."""
    return prev_fast >= prev_slow and curr_fast < curr_slow


def _closes(candles: Sequence[Mapping[str, float]]) -> List[float]:
    return [float(c["close"]) for c in candles]


def generate_signal(
    candles: Sequence[Mapping[str, float]],
    *,
    in_position: bool,
    fast_period: int = 20,
    slow_period: int = 50,
    rsi_period: int = 14,
    rsi_low: float = 50.0,
    rsi_high: float = 70.0,
) -> SignalResult:
    """Evaluate the dual-EMA + RSI momentum rule on the latest closed candle.

    Entry (only when flat): fast EMA crosses above slow EMA AND RSI is within
    ``[rsi_low, rsi_high]``.
    Exit (only when in a position): fast EMA crosses below slow EMA.
    Stops (hard/trailing) are handled by ``risk.py`` + server-side orders, not
    here, so this stays a pure trend signal.
    """
    closes = _closes(candles)
    price = closes[-1] if closes else None

    fast = ema(closes, fast_period)
    slow = ema(closes, slow_period)
    rsi_vals = rsi(closes, rsi_period)

    # Need at least two fully-formed EMA points to detect a crossover.
    if len(closes) < slow_period + 1:
        return SignalResult(Signal.HOLD, price, None, None, None, None, None,
                            "insufficient_data")

    curr_fast, curr_slow = fast[-1], slow[-1]
    prev_fast, prev_slow = fast[-2], slow[-2]
    curr_rsi = rsi_vals[-1]

    if None in (curr_fast, curr_slow, prev_fast, prev_slow):
        return SignalResult(Signal.HOLD, price, curr_fast, curr_slow,
                            prev_fast, prev_slow, curr_rsi, "insufficient_data")

    if in_position:
        if crossed_below(prev_fast, prev_slow, curr_fast, curr_slow):
            return SignalResult(Signal.EXIT_LONG, price, curr_fast, curr_slow,
                                prev_fast, prev_slow, curr_rsi,
                                "ema_cross_down")
        return SignalResult(Signal.HOLD, price, curr_fast, curr_slow,
                            prev_fast, prev_slow, curr_rsi, "in_position_hold")

    # Flat: look for entry.
    if crossed_above(prev_fast, prev_slow, curr_fast, curr_slow):
        if curr_rsi is None:
            return SignalResult(Signal.HOLD, price, curr_fast, curr_slow,
                                prev_fast, prev_slow, curr_rsi,
                                "rsi_unavailable")
        if rsi_low <= curr_rsi <= rsi_high:
            return SignalResult(Signal.ENTER_LONG, price, curr_fast, curr_slow,
                                prev_fast, prev_slow, curr_rsi,
                                "ema_cross_up_rsi_ok")
        return SignalResult(Signal.HOLD, price, curr_fast, curr_slow,
                            prev_fast, prev_slow, curr_rsi,
                            f"ema_cross_up_rsi_filtered({curr_rsi:.1f})")

    return SignalResult(Signal.HOLD, price, curr_fast, curr_slow,
                        prev_fast, prev_slow, curr_rsi, "no_cross")
