# momentum-bot

A production-grade, automated **crypto momentum / trend-following** trading bot
for [Alpaca](https://alpaca.markets/). Trades long-or-flat on a dual-EMA
crossover confirmed by RSI, with hard stops, trailing stops, a daily kill
switch, and exposure caps enforced in a dedicated risk module.

> **The bot defaults to Alpaca's PAPER endpoint.** Going live requires two
> explicit environment variables (see the go-live checklist). If either is
> missing it runs on paper and logs a loud warning.

---

## Strategy

- **Signal:** 20-period fast EMA over 50-period slow EMA on **1-hour** candles.
- **Entry (long only):** fast EMA crosses **above** slow EMA **and** RSI(14) is
  between **50 and 70** (filters weak and overextended signals).
- **Exit:** fast EMA crosses below slow EMA, **or** the trailing stop is hit,
  **or** the hard stop is hit.
- **No shorting. No pyramiding.** One open position per symbol, max.

The signal logic lives entirely in `strategy.py` as **pure functions** (candles
in, signal out), so the backtester and the live bot run *identical* code.

## Risk management (enforced in `risk.py`)

| Control | Default | Behaviour |
|---|---|---|
| Position sizing | 2% equity/trade | Size derived from the stop distance so a stop-out loses ~2% of equity |
| Hard stop | 3% below entry | A **real server-side stop-limit order** at Alpaca (2% limit band), not tracked in memory; a per-cycle gap check forces a market exit if price blows through the band unfilled |
| Trailing stop | activate +4%, trail 2.5% | Ratchets the server-side stop up as price rises; never moves down; the bot re-verifies a live stop exists every cycle |
| Daily kill switch | −5% from day start | Closes all positions, cancels all orders, halts. **Persists across restarts and day rolls** until you reset it: `python main.py --reset-kill-switch` (or set `RESET_KILL_SWITCH=yes` once, then unset it) |
| Max total exposure | 50% of equity | Never more than half of equity deployed across all positions |
| Order sanity checks | 25% / 5 min | Rejects orders > 25% of equity or on price data older than 5 minutes |

Because stops live **server-side at Alpaca**, a crash, redeploy, or SIGTERM
never leaves a position unprotected. The bot does **not** auto-close on
shutdown.

## Architecture

```
momentum-bot/
├── main.py        # entry point + main loop (poll every 60s; act on candle close)
├── config.py      # every tunable from env vars, safe paper-mode defaults
├── strategy.py    # pure EMA/RSI signal functions (no I/O)
├── risk.py        # sizing, stops, kill switch, exposure caps (pure + manager)
├── broker.py      # Alpaca wrapper: account, candles, orders, positions
├── state.py       # SQLite state + trades.csv ledger; day-start equity; kill switch
├── logger.py      # structured logging + optional Discord/Telegram notifier
├── backtest.py    # same strategy.py functions over historical candles
├── tests/         # pytest: signals, sizing math, kill switch, exposure, staleness
├── requirements.txt
├── Dockerfile
├── railway.toml
├── .env.example
└── README.md
```

---

## 1. Setup

1. **Create a free Alpaca account** and open the paper dashboard:
   <https://app.alpaca.markets/paper/dashboard/overview>. Generate **paper**
   API keys (top-right, "View API Keys").

2. **Clone & install:**
   ```bash
   cd momentum-bot
   python -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   ```

3. **Configure:**
   ```bash
   cp .env.example .env
   # edit .env — paste your PAPER keys. Leave the safety gate on paper.
   ```

4. **Run the tests** (no keys or network needed):
   ```bash
   pytest
   ```

5. **Run the bot locally** (paper):
   ```bash
   python main.py    # .env is loaded automatically via python-dotenv
   ```
   You'll see a `Running in PAPER mode` warning, a reconcile pass, then a
   `cycle` log each minute with equity, exposure, unrealized P&L, and the last
   signal per symbol.

---

## 2. Backtesting

```bash
python backtest.py --symbol BTC/USD --days 180 --fee-bps 25
```

Flags: `--symbol` (default `BTC/USD`), `--days` (default `180`),
`--cash` (starting equity, default `10000`), `--fee-bps` (fee per side in
basis points; Alpaca's crypto taker fee is ~25 bps — use it for honest
numbers).

The report prints total return, max drawdown, win rate, profit factor, number
of trades, and — honestly — the **buy-and-hold benchmark** for the same window
with an explicit outperformed/underperformed verdict. Momentum strategies
frequently *underperform* buy-and-hold in strong bull trends; the report will
say so plainly rather than cherry-pick.

**Interpreting results:**
- *Profit factor* < 1 means the strategy lost money gross. Above ~1.5 is
  respectable; be suspicious of anything that looks too good on one symbol/window.
- *Max drawdown* is your emotional stress test — could you hold through it live?
- Compare **vs buy-and-hold**: if it doesn't beat holding after costs, it isn't
  adding value on that window. One good window is not an edge; test several.
- Backtests assume fills at candle close / stop price. Fees are modeled only
  when you pass `--fee-bps`; **slippage is never modeled** — real results will
  be worse.

---

## 3. Railway deployment

1. Push this project to a GitHub repo.
2. In [Railway](https://railway.app/): **New Project → Deploy from GitHub repo**.
3. Railway detects the `Dockerfile` (and `railway.toml`). It builds a
   long-running worker — no port needed.
4. **Variables** tab: set `ALPACA_API_KEY`, `ALPACA_API_SECRET`, and keep
   `TRADING_MODE=paper` / `I_UNDERSTAND_REAL_MONEY=no` to start. Add any
   strategy/risk overrides and (optionally) `DISCORD_WEBHOOK_URL`.
5. **Persist state:** add a Volume mounted at `/app/data`, then set
   `STATE_DB_PATH=/app/data/state.db` and `TRADES_CSV_PATH=/app/data/trades.csv`
   so the ledger and kill-switch state survive redeploys.
6. Deploy. On each redeploy Railway sends `SIGTERM`; the bot persists state and
   exits cleanly, leaving positions open with stops live at Alpaca. On boot it
   reconciles local state against Alpaca (the source of truth).

---

## 4. Go-live checklist

**Do not set the live flags until every box is checked.**

- [ ] Run **2–4 weeks minimum of profitable paper trading** in the exact
      configuration you intend to run live.
- [ ] Read the entire `trades.csv` ledger. **Understand every single loss** —
      why it entered, why it exited, whether the stop behaved as designed.
- [ ] Confirm the kill switch fired correctly at least once (you can lower
      `DAILY_KILL_PCT` temporarily on paper to force it), that it halted
      trading, that a plain restart did NOT resume trading, and that
      `--reset-kill-switch` did.
- [ ] Verify stops exist server-side in the Alpaca dashboard for open positions.
- [ ] Re-run the backtest across several windows and symbols; confirm you are
      comfortable with the drawdowns.
- [ ] Decide the **real money** you can afford to lose — fund the live account
      with only that.
- [ ] **Only then** set both:
      ```
      TRADING_MODE=live
      I_UNDERSTAND_REAL_MONEY=yes
      ```
      and generate **live** API keys from the Alpaca live dashboard.

## 5. Risk disclaimer

This software is provided for educational purposes and comes with **no warranty
of any kind**. Trading cryptocurrencies involves substantial risk of loss.

- **Momentum strategies lose money in choppy, sideways markets** — they buy
  breakouts that reverse and get repeatedly stopped out ("whipsawed").
- **Past backtest performance does not predict future results.** Backtests here
  ignore fees and slippage and can overfit to a particular window.
- Automated systems fail in ways you don't expect: exchange outages, data gaps,
  API changes, bugs. Stops can slip through gaps.
- **Only trade money you can afford to lose entirely.** You are solely
  responsible for your own trading decisions and any losses incurred.
