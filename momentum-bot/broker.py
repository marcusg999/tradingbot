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
from decimal import ROUND_DOWN, Decimal
from typing import Dict, List, Optional

from alpaca.data.historical import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import (
    GetOrdersRequest,
    MarketOrderRequest,
    ReplaceOrderRequest,
    StopLimitOrderRequest,
)

from config import normalize_symbol

# Alpaca crypto quantity precision.
QTY_DECIMALS = 9


def floor_qty(qty: float, decimals: int = QTY_DECIMALS) -> float:
    """Floor a quantity to the exchange's precision using Decimal.

    Critically floors (never rounds up): a sell/stop order for even a hair
    more than the held balance is rejected for insufficient quantity, which
    would knock out the protective stop. Flooring guarantees qty <= holdings.
    """
    if qty <= 0:
        return 0.0
    quantum = Decimal(1).scaleb(-decimals)  # 1e-decimals
    return float(Decimal(str(qty)).quantize(quantum, rounding=ROUND_DOWN))


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


def round_price(price: float) -> float:
    """Round to 2 decimals for dollar-priced assets, 6 significant digits
    below $1 so sub-dollar pairs (DOGE, SHIB, ...) keep their precision."""
    if price >= 1:
        return round(price, 2)
    return float(f"{price:.6g}")


class Broker:
    # Limit price is placed this far below the stop trigger so a triggered
    # stop_limit still fills through fast-market slippage. Crypto moves hard;
    # a thin band risks the stop triggering but resting unfilled below.
    STOP_LIMIT_SLIPPAGE = 0.02

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

    def submit_market_buy(self, symbol: str, qty: float,
                          client_order_id: Optional[str] = None):
        req = MarketOrderRequest(
            symbol=symbol, qty=floor_qty(qty), side=OrderSide.BUY,
            time_in_force=TimeInForce.GTC, client_order_id=client_order_id)
        return self.trading.submit_order(req)

    def submit_stop_sell(self, symbol: str, qty: float, stop_price: float,
                         client_order_id: Optional[str] = None):
        """Server-side stop_limit sell — the hard/trailing stop."""
        stop_price = round_price(stop_price)
        limit_price = round_price(stop_price * (1.0 - self.STOP_LIMIT_SLIPPAGE))
        req = StopLimitOrderRequest(
            symbol=symbol, qty=floor_qty(qty), side=OrderSide.SELL,
            time_in_force=TimeInForce.GTC,
            stop_price=stop_price, limit_price=limit_price,
            client_order_id=client_order_id)
        return self.trading.submit_order(req)

    def replace_stop(self, order_id: str, stop_price: float,
                     client_order_id: Optional[str] = None):
        """Atomically move a stop's price server-side (no cancel/resubmit gap).

        Using replace avoids the window where the old stop is cancelled but the
        new one isn't live yet, and avoids the exchange rejecting a second
        full-qty sell whose quantity is still reserved by the first.
        """
        stop_price = round_price(stop_price)
        limit_price = round_price(stop_price * (1.0 - self.STOP_LIMIT_SLIPPAGE))
        req = ReplaceOrderRequest(stop_price=stop_price, limit_price=limit_price,
                                  client_order_id=client_order_id)
        return self.trading.replace_order_by_id(order_id, req)

    def cancel_order(self, order_id: str) -> None:
        try:
            self.trading.cancel_order_by_id(order_id)
        except Exception:  # pragma: no cover - already gone / filled
            pass

    def cancel_all_orders(self) -> None:
        self.trading.cancel_orders()

    def position_exists(self, symbol: str) -> bool:
        """True if Alpaca currently reports an open position for the symbol.

        Used to disambiguate a failed close (network error vs. the position
        already being gone because a server-side stop filled)."""
        norm = normalize_symbol(symbol)
        try:
            return any(normalize_symbol(p.symbol) == norm
                       for p in self.trading.get_all_positions())
        except Exception:  # pragma: no cover
            return True  # assume it still exists; safer than treating as gone

    def _resolve_asset_id(self, symbol: str):
        """Return the Alpaca asset-id (UUID) for an open position, or None.

        Closing/identifying a position by asset-id is unambiguous; closing by
        crypto symbol depends on the exact form Alpaca's path-param endpoint
        expects (BTC/USD vs BTCUSD), which varies. Prefer the UUID.
        """
        norm = normalize_symbol(symbol)
        try:
            for p in self.trading.get_all_positions():
                if normalize_symbol(p.symbol) == norm:
                    return getattr(p, "asset_id", None)
        except Exception:  # pragma: no cover - network/SDK error
            return None
        return None

    def close_position(self, symbol: str):
        # Prefer the asset-id UUID (unambiguous path param). Fall back to the
        # collapsed symbol form only if the id can't be resolved — a raw '/'
        # in the path would corrupt the URL, so never pass the slash form.
        asset_id = self._resolve_asset_id(symbol)
        if asset_id is not None:
            return self.trading.close_position(asset_id)
        return self.trading.close_position(symbol.replace("/", ""))

    def close_all_positions(self) -> None:
        self.trading.close_all_positions(cancel_orders=True)
