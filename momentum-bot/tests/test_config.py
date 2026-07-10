import importlib

import config as config_module


def reload_config(monkeypatch, env):
    for k, v in env.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)
    return config_module.load_config()


def test_defaults_to_paper(monkeypatch):
    c = reload_config(monkeypatch, {
        "TRADING_MODE": None, "I_UNDERSTAND_REAL_MONEY": None})
    assert c.live is False and c.paper is True
    assert c.symbols == ["BTC/USD", "ETH/USD"]


def test_live_requires_both_flags(monkeypatch):
    # Only one flag -> stays paper.
    c = reload_config(monkeypatch, {
        "TRADING_MODE": "live", "I_UNDERSTAND_REAL_MONEY": "no"})
    assert c.live is False

    c = reload_config(monkeypatch, {
        "TRADING_MODE": "paper", "I_UNDERSTAND_REAL_MONEY": "yes"})
    assert c.live is False


def test_live_enabled_with_both_flags(monkeypatch):
    c = reload_config(monkeypatch, {
        "TRADING_MODE": "live", "I_UNDERSTAND_REAL_MONEY": "yes"})
    assert c.live is True and c.paper is False
    assert c.mode_label == "LIVE"


def test_symbol_list_override(monkeypatch):
    c = reload_config(monkeypatch, {"SYMBOLS": "BTC/USD, SOL/USD ,DOGE/USD"})
    assert c.symbols == ["BTC/USD", "SOL/USD", "DOGE/USD"]


def test_numeric_overrides(monkeypatch):
    c = reload_config(monkeypatch, {
        "RISK_PER_TRADE": "0.01", "EMA_FAST": "9", "EMA_SLOW": "21"})
    assert c.risk_per_trade == 0.01 and c.ema_fast == 9 and c.ema_slow == 21
