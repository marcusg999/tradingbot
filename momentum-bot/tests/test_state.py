import os

import pytest

from state import PositionRecord, State


@pytest.fixture
def store(tmp_path):
    db = tmp_path / "state.db"
    csv = tmp_path / "trades.csv"
    s = State(str(db), str(csv))
    yield s
    s.close()


def test_day_roll_sets_start_equity(store):
    eq = store.roll_day_if_needed(10_000.0)
    assert eq == 10_000.0
    # Same day: should not overwrite even if equity moved.
    eq2 = store.roll_day_if_needed(9_000.0)
    assert eq2 == 10_000.0


def test_kill_switch_persist(store):
    assert store.is_kill_switch_active() is False
    store.set_kill_switch(True, "test reason")
    assert store.is_kill_switch_active() is True
    assert store.kill_switch_reason() == "test reason"


def test_new_day_does_NOT_clear_kill_switch(store):
    # The halt must persist across day rolls (and restarts) until an operator
    # explicitly resets it — no silent resume at UTC midnight.
    store.roll_day_if_needed(10_000.0)
    store.set_kill_switch(True, "halt")
    store._set_meta("day_start_date", "1970-01-01")
    store.roll_day_if_needed(10_000.0)
    assert store.is_kill_switch_active() is True
    # ...and an explicit reset clears it.
    store.set_kill_switch(False, "manual_reset")
    assert store.is_kill_switch_active() is False


def test_position_crud(store):
    rec = PositionRecord(
        symbol="BTC/USD", qty=0.5, entry_price=60000, stop_price=58200,
        stop_order_id="abc", high_water_mark=60000, trail_active=False,
        opened_at="2026-01-01T00:00:00+00:00")
    store.upsert_position(rec)
    got = store.get_position("BTC/USD")
    assert got is not None and got.qty == 0.5 and got.stop_order_id == "abc"

    rec.stop_price = 59000
    rec.trail_active = True
    store.upsert_position(rec)
    got = store.get_position("BTC/USD")
    assert got.stop_price == 59000 and got.trail_active is True

    assert "BTC/USD" in store.all_positions()
    store.delete_position("BTC/USD")
    assert store.get_position("BTC/USD") is None


def test_trade_ledger_writes_csv(store, tmp_path):
    store.record_trade(symbol="BTC/USD", side="BUY", qty=0.1, entry=60000,
                       exit=None, stop=58200, pnl=None,
                       reason_entry="ema_cross_up_rsi_ok", reason_exit="")
    store.record_trade(symbol="BTC/USD", side="SELL", qty=0.1, entry=60000,
                       exit=63000, stop=58200, pnl=300.0,
                       reason_entry="", reason_exit="ema_cross_down")
    with open(store.trades_csv_path) as fh:
        lines = fh.read().strip().splitlines()
    # header + 2 rows
    assert len(lines) == 3
    assert lines[0].startswith("timestamp,symbol,side")
    assert "SELL" in lines[2] and "300.0" in lines[2]
