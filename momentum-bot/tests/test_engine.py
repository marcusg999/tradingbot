"""Engine order-path tests with a fake broker.

Exercises the entry/exit/stop lifecycle in main.Engine without touching the
network, including the failure-hardening paths (stop submit fails; close also
fails) that are otherwise only reachable against a live broker.
"""
import types
from datetime import datetime, timedelta, timezone

import pytest
from alpaca.trading.enums import OrderSide

import config as config_module
import main
from logger import Notifier, get_logger
from risk import RiskManager
from state import PositionRecord, State

SIG_TS = datetime(2026, 7, 10, 15, 0, tzinfo=timezone.utc)


class FakeOrder:
    def __init__(self, oid, avg=None, qty=None, side=None, stop=None):
        self.id = oid
        self.filled_avg_price = avg
        self.filled_qty = qty
        self.side = side
        self.stop_price = stop


class FakeBroker:
    def __init__(self):
        self.stops = []          # live stop orders
        self.positions = {}      # symbol -> qty (models Alpaca positions)
        self.closed = []
        self.cancelled = []
        self.replaced = []       # (order_id, stop_price) of atomic replaces
        self.buy_coids = []      # client_order_ids seen on buys
        self.stop_coids = []     # client_order_ids seen on stop submits
        self.fail_close = False
        self.fail_stop = False
        self.orders = {}
        self.trading = types.SimpleNamespace(get_order_by_id=self._get_order)

    def _get_order(self, oid):
        return self.orders.get(oid, FakeOrder(oid, avg=100.0, qty=1.0))

    def get_equity(self):
        return 10_000.0

    def list_positions(self):
        return dict(self.positions)

    def list_open_orders(self, symbol=None):
        return list(self.stops)

    def submit_market_buy(self, symbol, qty, client_order_id=None):
        self.buy_coids.append(client_order_id)
        self.positions[symbol] = qty      # position now exists at "Alpaca"
        o = FakeOrder("buy-" + symbol, avg=100.0, qty=qty)
        self.orders[o.id] = o
        return o

    def submit_stop_sell(self, symbol, qty, stop_price, client_order_id=None):
        if self.fail_stop:
            raise RuntimeError("stop rejected")
        self.stop_coids.append(client_order_id)
        o = FakeOrder("stop-" + symbol, side=OrderSide.SELL, stop=stop_price)
        self.stops.append(o)
        self.orders[o.id] = o
        return o

    def replace_stop(self, order_id, stop_price, client_order_id=None):
        self.replaced.append((order_id, stop_price))
        for o in self.stops:
            if o.id == order_id:
                o.stop_price = stop_price
                return o
        o = FakeOrder(order_id, side=OrderSide.SELL, stop=stop_price)
        self.stops.append(o)
        return o

    def position_exists(self, symbol):
        return symbol in self.positions

    def cancel_order(self, oid):
        self.cancelled.append(oid)
        self.stops = [o for o in self.stops if o.id != oid]

    def cancel_all_orders(self):
        self.stops.clear()

    def close_position(self, symbol):
        if self.fail_close:
            raise RuntimeError("close failed")
        self.positions.pop(symbol, None)
        self.closed.append(symbol)
        return FakeOrder("close-" + symbol, avg=105.0, qty=1.0)

    def close_all_positions(self):
        self.positions.clear()


@pytest.fixture
def engine(tmp_path):
    eng = main.Engine.__new__(main.Engine)
    eng.c = config_module.load_config()
    eng.log = get_logger("test-engine", "CRITICAL", False)
    eng.notifier = Notifier(eng.c, eng.log)
    eng.risk = RiskManager(eng.c)
    eng.state = State(str(tmp_path / "s.db"), str(tmp_path / "t.csv"))
    eng.broker = FakeBroker()
    eng._shutdown = False
    eng._ready = True
    eng._last_candle_ts = {}
    yield eng
    eng.state.close()


def test_enter_creates_protected_position(engine):
    notional = engine.enter("BTC/USD", 100.0, 10_000.0, 0.0, 10.0, "reason", SIG_TS)
    rec = engine.state.get_position("BTC/USD")
    assert rec is not None
    assert rec.stop_order_id is not None
    assert rec.stop_price == pytest.approx(97.0, abs=0.5)
    assert notional > 0
    assert len(engine.broker.stops) == 1


def test_enter_stop_fail_but_close_ok_stays_flat(engine):
    engine.broker.fail_stop = True
    notional = engine.enter("BTC/USD", 100.0, 10_000.0, 0.0, 10.0, "reason", SIG_TS)
    # Stop couldn't be placed, so the entry was unwound: no position, no stop.
    assert engine.state.get_position("BTC/USD") is None
    assert notional == 0.0
    assert engine.broker.closed  # close was attempted and succeeded


