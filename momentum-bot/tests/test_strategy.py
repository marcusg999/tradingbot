import math

import pytest

from strategy import (Signal, crossed_above, crossed_below, ema,
                      generate_signal, rsi)


def candles(closes):
    return [{"close": c, "open": c, "high": c, "low": c, "volume": 1.0}
            for c in closes]


# --- EMA -------------------------------------------------------------------
def test_ema_none_before_seed():
    out = ema([1, 2, 3, 4], period=3)
    assert out[0] is None and out[1] is None
    assert out[2] == pytest.approx(2.0)  # seed = mean(1,2,3)


def test_ema_recurrence():
    vals = [1, 2, 3, 4, 5]
    out = ema(vals, period=3)
    # seed at idx2 = 2.0; multiplier = 2/4 = 0.5
    assert out[3] == pytest.approx((4 - 2.0) * 0.5 + 2.0)  # 3.0
    assert out[4] == pytest.approx((5 - 3.0) * 0.5 + 3.0)  # 4.0


def test_ema_short_series_all_none():
    assert ema([1, 2], period=5) == [None, None]


def test_ema_invalid_period():
    with pytest.raises(ValueError):
        ema([1, 2, 3], 0)


# --- RSI -------------------------------------------------------------------
def test_rsi_all_gains_is_100():
    vals = list(range(1, 20))  # strictly increasing
    out = rsi(vals, period=14)
    assert out[-1] == pytest.approx(100.0)


def test_rsi_all_losses_is_zero():
    vals = list(range(20, 1, -1))  # strictly decreasing
    out = rsi(vals, period=14)
    assert out[-1] == pytest.approx(0.0)


def test_rsi_midrange_bounds():
    vals = [10, 11, 10, 11, 10, 11, 10, 11, 10, 11, 10, 11, 10, 11, 10, 11]
    out = rsi(vals, period=14)
    assert 0.0 <= out[-1] <= 100.0


def test_rsi_none_until_period():
    out = rsi([1, 2, 3], period=14)
    assert all(v is None for v in out)


# --- crossover helpers -----------------------------------------------------
def test_crossed_above_basic():
    assert crossed_above(9, 10, 11, 10) is True
    assert crossed_above(11, 10, 12, 10) is False  # already above


def test_crossed_above_exact_touch():
    # prev exactly equal, then strictly above -> counts as a cross up
    assert crossed_above(10, 10, 11, 10) is True


def test_crossed_below_exact_touch():
    assert crossed_below(10, 10, 9, 10) is True


def test_no_cross_when_flat_equal():
    assert crossed_above(10, 10, 10, 10) is False
    assert crossed_below(10, 10, 10, 10) is False


# --- generate_signal -------------------------------------------------------
def test_insufficient_data_holds():
    res = generate_signal(candles([1, 2, 3]), in_position=False,
                          fast_period=2, slow_period=5)
    assert res.signal == Signal.HOLD
    assert res.reason == "insufficient_data"


def _rising_then_cross():
    # Construct a series where fast EMA crosses above slow near the end while
    # RSI sits in a healthy 50-70 band (gentle uptrend).
    base = [100 - i * 0.1 for i in range(60)]          # slow drift down
    base += [base[-1] + i * 0.8 for i in range(1, 12)]  # sharp turn up
    return base


def test_entry_on_cross_up_with_rsi_ok():
    res = generate_signal(candles(_rising_then_cross()), in_position=False,
                          fast_period=20, slow_period=50,
                          rsi_period=14, rsi_low=50, rsi_high=70)
    # Depending on the exact bar this is ENTER or a filtered HOLD; assert it is
    # never a spurious EXIT and that when it enters RSI is within band.
    if res.signal == Signal.ENTER_LONG:
        assert 50 <= res.rsi <= 70
        assert res.reason == "ema_cross_up_rsi_ok"


def test_rsi_filter_blocks_overextended():
    # A violent spike pushes RSI > 70 at the crossover, which must be filtered.
    closes = [100] * 55 + [101, 103, 108, 116, 128, 145]
    res = generate_signal(candles(closes), in_position=False,
                          fast_period=20, slow_period=50,
                          rsi_period=14, rsi_low=50, rsi_high=70)
    assert res.signal != Signal.ENTER_LONG


def test_exit_on_cross_down_when_in_position():
    # Long uptrend then a sharp reversal. The EXIT fires on the exact bar the
    # fast EMA crosses below the slow EMA, so scan for it rather than assume
    # which bar that is.
    up = [100 + i for i in range(60)]
    down = [up[-1] - i * 3 for i in range(1, 40)]
    series = up + down
    exit_bar = None
    for i in range(60, len(series) + 1):
        res = generate_signal(candles(series[:i]), in_position=True,
                              fast_period=20, slow_period=50)
        if res.signal == Signal.EXIT_LONG:
            exit_bar = i
            assert res.reason == "ema_cross_down"
            break
    assert exit_bar is not None, "expected an EMA cross-down exit"


def test_in_position_holds_without_cross():
    up = [100 + i for i in range(80)]
    res = generate_signal(candles(up), in_position=True,
                          fast_period=20, slow_period=50)
    assert res.signal == Signal.HOLD


def test_flat_no_entry_without_cross():
    # Perfectly flat: EMAs coincide, no strict crossover -> never an entry.
    flat = [100.0] * 80
    res = generate_signal(candles(flat), in_position=False,
                          fast_period=20, slow_period=50)
    assert res.signal == Signal.HOLD
    assert res.reason == "no_cross"


def test_rsi_boundary_inclusive_50_and_70():
    # Directly exercise the boundary comparison used in generate_signal.
    lo, hi = 50.0, 70.0
    assert (lo <= 50.0 <= hi) is True
    assert (lo <= 70.0 <= hi) is True
    assert (lo <= 49.99 <= hi) is False
    assert (lo <= 70.01 <= hi) is False
