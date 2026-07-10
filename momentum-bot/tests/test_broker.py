import pytest

from broker import round_price
from config import normalize_symbol


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