def test_enter_stop_fail_and_close_fail_keeps_tracked(engine):
    # The critical invariant: a double failure must NOT leave an untracked
    # position. It stays tracked so _ensure_stop_order re-arms it next cycle.
    engine.broker.fail_stop = True
    engine.broker.fail_close = True
    notional = engine.enter("BTC/USD", 100.0, 10_000.0, 0.0, 10.0, "reason", SIG_TS)
    rec = engine.state.get_position("BTC/USD")
    assert rec is not None            # still tracked
    assert rec.stop_order_id is None  # no stop yet
    assert notional > 0               # capital is deployed

    # Next cycle: broker recovers, _ensure_stop_order re-arms the stop.
    engine.broker.fail_stop = False
    engine._ensure_stop_order("BTC/USD")
    rec2 = engine.state.get_position("BTC/USD")
    assert rec2.stop_order_id is not None
    assert len(engine.broker.stops) == 1


def test_trailing_stop_raise(engine):
    engine.enter("BTC/USD", 100.0, 10_000.0, 0.0, 10.0, "reason", SIG_TS)
    engine.manage_trailing_stop("BTC/USD", 120.0)   # +20%
    rec = engine.state.get_position("BTC/USD")
    assert rec.stop_price > 97.0
    assert rec.trail_active is True


def test_trailing_uses_atomic_replace_not_cancel(engine):
    # The trailing ratchet must move the stop via replace (no cancel/resubmit
    # gap where the position is momentarily unprotected).
    engine.enter("BTC/USD", 100.0, 10_000.0, 0.0, 10.0, "reason", SIG_TS)
    engine.manage_trailing_stop("BTC/USD", 120.0)
    assert engine.broker.replaced            # replace_stop was used
    assert not engine.broker.cancelled       # no cancel of the live stop


def test_entry_and_stop_have_client_order_ids(engine):
    engine.enter("BTC/USD", 100.0, 10_000.0, 0.0, 10.0, "reason", SIG_TS)
    # Entry id is deterministic per signal bar so retries can't duplicate it.
    assert engine.broker.buy_coids == [main.entry_coid("BTC/USD", SIG_TS)]
    assert engine.broker.buy_coids[0] == f"e-BTCUSD-{int(SIG_TS.timestamp())}"
    # Stop submission also carries an id (dedupes SDK/network retries).
    assert engine.broker.stop_coids and engine.broker.stop_coids[0] is not None


def test_exit_mid_cycle_stop_fill_books_not_rearms(engine):
    # Position vanished at Alpaca (stop filled) AND close raises: must book the
    # exit, not re-arm a phantom stop for a position that no longer exists.
    engine.enter("BTC/USD", 100.0, 10_000.0, 0.0, 10.0, "reason", SIG_TS)
    engine.broker.positions.pop("BTC/USD")   # gone at Alpaca
    engine.broker.fail_close = True          # close now errors ("not found")
    engine.broker.stops.clear()
    engine.exit("BTC/USD", 95.0, "ema_cross_down")
    assert engine.state.get_position("BTC/USD") is None   # booked + untracked
    assert len(engine.broker.stops) == 0                  # no phantom re-arm


def test_exit_books_pnl_and_clears(engine):
    engine.enter("BTC/USD", 100.0, 10_000.0, 0.0, 10.0, "reason", SIG_TS)
    engine.exit("BTC/USD", 105.0, "ema_cross_down")
    assert engine.state.get_position("BTC/USD") is None
    assert engine.broker.closed  # a close order was placed


def test_exit_close_fail_rearms_stop(engine):
    engine.enter("BTC/USD", 100.0, 10_000.0, 0.0, 10.0, "reason", SIG_TS)
    engine.broker.fail_close = True
    engine.broker.stops.clear()
    engine.exit("BTC/USD", 90.0, "ema_cross_down")
    # Close failed -> position kept, stop re-armed, not left naked.
    assert engine.state.get_position("BTC/USD") is not None
    assert len(engine.broker.stops) == 1


def test_stop_gap_forces_exit(engine):
    engine.enter("SOL/USD", 100.0, 10_000.0, 0.0, 10.0, "reason", SIG_TS)
    engine._check_stop_gap("SOL/USD", 90.0)  # below stop*(1-2*slippage)
    assert engine.state.get_position("SOL/USD") is None


def test_sync_books_vanished_position(engine):
    old = (datetime.now(timezone.utc) - timedelta(seconds=300)).isoformat()
    engine.state.upsert_position(PositionRecord(
        symbol="DOGE/USD", qty=10.0, entry_price=0.1, stop_price=0.097,
        stop_order_id="s1", high_water_mark=0.1, trail_active=False,
        opened_at=old))
    engine._sync_closed_positions({})  # Alpaca shows no positions
    assert engine.state.get_position("DOGE/USD") is None


def test_sync_grace_keeps_recent_position(engine):
    recent = datetime.now(timezone.utc).isoformat()
    engine.state.upsert_position(PositionRecord(
        symbol="LTC/USD", qty=1.0, entry_price=50, stop_price=48.5,
        stop_order_id="s2", high_water_mark=50, trail_active=False,
        opened_at=recent))
    engine._sync_closed_positions({})
    assert engine.state.get_position("LTC/USD") is not None
