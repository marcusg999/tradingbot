#!/usr/bin/env python3
"""Read-only pre-flight check for your Alpaca connection.

Verifies that your keys work and the bot can reach both the trading and market
-data endpoints, WITHOUT placing any order. Run it before `python main.py`:

    python scripts/check_connection.py

Exit code 0 = all good; non-zero = something needs fixing. It prints which
endpoint (paper vs live) it used, your equity, open positions, and whether it
can pull candles for your first symbol.
"""
from __future__ import annotations

import os
import sys

# Allow running from anywhere: add the project root (parent of scripts/).
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

import config as config_module  # noqa: E402


PAPER_URL = "https://paper-api.alpaca.markets"
LIVE_URL = "https://api.alpaca.markets"

OK = "\033[32m✓\033[0m"
BAD = "\033[31m✗\033[0m"


def mask_secret(value: str) -> str:
    """Show only enough of a key to recognize it; never the whole thing."""
    if not value:
        return "(empty)"
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}…{value[-4:]}"


def main() -> int:
    cfg = config_module.load_config()

    print("momentum-bot · connection check")
    print("-" * 44)
    endpoint = LIVE_URL if cfg.live else PAPER_URL
    print(f"Mode         : {cfg.mode_label}")
    print(f"Endpoint     : {endpoint}")
    print(f"API key      : {mask_secret(cfg.api_key)}")
    print(f"API secret   : {mask_secret(cfg.api_secret)}")
    print(f"Symbols      : {', '.join(cfg.symbols)}")
    print("-" * 44)

    if not cfg.api_key or not cfg.api_secret:
        print(f"{BAD} No API keys set. Add ALPACA_API_KEY / ALPACA_API_SECRET "
              f"to your .env (paper keys from the Alpaca paper dashboard).")
        return 1

    if cfg.live:
        print("⚠  LIVE mode is enabled — this check will hit your REAL account.")

    # --- trading endpoint (read-only account fetch) ----------------------
    try:
        from broker import Broker
        broker = Broker(cfg)
        equity = broker.get_equity()
        cash = broker.get_cash()
        last_equity = broker.get_last_equity()
        print(f"{OK} Account reachable")
        print(f"    equity ${equity:,.2f} · cash ${cash:,.2f} · "
              f"prev-close ${last_equity:,.2f}")
    except Exception as exc:
        print(f"{BAD} Could not reach the trading account: {exc}")
        print("    → Check the key/secret are correct, not swapped, and match "
              "the mode (paper keys for paper).")
        return 1

    # --- positions -------------------------------------------------------
    try:
        positions = broker.list_positions()
        if positions:
            print(f"{OK} {len(positions)} open position(s):")
            for sym, p in positions.items():
                print(f"    {sym}: qty {p.qty} @ {p.avg_entry_price} "
                      f"(uPL {p.unrealized_pl:+.2f})")
        else:
            print(f"{OK} No open positions (flat)")
    except Exception as exc:
        print(f"{BAD} Could not list positions: {exc}")
        return 1

    # --- market data endpoint (recent candles for first symbol) ----------
    from datetime import datetime, timedelta, timezone
    sym = cfg.symbols[0]
    try:
        start = datetime.now(timezone.utc) - timedelta(hours=6)
        bars = broker.get_bars(sym, start)
        if bars:
            last = bars[-1]
            print(f"{OK} Market data OK — {len(bars)} recent {sym} candles, "
                  f"last close ${last.close:,.2f}")
        else:
            print(f"{BAD} Market data returned no candles for {sym}. "
                  f"Check the symbol is a valid Alpaca pair (e.g. BTC/USD).")
            return 1
    except Exception as exc:
        print(f"{BAD} Could not fetch market data for {sym}: {exc}")
        return 1

    print("-" * 44)
    print(f"{OK} All checks passed. You're ready to run: python main.py")
    if not cfg.live:
        print("   (paper mode — no real money at risk)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
