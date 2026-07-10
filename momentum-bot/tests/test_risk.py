from types import SimpleNamespace

import pytest

from risk import (RiskManager, exposure_ok, hard_stop_price,
                  kill_switch_triggered, position_size, sanity_check_order,
                  should_activate_trail, trailing_stop_price)


def make_config(**overrides):
    base = dict(
        risk_per_trade=0.02,
        hard_stop_pct=0.03,
        trail_activate_pct=0.04,
        trail_pct=0.025,
        daily_kill_pct=0.05,
        max_exposure_pct=0.50,
        max_order_pct=0.25,
        max_data_staleness_sec=300,
        min_notional=1.0,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# --- position sizing -------------------------------------------------------
def test_position_size_2pct_risk_math():
    # equity 10k, entry 100, stop 97 -> risk $200 over $3 distance = 66.666..
    qty = position_size(10_000, 100, 97, 0.02)
    assert qty == pytest.approx(200 / 3)
    # A stop-out loses exactly 2% of equity.
    assert qty * (100 - 97) == pytest.approx(200.0)


def test_position_size_zero_on_bad_distance():
    assert position_size(10_000, 100, 100, 0.02) == 0.0
    assert position_size(10_000, 100, 105, 0.02) == 0.0
    assert position_size(0, 100, 97, 0.02) == 0.0


def test_hard_stop_price():
    assert hard_stop_price(100, 0.03) == pytest.approx(97.0)


# --- trailing stop ---------------------------------------------------------
def test_trail_activation_boundary():
    # +4% activation: exactly +4% activates.
    assert should_activate_trail(100, 104, 0.04) is True
    assert should_activate_trail(100, 103.99, 0.04) is False


def test_trailing_stop_price_value():
    assert trailing_stop_price(120, 0.025) == pytest.approx(117.0)


def test_manager_trailing_only_ratchets_up():
    rm = RiskManager(make_config())
    # Not yet activated (only +2%): stop unchanged.
    stop, changed = rm.update_trailing_stop(100, 102, 102, 97)
    assert changed is False and stop == 97
    # +10%: activates, trail = 110*0.975 = 107.25 > 97 -> raise.
    stop, changed = rm.update_trailing_stop(100, 110, 110, 97)
    assert changed is True and stop == pytest.approx(107.25)
    # Price falls back: stop must not drop.
    stop2, changed2 = rm.update_trailing_stop(100, 110, 105, stop)
    assert changed2 is False and stop2 == pytest.approx(107.25)


# --- kill switch -----------------------------------------------------------
def test_kill_switch_triggers_at_threshold():
    assert kill_switch_triggered(10_000, 9_500, 0.05) is True   # exactly -5%
    assert kill_switch_triggered(10_000, 9_501, 0.05) is False  # -4.99%
    assert kill_switch_triggered(10_000, 9_499, 0.05) is True   # -5.01%


def test_kill_switch_safe_on_zero_start():
    assert kill_switch_triggered(0, 0, 0.05) is False


# --- exposure cap ----------------------------------------------------------
def test_exposure_cap():
    # 50% cap on 10k = 5k. Already 3k deployed; a 2k order is exactly at cap.
    assert exposure_ok(3_000, 2_000, 10_000, 0.50) is True
    assert exposure_ok(3_000, 2_001, 10_000, 0.50) is False


# --- sanity checks ---------------------------------------------------------
def test_sanity_rejects_oversized_order():
    r = sanity_check_order(3_000, 10_000, 0.25, 10, 300)  # 30% > 25%
    assert r.ok is False and "exceeds" in r.reason


def test_sanity_accepts_within_limits():
    r = sanity_check_order(2_500, 10_000, 0.25, 10, 300)
    assert r.ok is True


def test_sanity_rejects_stale_data():
    r = sanity_check_order(1_000, 10_000, 0.25, 600, 300)  # 600s > 300s
    assert r.ok is False and "stale" in r.reason


def test_sanity_rejects_below_min_notional():
    r = sanity_check_order(0.5, 10_000, 0.25, 10, 300, min_notional=1.0)
    assert r.ok is False and "min_notional" in r.reason


# --- RiskManager.plan_entry (full pipeline) --------------------------------
def test_plan_entry_clamped_by_order_cap():
    rm = RiskManager(make_config())
    # Raw 2%-risk size would be huge relative to price; must clamp to 25%.
    plan = rm.plan_entry(equity=10_000, entry_price=100,
                         current_exposure_value=0, data_age_sec=10)
    assert plan.accepted is True
    assert plan.order_value == pytest.approx(2_500, rel=1e-6)  # 25% cap
    assert plan.qty == pytest.approx(25.0)


def test_plan_entry_respects_exposure_headroom():
    rm = RiskManager(make_config())
    # 4k already deployed; 50% cap = 5k -> only 1k headroom left.
    plan = rm.plan_entry(equity=10_000, entry_price=100,
                         current_exposure_value=4_000, data_age_sec=10)
    assert plan.accepted is True
    assert plan.order_value == pytest.approx(1_000, rel=1e-6)


def test_plan_entry_rejected_when_exposure_full():
    rm = RiskManager(make_config())
    plan = rm.plan_entry(equity=10_000, entry_price=100,
                         current_exposure_value=5_000, data_age_sec=10)
    assert plan.accepted is False and plan.reason == "exposure_cap_full"


def test_plan_entry_rejected_on_stale_data():
    rm = RiskManager(make_config())
    plan = rm.plan_entry(equity=10_000, entry_price=100,
                         current_exposure_value=0, data_age_sec=999)
    assert plan.accepted is False and "stale" in plan.reason
