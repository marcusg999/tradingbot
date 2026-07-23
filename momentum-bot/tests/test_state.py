import os

import pytest

import state as state_module
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


def test_quantize_money_foots_to_cents():
    from state import quantize_money
    assert quantize_money(0.1 + 0.2) == 0.3          # 0.30000000000000004 -> 0.3
    assert quantize_money(93.375) == 93.38 or quantize_money(93.375) == 93.37
    assert quantize_money(None) is None
    # Each quantized P&L is exact to cents (equals its own 2dp round), so the
    # ledger has no sub-cent drift and the displayed total foots.
    pnls = [quantize_money(x) for x in (10.005, 20.004, -5.006)]
    assert all(p == round(p, 2) for p in pnls)
    assert f"{sum(pnls):.2f}" == "24.99"


def test_recorded_pnl_is_quantized(store):
    store.record_trade(symbol="BTC/USD", side="SELL", qty=0.1,
                       entry=60000.0, exit=60000.0 + 1.0 / 3.0, stop=58200.0,
                       pnl=(1.0 / 3.0) * 0.1, reason_exit="x")  # 0.0333...
    row = store.conn.execute(
        "SELECT pnl FROM trades ORDER BY id DESC LIMIT 1").fetchone()
    assert row["pnl"] == 0.03            # quantized to cents


def test_equity_history_record_and_read(store):
    for i in range(5):
        store.record_equity(10_000 + i)
    hist = store.equity_history()
    assert len(hist) == 5
    assert hist[0][1] == 10_000 and hist[-1][1] == 10_004
    # Each entry is (timestamp, equity).
    assert isinstance(hist[0][0], str) and isinstance(hist[0][1], float)


def test_equity_history_limit(store):
    for i in range(10):
        store.record_equity(100 + i)
    assert len(store.equity_history(limit=3)) == 3
    assert store.equity_history(limit=3)[-1][1] == 109


def test_equity_history_prunes_to_cap(store, monkeypatch):
    monkeypatch.setattr(state_module, "MAX_EQUITY_POINTS", 4)
    for i in range(10):
        store.record_equity(float(i))
    hist = store.equity_history()
    # Bounded to ~MAX_EQUITY_POINTS most-recent rows.
    assert len(hist) <= 5
    assert hist[-1][1] == 9.0  # newest retained


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
