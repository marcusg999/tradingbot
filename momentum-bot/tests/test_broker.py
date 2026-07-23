import types

import pytest

from broker import Broker, floor_qty, round_price
from config import normalize_symbol


def test_floor_qty_never_rounds_up():
    # The classic bug: round(0.7306202966318405, 9) rounds UP to 0.730620297,
    # which exceeds holdings and gets the sell/stop rejected. floor_qty must
    # round DOWN so the quantity never exceeds what's held.
    q = 0.7306202966318405
    assert floor_qty(q) <= q
    assert floor_qty(q) == 0.730620296
    assert round(q, 9) > q            # demonstrates the hazard we avoid


def test_floor_qty_zero_and_negative():
    assert floor_qty(0.0) == 0.0
    assert floor_qty(-1.0) == 0.0


def test_floor_qty_exact_value_unchanged():
    assert floor_qty(0.5) == 0.5
    assert floor_qty(1.234567890) == pytest.approx(1.23456789)


def test_normalize_slash_form_passthrough():
    assert normalize_symbol("BTC/USD") == "BTC/USD"
    assert normalize_symbol("eth/usd") == "ETH/USD"


def test_normalize_collapsed_form():
    assert normalize_symbol("BTCUSD") == "BTC/USD"
    assert normalize_symbol("SOLUSDT") == "SOL/USDT"
    assert normalize_symbol("DOGEUSDC") == "DOGE/USDC"


def test_normalize_unknown_quote_unchanged():
    assert normalize_symbol("FOO") == "FOO"


def test_round_price_dollar_assets_two_decimals():
    assert round_price(97123.456) == 97123.46
    assert round_price(1.0) == 1.0


def test_round_price_subdollar_keeps_precision():
    # DOGE-scale: 2dp rounding would turn 0.09678 into 0.10 (10x the stop
    # distance); 6 significant digits keeps it intact.
    assert round_price(0.09678) == pytest.approx(0.09678)
    # SHIB-scale must not collapse to zero.
    assert round_price(0.0000234567) == pytest.approx(2.34567e-05)
    assert round_price(0.0000234567) > 0


class _FakePos:
    def __init__(self, symbol, asset_id):
        self.symbol = symbol
        self.asset_id = asset_id


def _broker_with_positions(positions, closed_calls):
    b = Broker.__new__(Broker)  # skip __init__ (no network)
    b.trading = types.SimpleNamespace(
        get_all_positions=lambda: positions,
        close_position=lambda ident: closed_calls.append(ident) or "ok")
    return b


def test_close_position_prefers_asset_id():
    # Alpaca returns crypto positions in slash form; we match and close by the
    # unambiguous asset-id UUID rather than by symbol.
    calls = []
    b = _broker_with_positions([_FakePos("BTC/USD", "uuid-btc")], calls)
    b.close_position("BTC/USD")
    assert calls == ["uuid-btc"]


def test_close_position_matches_collapsed_symbol_form():
    calls = []
    b = _broker_with_positions([_FakePos("BTCUSD", "uuid-btc")], calls)
    b.close_position("BTC/USD")   # normalizes both sides before matching
    assert calls == ["uuid-btc"]


def test_close_position_falls_back_to_stripped_symbol():
    # No matching position found -> fall back to the collapsed symbol, never
    # the slash form (which would corrupt the URL path).
    calls = []
    b = _broker_with_positions([], calls)
    b.close_position("BTC/USD")
    assert calls == ["BTCUSD"]
