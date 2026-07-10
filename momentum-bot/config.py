"""Central configuration for the momentum bot.

Every tunable is read from an environment variable with a safe, paper-mode
default. Nothing here performs I/O against a broker; this module only reads
process environment. Import ``load_config()`` to get an immutable snapshot.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional


def _get_str(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _get_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"Env var {name}={raw!r} is not a valid float")


def _get_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"Env var {name}={raw!r} is not a valid int")


def _get_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _get_list(name: str, default: List[str]) -> List[str]:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return list(default)
    return [item.strip() for item in raw.split(",") if item.strip()]


# Curated default universe: liquid, long-established USD crypto pairs that
# Alpaca lists. Override entirely with the SYMBOLS env var. Note the risk caps
# are account-wide (50% max exposure, ~25% per position), so a longer list
# widens the opportunity set without adding total risk — only ~2 positions can
# be open at once regardless of how many symbols are watched.
DEFAULT_SYMBOLS = [
    "BTC/USD", "ETH/USD", "SOL/USD", "DOGE/USD", "LTC/USD",
    "LINK/USD", "AVAX/USD", "UNI/USD", "AAVE/USD", "DOT/USD",
]

# Broader set of USD pairs Alpaca has listed, for reference when customizing
# SYMBOLS. Confirm availability in your Alpaca dashboard before relying on one;
# listings change, and thin alts whipsaw the EMA strategy harder — backtest
# each before adding it.
KNOWN_ALPACA_CRYPTO_USD = DEFAULT_SYMBOLS + [
    "BCH/USD", "XRP/USD", "SHIB/USD", "MKR/USD", "CRV/USD",
    "GRT/USD", "SUSHI/USD", "YFI/USD", "XTZ/USD", "BAT/USD",
]


def normalize_symbol(symbol: str) -> str:
    """Canonical 'BASE/QUOTE' crypto pair form.

    The data API requires the slash form; position endpoints return the
    collapsed form. Normalizing here means a user setting SYMBOLS=BTCUSD
    still gets working candle requests.
    """
    s = symbol.upper().strip()
    if "/" in s:
        return s
    for quote in ("USDT", "USDC", "USD"):
        if s.endswith(quote) and len(s) > len(quote):
            return f"{s[:-len(quote)]}/{quote}"
    return s


@dataclass(frozen=True)
class Config:
    # --- Alpaca credentials & endpoint -------------------------------------
    api_key: str
    api_secret: str
    # Resolved trading mode after applying the two-flag safety gate.
    live: bool
    # Raw values, kept for logging/diagnostics.
    trading_mode_raw: str
    understand_real_money_raw: str

    # --- Universe & data ---------------------------------------------------
    symbols: List[str]
    timeframe_hours: int  # candle size in hours (strategy designed for 1h)

    # --- Strategy tunables -------------------------------------------------
    ema_fast: int
    ema_slow: int
    rsi_period: int
    rsi_low: float
    rsi_high: float

    # --- Risk tunables -----------------------------------------------------
    risk_per_trade: float       # fraction of equity risked per trade (0.02 = 2%)
    hard_stop_pct: float        # 0.03 = stop 3% below entry
    trail_activate_pct: float   # 0.04 = arm trailing stop after +4%
    trail_pct: float            # 0.025 = trail by 2.5%
    daily_kill_pct: float       # 0.05 = halt after -5% on the day
    max_exposure_pct: float     # 0.50 = at most 50% of equity deployed
    max_order_pct: float        # 0.25 = reject single orders > 25% of equity
    max_data_staleness_sec: int # reject signals on candles older than this

    # --- Loop / runtime ----------------------------------------------------
    poll_interval_sec: int      # stop/kill-switch check cadence
    min_notional: float         # smallest order Alpaca will accept (crypto)

    # --- Persistence -------------------------------------------------------
    state_db_path: str
    trades_csv_path: str

    # --- Notifications (optional) -----------------------------------------
    discord_webhook_url: Optional[str]
    telegram_bot_token: Optional[str]
    telegram_chat_id: Optional[str]

    # --- Logging -----------------------------------------------------------
    log_level: str
    log_json: bool

    @property
    def paper(self) -> bool:
        return not self.live

    @property
    def mode_label(self) -> str:
        return "LIVE" if self.live else "PAPER"


def _resolve_live_mode(trading_mode_raw: str, understand_raw: str) -> bool:
    """Live trading requires BOTH flags. Anything else falls back to paper."""
    wants_live = trading_mode_raw.strip().lower() == "live"
    confirmed = understand_raw.strip().lower() == "yes"
    return wants_live and confirmed


def load_config() -> Config:
    # Load a local .env if python-dotenv is installed; real env vars win.
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    trading_mode_raw = _get_str("TRADING_MODE", "paper")
    understand_raw = _get_str("I_UNDERSTAND_REAL_MONEY", "no")
    live = _resolve_live_mode(trading_mode_raw, understand_raw)

    return Config(
        api_key=_get_str("ALPACA_API_KEY"),
        api_secret=_get_str("ALPACA_API_SECRET"),
        live=live,
        trading_mode_raw=trading_mode_raw,
        understand_real_money_raw=understand_raw,
        symbols=[normalize_symbol(s)
                 for s in _get_list("SYMBOLS", DEFAULT_SYMBOLS)],
        timeframe_hours=_get_int("TIMEFRAME_HOURS", 1),
        ema_fast=_get_int("EMA_FAST", 20),
        ema_slow=_get_int("EMA_SLOW", 50),
        rsi_period=_get_int("RSI_PERIOD", 14),
        rsi_low=_get_float("RSI_LOW", 50.0),
        rsi_high=_get_float("RSI_HIGH", 70.0),
        risk_per_trade=_get_float("RISK_PER_TRADE", 0.02),
        hard_stop_pct=_get_float("HARD_STOP_PCT", 0.03),
        trail_activate_pct=_get_float("TRAIL_ACTIVATE_PCT", 0.04),
        trail_pct=_get_float("TRAIL_PCT", 0.025),
        daily_kill_pct=_get_float("DAILY_KILL_PCT", 0.05),
        max_exposure_pct=_get_float("MAX_EXPOSURE_PCT", 0.50),
        max_order_pct=_get_float("MAX_ORDER_PCT", 0.25),
        max_data_staleness_sec=_get_int("MAX_DATA_STALENESS_SEC", 300),
        poll_interval_sec=_get_int("POLL_INTERVAL_SEC", 60),
        min_notional=_get_float("MIN_NOTIONAL", 1.0),
        state_db_path=_get_str("STATE_DB_PATH", "state.db"),
        trades_csv_path=_get_str("TRADES_CSV_PATH", "trades.csv"),
        discord_webhook_url=_get_str("DISCORD_WEBHOOK_URL") or None,
        telegram_bot_token=_get_str("TELEGRAM_BOT_TOKEN") or None,
        telegram_chat_id=_get_str("TELEGRAM_CHAT_ID") or None,
        log_level=_get_str("LOG_LEVEL", "INFO").upper(),
        log_json=_get_bool("LOG_JSON", False),
    )
