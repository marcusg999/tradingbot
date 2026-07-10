"""Alpaca broker wrapper.

Thin, well-typed surface over alpaca-py so the rest of the bot never touches
the SDK directly. Handles crypto symbol normalization, candle fetching, market
entries, server-side stop orders, and position/order teardown.

Crypto note: Alpaca crypto supports market, limit and stop_limit orders (no
native trailing stop / bracket). The hard stop is therefore a real server-side
stop_limit order; the trailing stop is emulated by cancelling and re-submitting
that stop order at a higher level as price advances.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional

from alpaca.data.historical import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import (
    GetOrdersRequest,
    MarketOrderRequest,
    StopLimitOrderRequest,
)


@dataclass
class Candle:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float

    def as_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
        }


@dataclass
class PositionInfo:
    symbol: str            # normalized "BTC/USD" form
    qty: float
    avg_entry_price: float
    current_price: float
    market_value: float
    unrealized_pl: float


def normalize_symbol(symbol: str) -> str:
    """Return the canonical 'BASE/QUOTE' form (Alpaca positions drop the '/')."""
    s = symbol.upper()
    if "/" in s:
        return s
    for quote in ("USDT", "USDC", "USD"):
        if s.endswith(quote):
            return f"{s[:-len(quote)]}/{quote}"
    return s


class Broker:
    # Limit price is placed this far below the stop trigger so a triggered
    # stop_limit still fills through modest slippage.
    STOP_LIMIT_SLIPPAGE = 0.005

    def __init__(self, config) -> None:
        self.c = config
        self.trading = TradingClient(
            config.api_key, config.api_secret, paper=config.paper)
        # Crypto market data does not require auth, but pass keys when present.
        if config.api_key and config.api_secret:
            self.data = CryptoHistoricalDataClient(
                config.api_key, config.api_secret)
        else:
            self.data = CryptoHistoricalDataClient()

    # --- account ---------------------------------------------------------
    def get_equity(self) -> float:
        return float(self.trading.get_account().equity)

    def get_last_equity(self) -> float:
        return float(self.trading.get_account().last_equity)

    def get_cash(self) -> float:
        return float(self.trading.get_account().cash)

    # --- market data -----------------------------------------------------
    def get_bars(self, symbol: str, start: datetime,
                 end: Optional[datetime] = None,
                 timeframe_hours: Optional[int] = None) -> List[Candle]:
        hours = timeframe_hours or self.c.timeframe_hours
        tf = TimeFrame(hours, TimeFrameUnit.Hour)
        req = CryptoBarsRequest(
            symbol_or_symbols=[symbol], timeframe=tf, start=start, end=end)
        bars = self.data.get_crypto_bars(req)
        raw = bars.data.get(symbol, []) if hasattr(bars, "data") else []
        out: List[Candle] = []
        for b in raw:
            out.append(Candle(
                timestamp=b.timestamp,
                open=float(b.open), high=float(b.high), low=float(b.low),
                close=float(b.close), volume=float(b.volume)))
        return out

    @staticmethod
    def candle_age_seconds(candle: Candle) -> float:
        now = datetime.now(timezone.utc)
        ts = candle.timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (now - ts).total_seconds()

    # --- positions & orders ---------------------------------------------
    def list_positions(self) -> Dict[str, PositionInfo]:
        out: Dict[str, PositionInfo] = {}
        for p in self.trading.get_all_positions():
            sym = normalize_symbol(p.symbol)
            out[sym] = PositionInfo(
                symbol=sym,
                qty=float(p.qty),
                avg_entry_price=float(p.avg_entry_price),
                current_price=float(p.current_price or 0.0),
                market_value=float(p.market_value or 0.0),
                unrealized_pl=float(p.unrealized_pl or 0.0),
            )
        return out

    def list_open_orders(self, symbol: Optional[str] = None) -> List:
        req = GetOrdersRequest(status=QueryOrderStatus.OPEN,
                               symbols=[symbol] if symbol else None)
        return list(self.trading.get_orders(req))

    def submit_market_buy(self, symbol: str, qty: float):
        req = MarketOrderRequest(
            symbol=symbol, qty=round(qty, 9), side=OrderSide.BUY,
            time_in_force=TimeInForce.GTC)
        return self.trading.submit_order(req)

    def submit_stop_sell(self, symbol: str, qty: float, stop_price: float):
        """Server-side stop_limit sell — the hard/trailing stop."""
        stop_price = round(stop_price, 2)
        limit_price = round(stop_price * (1.0 - self.STOP_LIMIT_SLIPPAGE), 2)
        req = StopLimitOrderRequest(
            symbol=symbol, qty=round(qty, 9), side=OrderSide.SELL,
            time_in_force=TimeInForce.GTC,
            stop_price=stop_price, limit_price=limit_price)
        return self.trading.submit_order(req)

    def cancel_order(self, order_id: str) -> None:
        try:
            self.trading.cancel_order_by_id(order_id)
        except Exception:  # pragma: no cover - already gone / filled
            pass

    def cancel_all_orders(self) -> None:
        self.trading.cancel_orders()

    def close_position(self, symbol: str):
        return self.trading.close_position(symbol)

    def close_all_positions(self) -> None:
        self.trading.close_all_positions(cancel_orders=True)
