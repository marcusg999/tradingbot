# tradingbot

An automated **crypto momentum / trend-following trading bot** for
[Alpaca](https://alpaca.markets/), built in Python.

> ⚠️ **Defaults to Alpaca's PAPER endpoint.** Live trading requires *two*
> explicit env vars (`TRADING_MODE=live` **and** `I_UNDERSTAND_REAL_MONEY=yes`)
> plus a funded account. Only trade money you can afford to lose.

## 📖 Full documentation → [`momentum-bot/README.md`](momentum-bot/README.md)

The complete project lives in the [`momentum-bot/`](momentum-bot/) directory,
with detailed setup, usage, backtesting, deployment, a go-live checklist, and a
risk disclaimer. Start there.

## What it does

- **Strategy:** dual EMA crossover (20/50) on 1h candles, confirmed by RSI(14);
  long-or-flat, no shorting, one position per symbol.
- **Risk management:** 2% risk per trade, 3% server-side hard stop, trailing
  stop, a −5% daily kill switch, and a 50% total-exposure cap.
- **Read-only dashboard** with an equity/P&L chart, plus a backtester that runs
  the *same* strategy code over historical candles vs. buy-and-hold.
- Defaults to a 10-symbol crypto universe (BTC, ETH, SOL, DOGE, …).

## Quick start (local paper trading)

```bash
cd momentum-bot
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                     # paste your Alpaca PAPER keys
python scripts/check_connection.py       # verify keys (read-only)
python main.py                           # start paper trading
python dashboard.py                      # (2nd terminal) http://localhost:8080
```

## Layout

| Path | What |
|---|---|
| [`momentum-bot/`](momentum-bot/) | The bot: strategy, risk, broker, state, backtest, dashboard, tests |
| [`momentum-bot/README.md`](momentum-bot/README.md) | Full documentation |
| [`render.yaml`](render.yaml) | Optional [Render](https://render.com) deploy blueprint for always-on hosting |

## Disclaimer

For educational purposes, with **no warranty**. Momentum strategies lose money
in choppy markets; past backtest performance does not predict future results.
See the [full risk disclaimer](momentum-bot/README.md#9-risk-disclaimer).
