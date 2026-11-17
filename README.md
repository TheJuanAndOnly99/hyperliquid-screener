# Hyperliquid Mean-Reversion Screener

A read-only screener for Hyperliquid perpetuals. It scans the top 20 perps by 24h volume, flags
mean-reversion setups on the 1h timeframe, computes entry, stop, targets and R/R, and shows them in a
terminal dashboard with optional Discord alerts. It also backtests its own rules and keeps a
forward-test log of every setup it flags.

It never places orders. It uses only the public `Info` endpoint of the
[Hyperliquid Python SDK](https://github.com/hyperliquid-dex/hyperliquid-python-sdk): no wallet, no keys.

> **Not financial advice, and not a proven strategy.** The backtest below shows an edge that is
> not statistically distinguishable from zero. Treat the output as a research tool.

## What it flags

Signals use the last **closed** 1h candle.

| | Long | Short |
|---|---|---|
| Price | Close at or below the lower Bollinger Band (20, 2.0) | Close at or above the upper band |
| Momentum | RSI(14) below 25 | RSI(14) above 75 |
| Trend filter | Skipped if ADX(14) > 30 with -DI above +DI | Skipped if ADX(14) > 30 with +DI above -DI |

For each setup:

- **Entry:** current mark price
- **Stop:** 1.5 x ATR(14) beyond the 10-candle swing low/high
- **TP1:** SMA(20), the middle band
- **TP2:** the opposite outer band
- **R/R:** `|TP1 - entry| / |entry - stop|`; setups below 1.0 are dropped. **Net R/R** is the same after taker fees
- **Time exit:** 24 bars after the setup first appears ("Exit by" column)
- **Funding:** annualized rate, and whether it pays or costs the trade direction

A second table lists **near setups**: coins close to triggering, or signals the filters rejected.

## Usage

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python main.py             # live dashboard, refreshes every 5 minutes
python main.py --once      # one scan, then exit
python main.py --backtest  # replay the rules over ~208 days of history (takes about five minutes)
python main.py --report    # resolve logged setups and show forward-test results
python test_screener.py    # offline checks of the indicator, plan and backtest math
```

Discord alerts are off unless `DISCORD_WEBHOOK_URL` is set in the environment. Each new setup is
alerted once.

All thresholds live in [`config.py`](config.py).

### Position sizing

Sizing is off by default. Set `ACCOUNT_SIZE` (in USD, in `config.py` or as an environment variable) and the
dashboard and alerts add a **Size** and **Notional** for each setup, chosen so that hitting the stop
loses `RISK_PER_TRADE_PCT` (default 1%) of the account. It is arithmetic on the plan, not a recommendation.

## Files

| File | Purpose |
|---|---|
| `config.py` | Settings and thresholds |
| `screener.py` | Data fetch, indicators, trade plans, backtest, outcome resolution |
| `main.py` | Dashboard, Discord alerts, forward-test log, reports, CLI |
| `test_screener.py` | Offline tests |
| `.github/workflows/screener.yml` | Scheduled cloud run |

## Backtest results

`python main.py --backtest` replays the same plan logic the live scan uses. A trade enters at the
signal candle's close and exits at the stop, TP1, or the 24-bar time exit, net of taker fees on both
sides and of the historical funding paid or received while the trade was open.

Results for the default settings, 1h, about 208 days, today's top 20 perps (run on 2026-11-16):

| Group | Trades | Win rate | Avg R | 95% interval of avg R |
|---|---|---|---|---|
| All | 375 | 45.6% | +0.056 | -0.07 to +0.18 |
| All, before funding | 375 | 45.6% | +0.052 | -0.07 to +0.18 |
| Longs | 159 | 52.8% | +0.118 | -0.06 to +0.30 |
| Shorts | 216 | 40.3% | +0.010 | -0.16 to +0.19 |

How to read this:

- Funding barely matters at these holding times: it added about +0.004R per trade, helping shorts and
  costing longs slightly.
- Every interval includes zero. The true intervals are wider, because setups cluster in time and the
  bootstrap treats trades as independent.
- The settings were chosen by a grid search on part of this history, so the numbers are optimistic.
- The universe is today's most-traded perps, which is survivorship-biased.
- The original rules (RSI 32/68, no trend filter) and every 4h variant tested lost money, which is why
  the defaults differ from a textbook setup and 4h is not scanned.

## Forward test

Every new setup is appended to `setups_log.csv`. On each scan, open rows are resolved against
5-minute candles with the same exit rules and cost model as the backtest (fees and funding), and marked `TP1`, `STOP` or
`TIMEOUT` with the result in R. `python main.py --report` summarizes them. This is the real test of
whether the backtest holds up.

## Running in the cloud

The included GitHub Actions workflow runs `python main.py --once` every 15 minutes and commits
`setups_log.csv` back to the repository. To use it in your own copy:

1. Push the repository to GitHub.
2. Optionally add a `DISCORD_WEBHOOK_URL` repository secret for alerts.
3. Run `git pull` before `python main.py --report` to get the latest log.

Scheduled runs on GitHub often start several minutes late. A 15-minute schedule fits the free tier
only on a public repository; on a private one, use `"5 * * * *"` (hourly).

## Limitations

- Stops and targets are checked on candle highs and lows, so intrabar order is unknown; when one
  candle touches both, the backtest assumes the stop filled first.
- No slippage model beyond taker fees.
- The dashboard shows a plan, not a position: nothing tracks whether you took a trade.
